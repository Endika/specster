from pathlib import Path

import pytest

from specster.workspace import FILE_MAX_BYTES, ToolError, Workspace


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


def test_read_file_reports_and_marks_cut_lines(ws: Workspace, tmp_path: Path) -> None:
    long_line = "x" * 310
    (tmp_path / "long.txt").write_text(f"short\n{long_line}\n")
    out = ws.read_file("long.txt")
    assert out == f"1: short\n2: {'x' * 300} [cut]"
    assert ws.truncations == ["read long.txt: 1 lines cut to 300 chars"]


def test_grep_reports_and_marks_cut_lines(ws: Workspace, tmp_path: Path) -> None:
    long_line = "hit " + "x" * 310
    (tmp_path / "long.txt").write_text(long_line + "\n")
    out = ws.grep("hit", "long.txt")
    assert out == f"long.txt:1: {long_line[:300]} [cut]"
    assert ws.truncations == ["grep 'hit': 1 matched lines cut to 300 chars"]


def test_files_skips_oversized_files_and_reports(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("a" * (FILE_MAX_BYTES + 1))
    assert "big.txt" not in ws.files()
    assert ws.truncations == ["1 files over 2 MB skipped: big.txt"]


def test_read_file_refuses_oversized_file(ws: Workspace, tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("a" * (FILE_MAX_BYTES + 1))
    with pytest.raises(ToolError, match="larger than 2 MB"):
        ws.read_file("big.txt")


def test_symlinked_dir_escape_is_refused(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    (tmp_path / "linked_dir").symlink_to(outside)
    assert not any(f.startswith("linked_dir/") for f in ws.files())
    with pytest.raises(ToolError, match="outside"):
        ws.read_file("linked_dir/secret.txt")
    with pytest.raises(ToolError, match="outside"):
        ws.list_dir("linked_dir")
