import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pytest

from specster.agent import AgentError, AgentOutcome, run_agent
from specster.config import Config, ModelConfig, PersonaConfig, TrustConfig
from specster.github import Comment, Issue
from specster.llm.base import ChatModel, ChatSession, ToolResult, ToolSpec, Turn, Usage
from specster.pricing import cost_usd
from specster.prompts import context_block, system_prompt
from specster.repomap import build_repo_map
from specster.skills import load_skills
from specster.thread import Thread, build_thread
from specster.workspace import Workspace

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "repo_small"
MAX_TURNS = Config().budget.max_turns
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def key_env(cfg: ModelConfig) -> str | None:
    return cfg.api_key_env or KEY_ENV.get(cfg.provider)


class _RecordingSession:
    def __init__(self, inner: ChatSession, owner: "RecordingModel") -> None:
        self._inner, self._owner = inner, owner

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        turn = self._inner.send(results, user_text)
        self._owner.usage = self._owner.usage + turn.usage
        self._owner.tool_names += [c.name for c in turn.tool_calls]
        return turn


class RecordingModel:
    """Forwards to the real model and records tool call names and usage."""

    def __init__(self, inner: ChatModel) -> None:
        self._inner = inner
        self.provider, self.model = inner.provider, inner.model
        self.usage = Usage()
        self.tool_names: list[str] = []

    def start(
        self, system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> _RecordingSession:
        self.usage, self.tool_names = Usage(), []
        return _RecordingSession(self._inner.start(system, context, user, tools), self)


class CostMeter:
    def __init__(self, max_usd: float, allow_unknown: bool = False) -> None:
        self.max_usd, self.allow_unknown = max_usd, allow_unknown
        self.spent = 0.0
        self.unknown_runs = 0

    def add(self, cost: float | None) -> None:
        if cost is None:
            self.unknown_runs += 1
            if not self.allow_unknown:
                pytest.exit(
                    "eval budget reached: a run has unknown cost "
                    "(pass --eval-allow-unknown-cost to count it as zero)",
                    returncode=3,
                )
            return
        self.spent += cost
        if self.spent >= self.max_usd:
            pytest.exit(
                f"eval budget reached: ${self.spent:.4f} of ${self.max_usd:.2f}", returncode=3
            )


def add_costs(*costs: float | None) -> float | None:
    if any(c is None for c in costs):
        return None
    return round(sum(c for c in costs if c is not None), 6)


def model_cost(model: RecordingModel) -> float | None:
    return cost_usd(model.provider, model.model, model.usage, {})


class ResultLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


@dataclass
class AgentRun:
    outcome: AgentOutcome | None
    error: str | None
    thread: Thread
    ws: Workspace
    tool_names: list[str]
    usage: Usage
    cost: float | None
    turns: int

    def output_json(self) -> str:
        if self.outcome is None:
            return ""
        return self.outcome.result.model_dump_json()


def copy_fixture(dest: Path) -> Path:
    return Path(shutil.copytree(FIXTURE_REPO, dest))


def run_spec_agent(
    model: RecordingModel,
    repo: Path,
    issue: Issue,
    comments: Sequence[Comment] = (),
    trust: TrustConfig | None = None,
    language: str = "en",
) -> AgentRun:
    cfg = Config(trust=trust or TrustConfig(), persona=PersonaConfig(language=language))
    ws = Workspace(repo, cfg.repo_map.exclude)
    repo_map = build_repo_map(ws, cfg.repo_map.max_tokens)
    skills = load_skills(repo, cfg.skills, "spec", _no_fetch, None)
    thread = build_thread(issue, comments, cfg.trust, None)
    outcome: AgentOutcome | None = None
    error: str | None = None
    try:
        outcome = run_agent(
            model,
            system_prompt(cfg.persona, skills.on_demand),
            context_block(repo_map, skills.inline),
            thread.text,
            ws,
            skills,
            MAX_TURNS,
            cfg.persona.max_questions,
        )
        turns = outcome.turns
    except AgentError as e:
        error, turns = str(e), e.turns
    return AgentRun(
        outcome,
        error,
        thread,
        ws,
        list(model.tool_names),
        model.usage,
        model_cost(model),
        turns,
    )


def _no_fetch(url: str, _headers: Mapping[str, str]) -> bytes:
    raise RuntimeError(f"evals do not fetch skills: {url}")


@dataclass
class RunRecord:
    suite: str
    case: str
    run: int
    passed: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, int] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    turns: int = 0
    tool_names: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
