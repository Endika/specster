import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from specster.config import ModelConfig
from specster.github import GitHubError, Issue, PullInfo
from specster.llm.base import ChatModel
from specster.metrics import extract_markers
from specster.run import Env, main
from tests.fakes import FakeTracker, make_repo

CONFIG = ".github/specster/config.yml"
OPEN = PullInfo(
    number=5,
    state="open",
    draft=False,
    title="Add CSV export",
    body="b",
    author="ana",
    author_association="MEMBER",
    head_sha="h" * 40,
    head_ref="feature/csv",
    head_repo="o/r",
    base_sha="b" * 40,
    base_ref="main",
    base_repo="o/r",
)


def env(tmp_path: Path, label: str, config: str = "") -> Env:
    repo = tmp_path / "repo"
    if not repo.exists():
        # As actions/checkout leaves the default branch: HEAD at origin/main's tip.
        make_repo(repo, {"app.py": "A = 0\n", CONFIG: config}).run(
            "update-ref", "refs/remotes/origin/main", "HEAD"
        )
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "labeled",
                "label": {"name": label},
                "pull_request": {
                    "number": 5,
                    "head": {"ref": "feature/csv", "repo": {"full_name": "o/r"}},
                    "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                },
                "sender": {"login": "ana", "type": "User"},
            }
        )
    )
    return Env(
        workspace=repo,
        event_name="pull_request",
        event_path=event,
        repo="o/r",
        run_id="45",
        token="t",
        config_path=CONFIG,
        dispatch_issue=None,
        api_url="",
        graphql_url="",
        skills_token=None,
        secrets={},
        output_path=tmp_path / "out.txt",
    )


def tracker(label: str, pull: PullInfo | None = OPEN) -> FakeTracker:
    issue = Issue(5, "Add CSV export", "b", "ana", "MEMBER", (label,))
    return FakeTracker(issue=issue, pull_info=pull)


def no_model(_cfg: ModelConfig) -> ChatModel:
    raise AssertionError("a refused pull request never calls a model")


def go(e: Env, tr: FakeTracker) -> int:
    return main(e, tr, no_model, lambda *_: b"", timer=lambda: 0.0)


def outcome(tmp_path: Path) -> str:
    return (tmp_path / "out.txt").read_text()


@pytest.mark.parametrize(("label", "phase"), [("ai-evidence", "evidence"), ("ai-fix", "fix")])
def test_a_pull_request_label_reaches_its_phase(tmp_path: Path, label: str, phase: str) -> None:
    tr = tracker(label)
    assert go(env(tmp_path, label), tr) == 1
    assert len(tr.posted) == 1 and f"{label} is not implemented yet" in tr.posted[0]
    assert extract_markers(tr.posted[0])[0].phase == phase
    assert tr.issue.labels == () and outcome(tmp_path) == "outcome=refused\n"


@pytest.mark.parametrize(
    ("head_repo", "origin"), [("fork/r", "comes from fork/r"), ("", "comes from a deleted fork")]
)
def test_a_fork_is_refused_before_anything_runs(
    tmp_path: Path, head_repo: str, origin: str
) -> None:
    tr = tracker("ai-fix", dataclasses.replace(OPEN, head_repo=head_repo))
    assert go(env(tmp_path, "ai-fix"), tr) == 1
    assert len(tr.posted) == 1 and origin in tr.posted[0]
    assert "not implemented" not in tr.posted[0] and tr.replies == []
    assert tr.issue.labels == () and outcome(tmp_path) == "outcome=refused\n"


@pytest.mark.parametrize(
    ("pull", "reason", "fix"),
    [
        (
            dataclasses.replace(OPEN, state="closed"),
            "Pull request #5 is closed",
            "Reopen the pull request",
        ),
        (
            dataclasses.replace(OPEN, draft=True),
            "Pull request #5 is a draft",
            "Mark the pull request ready",
        ),
    ],
)
def test_a_closed_or_draft_pull_request_is_refused_with_the_reason(
    tmp_path: Path, pull: PullInfo, reason: str, fix: str
) -> None:
    tr = tracker("ai-evidence", pull)
    assert go(env(tmp_path, "ai-evidence"), tr) == 1
    assert len(tr.posted) == 1 and reason in tr.posted[0] and fix in tr.posted[0]
    assert "not implemented" not in tr.posted[0]
    assert outcome(tmp_path) == "outcome=refused\n"


