from collections.abc import Sequence

from specster.llm.base import ChatModel, ChatSession, ToolResult, ToolSpec, Turn
from specster.telemetry import span


class TracedSession:
    def __init__(self, inner: ChatSession, provider: str, model: str) -> None:
        self._inner = inner
        self._provider = provider
        self._model = model

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self._provider,
            "gen_ai.request.model": self._model,
        }
        with span(f"chat {self._model}", attributes) as current:
            turn = self._inner.send(results, user_text)
            u = turn.usage
            current.set_attributes(
                {
                    "gen_ai.usage.input_tokens": u.input_tokens,
                    "gen_ai.usage.output_tokens": u.output_tokens,
                    "specster.cache_read_tokens": u.cache_read_tokens,
                    "specster.cache_write_tokens": u.cache_write_tokens,
                }
            )
            return turn


class TracedModel:
    """A model whose every turn is a `chat <model>` span; the turns pass through untouched."""

    def __init__(self, inner: ChatModel, provider: str, model: str) -> None:
        self.inner = inner
        self.provider = provider
        self.model = model

    def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> ChatSession:
        session = self.inner.start(system, context, user, tools)
        return TracedSession(session, self.provider, self.model)
