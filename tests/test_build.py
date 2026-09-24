import re
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from specster.approved import ApprovedSpec
from specster.build import (
    BuildConflict,
    BuildSetup,
    apply_changes,
    budget_stop,
    commit_changes,
    run_build,
)
from specster.config import BudgetConfig, BuildConfig, Config, ModelConfig, SkillsConfig
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.ledger import Ledger
from specster.llm.base import ChatModel, ToolCall, Usage
from specster.repomap import RepoMap
from specster.sandbox import RunResult, Sandbox, SandboxError
from specster.schemas import PlanTask
from specster.skills import load_skills
from tests.fakes import ScriptBook, ScriptedModel, make_repo
from tests.test_approved import T0, human

PASS = [sys.executable, "-c", "pass"]


def task(id: str, file: str, deps: list[str] | None = None) -> PlanTask:
    return PlanTask(
        id=id,
        title=id.upper(),
        description="d",
        files=[file],
        acceptance=["x"],
        depends_on=deps or [],
    )


def write(path: str, content: str, n: str = "w") -> list[ToolCall]:
    return [ToolCall(n, "write_file", {"path": path, "content": content})]


def done(subject: str) -> list[ToolCall]:
    return [ToolCall("s", "submit_task", {"summary": "ok", "commit_subject": subject})]


def verdict(v: str, *findings: dict[str, str]) -> list[ToolCall]:
    return [ToolCall("r", "submit_review", {"verdict": v, "findings": list(findings)})]


def lock(sb: Sandbox, repo: Path) -> Sandbox:
    sb.lock_down(repo, Git(repo, Author("t", BOT_EMAIL), repo.parent / "lock-home"), {})
    return sb


def setup(
    tmp_path: Path,
    tasks: list[PlanTask],
    workers: ScriptBook,
    reviewer: ChatModel,
    build: BuildConfig | None = None,
    budget: BudgetConfig | None = None,
    sandboxes: Callable[[int], Sandbox] | None = None,
    time_left: Callable[[], float] | None = None,
) -> BuildSetup:
    repo = tmp_path / "repo"
    git = make_repo(repo, {"app.py": "A = 0\n", "util.py": "B = 0\n"})
    cfg = Config(build=build or BuildConfig(), budget=budget or BudgetConfig())
    skills = load_skills(repo, SkillsConfig(), "build", lambda *_: b"", None)
    spec = ApprovedSpec(human(1, T0), tasks, "0" * 64, "The spec text.")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def locked(_slot: int) -> Sandbox:
        return lock(Sandbox(None, 60, 20_000, {}), repo)

    return BuildSetup(
        cfg,
        spec,
        git,
        sandboxes or locked,
        Ledger({}),
        lambda: workers,
        lambda: reviewer,
        skills,
        skills,
        RepoMap("", None, 0),
        "specster/issue-7",
        git.head(),
        scratch,
        0.0,
        cfg.persona,
        time_left,
    )


def subjects(git: Git, base: str) -> list[str]:
    return git.run("log", "--format=%s", "--reverse", f"{base}..specster/issue-7").split("\n")[:-1]


def test_a_level_runs_in_parallel_worktrees_and_commits_once_per_task(tmp_path: Path) -> None:
    book = ScriptBook(
        {
            'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]],
            'id="b"': [[write("util.py", "B = 1\n"), done("feat(b): set B")]],
        },
        barrier=threading.Barrier(2),
    )
    reviewer = ScriptedModel(
        [
            verdict(
                "approve",
                {"task_id": "a", "file": "app.py", "severity": "minor", "description": "naming"},
            )
        ]
    )
    s = setup(tmp_path, [task("a", "app.py"), task("b", "util.py")], book, reviewer)
    report = run_build(s)
    assert report.status == "approved" and report.parallel_used == 2
    assert subjects(s.git, s.base) == ["feat(a): set A", "feat(b): set B"]
    assert [f.description for f in report.minor] == ["naming"] and report.final_tests is None
    assert "No tests were run" in reviewer.system and "+A = 1" in reviewer.user_text
    assert set(s.ledger.roles()) == {"worker", "reviewer"}


