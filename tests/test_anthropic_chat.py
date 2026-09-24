import json
from typing import Any

import anthropic
import httpx2
import pytest

from specster.llm.anthropic_chat import AnthropicChat
from specster.llm.base import ModelRefusal, ToolResult, ToolSpec

TOOLS = [ToolSpec("lookup", "Look up.", {"type": "object", "properties": {}})]


def _sse(message: dict[str, Any]) -> bytes:
    content = message.pop("content")
    stop = {"stop_reason": message.pop("stop_reason"), "stop_sequence": None}
    if "stop_details" in message:
        stop["stop_details"] = message.pop("stop_details")
    usage = message["usage"]
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}}
    ]
    empty: dict[str, Any]
    deltas: list[dict[str, Any]]
    for i, block in enumerate(content):
        if block["type"] == "tool_use":
            empty = {"input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        elif block["type"] == "thinking":
            empty = {"thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        else:
            empty = {"text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        events.append({"type": "content_block_start", "index": i, "content_block": block | empty})
        events += [{"type": "content_block_delta", "index": i, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": i})
    events.append(
        {"type": "message_delta", "delta": stop, "usage": {"output_tokens": usage["output_tokens"]}}
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class FakeAnthropic:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        reply = self.replies[len(self.requests) - 1]
        message = {
            "id": f"msg_{len(self.requests)}",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "stop_sequence": None,
            **reply,
        }
        return httpx2.Response(
            200, content=_sse(message), headers={"content-type": "text/event-stream"}
        )

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            api_key="k",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handle)),
        )


USAGE = {
    "input_tokens": 12,
    "output_tokens": 7,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 5,
}
THINKING = {"type": "thinking", "thinking": "Need the lookup.", "signature": "sig-abc"}
TEXT = {"type": "text", "text": "Looking it up."}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"key": "color"}}


def test_tool_round_trip_replays_assistant_content_verbatim() -> None:
    server = FakeAnthropic(
        [
            {"content": [THINKING, TEXT, TOOL_USE], "stop_reason": "tool_use", "usage": USAGE},
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        ]
    )
    chat = AnthropicChat(server.client(), "anthropic", "claude-test", 256, "high")
    session = chat.start("sys", "ctx", "hi", TOOLS)

    first = session.send()
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("toolu_1", "lookup", {"key": "color"})
    ]
    assert first.text == "Looking it up."
    assert (
        first.usage.input_tokens,
        first.usage.cache_read_tokens,
        first.usage.cache_write_tokens,
        first.usage.output_tokens,
    ) == (12, 30, 5, 7)

    second = session.send([ToolResult("toolu_1", "blue")], user_text="now submit")
    assert second.text == "done"
    assert second.usage.cache_read_tokens == 0

    sent = server.requests[1]
    assert sent["messages"][1] == {"role": "assistant", "content": [THINKING, TEXT, TOOL_USE]}
    assert sent["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "blue", "is_error": False},
            {"type": "text", "text": "now submit", "cache_control": {"type": "ephemeral"}},
        ],
    }


def test_request_caches_the_context_block_and_never_forces_a_tool() -> None:
    server = FakeAnthropic(
        [{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": USAGE}]
    )
    chat = AnthropicChat(server.client(), "anthropic", "claude-test", 256, None)
    chat.start("sys", "ctx", "hi", TOOLS).send()

    sent = server.requests[0]
    assert sent["system"] == [
        {"type": "text", "text": "sys"},
        {"type": "text", "text": "ctx", "cache_control": {"type": "ephemeral"}},
    ]
    assert sent["tools"] == [
        {"name": "lookup", "description": "Look up.", "input_schema": TOOLS[0].parameters}
    ]
    assert "tool_choice" not in sent
    assert "output_config" not in sent
    assert sent["stream"] is True


def test_effort_is_sent_as_output_config() -> None:
    server = FakeAnthropic(
        [{"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": USAGE}]
    )
    AnthropicChat(server.client(), "anthropic", "m", 256, "low").start("s", "c", "u", TOOLS).send()
    assert server.requests[0]["output_config"] == {"effort": "low"}


def test_refusal_raises_with_the_stop_details() -> None:
    server = FakeAnthropic(
        [
            {
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {
                    "type": "refusal",
                    "category": "cyber",
                    "explanation": "not this one",
                },
                "usage": USAGE,
            }
        ]
    )
    session = AnthropicChat(server.client(), "anthropic", "m", 256, None).start("s", "c", "u", [])
    with pytest.raises(ModelRefusal, match="not this one"):
        session.send()


def test_only_the_newest_message_carries_a_cache_breakpoint() -> None:
    reply = {"content": [TOOL_USE], "stop_reason": "tool_use", "usage": USAGE}
    server = FakeAnthropic([reply, reply, reply])
    session = AnthropicChat(server.client(), "anthropic", "claude-test", 256, None).start(
        "sys", "ctx", "hi", TOOLS
    )
    session.send()
    session.send([ToolResult("toolu_1", "a")])
    session.send([ToolResult("toolu_1", "b")])

    first, *_, last = server.requests
    assert first["messages"][0]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]
    marked = [
        i
        for i, message in enumerate(last["messages"])
        if isinstance(message["content"], list)
        and any("cache_control" in block for block in message["content"])
    ]
    assert marked == [len(last["messages"]) - 1]
    assert json.dumps(last).count("cache_control") == 2  # context block + newest message


def test_history_keeps_the_same_shape_across_turns_apart_from_the_breakpoint() -> None:
    reply = {"content": [TOOL_USE], "stop_reason": "tool_use", "usage": USAGE}
    server = FakeAnthropic([reply, reply])
    session = AnthropicChat(server.client(), "anthropic", "claude-test", 256, None).start(
        "sys", "ctx", "hi", TOOLS
    )
    session.send()
    session.send([ToolResult("toolu_1", "a")])

    def unmarked(message: dict[str, Any]) -> dict[str, Any]:
        blocks = [{k: v for k, v in b.items() if k != "cache_control"} for b in message["content"]]
        return {**message, "content": blocks}

    first_turn, second_turn = server.requests
    assert unmarked(first_turn["messages"][0]) == unmarked(second_turn["messages"][0])
