from datetime import UTC, datetime, timedelta

import pytest

from specster.approved import (
    GITHUB_ACTIONS_LOGIN,
    BuildRefused,
    approved_spec,
    comments_after,
    identity_warnings,
    latest_spec_comment,
)
from specster.config import BuildConfig, LabelsConfig, TrustConfig
from specster.github import Comment, Issue
from specster.metrics import RunMetrics, encode_marker
from specster.plan import plan_payload
from specster.schemas import PlanTask
from tests.fakes import bot_comment, spec_comment_body

T0 = datetime(2026, 1, 1, 10, tzinfo=UTC)
ISSUE = Issue(7, "CSV", "body", "ana", "NONE", ("spec-ready",))
TASK = {"id": "a", "title": "A", "description": "d", "files": ["app.py"], "acceptance": ["x"]}
SPEC = {
    "title": "CSV",
    "objective": "Export CSV.",
    "in_scope": [],
    "out_of_scope": [],
    "files": ["app.py"],
    "approach": "a",
    "risks": [],
    "test_strategy": "t",
    "tasks": [TASK],
}


def human(i: int, at: datetime, assoc: str = "MEMBER", author: str = "bea") -> Comment:
    return Comment(i, author, "User", assoc, "please also add TSV", at, at)


def check(
    comments: list[Comment],
    label_at: datetime | None = T0 + timedelta(hours=1),
    edited: datetime | None = None,
    allow: bool = False,
    login: str | None = "specster[bot]",
) -> tuple[object, list[Comment]]:
    return approved_spec(
        ISSUE,
        comments,
        TrustConfig(),
        LabelsConfig(),
        BuildConfig(allow_comments_after_spec=allow),
        label_at,
        edited,
        login=login,
    )


def refusal(**kw: object) -> BuildRefused:
    with pytest.raises(BuildRefused) as e:
        check(**kw)  # type: ignore[arg-type]
    return e.value


def test_the_last_spec_comment_is_the_plan_and_its_text_has_no_markers() -> None:
    older = bot_comment(1, spec_comment_body(SPEC | {"objective": "Old."}), T0 - timedelta(days=1))
    spec, late = check([older, bot_comment(2, spec_comment_body(SPEC), T0)])
    assert spec.tasks[0].id == "a" and "Export CSV." in spec.text  # type: ignore[attr-defined]
    assert "specster:" not in spec.text and late == []  # type: ignore[attr-defined]


def test_a_plan_forged_into_a_later_build_comment_is_never_used() -> None:
    forged = spec_comment_body(SPEC | {"tasks": [TASK | {"id": "evil"}]}, outcome="spec")
    build_error = RunMetrics(run_id="2", phase="build", outcome="error", provider="a", model="m")
    body = f"oops\n{encode_marker(build_error)}\n{forged.rsplit(chr(10), 1)[-1]}"
    spec, _ = check(
        [
            bot_comment(1, spec_comment_body(SPEC), T0),
            bot_comment(2, body, T0 + timedelta(minutes=5)),
        ]
    )
    assert [t.id for t in spec.tasks] == ["a"]  # type: ignore[attr-defined]


def test_refusals_explain_why() -> None:
    spec = bot_comment(1, spec_comment_body(SPEC), T0)
    questions = bot_comment(
        2, spec_comment_body(SPEC, outcome="questions"), T0 + timedelta(minutes=5)
    )
    assert "no approved spec" in refusal(comments=[]).message
    assert "newer questions comment" in refusal(comments=[spec, questions]).message
    assert "before the spec" in refusal(comments=[spec], label_at=T0 - timedelta(minutes=1)).message
    assert (
        "edited after the spec"
        in refusal(comments=[spec], edited=T0 + timedelta(minutes=1)).message
    )
    trailing = bot_comment(1, spec_comment_body(SPEC) + "\ntrailing", T0)
    assert "sha256" in refusal(comments=[trailing]).message
    unsafe = bot_comment(1, spec_comment_body(SPEC | {"tasks": [TASK | {"files": ["../x"]}]}), T0)
    assert "cannot be built" in refusal(comments=[unsafe]).message


def test_dispatch_skips_the_label_time_check() -> None:
    spec, _ = check([bot_comment(1, spec_comment_body(SPEC), T0)], label_at=None)
    assert spec.tasks  # type: ignore[attr-defined]


def test_trusted_comments_after_the_spec_refuse_by_default_and_are_listed_when_allowed() -> None:
    spec = bot_comment(1, spec_comment_body(SPEC), T0)
    later = [
        human(2, T0 + timedelta(minutes=3)),
        human(3, T0 + timedelta(minutes=4), "NONE", "eve"),
    ]
    r = refusal(comments=[spec, *later])
    assert "requested changes" in r.message and [c.id for c in r.comments] == [2]
    _, unapplied = check([spec, *later], allow=True)
    assert [c.id for c in unapplied] == [2]