def test_a_level_starts_from_the_previous_level_and_a_failure_skips_dependents(
    tmp_path: Path,
) -> None:
    read = [ToolCall("r", "read_file", {"path": "app.py"})]
    book = ScriptBook(
        {
            'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]],
            'id="b"': [[read, "no", "still no"]],
            'id="c"': [[done("feat(c): nothing")]],
        }
    )
    tasks = [task("a", "app.py"), task("b", "util.py", ["a"]), task("c", "other.py", ["b"])]
    report = run_build(setup(tmp_path, tasks, book, ScriptedModel([])))
    assert "1: A = 1" in book.sessions['id="b"'][0].received[1][0].content
    assert report.status == "failed" and report.review is None
    assert [r.status for r in report.tasks] == ["done", "failed", "skipped"]
    assert report.tasks[2].reason == "depends on b, which did not finish"


def test_blocking_findings_go_back_to_their_task_for_a_correction_round(tmp_path: Path) -> None:
    bad = {"task_id": "a", "file": "app.py", "severity": "important", "description": "A must be 2"}
    book = ScriptBook(
        {
            'id="a"': [
                [write("app.py", "A = 1\n"), done("feat(a): set A")],
                [write("app.py", "A = 2\n"), done("fix(a): set A to 2")],
            ]
        }
    )
    reviewer = ScriptedModel([verdict("changes", bad), verdict("approve")])
    s = setup(tmp_path, [task("a", "app.py")], book, reviewer)
    report = run_build(s)
    assert "A must be 2" in book.sessions['id="a"'][1].user_text
    assert report.status == "approved" and report.review_rounds == 1
    assert subjects(s.git, s.base) == ["feat(a): set A", "fix(a): set A to 2"]


def test_no_approval_after_the_last_round_leaves_the_findings_pending(tmp_path: Path) -> None:
    bad = {"task_id": "a", "file": "app.py", "severity": "critical", "description": "wrong"}
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    report = run_build(
        setup(
            tmp_path,
            [task("a", "app.py")],
            book,
            ScriptedModel([verdict("changes", bad)]),
            build=BuildConfig(max_review_rounds=0),
        )
    )
    assert report.status == "not_approved" and [f.description for f in report.pending] == ["wrong"]
    assert report.reason == "1 blocking finding left after 0 correction rounds"


def test_the_last_round_names_one_correction_round_in_the_singular(tmp_path: Path) -> None:
    bad = {"task_id": "a", "file": "app.py", "severity": "critical", "description": "wrong"}
    worse = bad | {"description": "worse"}
    book = ScriptBook(
        {
            'id="a"': [
                [write("app.py", "A = 1\n"), done("feat(a): set A")],
                [write("app.py", "A = 2\n"), done("fix(a): set A to 2")],
            ]
        }
    )
    reviewer = ScriptedModel([verdict("changes", bad), verdict("changes", bad, worse)])
    s = setup(
        tmp_path, [task("a", "app.py")], book, reviewer, build=BuildConfig(max_review_rounds=1)
    )
    report = run_build(s)
    assert report.reason == "2 blocking findings left after 1 correction round"
    exports = [a for a in s.git.argv_log if "checkout-index" in a]
    assert exports and all(not x.startswith("--work-tree") for a in exports for x in a)


