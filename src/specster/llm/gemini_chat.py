import time
from collections.abc import Callable, Sequence
from typing import Any

from google.genai import errors, types

from specster.llm.base import ModelRefusal, ToolCall, ToolResult, ToolSpec, Turn, Usage
from specster.llm.retry import with_retries

_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}


def _retryable(e: Exception) -> bool:
    return isinstance(e, errors.APIError) and e.code in (429, 500, 502, 503, 504)


class _Session:
    def __init__(
        self, chat: "GeminiChat", system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> None:
        self._chat = chat
        self._config = types.GenerateContentConfig(
            system_instruction=f"{system}\n\n{context}",
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ],
            max_output_tokens=chat.max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part.from_text(text=user)])
        ]
        # Function responses are matched by name; the model's own id is echoed when it sent one.
        self._calls: dict[str, tuple[str, str | None]] = {}
        self._turn = 0

    def _response(self, result: ToolResult) -> types.Part:
        name, model_id = self._calls[result.call_id]
        payload = {"error": result.content} if result.is_error else {"result": result.content}
        return types.Part(
            function_response=types.FunctionResponse(id=model_id, name=name, response=payload)
        )

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        parts = [self._response(r) for r in results]
        if user_text:
            parts.append(types.Part.from_text(text=user_text))
        if parts:
            self._contents.append(types.Content(role="user", parts=parts))
        self._turn += 1
        resp = with_retries(
            lambda: self._chat.client.models.generate_content(
                model=self._chat.model, contents=self._contents, config=self._config
            ),
            _retryable,
            self._chat.max_retries + 1,
            sleep=self._chat.sleep,
        )
        candidate = resp.candidates[0] if resp.candidates else None
        reason = candidate.finish_reason if candidate else None
        if candidate is None or (reason is not None and reason.name in _REFUSALS):
            raise ModelRefusal(str(reason.name if reason else "no candidate"))
        if candidate.content is not None:
            self._contents.append(candidate.content)
        calls = []
        for i, fc in enumerate(resp.function_calls or []):
            call_id = fc.id or f"call-{self._turn}-{i}"
            self._calls[call_id] = (fc.name or "", fc.id)
            calls.append(ToolCall(call_id, fc.name or "", dict(fc.args or {})))
        cparts = candidate.content.parts if candidate.content and candidate.content.parts else []
        text = "".join(p.text for p in cparts if p.text and not p.thought)
        m = resp.usage_metadata
        prompt = (m.prompt_token_count or 0) if m else 0
        cached = (m.cached_content_token_count or 0) if m else 0
        output = ((m.candidates_token_count or 0) + (m.thoughts_token_count or 0)) if m else 0
        return Turn(text, tuple(calls), Usage(prompt - cached, cached, 0, output))


class GeminiChat:
    def __init__(
        self,
        client: Any,
        provider: str,
        model: str,
        max_tokens: int,
        max_retries: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client, self.provider, self.model = client, provider, model
        self.max_tokens, self.max_retries, self.sleep = max_tokens, max_retries, sleep

    def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> _Session:
        return _Session(self, system, context, user, tools)