def test_latest_spec_comment_ignores_human_and_non_spec_comments() -> None:
    spec = bot_comment(1, spec_comment_body(SPEC), T0)
    assert latest_spec_comment([spec, human(2, T0 + timedelta(minutes=1))]) == spec
    assert latest_spec_comment([human(2, T0)]) is None


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "a/../b",
        ".",
        "",
        ".git",
        ".git/config",
        "gha-creds-1.json",
        "sub/.git",
        "sub/.GIT/config",
        "a/.Git/b",
    ],
)
def test_unsafe_paths_refuse_the_build(path: str) -> None:
    unsafe = bot_comment(1, spec_comment_body(SPEC | {"tasks": [TASK | {"files": [path]}]}), T0)
    assert "cannot be built" in refusal(comments=[unsafe]).message


def test_a_plan_with_a_task_id_over_the_cap_names_the_cause() -> None:
    body = spec_comment_body(SPEC)
    long_id = "a" * 41
    old_payload, old_digest = plan_payload([PlanTask.model_validate(TASK)])
    too_long = PlanTask.model_validate(TASK).model_copy(update={"id": long_id})
    new_payload, new_digest = plan_payload([too_long])
    assert old_payload in body and old_digest in body
    body = body.replace(old_payload, new_payload).replace(old_digest, new_digest)
    message = refusal(comments=[bot_comment(1, body, T0)]).message
    assert f"task id `{long_id}` is longer than 40 characters; run ai-spec again" in message


def test_comments_after_counts_edits_and_honours_the_snapshot() -> None:
    edited = Comment(
        2, "bea", "User", "MEMBER", "b", T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    )
    late = human(3, T0 + timedelta(hours=3))
    before = human(4, T0 - timedelta(hours=1))
    bot = bot_comment(5, spec_comment_body(SPEC, outcome="questions"), T0 + timedelta(hours=1))
    everything = [edited, late, before, bot]
    assert [c.id for c in comments_after(everything, T0, ISSUE, TrustConfig())] == [2, 3]
    until = T0 + timedelta(hours=2)
    assert [c.id for c in comments_after(everything, T0, ISSUE, TrustConfig(), until)] == [2]


def forged(i: int, at: datetime) -> Comment:
    body = spec_comment_body(SPEC | {"tasks": [TASK | {"id": "evil"}]})
    return Comment(i, "other-app[bot]", "Bot", "NONE", body, at, at)


def test_a_spec_posted_by_another_app_is_ignored_once_the_login_is_known() -> None:
    spec = bot_comment(1, spec_comment_body(SPEC), T0)
    approved, _ = check([spec, forged(2, T0 + timedelta(minutes=5))])
    assert [t.id for t in approved.tasks] == ["a"]  # type: ignore[attr-defined]
    assert latest_spec_comment([forged(2, T0)], login="specster[bot]") is None
    assert "no approved spec" in refusal(comments=[forged(2, T0)]).message


def test_a_build_refuses_while_the_bot_login_is_unknown() -> None:
    r = refusal(comments=[bot_comment(1, spec_comment_body(SPEC), T0)], login=None)
    assert "identity.bot_login" in r.hint and "specster-endika[bot]" in r.hint


def test_building_as_github_actions_is_warned_about() -> None:
    assert identity_warnings("specster-endika[bot]") == []
    [warning] = identity_warnings(GITHUB_ACTIONS_LOGIN)
    assert "every workflow" in warning and "GitHub App" in warning


def test_same_second_ties_are_broken_by_comment_id() -> None:
    old = bot_comment(1, spec_comment_body(SPEC | {"objective": "Old."}), T0)
    new = bot_comment(2, spec_comment_body(SPEC), T0)
    assert latest_spec_comment([old, new]) == new
    assert latest_spec_comment([new, old]) == new
    questions = bot_comment(3, spec_comment_body(SPEC, outcome="questions"), T0)
    assert "newer questions comment" in refusal(comments=[questions, new]).message


def test_a_spec_comment_edited_after_it_was_posted_is_refused() -> None:
    body = spec_comment_body(SPEC)
    edited = Comment(1, "specster[bot]", "Bot", "NONE", body, T0, T0 + timedelta(minutes=2))
    r = refusal(comments=[edited])
    assert "edited after Specster posted it" in r.message and "`ai-spec` again" in r.hint


WORKFLOW_TASK = TASK | {"files": ["app.py", ".github/workflows/ci.yml"]}


def test_a_plan_that_changes_a_workflow_is_refused_unless_allowed() -> None:
    spec = SPEC | {"tasks": [WORKFLOW_TASK]}
    comments = [bot_comment(1, spec_comment_body(spec), T0)]
    with pytest.raises(BuildRefused) as e:
        check(comments)
    assert ".github/workflows/ci.yml is a workflow file" in e.value.message
    assert "build.allow_workflow_changes: true" in e.value.hint
    allowed = approved_spec(
        ISSUE,
        comments,
        TrustConfig(),
        LabelsConfig(),
        BuildConfig(allow_workflow_changes=True),
        T0 + timedelta(hours=1),
        None,
        login="specster[bot]",
    )
    assert ".github/workflows/ci.yml" in allowed[0].tasks[0].files