def test_the_build_budget_stops_between_tasks(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    tasks = [task("a", "app.py"), task("b", "util.py", ["a"])]
    report = run_build(
        setup(
            tmp_path, tasks, book, ScriptedModel([]), budget=BudgetConfig(max_usd_per_build=0.0005)
        )
    )
    assert report.status == "budget_exhausted" and report.commits == 1
    assert (
        report.tasks[1].status == "not_started" and "build budget spent" in report.tasks[1].reason
    )


def test_final_tests_run_on_the_integrated_branch(tmp_path: Path) -> None:
    book = ScriptBook(
        {
            'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]],
            'id="b"': [[write("util.py", "B = 1\n"), done("feat(b): set B")]],
        }
    )
    reviewer = ScriptedModel([verdict("approve")])
    report = run_build(
        setup(
            tmp_path,
            [task("a", "app.py"), task("b", "util.py")],
            book,
            reviewer,
            build=BuildConfig(test_command=PASS),
        )
    )
    assert report.final_tests is not None and report.final_tests.ok and report.test_runs == 3
    assert "exit 0" in reviewer.user_text


def test_applying_onto_a_file_that_moved_is_a_specster_bug(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("changed\n")
    with pytest.raises(BuildConflict, match=r"app\.py"):
        apply_changes(tmp_path, {"app.py": "A = 1\n"}, {"app.py": b"A = 0\n"})


def test_applying_writes_every_change_and_creates_new_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("A = 0\n")
    changes = {"pkg/new.py": "N = 1\n", "app.py": "A = 1\n"}
    paths = apply_changes(tmp_path, changes, {"app.py": b"A = 0\n", "pkg/new.py": None})
    assert paths == ["app.py", "pkg/new.py"]
    assert (tmp_path / "app.py").read_text() == "A = 1\n"
    assert (tmp_path / "pkg" / "new.py").read_text() == "N = 1\n"
    with pytest.raises(BuildConflict, match=r"new\.py"):
        apply_changes(tmp_path, {"pkg/new.py": "N = 2\n"}, {"pkg/new.py": None})


def test_applying_never_follows_a_symlink_in_the_integration_tree(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "pkg").symlink_to(outside)
    with pytest.raises(BuildConflict, match="symlink"):
        apply_changes(tree, {"pkg/x.py": "X = 1\n"}, {"pkg/x.py": None})
    assert not (outside / "x.py").exists()


def test_the_budget_stop_names_the_cap_that_was_reached() -> None:
    ledger = Ledger({})
    assert budget_stop(ledger, BudgetConfig(), 0.0) is None
    ledger.add("worker", ModelConfig(model="claude-sonnet-5"), Usage(1_000_000, 0, 0, 0), 1)
    assert budget_stop(ledger, BudgetConfig(max_usd_per_build=2.0), 0.0) == (
        "build budget spent: $2.00 of $2.00"
    )
    assert budget_stop(ledger, BudgetConfig(max_usd_per_issue=3.0), 1.5) == (
        "issue budget spent: $3.50 of $3.00"
    )
    assert (
        budget_stop(ledger, BudgetConfig(max_usd_per_build=None, max_usd_per_issue=None), 9) is None
    )


def test_a_test_run_cannot_change_the_originals_the_changes_are_checked_against(
    tmp_path: Path,
) -> None:
    clobber = [sys.executable, "-c", "open('app.py', 'w').write('A = 9\\n')"]
    run_tests = [ToolCall("t", "run_tests", {})]
    book = ScriptBook(
        {'id="a"': [[run_tests, write("app.py", "A = 1\n") + done("feat(a): set A")]]}
    )
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([verdict("approve")]),
        build=BuildConfig(test_command=clobber),
    )
    report = run_build(s)
    assert report.status == "approved" and report.commits == 1
    assert s.git.run("show", "specster/issue-7:app.py") == "A = 1\n"


def test_each_worker_tree_is_a_fresh_repo_in_an_enclosure_and_is_removed_after(
    tmp_path: Path,
) -> None:
    check = [
        sys.executable,
        "-c",
        (
            "import os, subprocess\n"
            "assert os.path.isdir('.git'), 'not a repo of its own'\n"
            "assert os.stat('..').st_mode & 0o777 == 0o750, oct(os.stat('..').st_mode)\n"
            "git = lambda *a: subprocess.run(['git', *a], capture_output=True, text=True).stdout\n"
            "assert git('remote') == '', git('remote')\n"
            "assert len(git('log', '--format=%H').split()) == 1\n"
            "assert git('status', '--porcelain') == '', git('status', '--porcelain')\n"
        ),
    ]
    book = ScriptBook({'id="a"': [[done("feat(a): nothing yet")]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([]),
        build=BuildConfig(test_command=check),
    )
    report = run_build(s)
    assert report.tasks[0].status == "done", report.tasks[0].reason
    assert report.warnings == ["a changed no files"] and report.status == "failed"
    assert list(s.scratch.iterdir()) == []
    assert s.git.run("worktree", "list", "--porcelain").count("worktree ") == 1


class SlotSandbox(Sandbox):
    """Records which slot each run used and whether two runs ever shared a slot at once."""

    def __init__(self, slot: int, repo: Path, log: "SlotLog") -> None:
        super().__init__(None, 60, 20_000, {})
        self.slot, self.log = slot, log
        lock(self, repo)

    def run(self, argv: Sequence[str], cwd: Path, home: Path, label: str) -> RunResult:
        with self.log.lock:
            self.log.active[self.slot] = self.log.active.get(self.slot, 0) + 1
            self.log.shared |= self.log.active[self.slot] > 1
            self.log.runs.append((self.slot, label))
        try:
            return super().run(argv, cwd, home, label)
        finally:
            with self.log.lock:
                self.log.active[self.slot] -= 1


class SlotLog:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active: dict[int, int] = {}
        self.runs: list[tuple[int, str]] = []
        self.shared = False
        self.made: list[int] = []


def test_workers_and_final_tests_each_hold_a_sandbox_slot_of_their_own(tmp_path: Path) -> None:
    book = ScriptBook(
        {
            'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]],
            'id="b"': [[write("util.py", "B = 1\n"), done("feat(b): set B")]],
        },
        barrier=threading.Barrier(2),
    )
    log = SlotLog()
    slow = [sys.executable, "-c", "import time; time.sleep(0.2)"]

    def make(slot: int) -> Sandbox:
        log.made.append(slot)
        return SlotSandbox(slot, tmp_path / "repo", log)

    s = setup(
        tmp_path,
        [task("a", "app.py"), task("b", "util.py")],
        book,
        ScriptedModel([verdict("approve")]),
        build=BuildConfig(test_command=slow),
        sandboxes=make,
    )
    report = run_build(s)
    assert report.status == "approved" and report.parallel_used == 2 and not log.shared
    workers = {slot for slot, label in log.runs if label.startswith("tests ") and slot != 0}
    assert workers == {1, 2} and sorted(set(log.made)) == [0, 1, 2]
    assert [slot for slot, label in log.runs if label == "tests final"] == [0]


def test_a_sandbox_error_in_a_worker_is_fatal_for_the_build(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[done("feat(a): set A")]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([]),
        build=BuildConfig(test_command=PASS),
        sandboxes=lambda _slot: Sandbox(None, 60, 20_000, {}),
    )
    with pytest.raises(SandboxError, match="lock_down"):
        run_build(s)
    assert list(s.scratch.iterdir()) == []


def test_a_sandbox_error_in_the_final_tests_is_fatal_for_the_build(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})

    def make(slot: int) -> Sandbox:
        sb = Sandbox(None, 60, 20_000, {})
        return sb if slot == 0 else lock(sb, tmp_path / "repo")

    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([verdict("approve")]),
        build=BuildConfig(test_command=PASS),
        sandboxes=make,
    )
    with pytest.raises(SandboxError, match="lock_down"):
        run_build(s)


def test_the_reviewer_gets_a_nonce_no_worker_ever_saw(tmp_path: Path) -> None:
    bad = {"task_id": "a", "file": "app.py", "severity": "important", "description": "A must be 2"}
    book = ScriptBook(
        {
            'id="a"': [
                [write("app.py", "A = 1\n"), done("feat(a): set A")],
                [write("app.py", "A = 2\n"), done("fix(a): set A to 2")],
            ],
            'id="b"': [[write("util.py", "B = 1\n"), done("feat(b): set B")]],
        }
    )
    reviewer = ScriptBook({"<diff-": [[verdict("changes", bad)], [verdict("approve")]]})
    report = run_build(setup(tmp_path, [task("a", "app.py"), task("b", "util.py")], book, reviewer))
    assert report.status == "approved"
    workers = [m.user_text for sessions in book.sessions.values() for m in sessions]
    reviews = [m.user_text for m in reviewer.sessions["<diff-"]]
    task_nonces = [re.findall(r"<task-([0-9a-f]+) ", u)[0] for u in workers]
    review_nonces = [re.findall(r"<diff-([0-9a-f]+)>", u)[0] for u in reviews]
    assert len(task_nonces) == 3 and len(review_nonces) == 2
    assert len(set(task_nonces + review_nonces)) == 5


def test_a_diff_over_the_cap_is_cut_and_reported(tmp_path: Path) -> None:
    big = "".join(f"LINE_{i} = {i}\n" for i in range(12_000))
    book = ScriptBook({'id="a"': [[write("app.py", big), done("feat(a): grow A")]]})
    reviewer = ScriptedModel([verdict("approve")])
    s = setup(tmp_path, [task("a", "app.py")], book, reviewer)
    report = run_build(s)
    total = len(s.git.diff(s.base, "specster/issue-7"))
    note = f"diff cut to the first 150,000 of {total:,} characters"
    assert total > 150_000 and report.truncations == [note] and note in reviewer.user_text


def test_applying_refuses_a_git_path_component(tmp_path: Path) -> None:
    with pytest.raises(BuildConflict, match=r"sub/\.GIT"):
        apply_changes(tmp_path, {"sub/.GIT": "gitdir: /x\n"}, {"sub/.GIT": None})
    assert not (tmp_path / "sub").exists()


def test_a_path_the_commit_dropped_is_a_conflict_never_a_silent_loss(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "A = 0\n"})
    other = make_repo(tmp_path / "other", {"o.py": "O = 0\n"})
    (git.repo / "sub").mkdir()
    (git.repo / "sub" / ".git").write_text(f"gitdir: {other.repo / '.git'}\n")
    changes = {"app.py": "A = 1\n", "sub/x.py": "X = 1\n"}
    with pytest.raises(BuildConflict, match=r"commit dropped paths: sub/x\.py"):
        commit_changes(git, git.repo, changes, {"app.py": b"A = 0\n", "sub/x.py": None}, "feat: x")


