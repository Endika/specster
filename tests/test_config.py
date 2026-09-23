from pathlib import Path

import pytest

from specster.config import ConfigError, load_config


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
