import os
from pathlib import Path

import pytest

from specster.config import ModelConfig
from specster.llm.base import ToolResult, ToolSpec
from specster.llm.factory import build_chat_model

CASSETTES = Path(__file__).parent / "cassettes"

# provider id -> ModelConfig used when recording; edit model ids to what the account has.
PROVIDERS: dict[str, ModelConfig] = {
    "anthropic": ModelConfig(provider="anthropic", model="claude-haiku-4-5", max_tokens=1024),
    "bedrock": ModelConfig(
        provider="bedrock", model="anthropic.claude-haiku-4-5", region="eu-west-1", max_tokens=1024
    ),
    "vertex-anthropic": ModelConfig(
        provider="vertex-anthropic",
        model="claude-haiku-4-5",
        region="global",
        project="specster-dev",
        max_tokens=1024,
    ),
}

TOOLS = [
    ToolSpec(
        "lookup",
        "Look up a stored value.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "submit_answer",
        "Submit the final answer.",
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    ),
]
PROMPT = (
    "Use the lookup tool with key 'color', then call submit_answer with exactly the value "
    "it returned. Do not answer in text."
)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.vcr
def test_tool_round_trip(provider: str, request: pytest.FixtureRequest) -> None:
    cassette = CASSETTES / f"test_tool_round_trip[{provider}].yaml"
    recording = request.config.getoption("--record-mode") not in (None, "none")
    if not cassette.exists() and not recording:
        pytest.skip(
            f"no cassette for {provider}: record with "
            f"`uv run pytest 'tests/contract/test_contract.py::test_tool_round_trip[{provider}]' "
            "--record-mode=once`"
        )
    model = build_chat_model(PROVIDERS[provider], dict(os.environ), replay=not recording)
    session = model.start("You are a test harness.", "Context padding. " * 200, PROMPT, TOOLS)

    first = session.send()
    lookup = [c for c in first.tool_calls if c.name == "lookup"]
    assert lookup
    assert lookup[0].arguments == {"key": "color"}
    usage = first.usage
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens > 0

    second = session.send([ToolResult(lookup[0].id, "blue")])
    submit = [c for c in second.tool_calls if c.name == "submit_answer"]
    assert submit
    assert submit[0].arguments == {"answer": "blue"}
    assert second.usage.output_tokens > 0
