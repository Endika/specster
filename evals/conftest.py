import os
from pathlib import Path

import pytest

from evals.harness import CostMeter, RecordingModel, ResultLog, key_env
from specster.config import ModelConfig
from specster.llm.base import Usage
from specster.llm.factory import build_chat_model
from specster.pricing import cost_usd

collect_ignore = ["fixtures"]
REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("evals")
    group.addoption("--eval-k", type=int, default=3, help="runs per case (default 3)")
    group.addoption(
        "--eval-max-usd", type=float, default=2.0, help="hard dollar cap for the session"
    )
    group.addoption(
        "--eval-out", default=None, help="JSONL results path, outside the repository (required)"
    )
    group.addoption(
        "--eval-allow-unknown-cost",
        action="store_true",
        help="run models with no known price (their cost counts as zero)",
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    config = session.config
    if config.option.collectonly or not session.items:
        return
    out = config.getoption("--eval-out")
    if not out:
        raise pytest.UsageError(
            "--eval-out is required to run evals, e.g. --eval-out $RUNNER_TEMP/evals.jsonl"
        )
    if Path(out).expanduser().resolve().is_relative_to(REPO_ROOT):
        raise pytest.UsageError(f"--eval-out must be outside the repository ({REPO_ROOT})")


def _require(cfg: ModelConfig, allow_unknown: bool, role: str) -> None:
    var = key_env(cfg)
    if var and not os.environ.get(var):
        pytest.skip(f"{var} is not set: needed for the {role} model {cfg.provider}/{cfg.model}")
    if cost_usd(cfg.provider, cfg.model, Usage(1), {}) is None and not allow_unknown:
        pytest.exit(
            f"no known price for {cfg.provider}/{cfg.model}: "
            "pass --eval-allow-unknown-cost to run it without a dollar cap",
            returncode=3,
        )


@pytest.fixture(scope="session")
def eval_model_cfg() -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "provider": os.environ.get("SPECSTER_EVAL_PROVIDER", "anthropic"),
            "model": os.environ.get("SPECSTER_EVAL_MODEL", "claude-haiku-4-5"),
        }
    )


@pytest.fixture(scope="session")
def judge_model_cfg() -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "provider": os.environ.get("SPECSTER_JUDGE_PROVIDER", "anthropic"),
            "model": os.environ.get("SPECSTER_JUDGE_MODEL", "claude-sonnet-5"),
        }
    )


@pytest.fixture(scope="session")
def allow_unknown_cost(pytestconfig: pytest.Config) -> bool:
    return bool(pytestconfig.getoption("--eval-allow-unknown-cost"))


@pytest.fixture
def eval_model(eval_model_cfg: ModelConfig, allow_unknown_cost: bool) -> RecordingModel:
    _require(eval_model_cfg, allow_unknown_cost, "eval")
    return RecordingModel(build_chat_model(eval_model_cfg, os.environ))


@pytest.fixture
def judge_model(
    judge_model_cfg: ModelConfig, eval_model_cfg: ModelConfig, allow_unknown_cost: bool
) -> RecordingModel:
    _require(judge_model_cfg, allow_unknown_cost, "judge")
    same = (judge_model_cfg.provider, judge_model_cfg.model) == (
        eval_model_cfg.provider,
        eval_model_cfg.model,
    )
    if same:
        pytest.fail("the judge must be a different model from the one under test")
    return RecordingModel(build_chat_model(judge_model_cfg, os.environ))


@pytest.fixture(scope="session")
def budget(pytestconfig: pytest.Config, allow_unknown_cost: bool) -> CostMeter:
    return CostMeter(float(pytestconfig.getoption("--eval-max-usd")), allow_unknown_cost)


@pytest.fixture(scope="session")
def k(pytestconfig: pytest.Config) -> int:
    value = int(pytestconfig.getoption("--eval-k"))
    if value < 1:
        raise pytest.UsageError("--eval-k must be at least 1")
    return value


@pytest.fixture(scope="session")
def results(pytestconfig: pytest.Config) -> ResultLog:
    return ResultLog(Path(str(pytestconfig.getoption("--eval-out"))).expanduser())
