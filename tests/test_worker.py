import shutil
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from specster.config import PersonaConfig, SkillsConfig
from specster.llm.base import ToolCall
from specster.prompts import task_block, worker_system_prompt
from specster.sandbox import Sandbox, SandboxError, scratch_dir, slot_identity
from specster.schemas import Finding, PlanTask
from specster.skills import load_skills
from specster.worker import (
    CHANGED_SINCE,
    ONE_RUN,
    TaskTools,
    WorkerResult,
    fallback_subject,
    run_worker,
    valid_subject,
)
from specster.workspace import TaskWorkspace
from tests.fakes import ScriptBook, ScriptedModel, make_repo
from tests.test_sandbox import ROOT_ONLY

TASK = PlanTask(
    id="bump", title="Bump the value", description="d", files=["app.py"], acceptance=["x"]
)
CHECK = [sys.executable, "-c", "import app; assert app.VALUE == 2, app.VALUE"]


def locked(tmp_path: Path, sb: Sandbox) -> Sandbox:
    workspace = tmp_path / "workspace"
    sb.lock_down(workspace, make_repo(workspace, {"a.txt": "a\n"}), {})
    return sb


def worker(
    tmp_path: Path,
    script: list[list[ToolCall] | str],
    test: list[str] | None = CHECK,
    setup: list[str] | None = None,
    lock: bool = True,
) -> tuple[WorkerResult, ScriptedModel, TaskTools]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "app.py").write_text("VALUE = 1\n")
    home = tmp_path / "home"
    home.mkdir()
    sb = Sandbox(None, 60, 20_000, {})
    if lock:
        locked(tmp_path, sb)
    tools = TaskTools(TaskWorkspace(tree, [], TASK.files), sb, home, setup, test, TASK.id)
    skills = load_skills(tree, SkillsConfig(), "build", lambda *_: b"", None)
    model = ScriptedModel(script)
    system = worker_system_prompt(PersonaConfig(), skills.on_demand, test is not None)
    return run_worker(model, system, "ctx", "task", tools, TASK, skills, 6, False), model, tools


def submit(subject: str = "feat(app): bump the value to 2") -> ToolCall:
    return ToolCall("s", "submit_task", {"summary": "Bumped.", "commit_subject": subject})


def test_a_worker_writes_its_file_and_submits_once_the_tests_pass(tmp_path: Path) -> None:
    out, model, tools = worker(
        tmp_path,
        [
            [ToolCall("1", "write_file", {"path": "other.py", "content": "x"})],
            [
                ToolCall(
                    "2", "edit_file", {"path": "app.py", "old": "VALUE = 1", "new": "VALUE = 2"}
                )
            ],
            [submit()],
        ],
    )
    assert "not a file of this task" in model.received[1][0].content
    assert out.status == "done" and out.changes == {"app.py": "VALUE = 2\n"}
    assert out.subject == "feat(app): bump the value to 2" and out.tests_passed is True
    assert out.originals == {"app.py": b"VALUE = 1\n"} and out.summary == "Bumped."
    assert tools.runs == 1
    names = [t.name for t in model.tools]
    assert names == ["list_dir", "read_file", "grep", "write_file", "edit_file", "run_tests",
                     "submit_task"]  # fmt: skip


def test_failing_tests_send_the_submission_back_with_the_output(tmp_path: Path) -> None:
    out, model, tools = worker(
        tmp_path,
        [
            [submit()],
            [ToolCall("2", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})],
            [submit("not conventional")],
        ],
    )
    assert "Tests fail" in model.received[1][0].content
    assert "AssertionError: 1" in model.received[1][0].content
    assert out.status == "done" and out.subject == "feat(bump): Bump the value"
    assert tools.runs == 2


def test_a_task_whose_tests_never_pass_fails_with_the_reason(tmp_path: Path) -> None:
    out, _, _ = worker(tmp_path, [[submit()] for _ in range(6)])
    assert out.status == "failed" and "tests still fail" in out.reason
    assert out.turns == 6 and out.tests_passed is False


def test_setup_runs_once_per_worktree_and_no_test_command_is_said_plainly(tmp_path: Path) -> None:
    setup = [sys.executable, "-c", "open('setup.log', 'a').write('x')"]
    run_tests = ToolCall("1", "run_tests", {})
    worker(
        tmp_path,
        [[run_tests], [run_tests], [submit()]],
        setup=setup,
        test=[sys.executable, "-c", "pass"],
    )
    assert (tmp_path / "tree" / "setup.log").read_text() == "x"
    out2, model, _ = worker(tmp_path / "b", [[run_tests], [submit()]], test=None)
    assert "No test command is configured" in model.received[1][0].content
    assert out2.status == "done" and out2.tests_passed is None


