import os
from pathlib import Path

import pytest

from specster.workspace import FILE_MAX_BYTES, TaskWorkspace, ToolError


def ws(tmp_path: Path) -> TaskWorkspace:
    (tmp_path / "app.py").write_text("a = 1\nb = 1\n")
    (tmp_path / "other.py").write_text("x = 1\n")
    return TaskWorkspace(tmp_path, [], ["app.py", "pkg/new.py"])


def test_only_the_tasks_files_can_be_written(tmp_path: Path) -> None:
    w = ws(tmp_path)
    with pytest.raises(ToolError, match="not a file of this task"):
        w.write_file("other.py", "x = 2\n")
    w.write_file("./pkg/new.py", "n = 1\n")
    assert (tmp_path / "pkg" / "new.py").read_text() == "n = 1\n"
    assert w.changes == {"pkg/new.py": "n = 1\n"} and w.originals == {"pkg/new.py": None}
    assert "pkg/new.py" in w.files()


def test_edit_needs_a_unique_old_text(tmp_path: Path) -> None:
    w = ws(tmp_path)
    with pytest.raises(ToolError, match="found 2 times"):
        w.edit_file("app.py", " = 1", " = 2")
    w.edit_file("app.py", "b = 1", "b = 2")
    assert w.changes["app.py"] == "a = 1\nb = 2\n" and w.originals["app.py"] == b"a = 1\nb = 1\n"


def test_a_symlinked_task_file_is_refused(tmp_path: Path) -> None:
    w = ws(tmp_path)
    (tmp_path / "app.py").unlink()
    (tmp_path / "app.py").symlink_to(tmp_path / "other.py")
    with pytest.raises(ToolError, match="symlink"):
        w.write_file("app.py", "evil")
    assert (tmp_path / "other.py").read_text() == "x = 1\n"


def test_a_symlinked_directory_on_the_way_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "pkg").symlink_to(outside)
    w = TaskWorkspace(root, [], ["pkg/new.py"])
    with pytest.raises(ToolError, match="symlink"):
        w.write_file("pkg/new.py", "n = 1\n")
    assert not (outside / "new.py").exists() and w.changes == {}


def test_the_original_is_kept_from_before_the_first_write_only(tmp_path: Path) -> None:
    w = ws(tmp_path)
    w.write_file("app.py", "a = 2\n")
    w.edit_file("app.py", "a = 2", "a = 3")
    assert w.originals == {"app.py": b"a = 1\nb = 1\n"} and w.changes == {"app.py": "a = 3\n"}


def test_empty_old_text_oversized_content_and_escapes_are_refused(tmp_path: Path) -> None:
    w = ws(tmp_path)
    with pytest.raises(ToolError, match="old text"):
        w.edit_file("app.py", "", "x")
    with pytest.raises(ToolError, match="found 0 times"):
        w.edit_file("app.py", "zzz", "x")
    with pytest.raises(ToolError, match="larger than"):
        w.write_file("app.py", "x" * (FILE_MAX_BYTES + 1))
    with pytest.raises(ToolError, match="not a file of this task"):
        w.write_file("pkg/../other.py", "x")
    with pytest.raises(ToolError, match="not a file of this task"):
        w.write_file(str(tmp_path / "app.py"), "x")
    assert w.changes == {} and (tmp_path / "app.py").read_text() == "a = 1\nb = 1\n"


def test_a_same_size_rewrite_moves_the_mtime_to_a_later_second(tmp_path: Path) -> None:
    w = ws(tmp_path)
    before = (tmp_path / "app.py").stat().st_mtime_ns // 1_000_000_000
    w.write_file("app.py", "a = 2\nb = 1\n")
    assert (tmp_path / "app.py").stat().st_mtime_ns // 1_000_000_000 > before


def test_what_a_test_run_writes_into_a_task_file_never_reaches_the_changes(tmp_path: Path) -> None:
    w = ws(tmp_path)
    (tmp_path / "app.py").write_text("a = 1\nb = 1\nimport os; os.system('evil')\n")
    w.edit_file("app.py", "a = 1", "a = 2")
    with (tmp_path / "app.py").open("a") as f:
        f.write("import os; os.system('evil')\n")
    w.edit_file("app.py", "b = 1", "b = 2")
    assert w.changes == {"app.py": "a = 2\nb = 2\n"}
    assert w.originals == {"app.py": b"a = 1\nb = 1\n"}
    assert (tmp_path / "app.py").read_text() == "a = 2\nb = 2\n"


def test_a_parent_swapped_for_a_symlink_after_construction_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    (root / "pkg").mkdir(parents=True)
    w = TaskWorkspace(root, [], ["pkg/new.py"])
    (root / "pkg").rmdir()
    (root / "pkg").symlink_to(outside)
    with pytest.raises(ToolError, match="pkg is a symlink"):
        w.write_file("pkg/new.py", "n = 1\n")
    assert list(outside.iterdir()) == [] and w.changes == {}


def test_a_hard_linked_task_file_is_refused_untouched(tmp_path: Path) -> None:
    w = ws(tmp_path)
    (tmp_path / "app.py").unlink()
    (tmp_path / "app.py").hardlink_to(tmp_path / "other.py")
    with pytest.raises(ToolError, match="hard link"):
        w.write_file("app.py", "evil")
    assert (tmp_path / "other.py").read_text() == "x = 1\n" and w.changes == {}


def test_a_fifo_in_place_of_a_task_file_is_refused_without_blocking(tmp_path: Path) -> None:
    w = ws(tmp_path)
    (tmp_path / "app.py").unlink()
    os.mkfifo(tmp_path / "app.py")
    with pytest.raises(ToolError, match="is not a regular file"):
        w.write_file("app.py", "x")


@pytest.mark.parametrize("path", ["sub/.git", "sub/.GIT", "a/.Git/config", ".git"])
def test_a_git_path_component_is_never_written(tmp_path: Path, path: str) -> None:
    ws = TaskWorkspace(tmp_path, [], [path])
    with pytest.raises(ToolError, match="ignored path"):
        ws.write_file(path, "gitdir: /elsewhere\n")
    assert not (tmp_path / "sub").exists() and not (tmp_path / "a").exists()


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", ".github/actions/setup/action.yml", ".GitHub/Workflows/x"]
)
def test_workflow_and_action_files_are_refused_unless_allowed(tmp_path: Path, path: str) -> None:
    with pytest.raises(ToolError, match=r"build\.allow_workflow_changes is off"):
        TaskWorkspace(tmp_path, [], [path]).write_file(path, "on: push\n")
    assert not (tmp_path / path).exists()
    TaskWorkspace(tmp_path, [], [path], allow_workflows=True).write_file(path, "on: push\n")
    assert (tmp_path / path).read_text() == "on: push\n"


def test_other_files_under_github_stay_writable(tmp_path: Path) -> None:
    TaskWorkspace(tmp_path, [], [".github/dependabot.yml"]).write_file(".github/dependabot.yml", "")
    assert (tmp_path / ".github" / "dependabot.yml").exists()
