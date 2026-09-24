import socket
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from specster.config import PersonaConfig
from specster.git import BOT_EMAIL, Author, Git
from specster.github import Comment, GitHubError, Issue, PullRequest
from specster.llm.base import ToolCall, ToolResult, ToolSpec, Turn, Usage
from specster.metrics import RunMetrics
from specster.plan import normalize_plan
from specster.render import RenderContext, render_spec
from specster.schemas import SpecResult


@dataclass
class FakeTracker:
    issue: Issue
    comments: list[Comment] = field(default_factory=list)
    label_events: dict[str, datetime] = field(default_factory=dict)
    edited_at: datetime | None = None
    repo_labels: set[str] = field(default_factory=set)
    posted: list[str] = field(default_factory=list)
    now: datetime = datetime(2026, 1, 1, 12, tzinfo=UTC)
    login: str | None = None
    default: str = "main"
    remote_branches: set[str] = field(default_factory=set)
    pulls: list[tuple[str, str, str, str]] = field(default_factory=list)
    pull_error: str | None = None
    pull_status: int = 403

    def get_issue(self, number: int) -> Issue:
        return self.issue

    def list_comments(self, number: int) -> list[Comment]:
        return list(self.comments)

    def label_applied_at(self, number: int, label: str) -> datetime | None:
        return self.label_events.get(label)

    def body_edited_at(self, number: int) -> datetime | None:
        return self.edited_at

    def ensure_labels(self, labels: Mapping[str, str]) -> None:
        self.repo_labels |= set(labels)

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        self.issue = _with_labels(self.issue, set(self.issue.labels) | set(labels))

    def remove_label(self, number: int, label: str) -> None:
        self.issue = _with_labels(self.issue, set(self.issue.labels) - {label})

    def post_comment(self, number: int, body: str) -> None:
        self.posted.append(body)

    def own_login(self) -> str | None:
        return self.login

    def default_branch(self) -> str:
        return self.default

    def branch_exists(self, branch: str) -> bool:
        return branch in self.remote_branches

    def create_pull(self, title: str, body: str, head: str, base: str) -> PullRequest:
        if self.pull_error is not None:
            raise GitHubError(self.pull_error, self.pull_status)
        self.pulls.append((title, body, head, base))
        n = len(self.pulls)
        return PullRequest(n, f"https://github.com/o/r/pull/{n}")


def _with_labels(issue: Issue, labels: set[str]) -> Issue:
    return Issue(
        issue.number,
        issue.title,
        issue.body,
        issue.author,
        issue.author_association,
        tuple(sorted(labels)),
    )


class ScriptedModel:
    provider = "fake"
    model = "fake-1"

    def __init__(self, script: list[list[ToolCall] | str]) -> None:
        self.script = list(script)
        self.sessions_started = 0
        self.user_text = ""
        self.system = ""
        self.context = ""
        self.tools: list[ToolSpec] = []
        self.received: list[tuple[ToolResult, ...]] = []
        self.nudges: list[str] = []

    def start(
        self, system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> "ScriptedModel":
        self.sessions_started += 1
        self.system, self.context, self.user_text, self.tools = system, context, user, list(tools)
        return self

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        self.received.append(tuple(results))
        if user_text:
            self.nudges.append(user_text)
        item = self.script.pop(0)
        usage = Usage(100, 50, 0, 20)
        if isinstance(item, str):
            return Turn(item, (), usage)
        return Turn("", tuple(item), usage)


class ScriptBook:
    """A ChatModel whose sessions each take the next script of the first key in the user text."""

    provider = "fake"
    model = "fake-1"

    def __init__(
        self,
        scripts: Mapping[str, list[list[list[ToolCall] | str]]],
        barrier: threading.Barrier | None = None,
    ) -> None:
        self.scripts = {key: list(queue) for key, queue in scripts.items()}
        self.barrier = barrier
        self.sessions: dict[str, list[ScriptedModel]] = {}
        self._lock = threading.Lock()

    def start(
        self, system: str, context: str, user: str, tools: Sequence[ToolSpec]
    ) -> ScriptedModel:
        with self._lock:
            key = next(k for k in self.scripts if k in user)
            session = ScriptedModel(self.scripts[key].pop(0))
            self.sessions.setdefault(key, []).append(session)
        session.start(system, context, user, tools)
        if self.barrier is not None:
            self.barrier.wait()
        return session


def spec_comment_body(spec: Mapping[str, Any], outcome: str = "spec") -> str:
    result = SpecResult.model_validate(spec)
    tasks, fixes = normalize_plan(result.tasks)
    metrics = RunMetrics.model_validate(
        {"run_id": "1", "outcome": outcome, "provider": "fake", "model": "fake-1"}
    )
    return render_spec(result, tasks, fixes, RenderContext(PersonaConfig(), metrics, (), ()))


def bot_comment(id: int, body: str, at: datetime) -> Comment:
    return Comment(id, "specster[bot]", "Bot", "NONE", body, at, at)


def make_repo(root: Path, files: Mapping[str, str]) -> Git:
    root.mkdir(parents=True)
    git = Git(root, Author("Specster", BOT_EMAIL), root.parent / f"{root.name}-home", log_argv=True)
    git.run("init", "-q", "-b", "main")
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git.run("add", "--", *files)
    git.run("commit", "-q", "-m", "chore: init")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\ntouch HOOK_RAN\nexit 1\n")
    hook.chmod(0o755)
    return git


def unix_socket(path: Path, mode: int) -> Path:
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
    path.chmod(mode)
    return path


def make_remote(root: Path) -> Path:
    root.parent.mkdir(parents=True, exist_ok=True)
    home = root.parent / f"{root.name}-home"
    Git(root.parent, Author("Specster", BOT_EMAIL), home).run("init", "-q", "--bare", str(root))
    return root


def make_repo_log(remote: Path, branch: str = "specster/issue-7") -> list[str]:
    git = Git(remote, Author("Specster", BOT_EMAIL), remote.parent / f"{remote.name}-log-home")
    out = git.run(f"--git-dir={remote}", "log", "--format=%s", "--reverse", branch)
    return out.splitlines()
