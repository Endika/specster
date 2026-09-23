import json
from collections.abc import Sequence
from typing import Any

from specster.llm.base import ModelRefusal, ToolCall, ToolResult, ToolSpec, Turn, Usage


class _Session:
    def __init__(
        self, chat: "OpenAIChat", system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> None:
        self._chat = chat
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system}\n\n{context}"},
            {"role": "user", "content": user},
        ]

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        for r in results:
            content = f"ERROR: {r.content}" if r.is_error else r.content
            self._messages.append({"role": "tool", "tool_call_id": r.call_id, "content": content})
        if user_text:
            self._messages.append({"role": "user", "content": user_text})
        resp = self._chat.client.chat.completions.create(
            model=self._chat.model,
            messages=self._messages,
            tools=self._tools,
            **{self._chat.token_param: self._chat.max_tokens},
        )
        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            raise ModelRefusal("content_filter")
        message = choice.message
        if message.refusal:
            raise ModelRefusal(message.refusal)
        self._messages.append(message.model_dump(exclude_none=True))
        calls = []
        for tc in message.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                calls.append(ToolCall(tc.id, tc.function.name, {}, raw_arguments=raw))
                continue
            calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
        u = resp.usage
        if u is None:
            usage = Usage()
        else:
            details = u.prompt_tokens_details
            cached = (details.cached_tokens or 0) if details else 0
            usage = Usage(u.prompt_tokens - cached, cached, 0, u.completion_tokens)
        return Turn(message.content or "", tuple(calls), usage)


class OpenAIChat:
    def __init__(
        self, client: Any, provider: str, model: str, max_tokens: int, token_param: str
    ) -> None:
        self.client, self.provider, self.model = client, provider, model
        self.max_tokens, self.token_param = max_tokens, token_param

    def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> _Session:
        return _Session(self, system, context, user, tools)
