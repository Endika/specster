from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from specster.evidence_branch import EVIDENCE_BRANCH, EvidenceBranchError, publish, remove
from specster.git import BOT_EMAIL, Author, Git
from tests.fakes import make_remote, make_repo

TOKEN = "ghs_SECRETTOKEN"
AUTHOR = Author("Specster", BOT_EMAIL)


def _read(remote: Path, *args: str) -> str:
    git = Git(remote, AUTHOR, remote.parent / f"{remote.name}-read-home")
    return git.run(f"--git-dir={remote}", *args)


def ls_tree(remote: Path, branch: str) -> list[str]:
    return sorted(_read(remote, "ls-tree", "-r", "--name-only", branch).splitlines())


def parents(remote: Path, sha: str) -> list[str]:
    return _read(remote, "log", "-1", "--format=%P", sha).split()


def subject(remote: Path, sha: str) -> str:
    return _read(remote, "log", "-1", "--format=%s", sha).strip()


def setup(tmp_path: Path) -> tuple[Git, Path]:
    return make_repo(tmp_path / "repo", {"app.py": "x = 1\n"}), make_remote(tmp_path / "r.git")


def test_publish_creates_the_orphan_branch(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    sha = publish(git, str(remote), TOKEN, "pr-7", {"a.json": b"{}"}, tmp_path / "s")
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-7/a.json"]
    assert parents(remote, sha) == []
    assert subject(remote, sha) == "evidence: pr-7 [skip ci]"
    assert _read(remote, "show", f"{EVIDENCE_BRANCH}:pr-7/a.json") == "{}"
    assert all(TOKEN not in a for argv in git.argv_log for a in argv)
    assert git.run("status", "--porcelain") == ""


def test_publish_replaces_its_folder_and_keeps_others(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    url, s = str(remote), tmp_path / "s"
    publish(git, url, TOKEN, "pr-7", {"a.json": b"1", "old.json": b"x"}, s)
    publish(git, url, TOKEN, "pr-8", {"b.json": b"2"}, s)
    sha = publish(git, url, TOKEN, "pr-7", {"a.json": b"3"}, s)
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-7/a.json", "pr-8/b.json"]
    assert _read(remote, "show", f"{EVIDENCE_BRANCH}:pr-7/a.json") == "3"
    assert len(parents(remote, sha)) == 1


class _RacedGit(Git):
    """Another build publishes pr-9 right after this one reads the branch tip, once."""

    def __init__(self, *args: Any, rival: Callable[[], object], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rival: Callable[[], object] | None = rival

    def fetch_ref(self, url: str, branch: str, token: str, into: str) -> None:
        super().fetch_ref(url, branch, token, into)
        if self._rival is not None:
            rival, self._rival = self._rival, None
            rival()


class _AlwaysRacedGit(Git):
    def __init__(self, *args: Any, rival: Callable[[int], object], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rival, self._n = rival, 0

    def fetch_ref(self, url: str, branch: str, token: str, into: str) -> None:
        super().fetch_ref(url, branch, token, into)
        self._n += 1
        self._rival(self._n)


def test_publish_retries_once_when_the_branch_moved(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    url = str(remote)
    publish(git, url, TOKEN, "pr-8", {"b.json": b"2"}, tmp_path / "s1")
    other = make_repo(tmp_path / "other", {"z.py": "z = 1\n"})
    raced = _RacedGit(
        tmp_path / "repo",
        AUTHOR,
        tmp_path / "h3",
        rival=lambda: publish(other, url, TOKEN, "pr-9", {"c.json": b"3"}, tmp_path / "s2"),
    )
    publish(raced, url, TOKEN, "pr-7", {"a.json": b"1"}, tmp_path / "s3")
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-7/a.json", "pr-8/b.json", "pr-9/c.json"]


def test_publish_gives_up_when_the_branch_moves_twice(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    url = str(remote)
    publish(git, url, TOKEN, "pr-8", {"b.json": b"2"}, tmp_path / "s1")
    other = make_repo(tmp_path / "other", {"z.py": "z = 1\n"})
    raced = _AlwaysRacedGit(
        tmp_path / "repo",
        AUTHOR,
        tmp_path / "h3",
        rival=lambda n: publish(
            other, url, TOKEN, f"pr-{20 + n}", {"c.json": b"3"}, tmp_path / "s2"
        ),
    )
    with pytest.raises(EvidenceBranchError, match="moved twice"):
        publish(raced, url, TOKEN, "pr-7", {"a.json": b"1"}, tmp_path / "s3")
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-21/c.json", "pr-22/c.json", "pr-8/b.json"]


def test_remove_drops_only_its_folder(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    url, s = str(remote), tmp_path / "s"
    publish(git, url, TOKEN, "pr-7", {"a.json": b"1"}, s)
    publish(git, url, TOKEN, "pr-8", {"b.json": b"2"}, s)
    assert remove(git, url, TOKEN, "pr-7", s) is True
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-8/b.json"]
    assert subject(remote, EVIDENCE_BRANCH) == "evidence: remove pr-7 [skip ci]"
    assert remove(git, url, TOKEN, "pr-7", s) is False


def test_remove_does_not_touch_a_folder_that_only_shares_the_prefix(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    url, s = str(remote), tmp_path / "s"
    publish(git, url, TOKEN, "pr-70", {"a.json": b"1"}, s)
    assert remove(git, url, TOKEN, "pr-7", s) is False
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-70/a.json"]


def test_remove_without_branch_is_a_no_op(tmp_path: Path) -> None:
    git, remote = setup(tmp_path)
    assert remove(git, str(remote), TOKEN, "pr-7", tmp_path / "s") is False
    assert _read(remote, "ls-remote", str(remote)) == ""


@pytest.mark.parametrize(
    ("folder", "name"),
    [(".github/workflows", "x.yml"), (".github", "workflows/x.yml"), ("pr-7", "../x"), ("..", "x")],
)
def test_publish_refuses_workflow_and_escaping_paths(
    tmp_path: Path, folder: str, name: str
) -> None:
    git, remote = setup(tmp_path)
    with pytest.raises(EvidenceBranchError):
        publish(git, str(remote), TOKEN, folder, {name: b""}, tmp_path / "s")
    assert _read(remote, "ls-remote", str(remote)) == ""