def test_a_failed_setup_is_the_tool_text_and_runs_again_next_time(tmp_path: Path) -> None:
    setup = [sys.executable, "-c", "open('setup.log', 'a').write('x'); raise SystemExit(3)"]
    run_tests = ToolCall("1", "run_tests", {})
    out, model, tools = worker(
        tmp_path, [[run_tests], [run_tests], "done", "done"], setup=setup, test=CHECK
    )
    assert "exit 3" in model.received[1][0].content and "setup" in model.received[1][0].content
    assert (tmp_path / "tree" / "setup.log").read_text() == "xx"
    assert tools.runs == 0 and out.status == "failed" and "tests still fail" not in out.reason


def test_at_most_one_test_run_happens_per_turn(tmp_path: Path) -> None:
    run_tests = ToolCall("1", "run_tests", {})
    out, model, tools = worker(tmp_path, [[run_tests, run_tests], "stop", "stop"])
    assert model.received[1][1].is_error and ONE_RUN in model.received[1][1].content
    assert tools.runs == 1 and out.status == "failed"


def test_a_submit_after_a_passing_run_in_the_same_turn_reuses_it(tmp_path: Path) -> None:
    run_tests = ToolCall("1", "run_tests", {})
    edit = ToolCall("2", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})
    out, _, tools = worker(tmp_path, [[edit, run_tests, submit()]])
    assert out.status == "done" and out.tests_passed is True and tools.runs == 1


def test_a_submit_after_a_failing_run_in_the_same_turn_is_sent_back(tmp_path: Path) -> None:
    run_tests = ToolCall("1", "run_tests", {})
    out, model, tools = worker(tmp_path, [[run_tests, submit()], "stop", "stop"])
    refused = model.received[1][1]
    assert refused.is_error and "Tests fail" in refused.content
    assert "AssertionError: 1" in refused.content and tools.runs == 1
    assert out.status == "failed" and "tests still fail" in out.reason


def test_a_submit_after_a_write_that_followed_the_run_waits_a_turn(tmp_path: Path) -> None:
    run_tests = ToolCall("1", "run_tests", {})
    edit = ToolCall("2", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})
    out, model, tools = worker(tmp_path, [[run_tests, edit, submit()], [submit()]])
    assert model.received[1][2].is_error and CHANGED_SINCE in model.received[1][2].content
    assert out.status == "done" and tools.runs == 2


def test_a_sandbox_error_is_fatal_not_a_failed_task(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="lock_down"):
        worker(tmp_path, [[ToolCall("1", "run_tests", {})], [submit()]], lock=False)
    with pytest.raises(SandboxError, match="lock_down"):
        worker(tmp_path / "b", [[submit()]], lock=False)


def test_output_cut_by_the_sandbox_is_reported(tmp_path: Path) -> None:
    noisy = [sys.executable, "-c", "print('x' * 50_000)"]
    out, _, _ = worker(tmp_path, [[submit()]], test=noisy)
    assert out.truncations == ["tests bump: output cut to the last 19.5 KB of 48.8 KB"]


