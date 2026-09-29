import os
import re
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from specster.approved import ApprovedSpec
from specster.build import (
    BuildConflict,
    BuildSetup,
    _freeze,
    apply_changes,
    budget_stop,
    commit_changes,
    run_build,
)
from specster.config import BudgetConfig, BuildConfig, Config, ModelConfig, SkillsConfig
from specster.git import BOT_EMAIL, Author, Git, GitError
from specster.ledger import Ledger
from specster.llm.base import ChatModel, ToolCall, ToolResult, ToolSpec, Turn, Usage
from specster.repomap import RepoMap
from specster.sandbox import RunResult, Sandbox, SandboxError
from specster.schemas import PlanTask
from specster.skills import load_skills
from specster.workspace import CONFIG_PATH
from tests.fakes import ScriptBook, ScriptedModel, make_repo
from tests.test_approved import T0, human
from tests.test_sandbox import ROOT_ONLY

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
    gate_base: bool = False,
    files: Mapping[str, str] | None = None,
    escalation: ChatModel | None = None,
) -> BuildSetup:
    repo = tmp_path / "repo"
    git = make_repo(repo, {"app.py": "A = 0\n", "util.py": "B = 0\n", **(files or {})})
    # The fixtures' test commands fail on the base on purpose, so only the base-gate tests gate.
    build = (build or BuildConfig()).model_copy(update={"allow_failing_base": not gate_base})
    cfg = Config(build=build, budget=budget or BudgetConfig())
    if escalation is not None:
        stronger = ModelConfig(provider="anthropic", model="claude-opus-5-5")
        cfg = cfg.model_copy(
            update={"models": cfg.models.model_copy(update={"escalation": stronger})}
        )
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
        CONFIG_PATH,
        (lambda: escalation) if escalation is not None else None,
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


def test_a_failed_task_keeps_its_last_test_run_for_the_comment(tmp_path: Path) -> None:
    fail = [sys.executable, "-c", "raise SystemExit(3)"]
    book = ScriptBook({'id="a"': [[done("feat(a): set A"), done("feat(a): set A")]]})
    build = BuildConfig(test_command=fail, max_turns_per_task=2)
    report = run_build(setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([]), build))
    last = report.tasks[0].last_tests
    assert report.tasks[0].status == "failed" and last is not None and last.exit_code == 3


FAKE_MISE = """\
import os, pathlib, shutil, sys
pathlib.Path(sys.argv[0]).with_name("calls").open("a").write(sys.argv[1] + "\\n")
data = pathlib.Path(os.environ["MISE_DATA_DIR"])
bin_dir = data / "installs" / "faketool" / "1" / "bin"
if sys.argv[1] == "install":
    if "broken" in pathlib.Path(os.environ["MISE_GLOBAL_CONFIG_FILE"]).read_text():
        sys.exit("mise ERROR no such tool: broken")
    bin_dir.mkdir(parents=True)
    tool = bin_dir / "faketool"
    tool.write_text("#!/bin/sh\\nexit 0\\n")
    tool.chmod(0o755)
    shutil.copy(os.environ["MISE_GLOBAL_CONFIG_FILE"], pathlib.Path(sys.argv[0]).with_name("asked"))
else:
    print("mise WARN a newer mise is available")
    print(bin_dir)
    print("/etc")
"""


def fake_mise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    mise = tmp_path / "mise"
    mise.write_text(f"#!{sys.executable}\n{FAKE_MISE}")
    mise.chmod(0o755)
    monkeypatch.setattr("specster.build.MISE", mise)
    return mise


def test_declared_toolchains_are_installed_once_and_put_on_every_command_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_asked = fake_mise(tmp_path, monkeypatch).with_name("asked")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    build = BuildConfig(test_command=["faketool"], tools={"node": "22"})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([verdict("approve")]), build)
    report = run_build(s)
    assert report.status == "approved" and report.final_tests is not None
    assert report.final_tests.ok
    assert fake_asked.read_text() == '[tools]\nnode = "22"\n'
    assert not (s.scratch / "tools").exists()


def test_a_version_file_in_the_repo_is_enough_to_install_its_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_mise(tmp_path, monkeypatch).with_name("calls")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([verdict("approve")]),
        BuildConfig(test_command=["faketool"]),
        files={".nvmrc": "22\n"},
    )
    assert run_build(s).status == "approved" and calls.read_text() == "install\nbin-paths\n"


