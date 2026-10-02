import dataclasses
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from specster.config import ModelConfig
from specster.git import BOT_EMAIL, Author, Git
from specster.github import GitHubRest, Issue, PullRequest
from specster.llm.base import ChatModel, ToolCall
from specster.metrics import RunMetrics, encode_marker, last_marker
from specster.run import Env, main
from specster.sandbox import Identity, RunResult, Sandbox, SandboxError
from tests.fakes import (
    FakeTracker,
    ScriptBook,
    ScriptedModel,
    bot_comment,
    make_remote,
    make_repo,
    make_repo_log,
    spec_comment_body,
    unix_socket,
)
from tests.test_approved import SPEC, T0, TASK, human
from tests.test_build import BASE_ROUTES, HEAD_ROUTES, app, free_port
from tests.test_evidence_branch import ls_tree
from tests.test_sandbox import ROOT_ONLY

CHECK = json.dumps([sys.executable, "-c", "import app; assert app.A == 1"])


def world(
    tmp_path: Path,
    config: str = "",
    gate_base: bool = False,
    files: Mapping[str, str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> tuple[Env, FakeTracker, Path]:
    # CHECK fails on the base on purpose, so only the base-gate tests gate.
    allow = "" if gate_base else "  allow_failing_base: true\n"
    text = f"build:\n  test_command: {CHECK}\n{allow}{config}"
    tree = {"app.py": "A = 0\n", ".github/specster/config.yml": text, **(files or {})}
    make_repo(tmp_path / "repo", tree)
    remote = make_remote(tmp_path / "remote" / "o" / "r.git")
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "labeled",
                "label": {"name": "ai-build"},
                "issue": {"number": 7},
                "sender": {"login": "endika", "type": "User"},
            }
        )
    )
    e = Env(
        workspace=tmp_path / "repo",
        event_name="issues",
        event_path=event,
        repo="o/r",
        run_id="43",
        token="ghs_TOKEN",
        config_path=".github/specster/config.yml",
        dispatch_issue=None,
        api_url="",
        graphql_url="",
        skills_token=None,
        secrets={},
        output_path=tmp_path / "out.txt",
        server_url=str(tmp_path / "remote"),
        ref="refs/heads/main",
    )
    tr = FakeTracker(
        issue=Issue(7, "CSV export", "b", "ana", "NONE", ("ai-build", "spec-ready")),
        comments=[bot_comment(1, spec_comment_body(SPEC, evidence=evidence), T0)],
        label_events={"ai-build": T0 + timedelta(hours=1)},
        login="specster[bot]",
    )
    return e, tr, remote


# Worker and reviewer share a default model; only the reviewer's has an effort.
def is_reviewer(cfg: ModelConfig) -> bool:
    return cfg.effort is not None


def models(worker: ChatModel, reviewer: ChatModel) -> Callable[[ModelConfig], ChatModel]:
    return lambda cfg: reviewer if is_reviewer(cfg) else worker


def unprivileged(_slot: int) -> Identity | None:
    return None


def go(e: Env, tr: FakeTracker, worker: ChatModel, reviewer: ChatModel) -> int:
    return main(
        e, tr, models(worker, reviewer), lambda *_: b"", timer=lambda: 0.0, identity=unprivileged
    )


def book() -> ScriptBook:
    return ScriptBook(
        {
            'id="a"': [
                [
                    [ToolCall("1", "write_file", {"path": "app.py", "content": "A = 1\n"})],
                    [
                        ToolCall(
                            "2",
                            "submit_task",
                            {"summary": "Set A.", "commit_subject": "feat(app): set A"},
                        )
                    ],
                ]
            ]
        }
    )


def approve() -> ScriptedModel:
    return ScriptedModel([[ToolCall("r", "submit_review", {"verdict": "approve", "findings": []})]])


def test_an_approved_build_pushes_one_commit_per_task_and_opens_the_pr(tmp_path: Path) -> None:
    e, tr, remote = world(tmp_path)
    assert go(e, tr, book(), approve()) == 0
    title, body, head, base = tr.pulls[0]
    assert (head, base) == ("specster/issue-7", "main") and "Closes #7" in body
    assert title == "CSV"
    assert make_repo_log(remote) == ["chore: init", "feat(app): set A"]
    assert tr.issue.labels == ("ai-pr",) and "pull/1" in tr.posted[-1]
    m = last_marker(tr.posted[-1])
    assert m is not None and m.phase == "build" and m.outcome == "pr_opened"
    assert set(m.roles) == {"worker", "reviewer"} and m.tasks_done == 1 and m.test_runs >= 2
    assert (tmp_path / "out.txt").read_text() == "outcome=pr_opened\n"