class BrokenPulls(FakeTracker):
    error: Exception = GitHubError("pull request 5 not found", 404)

    def get_pull(self, number: int) -> PullInfo:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [GitHubError("pull request 5 not found", 404), httpx.ConnectError("connection refused")],
)
def test_an_unreadable_pull_request_fails_with_a_hint_not_a_crash(
    tmp_path: Path, error: Exception
) -> None:
    tr = BrokenPulls(issue=Issue(5, "t", "b", "ana", "MEMBER", ("ai-fix",)))
    tr.error = error
    assert go(env(tmp_path, "ai-fix"), tr) == 1
    assert len(tr.posted) == 1 and "could not read pull request #5" in tr.posted[0]
    assert "Check that the token can read pull requests" in tr.posted[0]
    assert tr.issue.labels == () and outcome(tmp_path) == "outcome=error\n"


def test_another_label_on_a_pull_request_is_skipped(tmp_path: Path) -> None:
    tr = tracker("ai-build")
    assert go(env(tmp_path, "ai-build"), tr) == 0
    assert tr.posted == [] and outcome(tmp_path) == "outcome=skipped\n"


def test_a_pull_request_cannot_change_the_config_specster_applies(tmp_path: Path) -> None:
    ours = "persona:\n  language: es\nlabels:\n  evidence: show-me\n"
    git = make_repo(tmp_path / "repo", {"app.py": "A = 0\n", CONFIG: ours})
    # The pull request's head, with its own config, sits in the checkout's history.
    git.run("checkout", "-q", "-b", "feature/csv")
    (tmp_path / "repo" / CONFIG).write_text("persona:\n  language: en\n")
    git.run("commit", "-q", "--no-verify", "-am", "feat: take the defaults back")
    pull = dataclasses.replace(OPEN, head_sha=git.run("rev-parse", "HEAD").strip())
    git.run("checkout", "-q", "main")
    git.run("update-ref", "refs/remotes/origin/main", "HEAD")

    tr = tracker("ai-evidence", pull)
    assert go(env(tmp_path, "ai-evidence"), tr) == 0
    assert tr.posted == [] and outcome(tmp_path) == "outcome=skipped\n"

    tr = tracker("show-me", pull)
    assert go(env(tmp_path, "show-me"), tr) == 1
    assert "show-me is not implemented yet" in tr.posted[0]
    assert "No se ha ejecutado nada" in tr.posted[0]


class NoPullReads(FakeTracker):
    def get_pull(self, number: int) -> PullInfo:
        raise AssertionError("a run outside the default branch never reads the pull request")


def test_a_checkout_of_the_pull_request_is_refused_before_anything_runs(tmp_path: Path) -> None:
    e = env(tmp_path, "ai-fix", "labels:\n  fix: ai-fix\n")
    git = make_repo(tmp_path / "unused", {"a": ""})
    workspace = ["-C", str(e.workspace)]
    git.run(*workspace, "checkout", "-q", "-b", "feature/csv")
    (e.workspace / CONFIG).write_text("trust:\n  comments: all\n")
    git.run(*workspace, "commit", "-q", "--no-verify", "-am", "feat: trust everyone")
    tr = NoPullReads(issue=Issue(5, "t", "b", "ana", "MEMBER", ("ai-fix",)))
    assert go(e, tr) == 1
    assert len(tr.posted) == 1 and "not at the tip of main" in tr.posted[0]
    assert "ref: ${{ github.event.repository.default_branch }}" in tr.posted[0]
    assert tr.issue.labels == () and outcome(tmp_path) == "outcome=refused\n"


def test_a_checkout_without_the_default_branch_fails_closed(tmp_path: Path) -> None:
    e = env(tmp_path, "ai-evidence")
    git = make_repo(tmp_path / "unused", {"a": ""})
    # actions/checkout of a pull_request event fetches only refs/pull/N/merge.
    git.run("-C", str(e.workspace), "update-ref", "-d", "refs/remotes/origin/main")
    tr = NoPullReads(issue=Issue(5, "t", "b", "ana", "MEMBER", ("ai-evidence",)))
    assert go(e, tr) == 1
    assert "not at main, which was not fetched" in tr.posted[0]
    assert outcome(tmp_path) == "outcome=refused\n"
