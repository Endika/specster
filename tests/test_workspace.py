from pathlib import Path

import pytest

from specster.workspace import ToolError, Workspace


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def export():\n    return 1\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "gen.py").write_text("x = 1\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret")
    return Workspace(tmp_path)


def test_files_skip_gitignored_git_dir_and_binaries(ws: Workspace) -> None:
    assert ws.files() == [".gitignore", "src/app.py"]


def test_read_file_numbers_lines_and_records_the_read(ws: Workspace) -> None:
    assert ws.read_file("src/app.py", 2, 2) == "2: " + "    return 1"
    assert ws.files_read == {"src/app.py"}


def test_path_escape_is_refused(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="outside"):
        ws.read_file("../etc/passwd")


def test_absolute_path_is_refused(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="outside"):
        ws.read_file("/etc/passwd")


def test_symlink_escape_is_refused(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope")
    (tmp_path / "link.txt").symlink_to(outside)
    with pytest.raises(ToolError, match="outside"):
        ws.read_file("link.txt")


def test_ignored_file_cannot_be_read(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="ignored"):
        ws.read_file(".git/config")


def test_grep_finds_matches_with_location(ws: Workspace) -> None:
    assert ws.grep(r"def \w+") == "src/app.py:1: def export():"


def test_grep_reports_when_it_truncates(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("hit\n" * 250)
    out = ws.grep("hit", "many.txt")
    assert out.endswith("[truncated: 200 of 250 matches shown]")
    assert ws.truncations == ["grep 'hit': 200 of 250 matches shown"]


def test_invalid_regex_is_a_tool_error(ws: Workspace) -> None:
    with pytest.raises(ToolError, match="invalid pattern"):
        ws.grep("(")
