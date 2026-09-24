from collections.abc import Sequence
from typing import Any

from specster.llm.base import ModelRefusal, ToolCall, ToolResult, ToolSpec, Turn, Usage

_BREAKPOINT = {"type": "ephemeral"}


def _with_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A cache breakpoint on the newest message lets the next turn read the whole conversation
    # from cache; the stored history stays unmarked so only one message breakpoint is ever sent.
    last = messages[-1]
    blocks = list(last["content"])
    blocks[-1] = {**blocks[-1], "cache_control": _BREAKPOINT}
    return [*messages[:-1], {**last, "content": blocks}]


class _Session:
    def __init__(
        self, chat: "AnthropicChat", system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> None:
        self._chat = chat
        self._system = [
            {"type": "text", "text": system},
            {"type": "text", "text": context, "cache_control": _BREAKPOINT},
        ]
        self._tools = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        self._messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": user}]}
        ]

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        if results or user_text:
            content: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": r.call_id,
                    "content": r.content,
                    "is_error": r.is_error,
                }
                for r in results
            ]
            if user_text:
                content.append({"type": "text", "text": user_text})
            self._messages.append({"role": "user", "content": content})
        extra: dict[str, Any] = {}
        if self._chat.effort:
            extra["output_config"] = {"effort": self._chat.effort}
        with self._chat.client.messages.stream(
            model=self._chat.model,
            max_tokens=self._chat.max_tokens,
            system=self._system,
            tools=self._tools,
            messages=_with_breakpoint(self._messages),
            **extra,
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise ModelRefusal(str(message.stop_details or "refused"))
        self._messages.append({"role": "assistant", "content": message.content})
        calls = tuple(
            ToolCall(block.id, block.name, dict(block.input))
            for block in message.content
            if block.type == "tool_use"
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        u = message.usage
        usage = Usage(
            u.input_tokens,
            u.cache_read_input_tokens or 0,
            u.cache_creation_input_tokens or 0,
            u.output_tokens,
        )
        return Turn(text, calls, usage)


class AnthropicChat:
    def __init__(
        self, client: Any, provider: str, model: str, max_tokens: int, effort: str | None
    ) -> None:
        self.client, self.provider, self.model = client, provider, model
        self.max_tokens, self.effort = max_tokens, effort

    def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> _Session:
        return _Session(self, system, context, user, tools)
