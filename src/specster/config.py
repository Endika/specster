from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Phase = Literal["spec", "build", "review"]
ALL_PHASES: frozenset[Phase] = frozenset({"spec", "build", "review"})
Provider = Literal[
    "anthropic",
    "bedrock",
    "vertex-anthropic",
    "openai",
    "openai-compatible",
    "azure-openai",
    "gemini",
    "vertex-gemini",
]
DEFAULT_AVATAR_URL = (
    "https://raw.githubusercontent.com/Endika/specster/main/assets/specster-avatar.png"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Strict):
    provider: Provider = "anthropic"
    model: str = "claude-opus-5-5"
    max_tokens: int = Field(default=16000, gt=0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    base_url: str | None = None
    region: str | None = None
    project: str | None = None
    api_version: str | None = None
    api_key_env: str | None = None
    token_param: Literal["max_tokens", "max_completion_tokens"] | None = None
    max_retries: int = Field(default=4, ge=0)


class ModelsConfig(_Strict):
    planner: ModelConfig = ModelConfig()
    worker: ModelConfig | None = None
    reviewer: ModelConfig | None = None


class LabelsConfig(_Strict):
    spec: str = "ai-spec"
    needs_human: str = "needs-human"
    ready: str = "spec-ready"
    build: str = "ai-build"


class TrustConfig(_Strict):
    comments: Literal["all", "collaborators", "owner"] = "collaborators"
    issue_author: bool = True
    snapshot_at_label: bool = True


class SkillSource(_Strict):
    path: str | None = None
    url: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    phases: list[Phase] | Literal["all"] | None = None

    @model_validator(mode="after")
    def _one_origin(self) -> Self:
        if (self.path is None) == (self.url is None):
            raise ValueError("exactly one of path or url is required")
        return self

    def phase_set(self) -> frozenset[Phase] | None:
        if self.phases is None:
            return None
        if self.phases == "all":
            return ALL_PHASES
        return frozenset(self.phases)


class SkillsConfig(_Strict):
    autodiscover: bool = True
    sources: list[SkillSource] = []
    load: Literal["always", "on_demand"] = "on_demand"
    max_tokens: int = Field(default=20000, gt=0)


class RepoMapConfig(_Strict):
    max_tokens: int = Field(default=8000, gt=0)
    exclude: list[str] = ["node_modules/", "dist/", "build/", "vendor/", "*.lock", "*.min.js"]


class PersonaConfig(_Strict):
    name: str = "Specster"
    avatar_url: str | None = DEFAULT_AVATAR_URL
    header: bool = True
    humor: Literal["off", "light", "spooky"] = "light"
    closing_line: Literal["off", "generated", "fixed"] = "generated"
    closing_text: str = ""
    language: str = "en"
    style: Literal["concise", "detailed"] = "concise"
    max_questions: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def _fixed_needs_text(self) -> Self:
        if self.closing_line == "fixed" and not self.closing_text.strip():
            raise ValueError("closing_line: fixed requires closing_text")
        return self


class BudgetConfig(_Strict):
    max_usd_per_issue: float | None = Field(default=5.0, gt=0)
    max_turns: int = Field(default=30, gt=1)


class PriceEntry(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)


class Config(_Strict):
    models: ModelsConfig = ModelsConfig()
    labels: LabelsConfig = LabelsConfig()
    trust: TrustConfig = TrustConfig()
    skills: SkillsConfig = SkillsConfig()
    repo_map: RepoMapConfig = RepoMapConfig()
    persona: PersonaConfig = PersonaConfig()
    budget: BudgetConfig = BudgetConfig()
    pricing: dict[str, PriceEntry] = {}


class ConfigError(Exception):
    pass


def load_config(path: Path) -> Config:
    if not path.is_file():
        return Config()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "(root)"
            lines.append(f"{where}: {err['msg']}")
        raise ConfigError(f"{path}:\n" + "\n".join(lines)) from e