def test_a_plan_that_changes_the_configured_config_file_is_refused(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    e = dataclasses.replace(e, config_path="ops/specster.yml")
    spec = SPEC | {"tasks": [TASK | {"files": ["app.py", "ops/specster.yml"]}]}
    tr.comments = [bot_comment(1, spec_comment_body(spec), T0)]
    assert go(e, tr, ScriptedModel([]), ScriptedModel([])) == 1
    assert "ops/specster.yml is Specster's own configuration" in tr.posted[-1]
    assert tr.pulls == []


def test_build_commits_carry_the_bot_accounts_noreply_email(tmp_path: Path) -> None:
    e, tr, remote = world(tmp_path)
    tr.user_ids = {"specster[bot]": 42}
    assert go(e, tr, book(), approve()) == 0
    git = Git(remote, Author("Specster", BOT_EMAIL), tmp_path / "log-home")
    emails = git.run(f"--git-dir={remote}", "log", "--format=%ae", "-1", "specster/issue-7")
    assert emails.strip() == "42+specster[bot]@users.noreply.github.com"


def test_a_red_base_is_reported_with_its_test_output_and_no_model_cost(tmp_path: Path) -> None:
    e, tr, remote = world(tmp_path, gate_base=True)
    assert go(e, tr, ScriptBook({}), ScriptedModel([])) == 0
    body = tr.posted[-1]
    assert "the tests fail on the base commit" in body and "AssertionError" in body
    m = last_marker(body)
    assert m is not None and m.outcome == "build_failed" and m.cost_usd == 0
    assert "needs-human" in tr.issue.labels and not list((remote / "refs" / "heads").iterdir())


def test_a_configured_escalation_model_rescues_a_failed_task_and_the_comment_says_so(
    tmp_path: Path,
) -> None:
    config = "models:\n  escalation: {provider: anthropic, model: claude-opus-5}\n"
    e, tr, _ = world(tmp_path, config)
    stuck = ScriptBook({'id="a"': [["thinking", "still thinking"]]})
    reviewer = approve()

    def make(cfg: ModelConfig) -> ChatModel:
        if cfg.model == "claude-opus-5":
            return book()
        return reviewer if is_reviewer(cfg) else stuck

    code = main(e, tr, make, lambda *_: b"", timer=lambda: 0.0, identity=unprivileged)
    assert code == 0 and tr.pulls
    assert "(escalated to `claude-opus-5`)" in tr.posted[-1]
    m = last_marker(tr.posted[-1])
    assert m is not None and set(m.roles) == {"worker", "worker-escalated", "reviewer"}


def test_a_build_without_approval_pushes_the_branch_and_asks_for_a_human(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path, "  max_review_rounds: 0\n")
    bad = {"task_id": "a", "file": "app.py", "severity": "critical", "description": "wrong value"}
    reviewer = ScriptedModel(
        [[ToolCall("r", "submit_review", {"verdict": "changes", "findings": [bad]})]]
    )
    assert go(e, tr, book(), reviewer) == 0
    assert tr.pulls == [] and tr.issue.labels == ("needs-human", "spec-ready")
    assert "wrong value" in tr.posted[-1] and "tree/specster/issue-7" in tr.posted[-1]


def test_trusted_comments_after_the_spec_refuse_without_calling_a_model(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.comments.append(human(2, T0 + timedelta(minutes=5)))
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert worker.sessions == {} and "requested changes" in tr.posted[-1]
    assert "issuecomment-2" in tr.posted[-1] and "ai-build" not in tr.issue.labels


def test_allow_comments_after_spec_builds_and_lists_them_as_not_applied(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path, "  allow_comments_after_spec: true\n")
    tr.comments.append(human(2, T0 + timedelta(minutes=5)))
    assert go(e, tr, book(), approve()) == 0
    assert "not applied" in tr.pulls[0][1] and "issuecomment-2" in tr.posted[-1]


def test_an_existing_remote_branch_fails_before_any_model_call(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.remote_branches.add("specster/issue-7")
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert "already exists" in tr.posted[-1] and worker.sessions == {}


@pytest.mark.skipif(
    os.geteuid() == 0, reason="as root the drop succeeds; --isolation-check covers it"
)
def test_the_real_identity_needs_root(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    code = main(e, tr, models(book(), approve()), lambda *_: b"", timer=lambda: 0.0)
    assert code == 1 and "must start as root" in tr.posted[-1]


def test_dispatch_with_phase_build_runs_the_build(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    e = dataclasses.replace(
        e, event_name="workflow_dispatch", dispatch_issue="7", dispatch_phase="build"
    )
    tr.label_events = {}
    assert go(e, tr, book(), approve()) == 0 and tr.pulls


def test_an_unknown_bot_login_refuses_before_any_model_call(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.login = None
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert "bot login is unknown" in tr.posted[-1] and worker.sessions == {}
    m = last_marker(tr.posted[-1])
    assert m is not None and m.phase == "build" and m.outcome == "refused"


def test_a_bot_login_the_token_does_not_act_as_is_warned_about(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path, "identity:\n  bot_login: specster[bot]\n")
    tr.login = "other[bot]"
    assert go(e, tr, book(), approve()) == 0
    m = last_marker(tr.posted[-1])
    assert m is not None
    assert (
        "identity.bot_login is specster[bot] but the token acts as other[bot]; "
        "Specster won't recognise its own comments"
    ) in m.warnings


def test_a_run_off_the_default_branch_is_refused(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    e = dataclasses.replace(e, ref="refs/heads/feature")
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert "The workflow ran on refs/heads/feature, not on the default branch main" in tr.posted[-1]
    assert worker.sessions == {} and "ai-build" not in tr.issue.labels


def paid(cost: float) -> str:
    m = RunMetrics(run_id="1", outcome="spec", provider="fake", model="fake-1", cost_usd=cost)
    return f"earlier run\n{encode_marker(m)}"


def test_a_spent_issue_budget_stops_before_any_model_call(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.comments.insert(0, bot_comment(0, paid(8.5), T0 - timedelta(days=1)))
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 0
    assert "Budget for this issue is spent" in tr.posted[-1] and worker.sessions == {}
    assert "ai-build" not in tr.issue.labels
    assert (tmp_path / "out.txt").read_text() == "outcome=budget_exhausted\n"


def test_a_build_that_would_outrun_the_issue_budget_starts_with_a_warning(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.comments.insert(0, bot_comment(0, paid(6.0), T0 - timedelta(days=1)))
    assert go(e, tr, book(), approve()) == 0
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "pr_opened"
    assert any("this build stops at $2.00" in w for w in m.warnings), m.warnings


class Broken(Sandbox):
    """Fails the final tests only, after the worker has been billed."""

    def run(
        self, argv: Sequence[str], cwd: Path, home: Path, label: str, reap: bool = True
    ) -> RunResult:
        if label == "tests final":
            raise SandboxError("uid 61000 survived the kill")
        return super().run(argv, cwd, home, label, reap)


def test_a_sandbox_error_bills_the_run_asks_for_a_human_and_pushes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, tr, remote = world(tmp_path)
    monkeypatch.setattr("specster.usecases.build_phase.Sandbox", Broken)
    worker = book()
    assert go(e, tr, worker, approve()) == 1
    assert "survived the kill" in tr.posted[-1] and tr.pulls == []
    assert "ai-build" not in tr.issue.labels and "needs-human" in tr.issue.labels
    m = last_marker(tr.posted[-1])
    assert m is not None and m.phase == "build" and m.outcome == "error"
    assert "worker" in m.roles and m.turns > 0
    assert m.tasks_total == 1 and m.tasks_done == 0
    assert (remote / "refs" / "heads").exists() and not list((remote / "refs" / "heads").iterdir())


class BrokenMidTask(Sandbox):
    def run(
        self, argv: Sequence[str], cwd: Path, home: Path, label: str, reap: bool = True
    ) -> RunResult:
        if label == "tests a":
            raise SandboxError("uid 61001 survived the kill")
        return super().run(argv, cwd, home, label, reap)


def test_a_sandbox_error_mid_task_bills_the_worker_turns_paid_so_far(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, tr, _ = world(tmp_path)
    monkeypatch.setattr("specster.usecases.build_phase.Sandbox", BrokenMidTask)
    assert go(e, tr, book(), approve()) == 1
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "error" and m.roles["worker"].turns == 2
    assert m.cost_usd is not None and m.cost_usd > 0 and m.cost_usd == m.roles["worker"].cost_usd
    assert m.tasks_total == 1 and "0 of 1 tasks" in tr.posted[-1]


def test_a_refused_pull_request_says_the_branch_was_pushed(tmp_path: Path) -> None:
    e, tr, remote = world(tmp_path)
    tr.pull_error = "GitHub Actions is not permitted to create or approve pull requests."
    assert go(e, tr, book(), approve()) == 1
    assert "not permitted" in tr.posted[-1] and "The branch was pushed" in tr.posted[-1]
    assert make_repo_log(remote) == ["chore: init", "feat(app): set A"]
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "error" and set(m.roles) == {"worker", "reviewer"}


def test_no_low_budget_warning_without_a_per_build_cap(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path, "budget:\n  max_usd_per_build: null\n")
    tr.comments.insert(0, bot_comment(0, paid(6.0), T0 - timedelta(days=1)))
    assert go(e, tr, book(), approve()) == 0
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "pr_opened"
    assert not any("this build stops at" in w for w in m.warnings), m.warnings


def test_a_pull_request_refused_for_another_reason_names_the_status(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.pull_error, tr.pull_status = "create pull request: HTTP 422: Validation Failed", 422
    assert go(e, tr, book(), approve()) == 1
    assert "refused the pull request (HTTP 422)" in tr.posted[-1]
    assert "Allow GitHub Actions" not in tr.posted[-1]


def test_a_failed_push_after_a_paid_build_asks_for_a_human(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    e = dataclasses.replace(e, server_url=str(tmp_path / "nowhere"))
    assert go(e, tr, book(), approve()) == 1
    assert "the branch could not be pushed: git push failed" in tr.posted[-1] and tr.pulls == []
    assert "ai-build" not in tr.issue.labels and "needs-human" in tr.issue.labels
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "error" and set(m.roles) == {"worker", "reviewer"}
    assert (m.tasks_total, m.tasks_done) == (1, 1) and m.test_runs >= 2
    assert "Build: 1 of 1 tasks" in tr.posted[-1]
    assert "ghs_TOKEN" not in tr.posted[-1]


def test_build_hints_follow_the_comment_language(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path, "persona:\n  language: es\n")
    tr.remote_branches.add("specster/issue-7")
    assert go(e, tr, ScriptBook({}), ScriptedModel([])) == 1
    assert "Borra la rama (o fusiona su pull request)" in tr.posted[-1]


def behind(repo: Path) -> str:
    """Leave the checkout one commit behind the default branch's tip; return that tip."""
    git = Git(repo, Author("t", BOT_EMAIL), repo.parent / "behind-home")
    (repo / "later.py").write_text("L = 1\n")
    git.run("add", "--", "later.py")
    git.run("commit", "-q", "-m", "feat: later")
    tip = git.head()
    git.run("reset", "-q", "--hard", "HEAD~1")
    return tip


def test_a_checkout_behind_the_remote_default_branch_is_refused(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tip = behind(e.workspace)
    Git(e.workspace, Author("t", BOT_EMAIL), tmp_path / "h").run(
        "update-ref", "refs/remotes/origin/main", tip
    )
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert f"not at the tip of main ({tip[:12]})" in tr.posted[-1] and worker.sessions == {}
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "refused" and "ai-build" not in tr.issue.labels


def test_a_checkout_other_than_the_event_sha_is_refused(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    e = dataclasses.replace(e, sha=behind(e.workspace))
    assert go(e, tr, ScriptBook({}), ScriptedModel([])) == 1
    assert "not at the tip of main" in tr.posted[-1]


def test_a_checkout_at_the_event_sha_builds(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    head = Git(e.workspace, Author("t", BOT_EMAIL), tmp_path / "h").head()
    e = dataclasses.replace(e, sha=head)
    assert go(e, tr, book(), approve()) == 0 and tr.pulls


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_build_past_max_minutes_pushes_what_is_done_and_asks_for_a_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, tr, remote = world(tmp_path, "  max_minutes: 5\n")
    clock = Clock()

    class Late(Sandbox):
        def run(
            self, argv: Sequence[str], cwd: Path, home: Path, label: str, reap: bool = True
        ) -> RunResult:
            res = super().run(argv, cwd, home, label, reap)
            clock.now = 5 * 60.0
            return res

    monkeypatch.setattr("specster.usecases.build_phase.Sandbox", Late)
    reviewer = ScriptedModel([])
    code = main(e, tr, models(book(), reviewer), lambda *_: b"", timer=clock, identity=unprivileged)
    assert code == 0 and tr.pulls == [] and reviewer.sessions_started == 0
    assert "reached its time limit (`build.max_minutes`)" in tr.posted[-1]
    assert "build time limit reached: build.max_minutes is 5" in tr.posted[-1]
    assert make_repo_log(remote) == ["chore: init", "feat(app): set A"]
    assert tr.issue.labels == ("needs-human", "spec-ready")
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "budget_exhausted" and "worker" in m.roles


def test_a_docker_socket_other_users_can_reach_fails_before_any_model_call(
    tmp_path: Path, short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e, tr, _ = world(tmp_path)
    sock = unix_socket(short_dir / "d.sock", 0o666)
    monkeypatch.setattr("specster.usecases.build_phase.DOCKER_SOCKET", sock)
    worker = ScriptBook({})
    assert go(e, tr, worker, ScriptedModel([])) == 1
    assert f"{sock} is readable or writable by a sandbox uid" in tr.posted[-1]
    assert "Remove the Docker socket mount" in tr.posted[-1] and worker.sessions == {}


def test_an_opened_pull_request_clears_an_earlier_needs_human(tmp_path: Path) -> None:
    e, tr, _ = world(tmp_path)
    tr.issue = dataclasses.replace(tr.issue, labels=("ai-build", "needs-human", "spec-ready"))
    assert go(e, tr, book(), approve()) == 0 and tr.pulls
    assert tr.issue.labels == ("ai-pr",)


def test_the_pull_request_title_is_the_spec_title_with_no_closing_reference(
    tmp_path: Path,
) -> None:
    e, tr, _ = world(tmp_path)
    spec = SPEC | {"title": "Export  CSV & <TSV>\n fixes #3"}
    tr.comments = [bot_comment(1, spec_comment_body(spec), T0)]
    assert go(e, tr, book(), approve()) == 0
    assert tr.pulls[0][0] == "Export CSV & <TSV> fixes issue 3"


@ROOT_ONLY
@pytest.mark.parametrize("broken", [False, True])
def test_every_build_gives_the_git_dir_back_to_the_workspace_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    e, tr, _ = world(tmp_path)
    for p in [e.workspace, *e.workspace.rglob("*")]:
        os.chown(p, 4321, 4321, follow_symlinks=False)
    if broken:
        monkeypatch.setattr("specster.usecases.build_phase.Sandbox", Broken)
    assert go(e, tr, book(), approve()) == (1 if broken else 0)
    git_dir = e.workspace / ".git"
    assert {(p.lstat().st_uid, p.lstat().st_gid) for p in [git_dir, *git_dir.rglob("*")]} == {
        (4321, 4321)
    }


def test_the_build_footer_names_the_build_and_review_skills(tmp_path: Path) -> None:
    skills = (
        "skills:\n  autodiscover: false\n  sources:\n"
        "    - {path: tdd.md, phases: [build]}\n"
        "    - {path: arch.md, phases: [review]}\n"
    )
    e, tr, _ = world(tmp_path, skills)
    (e.workspace / "tdd.md").write_text("---\nname: tdd\n---\nTest first.\n")
    (e.workspace / "arch.md").write_text("---\nname: arch\n---\nKeep it small.\n")
    assert go(e, tr, book(), approve()) == 0
    m = last_marker(tr.posted[-1])
    assert m is not None and m.skills_available == ["tdd", "arch"]
    assert m.skills_inlined == ["tdd", "arch"]
    assert "- Skills: 2 available; loaded: tdd, arch;" in tr.posted[-1]


# serve.py runs the base routes until the task sets A = 1.
SERVE = 'import runpy, app\nrunpy.run_path("base_app.py" if app.A == 0 else "head_app.py")\n'
EVIDENCE = [
    {"name": "users", "method": "GET", "path": "/users", "why": "w"},
    {"name": "new", "method": "GET", "path": "/new", "why": "w"},
]


def evidence_world(tmp_path: Path) -> tuple[Env, FakeTracker, Path]:
    port = free_port()
    serve = json.dumps([sys.executable, "serve.py"])
    config = f"  preview:\n    serve_command: {serve}\n"
    config += f"    ready_url: http://127.0.0.1:{port}/users\n"
    files = {
        "serve.py": SERVE,
        "base_app.py": app(port, BASE_ROUTES),
        "head_app.py": app(port, HEAD_ROUTES),
    }
    return world(tmp_path, config, files=files, evidence=EVIDENCE)


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_pr_gets_evidence_files_and_links(tmp_path: Path) -> None:
    e, tr, remote = evidence_world(tmp_path)
    assert go(e, tr, book(), approve()) == 0
    body = tr.pulls[0][1]
    assert ls_tree(remote, "specster-evidence") == [
        "pr-1/new.base.txt",
        "pr-1/new.diff",
        "pr-1/new.head.json",
        "pr-1/server-base.log",
        "pr-1/server-head.log",
        "pr-1/users.base.json",
        "pr-1/users.diff",
        "pr-1/users.head.json",
    ]
    assert f"{tmp_path}/remote/o/r/tree/specster-evidence/pr-1" in body
    assert "Before and after" in body and "| `new` | `GET /new` | 404 → 200 | yes |" in body
    assert "could not be uploaded" not in tr.posted[-1]


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_upload_failure_keeps_the_pr_and_says_why(tmp_path: Path) -> None:
    e, tr, remote = evidence_world(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nwhile read old new ref; do\n"
        '  [ "$ref" = refs/heads/specster-evidence ] && exit 1\ndone\nexit 0\n'
    )
    hook.chmod(0o755)
    assert go(e, tr, book(), approve()) == 0
    body = tr.pulls[0][1]
    assert "Before and after" in body and "specster-evidence" not in body
    assert make_repo_log(remote) == ["chore: init", "feat(app): set A"]
    assert "could not be uploaded" in tr.posted[-1] and "pull/1" in tr.posted[-1]
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "pr_opened"


class _BlockedScratch(FakeTracker):
    """Puts a file where the evidence upload wants its scratch directory, once the PR exists."""

    def create_pull(self, title: str, body: str, head: str, base: str) -> PullRequest:
        pull = super().create_pull(title, body, head, base)
        (scratch,) = Path(tempfile.gettempdir()).glob("specster-build-*")
        (scratch / "evidence-branch").write_text("in the way")
        return pull


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_an_os_error_in_the_upload_still_finishes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    e, tr, _ = evidence_world(tmp_path)
    tr = _BlockedScratch(**{f.name: getattr(tr, f.name) for f in dataclasses.fields(tr)})
    assert go(e, tr, book(), approve()) == 0
    assert "Before and after" in tr.pulls[0][1] and "specster-evidence" not in tr.pulls[0][1]
    assert "could not be uploaded" in tr.posted[-1] and "File exists" in tr.posted[-1]
    m = last_marker(tr.posted[-1])
    assert m is not None and m.outcome == "pr_opened" and tr.issue.labels == ("ai-pr",)


class _NoPatch(FakeTracker):
    """Updates the pull request through the real client, which GitHub answers with a 502."""

    def update_pull(self, number: int, body: str) -> None:
        down = httpx.MockTransport(lambda _: httpx.Response(502, json={"message": "Server Error"}))
        GitHubRest("o/r", "tok", transport=down).update_pull(number, body)


@pytest.mark.block_network(allowed_hosts=["127.0.0.1"])
def test_a_failed_link_says_the_files_are_on_the_branch(tmp_path: Path) -> None:
    e, tr, remote = evidence_world(tmp_path)
    tr = _NoPatch(**{f.name: getattr(tr, f.name) for f in dataclasses.fields(tr)})
    assert go(e, tr, book(), approve()) == 0
    assert "pr-1/users.diff" in ls_tree(remote, "specster-evidence")
    assert "specster-evidence" not in tr.pulls[0][1]
    comment = tr.posted[-1]
    assert "could not be uploaded" not in comment
    assert "could not be updated with the link: update pull request: HTTP 502" in comment
    m = last_marker(comment)
    assert m is not None and m.outcome == "pr_opened"
