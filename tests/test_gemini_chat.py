import json
from typing import Any

import httpx
import pytest
from google import genai
from google.genai import types

from specster.llm.base import ModelRefusal, ToolResult, ToolSpec
from specster.llm.gemini_chat import GeminiChat

TOOLS = [
    ToolSpec("lookup", "Look up.", {"type": "object", "properties": {"key": {"type": "string"}}}),
    ToolSpec("grep", "Search.", {"type": "object", "properties": {}}),
]
THOUGHT = {"text": "I should look it up.", "thought": True, "thoughtSignature": "c2lnLTE="}
FIRST_PARTS: list[dict[str, Any]] = [
    THOUGHT,
    {"text": "Visible."},
    {"functionCall": {"name": "lookup", "args": {"key": "color"}}},
    {"functionCall": {"id": "fc-7", "name": "grep", "args": {}}},
]
USAGE = {
    "promptTokenCount": 100,
    "cachedContentTokenCount": 60,
    "candidatesTokenCount": 5,
    "thoughtsTokenCount": 7,
}


class FakeGemini:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json=self.replies[len(self.requests) - 1])

    def client(self) -> genai.Client:
        return genai.Client(
            api_key="k",
            http_options=types.HttpOptions(
                base_url="http://fake/",
                httpx_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            ),
        )


def _reply(parts: list[dict[str, Any]], finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": USAGE,
    }


def test_function_calls_round_trip_keyed_by_name_with_the_model_turn_verbatim() -> None:
    server = FakeGemini([_reply(FIRST_PARTS), _reply([{"text": "done"}])])
    chat = GeminiChat(server.client(), "gemini", "gemini-test", 256, 0)
    session = chat.start("sys", "ctx", "hi", TOOLS)

    first = session.send()
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("call-1-0", "lookup", {"key": "color"}),
        ("fc-7", "grep", {}),
    ]
    assert first.text == "Visible."
    assert (
        first.usage.input_tokens,
        first.usage.cache_read_tokens,
        first.usage.cache_write_tokens,
        first.usage.output_tokens,
    ) == (40, 60, 0, 12)

    second = session.send(
        [ToolResult("call-1-0", "blue"), ToolResult("fc-7", "no matches", is_error=True)],
        user_text="now submit",
    )
    assert second.text == "done"

    sent = server.requests[1]
    assert sent["contents"][1] == {"role": "model", "parts": FIRST_PARTS}
    assert sent["contents"][2] == {
        "role": "user",
        "parts": [
            {"functionResponse": {"name": "lookup", "response": {"result": "blue"}}},
            {
                "functionResponse": {
                    "id": "fc-7",
                    "name": "grep",
                    "response": {"error": "no matches"},
                }
            },
            {"text": "now submit"},
        ],
    }


def test_request_carries_the_context_tools_and_token_cap() -> None:
    server = FakeGemini([_reply([{"text": "ok"}])])
    GeminiChat(server.client(), "gemini", "gemini-test", 256, 0).start("s", "c", "u", TOOLS).send()

    assert server.paths == ["/v1beta/models/gemini-test:generateContent"]
    sent = server.requests[0]
    assert sent["systemInstruction"]["parts"] == [{"text": "s\n\nc"}]
    assert sent["generationConfig"]["maxOutputTokens"] == 256
    (tool,) = sent["tools"]
    # The SDK sends this field in snake_case, which the proto JSON parser also accepts.
    assert [
        (d["name"], d["description"], d["parameters_json_schema"])
        for d in tool["functionDeclarations"]
    ] == [(t.name, t.description, t.parameters) for t in TOOLS]


def test_safety_stop_is_a_refusal() -> None:
    server = FakeGemini([_reply([], finish="SAFETY")])
    session = GeminiChat(server.client(), "gemini", "m", 256, 0).start("s", "c", "u", TOOLS)
    with pytest.raises(ModelRefusal, match="SAFETY"):
        session.send()


def test_transient_errors_are_retried() -> None:
    attempts: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error": {"code": 503, "status": "UNAVAILABLE"}})
        return httpx.Response(200, json=_reply([{"text": "ok"}]))

    client = genai.Client(
        api_key="k",
        http_options=types.HttpOptions(
            base_url="http://fake/",
            httpx_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    chat = GeminiChat(client, "gemini", "m", 256, 1, sleep=lambda _: None)
    assert chat.start("s", "c", "u", TOOLS).send().text == "ok"
    assert len(attempts) == 2