def test_an_unchanged_rewrite_is_not_a_dropped_path(tmp_path: Path) -> None:
    git = make_repo(tmp_path / "repo", {"app.py": "A = 0\n", "util.py": "B = 0\n"})
    changes = {"app.py": "A = 1\n", "util.py": "B = 0\n"}
    sha = commit_changes(
        git, git.repo, changes, {"app.py": b"A = 0\n", "util.py": b"B = 0\n"}, "feat: a"
    )
    assert sha is not None and git.run("show", "--name-only", "--format=", sha) == "app.py\n"
    same = {"app.py": "A = 1\n"}
    assert commit_changes(git, git.repo, same, {"app.py": b"A = 1\n"}, "feat: b") is None


def test_a_fatal_error_stops_the_queued_workers_of_the_level(tmp_path: Path) -> None:
    book = ScriptBook({f'id="{t}"': [[done(f"feat({t}): x")]] for t in "abc"})
    tasks = [task("a", "app.py"), task("b", "util.py"), task("c", "other.py")]
    s = setup(
        tmp_path,
        tasks,
        book,
        ScriptedModel([]),
        build=BuildConfig(test_command=PASS, max_parallel=1),
        sandboxes=lambda _slot: Sandbox(None, 60, 20_000, {}),
    )
    with pytest.raises(SandboxError, match="lock_down"):
        run_build(s)
    assert list(book.sessions) == ['id="a"']


