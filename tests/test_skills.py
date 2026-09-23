import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from specster.config import SkillsConfig, SkillSource
from specster.skills import SkillIntegrityError, load_skills


class FakeWeb:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.seen_headers: dict[str, dict[str, str]] = {}

    def __call__(self, url: str, headers: Mapping[str, str]) -> bytes:
        self.seen_headers[url] = dict(headers)
        return self.pages[url]


def test_autodiscovers_agent_files_and_reads_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Use uv.\n")
    (tmp_path / ".github" / "specster" / "skills").mkdir(parents=True)
    (tmp_path / ".github" / "specster" / "skills" / "style.md").write_text(
        "---\nname: style\ndescription: Python style\nphases: [build]\n---\nUse ruff.\n"
    )
    book = load_skills(tmp_path, SkillsConfig(load="always"), "spec", FakeWeb({}), None)
    assert [s.name for s in book.skills] == ["AGENTS", "style"]
    assert [s.name for s in book.inline] == ["AGENTS"]  # style is build-only


def test_config_phases_override_frontmatter(tmp_path: Path) -> None:
    (tmp_path / "rules.md").write_text("---\nphases: [build]\n---\nRule.\n")
    cfg = SkillsConfig(
        autodiscover=False, load="always", sources=[SkillSource(path="rules.md", phases=["spec"])]
    )
    book = load_skills(tmp_path, cfg, "spec", FakeWeb({}), None)
    assert [s.name for s in book.inline] == ["rules"]


def test_url_sha_mismatch_fails_the_run(tmp_path: Path) -> None:
    cfg = SkillsConfig(
        autodiscover=False, sources=[SkillSource(url="https://x.example/a.md", sha256="0" * 64)]
    )
    with pytest.raises(SkillIntegrityError, match="sha256 mismatch"):
        load_skills(tmp_path, cfg, "spec", FakeWeb({"https://x.example/a.md": b"body"}), None)


def test_url_with_matching_sha_is_verified(tmp_path: Path) -> None:
    body = b"# Security\nNo secrets.\n"
    url = "https://raw.githubusercontent.com/o/r/main/sec.md"
    cfg = SkillsConfig(
        autodiscover=False,
        sources=[SkillSource(url=url, sha256=hashlib.sha256(body).hexdigest())],
    )
    book = load_skills(tmp_path, cfg, "spec", FakeWeb({url: body}), "tok")
    assert book.skills[0].verified is True
    assert book.skills[0].description == "# Security"


def test_token_is_only_sent_to_github_hosts(tmp_path: Path) -> None:
    gh, other = "https://raw.githubusercontent.com/o/r/main/a.md", "https://evil.example/a.md"
    web = FakeWeb({gh: b"a", other: b"b"})
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(url=gh), SkillSource(url=other)])
    book = load_skills(tmp_path, cfg, "spec", web, "tok")
    assert web.seen_headers[gh] == {"Authorization": "Bearer tok"}
    assert web.seen_headers[other] == {}
    assert f"{other}: no sha256 pinned" in book.warnings


def test_always_mode_over_budget_moves_rest_on_demand_and_warns(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("a" * 400)
    (tmp_path / "CLAUDE.md").write_text("b" * 400)
    book = load_skills(
        tmp_path, SkillsConfig(load="always", max_tokens=150), "spec", FakeWeb({}), None
    )
    assert [s.name for s in book.inline] == ["AGENTS"]
    assert [s.name for s in book.on_demand] == ["CLAUDE"]
    assert any("loaded on demand" in w for w in book.warnings)


def test_get_records_reads(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x")
    book = load_skills(tmp_path, SkillsConfig(), "spec", FakeWeb({}), None)
    assert book.get("AGENTS").body == "x"
    assert book.read == {"AGENTS"}


def test_configured_path_traversal_raises(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("data")
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(path="../outside.txt")])
    with pytest.raises(SkillIntegrityError, match="outside the repository"):
        load_skills(root, cfg, "spec", FakeWeb({}), None)


def test_configured_absolute_path_raises(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.md").write_text("A")
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(path=str(root / "a.md"))])
    with pytest.raises(SkillIntegrityError, match="outside the repository"):
        load_skills(root, cfg, "spec", FakeWeb({}), None)


def test_autodiscovered_symlink_outside_repo_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    (root / "AGENTS.md").symlink_to(outside)
    book = load_skills(root, SkillsConfig(), "spec", FakeWeb({}), None)
    assert book.skills == []
    assert any("AGENTS.md" in w for w in book.warnings)


def test_invalid_frontmatter_yaml_is_ignored_with_warning(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text("---\nname: [unterminated\n---\nBody text.\n")
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(path="bad.md")])
    book = load_skills(tmp_path, cfg, "spec", FakeWeb({}), None)
    assert book.skills[0].name == "bad"
    assert "bad.md: invalid frontmatter ignored" in book.warnings


def test_scalar_phases_string_becomes_one_item_list(tmp_path: Path) -> None:
    (tmp_path / "solo.md").write_text("---\nphases: spec\n---\nBody.\n")
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(path="solo.md")])
    book = load_skills(tmp_path, cfg, "spec", FakeWeb({}), None)
    assert book.skills[0].phases == frozenset({"spec"})


def test_get_ignores_skills_inactive_in_current_phase(tmp_path: Path) -> None:
    (tmp_path / "buildonly.md").write_text("---\nphases: [build]\n---\nBody.\n")
    cfg = SkillsConfig(autodiscover=False, sources=[SkillSource(path="buildonly.md")])
    book = load_skills(tmp_path, cfg, "spec", FakeWeb({}), None)
    with pytest.raises(KeyError):
        book.get("buildonly")