def test_a_repo_that_declares_no_toolchain_never_runs_mise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_mise(tmp_path, monkeypatch).with_name("calls")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    build = BuildConfig(test_command=PASS)
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([verdict("approve")]), build)
    assert run_build(s).status == "approved" and not calls.exists()


def test_toolchains_that_fail_to_install_stop_the_build_with_mise_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mise(tmp_path, monkeypatch)
    book = ScriptBook({'id="a"': [[done("feat(a): set A")]]})
    build = BuildConfig(test_command=PASS, tools={"broken": "1"})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([]), build)
    report = run_build(s)
    assert report.status == "failed" and report.reason == "the toolchains could not be installed"
    assert report.final_tests is not None and "no such tool: broken" in report.final_tests.output
    assert book.sessions == {}


def test_build_tools_without_mise_in_the_image_is_said_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("specster.build.MISE", tmp_path / "no-mise")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    build = BuildConfig(test_command=PASS, tools={"node": "22"})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([verdict("approve")]), build)
    report = run_build(s)
    assert report.status == "approved"
    assert any("toolchains are declared" in w and "none installed" in w for w in report.warnings)


@ROOT_ONLY
def test_frozen_toolchains_belong_to_root_with_no_setuid_bit(tmp_path: Path) -> None:
    root = tmp_path / "tools"
    (root / "bin").mkdir(parents=True)
    tool = root / "bin" / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o6777)
    (root / "link").symlink_to("/etc/passwd")
    for path in (root, root / "bin", tool):
        os.chown(path, 61001, 61001, follow_symlinks=False)
    _freeze(root)
    assert tool.stat().st_uid == 0 and stat.S_IMODE(tool.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "bin").stat().st_mode) & 0o022 == 0
    assert Path("/etc/passwd").stat().st_uid == 0 and (root / "link").is_symlink()


def test_a_red_base_stops_the_build_before_any_worker_is_paid(tmp_path: Path) -> None:
    fail = [sys.executable, "-c", "raise SystemExit(4)"]
    book = ScriptBook({'id="a"': [[done("feat(a): set A")]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([]),
        BuildConfig(test_command=fail),
        gate_base=True,
    )
    report = run_build(s)
    assert report.status == "failed" and "the tests fail on the base commit" in report.reason
    assert "build.allow_failing_base: true" in report.reason
    assert report.final_tests is not None and report.final_tests.exit_code == 4
    assert book.sessions == {} and s.ledger.turns() == 0 and report.test_runs == 1


def test_a_green_base_is_checked_once_and_the_build_goes_on(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    reviewer = ScriptedModel([verdict("approve")])
    build = BuildConfig(test_command=PASS)
    report = run_build(
        setup(tmp_path, [task("a", "app.py")], book, reviewer, build, gate_base=True)
    )
    assert report.status == "approved" and report.test_runs == 3


def never_submits() -> list[list[ToolCall] | str]:
    return ["thinking", "still thinking"]


def test_a_failed_task_gets_one_more_try_with_the_escalation_model(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [never_submits()]})
    stronger = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        ScriptedModel([verdict("approve")]),
        escalation=stronger,
    )
    report = run_build(s)
    assert report.status == "approved" and report.tasks[0].escalated_to == "claude-opus-5-5"
    assert set(s.ledger.roles()) == {"worker", "worker-escalated", "reviewer"}
    assert subjects(s.git, s.base) == ["feat(a): set A"]


def test_a_task_that_fails_with_both_models_stays_failed(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [never_submits()]})
    stronger = ScriptBook({'id="a"': [never_submits()]})
    s = setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([]), escalation=stronger)
    report = run_build(s)
    assert report.status == "failed" and report.tasks[0].status == "failed"
    assert report.tasks[0].escalated_to == "claude-opus-5-5"
    assert len(stronger.sessions['id="a"']) == 1


def test_without_an_escalation_model_a_failed_task_is_not_retried(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [never_submits()]})
    report = run_build(setup(tmp_path, [task("a", "app.py")], book, ScriptedModel([])))
    assert report.status == "failed" and report.tasks[0].escalated_to is None
    assert len(book.sessions['id="a"']) == 1