def test_a_failing_worktree_drop_never_masks_the_build_or_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_path: Path) -> None:
        raise GitError("git worktree failed (1): prune broke")

    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([verdict("approve")]))
    monkeypatch.setattr(s.git, "drop_worktree", broken)
    assert run_build(s).status == "approved"
    assert "could not drop the integration worktree: git worktree failed" in capsys.readouterr().err
    fatal = setup(
        tmp_path / "b",
        [task("a", "app.py")],
        ScriptBook({'id="a"': [[done("feat(a): set A")]]}),
        ScriptedModel([]),
        build=BuildConfig(test_command=PASS),
        sandboxes=lambda _slot: Sandbox(None, 60, 20_000, {}),
    )
    monkeypatch.setattr(fatal.git, "drop_worktree", broken)
    with pytest.raises(SandboxError, match="lock_down"):
        run_build(fatal)


class FailsOnWorkerTests(Sandbox):
    def run(self, argv: Sequence[str], cwd: Path, home: Path, label: str) -> RunResult:
        if label == "tests a":
            raise SandboxError("uid 61001 survived the kill")
        return super().run(argv, cwd, home, label)


def test_a_sandbox_error_mid_task_still_bills_the_worker_turns(tmp_path: Path) -> None:
    run_tests = [ToolCall("t", "run_tests", {})]
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), run_tests]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([]),
        build=BuildConfig(test_command=PASS),
        sandboxes=lambda _slot: lock(FailsOnWorkerTests(None, 60, 20_000, {}), tmp_path / "repo"),
    )
    with pytest.raises(SandboxError, match="survived the kill"):
        run_build(s)
    worker = s.ledger.roles()["worker"]
    assert worker.turns == 2 and worker.input_tokens == 200
    assert worker.cost_usd is not None and worker.cost_usd > 0
    assert s.ledger.cost() == s.ledger.known_cost() == worker.cost_usd


