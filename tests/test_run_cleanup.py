import dataclasses
import json
import tempfile
from pathlib import Path

from specster.config import ModelConfig
from specster.evidence_branch import EVIDENCE_BRANCH, publish
from specster.git import Git
from specster.github import Issue
from specster.llm.base import ChatModel
from specster.run import Env, main
from tests.fakes import FakeTracker, make_remote, make_repo
from tests.test_evidence_branch import ls_tree

CONFIG = ".github/specster/config.yml"


def world(tmp_path: Path, head_ref: str, config: str = "") -> tuple[Env, Git, Path]:
    git = make_repo(tmp_path / "repo", {"app.py": "A = 0\n", CONFIG: config})
    remote = make_remote(tmp_path / "remote" / "o" / "r.git")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "closed",
                "pull_request": {"number": 5, "head": {"ref": head_ref}},
                "sender": {"login": "github-actions[bot]", "type": "Bot"},
            }
        )
    )
    e = Env(
        workspace=tmp_path / "repo",
        event_name="pull_request",
        event_path=event,
        repo="o/r",
        run_id="44",
        token="ghs_TOKEN",
        config_path=CONFIG,
        dispatch_issue=None,
        api_url="",
        graphql_url="",
        skills_token=None,
        secrets={},
        output_path=tmp_path / "out.txt",
        server_url=str(tmp_path / "remote"),
    )
    return e, git, remote


def seed(tmp_path: Path, git: Git, remote: Path, *folders: str) -> None:
    for folder in folders:
        publish(git, str(remote), "ghs_TOKEN", folder, {"a.json": b"{}"}, tmp_path / "seed")


def tracker() -> FakeTracker:
    return FakeTracker(issue=Issue(5, "t", "b", "ana", "NONE", ()))


def no_model(_cfg: ModelConfig) -> ChatModel:
    raise AssertionError("a cleanup never calls a model")


def go(e: Env, tr: FakeTracker) -> int:
    return main(e, tr, no_model, lambda *_: b"", timer=lambda: 0.0)


def scratches() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("specster-build-*"))


def test_closing_a_specster_pr_removes_its_evidence(tmp_path: Path) -> None:
    e, git, remote = world(tmp_path, "specster/issue-3")
    seed(tmp_path, git, remote, "pr-5", "pr-6")
    before, tr = scratches(), tracker()
    assert go(e, tr) == 0
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-6/a.json"]
    assert tr.posted == [] and (tmp_path / "out.txt").read_text() == "outcome=cleaned\n"
    assert scratches() <= before


def test_closing_a_pr_without_evidence_is_skipped(tmp_path: Path) -> None:
    e, git, remote = world(tmp_path, "specster/issue-3")
    tr = tracker()
    assert go(e, tr) == 0
    seed(tmp_path, git, remote, "pr-6")
    assert go(e, tr) == 0
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-6/a.json"]
    assert tr.posted == []
    assert (tmp_path / "out.txt").read_text() == "outcome=skipped\noutcome=skipped\n"


def test_closing_someone_elses_pr_touches_nothing(tmp_path: Path) -> None:
    e, git, remote = world(tmp_path, "feature/x")
    seed(tmp_path, git, remote, "pr-5")
    tr = tracker()
    assert go(e, tr) == 0
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-5/a.json"]
    assert tr.posted == [] and (tmp_path / "out.txt").read_text() == "outcome=skipped\n"


def test_cleanup_failure_never_comments(tmp_path: Path) -> None:
    e, git, remote = world(tmp_path, "specster/issue-3")
    seed(tmp_path, git, remote, "pr-5")
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before, tr = scratches(), tracker()
    assert go(e, tr) == 1
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-5/a.json"]
    assert tr.posted == [] and (tmp_path / "out.txt").read_text() == "outcome=error\n"
    assert scratches() <= before


def test_a_broken_config_during_cleanup_only_logs(tmp_path: Path) -> None:
    e, git, remote = world(tmp_path, "specster/issue-3", "labels: [oops\n")
    seed(tmp_path, git, remote, "pr-5")
    tr = tracker()
    assert go(e, tr) == 1
    assert ls_tree(remote, EVIDENCE_BRANCH) == ["pr-5/a.json"]
    assert tr.posted == [] and (tmp_path / "out.txt").read_text() == "outcome=error\n"


def test_an_unexpected_cleanup_error_still_writes_the_outcome(tmp_path: Path) -> None:
    e, _, _ = world(tmp_path, "specster/issue-3")
    before, tr = scratches(), tracker()
    # A NUL in the remote URL makes subprocess raise ValueError, outside git's own errors.
    assert go(dataclasses.replace(e, repo="o/r\0"), tr) == 1
    assert tr.posted == [] and (tmp_path / "out.txt").read_text() == "outcome=error\n"
    assert scratches() <= before