@ROOT_ONLY
def test_as_root_the_tree_is_handed_to_the_slot_before_the_tests_run(tmp_path: Path) -> None:
    scratch = scratch_dir()
    try:
        tree = scratch / "tree"
        tree.mkdir()
        (tree / "app.py").write_text("VALUE = 1\n")
        sb = locked(tmp_path, Sandbox(slot_identity(1), 60, 20_000, {}))
        tools = TaskTools(
            TaskWorkspace(tree, [], TASK.files),
            sb,
            sb.new_home(scratch, "h"),
            None,
            [*CHECK[:2], "import app, os; assert app.VALUE == 2; open('w', 'w')"],
            TASK.id,
        )
        skills = load_skills(tree, SkillsConfig(), "build", lambda *_: b"", None)
        edit = ToolCall("1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})
        model = ScriptedModel([[edit], [submit()]])
        out = run_worker(model, "sys", "ctx", "task", tools, TASK, skills, 6, False)
        assert out.status == "done" and (tree / "w").stat().st_uid == slot_identity(1).uid
        assert (tree / "app.py").stat().st_uid == slot_identity(1).uid
    finally:
        shutil.rmtree(scratch)


def test_subject_rules() -> None:
    assert valid_subject("fix(api): handle empty bodies")
    assert valid_subject("feat!: drop python 3.11")
    for bad in (
        "Fix things",
        "feat: " + "x" * 70,
        "feat(a): two\nlines",
        "feat(a):missing space",
        "feat(A): upper scope",
        "feat(a): tab\there",
        "fix(a): fix #3",
        "feat(a): closes o/r#3",
        "feat(a): resolves https://github.com/o/r/issues/3",
        "feat(a): resolves github.com/o/r/issues/3",
        "feat(a): fixes GH-3",
    ):
        assert not valid_subject(bad)
    assert valid_subject("fix(a): see #3")


def test_the_fallback_subject_never_names_an_issue_to_close() -> None:
    titled = TASK.model_copy(update={"title": "Fixes #12 in the export"})
    assert fallback_subject(titled, False) == ("feat(bump): Fixes issue 12 in the export", None)
    url = TASK.model_copy(update={"title": "Fixes https://github.com/o/r/issues/1, fixes GH-2"})
    assert fallback_subject(url, False) == ("feat(bump): Fixes o/r issue 1, fixes issue 2", None)


def test_the_fallback_subject_is_valid_and_reports_a_cut() -> None:
    assert fallback_subject(TASK, False) == ("feat(bump): Bump the value", None)
    assert fallback_subject(TASK, True) == ("fix(bump): address review findings", None)
    long = TASK.model_copy(update={"title": "Make\n the   value " + "much " * 20})
    subject, note = fallback_subject(long, False)
    assert len(subject) <= 72 and valid_subject(subject)
    assert subject.startswith("feat(bump): Make the value much")
    assert note == "commit subject for bump cut to 72 characters"


@pytest.mark.parametrize("bad", ["\x1b[31m", "\x00", chr(0x202E)])
def test_the_fallback_subject_drops_control_characters(bad: str) -> None:
    subject, _ = fallback_subject(TASK.model_copy(update={"title": f"Bump{bad}it"}), False)
    assert valid_subject(subject) and subject.isprintable() and bad not in subject
    assert subject.startswith("feat(bump): Bump")


def test_a_title_with_nothing_printable_falls_back_to_a_plain_subject() -> None:
    bare = TASK.model_copy(update={"title": "\x00\x07 \x1b"})
    assert fallback_subject(bare, False) == ("feat(bump): implement the task", None)


def test_a_long_title_is_cut_but_the_longest_id_and_its_scope_never_are() -> None:
    task_id = "a" * 40
    long = TASK.model_copy(update={"id": task_id, "title": "Make it " + "much " * 20})
    subject, note = fallback_subject(long, False)
    assert subject.startswith(f"feat({task_id}): Make it much") and len(subject) <= 72
    assert valid_subject(subject) and note == f"commit subject for {task_id} cut to 72 characters"
    assert valid_subject(fallback_subject(long, True)[0])
    with pytest.raises(ValidationError):
        PlanTask(id="a" * 41, title="t", description="d", files=["f"], acceptance=["x"])


def test_the_worker_prompt_switches_on_tests_and_the_task_block_carries_the_findings() -> None:
    with_tests = worker_system_prompt(PersonaConfig(language="es"), [], True)
    without = worker_system_prompt(PersonaConfig(), [], False)
    assert "submit_task runs it again" in with_tests and "Write summary in es." in with_tests
    assert "no test command configured" in without and "run_tests runs" not in without
    finding = Finding(task_id="bump", file="app.py", severity="important", description="Off.")
    block = task_block("The spec.", TASK, [finding], "n0nce")
    assert block.startswith('<task-n0nce id="bump">') and block.endswith("</task-n0nce>")
    assert "The spec." in block and "app.py" in block and "[important] app.py: Off." in block


def test_a_script_book_hands_each_session_the_script_named_in_its_user_text() -> None:
    book = ScriptBook({"alpha": [["one"], ["two"]], "beta": [["three"]]}, threading.Barrier(1))
    first = book.start("sys", "ctx", "task alpha", [])
    second = book.start("sys", "ctx", "task alpha again", [])
    third = book.start("sys", "ctx", "beta", [])
    assert [first.send().text, second.send().text, third.send().text] == ["one", "two", "three"]
    assert [len(v) for v in book.sessions.values()] == [2, 1]


def test_a_submit_after_a_passing_run_with_no_write_since_reuses_that_run(tmp_path: Path) -> None:
    write = ToolCall("1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})
    out, _, tools = worker(tmp_path, [[write], [ToolCall("2", "run_tests", {})], [submit()]])
    assert out.status == "done" and tools.runs == 1


def test_a_submit_after_a_failing_run_runs_the_tests_again(tmp_path: Path) -> None:
    out, model, tools = worker(tmp_path, [[ToolCall("1", "run_tests", {})], [submit()], [submit()]])
    assert out.status == "failed" and tools.runs == 3
    assert "Tests fail" in model.received[2][0].content


def test_a_submit_after_a_write_since_the_passing_run_runs_the_tests_again(
    tmp_path: Path,
) -> None:
    good = ToolCall("1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})
    bad = ToolCall("3", "write_file", {"path": "app.py", "content": "VALUE = 3\n"})
    out, model, tools = worker(
        tmp_path, [[good], [ToolCall("2", "run_tests", {})], [bad], [submit()], [good], [submit()]]
    )
    assert "Tests fail" in model.received[4][0].content
    assert out.status == "done" and tools.runs == 3


def test_a_subject_whose_closing_reference_hides_behind_backticks_is_invalid() -> None:
    assert not valid_subject("feat(app): fixes `#3`")
    assert not valid_subject("feat(app): `closes` #3")
