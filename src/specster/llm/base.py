from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Turn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    usage: Usage


class ModelRefusal(Exception):
    pass


class ChatSession(Protocol):
    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn: ...


class ChatModel(Protocol):
    provider: str
    model: str

    def start(
        self, system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> ChatSession: ...
