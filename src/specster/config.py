import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

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


def _worker_default() -> ModelConfig:
    return ModelConfig(model="claude-sonnet-5")


def _reviewer_default() -> ModelConfig:
    return ModelConfig(model="claude-opus-5-5", effort="medium")


class ModelsConfig(_Strict):
    planner: ModelConfig = ModelConfig()
    worker: ModelConfig = Field(default_factory=_worker_default)
    reviewer: ModelConfig = Field(default_factory=_reviewer_default)

    @field_validator("worker", "reviewer", mode="before")
    @classmethod
    def _null_is_default(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            return _worker_default() if info.field_name == "worker" else _reviewer_default()
        return value


class LabelsConfig(_Strict):
    spec: str = "ai-spec"
    needs_human: str = "needs-human"
    ready: str = "spec-ready"
    build: str = "ai-build"
    built: str = "ai-pr"

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        seen: dict[str, str] = {}
        for name, value in self.model_dump().items():
            if value in seen:
                raise ValueError(f"labels.{seen[value]} and labels.{name} are both {value!r}")
            seen[value] = name
        return self


class IdentityConfig(_Strict):
    # e.g. specster-endika[bot]; unset, it is asked of the token, which an App token may not answer.
    bot_login: str | None = None


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
    # "on_demand" is the pre-0.3 name of "model_decides", still accepted.
    load: Literal["always", "model_decides", "on_demand"] = "always"
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


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Command = Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]


class BuildConfig(_Strict):
    max_parallel: int = Field(default=2, ge=1, le=8)
    max_turns_per_task: int = Field(default=40, gt=1)
    max_review_rounds: int = Field(default=2, ge=0)
    setup_command: Command | None = None
    test_command: Command | None = None
    test_env: dict[str, str] = {}
    test_timeout_s: int = Field(default=600, gt=0)
    test_output_max_kb: int = Field(default=20, gt=0)
    test_output_max_file_mb: int = Field(default=1024, gt=0)
    max_minutes: int = Field(default=100, gt=0)
    close_issue: bool = True
    allow_comments_after_spec: bool = False
    allow_workflow_changes: bool = False

    @field_validator("test_env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if not _ENV_NAME.match(name):
                raise ValueError(f"{name!r} is not an environment variable name")
            if name == "HOME":
                raise ValueError("HOME is set by Specster to a private temporary directory")
        return value


class BudgetConfig(_Strict):
    max_usd_per_issue: float | None = Field(default=8.0, gt=0)
    max_usd_per_build: float | None = Field(default=5.0, gt=0)
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
    identity: IdentityConfig = IdentityConfig()
    skills: SkillsConfig = SkillsConfig()
    repo_map: RepoMapConfig = RepoMapConfig()
    persona: PersonaConfig = PersonaConfig()
    budget: BudgetConfig = BudgetConfig()
    build: BuildConfig = BuildConfig()
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