def test_a_spent_build_budget_skips_the_escalation_and_says_so(tmp_path: Path) -> None:
    book = ScriptBook({'id="a"': [never_submits()]})
    stronger = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    budget = BudgetConfig(max_usd_per_build=0.000001)
    s = setup(
        tmp_path, [task("a", "app.py")], book, ScriptedModel([]), budget=budget, escalation=stronger
    )
    report = run_build(s)
    assert report.tasks[0].escalated_to is None and "a" not in stronger.sessions
    assert any(w.startswith("a was not escalated: ") for w in report.warnings)


def test_a_task_blocked_again_after_a_correction_round_is_escalated(tmp_path: Path) -> None:
    bad = {"task_id": "a", "file": "app.py", "severity": "important", "description": "A must be 3"}
    book = ScriptBook(
        {
            'id="a"': [
                [write("app.py", "A = 1\n"), done("feat(a): set A")],
                [write("app.py", "A = 2\n"), done("fix(a): set A to 2")],
            ]
        }
    )
    stronger = ScriptBook({'id="a"': [[write("app.py", "A = 3\n"), done("fix(a): set A to 3")]]})
    reviewer = ScriptedModel([verdict("changes", bad), verdict("changes", bad), verdict("approve")])
    s = setup(tmp_path, [task("a", "app.py")], book, reviewer, escalation=stronger)
    report = run_build(s)
    assert report.status == "approved" and report.review_rounds == 2
    assert subjects(s.git, s.base) == ["feat(a): set A", "fix(a): set A to 2", "fix(a): set A to 3"]
    assert report.tasks[0].escalated_to == "claude-opus-5-5"


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


class Fatal(BaseException):
    pass


def test_a_fatal_error_stops_a_running_worker_before_its_next_paid_turn(tmp_path: Path) -> None:
    reading, failing = threading.Event(), threading.Event()

    class FailsOnceOtherStarted(ScriptedModel):
        def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
            reading.wait(5)
            failing.set()
            raise Fatal("the worker's machine went away")

    class KeepsReading(ScriptedModel):
        def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
            if not self.received:
                reading.set()
                failing.wait(5)
                time.sleep(0.2)
            return super().send(results, user_text)

    read = [ToolCall("r", "read_file", {"path": "util.py"})]
    reader = KeepsReading([read] * 8)

    class Book(ScriptBook):
        def start(self, system: str, context: str, user: str, tools: Sequence[ToolSpec]) -> Any:
            model = FailsOnceOtherStarted([]) if 'id="a"' in user else reader
            model.start(system, context, user, tools)
            return model

    tasks = [task("a", "app.py"), task("b", "util.py")]
    s = setup(tmp_path, tasks, Book({}), ScriptedModel([]), build=BuildConfig(max_parallel=2))
    with pytest.raises(Fatal):
        run_build(s)
    assert len(reader.received) == 1 and reader.script


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


class LateReviewer(ScriptedModel):
    """Reads a file on its first turn, and the build's time runs out while it does."""

    def __init__(self, clock: Clock) -> None:
        super().__init__([[ToolCall("r", "read_file", {"path": "app.py"})], *[verdict("approve")]])
        self.clock = clock

    def send(self, results: Sequence[ToolResult] = (), user_text: str | None = None) -> Turn:
        turn = super().send(results, user_text)
        self.clock.left = 0.0
        return turn


def test_the_time_limit_stops_the_reviewer_before_its_next_paid_turn(tmp_path: Path) -> None:
    clock = Clock("never")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    reviewer = LateReviewer(clock)
    s = setup(
        tmp_path,
        [task("a", "app.py")],
        book,
        reviewer,
        build=BuildConfig(max_minutes=1),
        time_left=clock.time_left,
    )
    report = run_build(s)
    assert report.status == "budget_exhausted" and report.out_of_time and report.commits == 1
    assert report.reason == "build time limit reached: build.max_minutes is 1"
    assert len(reviewer.received) == 1 and s.ledger.roles()["reviewer"].turns == 1


def test_the_time_limit_stops_after_the_final_tests(tmp_path: Path) -> None:
    clock = Clock("tests final")
    book = ScriptBook({'id="a"': [[write("app.py", "A = 1\n"), done("feat(a): set A")]]})
    report = run_build(late_setup(tmp_path, clock, [task("a", "app.py")], book))
    assert report.status == "budget_exhausted" and report.out_of_time and report.commits == 1
    assert report.final_tests is not None and report.review is None