def test_a_worker_that_dies_without_its_usage_makes_the_cost_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr("specster.build.run_worker", boom)
    s = setup(tmp_path, [task("a", "app.py")], ScriptBook({}), ScriptedModel([]))
    with pytest.raises(RuntimeError, match="boom"):
        run_build(s)
    assert s.ledger.cost() is None and "worker" in s.ledger.roles()


def test_a_reviewer_that_fails_is_still_billed(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel(["no", "still no"]))
    report = run_build(s)
    assert report.status == "failed" and report.reason.startswith("reviewer:")
    assert s.ledger.roles()["reviewer"].turns == 2


def test_a_worker_subject_that_closes_an_issue_never_reaches_the_commit(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("fix(a): fix #3")]]})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([verdict("approve")]))
    assert run_build(s).status == "approved"
    assert subjects(s.git, s.base) == ["feat(a): A"]


class Clock:
    """Seconds left before build.max_minutes; a test run of `label` uses them all up."""

    def __init__(self, label: str) -> None:
        self.label, self.left = label, 3600.0

    def time_left(self) -> float:
        return self.left


class LateSandbox(Sandbox):
    def __init__(self, repo: Path, clock: Clock) -> None:
        super().__init__(None, 60, 20_000, {}, time_left=clock.time_left)
        self.clock = clock
        lock(self, repo)

    def run(self, argv: Sequence[str], cwd: Path, home: Path, label: str) -> RunResult:
        res = super().run(argv, cwd, home, label)
        if label == self.clock.label:
            self.clock.left = 0.0
        return res


def late_setup(tmp_path: Path, clock: Clock, tasks: list[PlanTask], book: ScriptBook) -> BuildSetup:
    repo = tmp_path / "repo"
    return setup(
        tmp_path,
        tasks,
        book,
        ScriptedModel([]),
        build=BuildConfig(test_command=PASS, max_minutes=1),
        sandboxes=lambda _slot: LateSandbox(repo, clock),
        time_left=clock.time_left,
    )


def test_the_time_limit_stops_between_tasks_like_the_budget(tmp_path: Path) -> None:
    clock = Clock("tests a")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    tasks = [task("a", "app.py"), task("b", "util.py", ["a"])]
    report = run_build(late_setup(tmp_path, clock, tasks, book))
    assert report.status == "budget_exhausted" and report.out_of_time and report.commits == 1
    assert report.tasks[1].status == "not_started"
    assert report.tasks[1].reason == "build time limit reached: build.max_minutes is 1"


def test_the_time_limit_stops_a_worker_before_its_next_paid_turn(tmp_path: Path) -> None:
    clock = Clock("tests a")
    run_tests = [ToolCall("t", "run_tests", {})]
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), run_tests, done("feat(a): set A")]]})
    s = late_setup(tmp_path, clock, [task("a", "app.py")], book)
    report = run_build(s)
    assert report.status == "budget_exhausted" and report.out_of_time and report.commits == 0
    assert report.reason == "build time limit reached: build.max_minutes is 1"
    assert "build.max_minutes" in report.tasks[0].reason
    assert s.ledger.turns() == 2


def test_the_time_limit_stops_after_the_final_tests(tmp_path: Path) -> None:
    clock = Clock("tests final")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    report = run_build(late_setup(tmp_path, clock, [task("a", "app.py")], book))
    assert report.status == "budget_exhausted" and report.out_of_time and report.commits == 1
    assert report.final_tests is not None and report.review is None
