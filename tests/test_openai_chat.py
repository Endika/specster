import json
from typing import Any

import httpx2
import openai
import pytest

from specster.llm.base import ModelRefusal, ToolResult, ToolSpec
from specster.llm.openai_chat import OpenAIChat

TOOLS = [ToolSpec("grep", "g", {"type": "object", "properties": {}})]
BAD_CALL = {
    "id": "c1",
    "type": "function",
    "function": {"name": "grep", "arguments": "{bad json"},
}


class FakeOpenAI:
    def __init__(self, finish_reason: str = "stop") -> None:
        self.finish_reason = finish_reason
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if body["messages"][-1]["role"] == "tool":
            msg: dict[str, Any] = {"role": "assistant", "content": "done", "tool_calls": None}
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [BAD_CALL]}
        return httpx2.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": msg, "finish_reason": self.finish_reason}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "total_tokens": 105,
                    "prompt_tokens_details": {"cached_tokens": 60},
                },
            },
        )

    def client(self) -> openai.OpenAI:
        return openai.OpenAI(
            api_key="k",
            base_url="http://fake/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handle)),
        )


def test_malformed_arguments_surface_as_raw_and_cached_tokens_are_split() -> None:
    server = FakeOpenAI()
    chat = OpenAIChat(server.client(), "openai-compatible", "m", 100, "max_tokens")
    s = chat.start("sys", "ctx", "hi", TOOLS)
    turn = s.send()
    assert turn.tool_calls[0].raw_arguments == "{bad json"
    assert turn.tool_calls[0].arguments == {}
    assert (turn.usage.input_tokens, turn.usage.cache_read_tokens) == (40, 60)
    assert turn.usage.output_tokens == 5
    assert s.send([ToolResult("c1", "arguments were not valid JSON", True)]).text == "done"

    first, second = server.requests
    assert first["messages"][0] == {"role": "system", "content": "sys\n\nctx"}
    assert first["max_tokens"] == 100
    assert "max_completion_tokens" not in first
    assert first["tools"] == [
        {
            "type": "function",
            "function": {"name": "grep", "description": "g", "parameters": TOOLS[0].parameters},
        }
    ]
    assert second["messages"][2] == {"role": "assistant", "tool_calls": [BAD_CALL]}
    assert second["messages"][3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "ERROR: arguments were not valid JSON",
    }


def test_content_filter_is_a_refusal() -> None:
    chat = OpenAIChat(FakeOpenAI("content_filter").client(), "openai", "m", 100, "max_tokens")
    with pytest.raises(ModelRefusal):
        chat.start("s", "c", "u", TOOLS).send()
