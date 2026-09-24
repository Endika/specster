from pathlib import Path

import pytest

from specster.config import Config, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yml"
    p.write_text(text)
    return p


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yml")
    assert cfg.models.planner.provider == "anthropic"
    assert cfg.models.planner.model == "claude-opus-5-5"
    assert cfg.trust.comments == "collaborators"
    assert cfg.labels.spec == "ai-spec"
    assert cfg.persona.language == "en"


def test_unknown_key_names_the_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"persona\.humour"):
        load_config(write(tmp_path, "persona:\n  humour: light\n"))


def test_invalid_value_names_key_and_reason(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"trust\.comments"):
        load_config(write(tmp_path, "trust:\n  comments: everyone\n"))


def test_fixed_closing_line_requires_text(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="closing_text"):
        load_config(write(tmp_path, "persona:\n  closing_line: fixed\n"))


def test_skill_source_needs_exactly_one_of_path_or_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="exactly one of path or url"):
        load_config(write(tmp_path, "skills:\n  sources:\n    - phases: all\n"))


def test_phases_all_expands(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "skills:\n  sources:\n    - path: a.md\n      phases: all\n"))
    assert cfg.skills.sources[0].phase_set() == frozenset({"spec", "build", "review"})


def test_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- a\n- b\n"))


def test_question_cap_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"persona\.max_questions"):
        load_config(write(tmp_path, "persona:\n  max_questions: 9\n"))


def test_build_defaults_match_the_spec() -> None:
    cfg = Config()
    assert cfg.models.worker.model == "claude-sonnet-5" and cfg.models.worker.effort is None
    assert (cfg.models.reviewer.model, cfg.models.reviewer.effort) == ("claude-opus-5-5", "medium")
    assert (cfg.labels.build, cfg.labels.built) == ("ai-build", "ai-pr")
    b = cfg.build
    assert (b.max_parallel, b.max_review_rounds, b.test_timeout_s) == (2, 2, 600)
    assert b.close_issue is True and b.allow_comments_after_spec is False
    assert b.setup_command is None and b.test_command is None and b.test_env == {}
    assert b.test_output_max_file_mb == 1024
    assert cfg.budget.max_usd_per_build == 5.0
    assert cfg.budget.max_usd_per_issue == 8.0


def test_a_config_with_null_worker_and_reviewer_still_loads(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "models:\n  worker: null\n  reviewer: null\n"))
    assert cfg.models.worker.model == "claude-sonnet-5"
    assert cfg.models.reviewer.effort == "medium"


def test_commands_are_argument_lists(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, "build:\n  test_command: [uv, run, pytest, -q]\n"))
    assert cfg.build.test_command == ["uv", "run", "pytest", "-q"]


@pytest.mark.parametrize(
    "line",
    [
        "test_command: uv run pytest",
        "test_command: []",
        "setup_command: ['']",
        "test_env: {HOME: /root}",
        "test_env: {'BAD-NAME': x}",
        "max_parallel: 0",
    ],
)
def test_invalid_build_settings_name_the_key(tmp_path: Path, line: str) -> None:
    with pytest.raises(ConfigError, match=r"build\."):
        load_config(write(tmp_path, f"build:\n  {line}\n"))


def test_two_labels_with_the_same_name_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.yml"
    path.write_text("labels:\n  build: ai-spec\n")
    with pytest.raises(ConfigError, match=r"labels\.spec and labels\.build are both 'ai-spec'"):
        load_config(path)
