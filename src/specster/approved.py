from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

import pathspec
from pydantic import ValidationError

from specster.config import BuildConfig, LabelsConfig, TrustConfig
from specster.github import Comment, Issue
from specster.metrics import last_marker, last_plan_marker
from specster.plan import PlanError, levels, norm_path, normalize_plan
from specster.sanitize import sanitize
from specster.schemas import TASK_ID_MAX, PlanTask
from specster.thread import is_specster, is_trusted, own_text
from specster.workspace import HARD_EXCLUDE, has_git_component, is_workflow_path

BAD_MARKER = "The plan in the last spec comment is missing or does not match its sha256"
CANNOT_BUILD = "The approved plan cannot be built: "
FRESH_SPEC = "Add `{spec}` again for a fresh spec."
GITHUB_ACTIONS_LOGIN = "github-actions[bot]"
_HARD = pathspec.GitIgnoreSpec.from_lines(HARD_EXCLUDE)


def _order(comment: Comment) -> tuple[datetime, int]:
    # GitHub timestamps have one-second resolution; ids break the tie in posting order.
    return comment.created_at, comment.id


@dataclass(frozen=True)
class ApprovedSpec:
    comment: Comment
    tasks: list[PlanTask]
    sha256: str
    text: str


class BuildRefused(Exception):
    def __init__(self, message: str, hint: str, comments: Sequence[Comment] = ()) -> None:
        super().__init__(message)
        self.message, self.hint, self.comments = message, hint, tuple(comments)


def identity_warnings(login: str) -> list[str]:
    if login != GITHUB_ACTIONS_LOGIN:
        return []
    return [
        f"Specster posts as {GITHUB_ACTIONS_LOGIN}, and so does every workflow in this "
        "repository, so any of them could post a spec; use a GitHub App for builds"
    ]


def latest_spec_comment(comments: Sequence[Comment], login: str | None = None) -> Comment | None:
    for comment in sorted(comments, key=_order, reverse=True):
        if not is_specster(comment, login):
            continue
        marker = last_marker(comment.body)
        if marker is not None and marker.phase == "spec" and marker.outcome == "spec":
            return comment
    return None


def _safe_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or str(pure) == ".":
        return False
    if has_git_component(str(pure)):
        return False
    # ".git" as a file (a gitdir pointer) is as dangerous as the directory.
    return not (_HARD.match_file(str(pure)) or _HARD.match_file(f"{pure}/"))


def _check_levels_disjoint(tasks: Sequence[PlanTask]) -> None:
    owner: dict[tuple[int, str], str] = {}
    lv = levels(tasks)
    for task in tasks:
        for f in task.files:
            other = owner.setdefault((lv[task.id], norm_path(f)), task.id)
            if other != task.id:
                raise BuildRefused(
                    f"{CANNOT_BUILD}tasks {other} and {task.id} run at once and both touch {f}",
                    FRESH_SPEC.format(spec=LabelsConfig().spec),
                )


def load_spec(comment: Comment) -> ApprovedSpec:
    hint = FRESH_SPEC.format(spec=LabelsConfig().spec)
    found = last_plan_marker(comment.body)
    if found is None:
        raise BuildRefused(BAD_MARKER, hint)
    raw, digest = found
    too_long = next(
        (
            t["id"]
            for t in raw
            if isinstance(t, dict) and isinstance(t.get("id"), str) and len(t["id"]) > TASK_ID_MAX
        ),
        None,
    )
    if too_long is not None:
        raise BuildRefused(
            f"{CANNOT_BUILD}task id `{too_long}` is longer than {TASK_ID_MAX} characters; "
            f"run {LabelsConfig().spec} again",
            hint,
        )
    try:
        tasks = [PlanTask.model_validate(t) for t in raw]
        normalized, fixes = normalize_plan(tasks)
    except (ValidationError, PlanError, TypeError) as e:
        raise BuildRefused(f"{CANNOT_BUILD}{e}", hint) from e
    unsafe = sorted({f for t in tasks for f in t.files if not _safe_path(f)})
    if unsafe:
        raise BuildRefused(f"{CANNOT_BUILD}unsafe path {unsafe[0]}", hint)
    if fixes or normalized != tasks:
        raise BuildRefused(f"{CANNOT_BUILD}its order is not normalized", hint)
    _check_levels_disjoint(tasks)
    return ApprovedSpec(comment, tasks, digest, sanitize(own_text(comment.body)).text)


def comments_after(
    comments: Sequence[Comment],
    since: datetime,
    issue: Issue,
    trust: TrustConfig,
    until: datetime | None = None,
    *,
    login: str | None = None,
) -> list[Comment]:
    found = []
    for c in sorted(comments, key=_order):
        if is_specster(c, login) or not is_trusted(c, issue, trust):
            continue
        if not (c.created_at > since or c.updated_at > since):
            continue
        if until is not None and (c.created_at > until or c.updated_at > until):
            continue
        found.append(c)
    return found


def approved_spec(
    issue: Issue,
    comments: Sequence[Comment],
    trust: TrustConfig,
    labels: LabelsConfig,
    build: BuildConfig,
    label_at: datetime | None,
    body_edited_at: datetime | None,
    *,
    login: str | None,
) -> tuple[ApprovedSpec, list[Comment]]:
    spec_label, build_label = labels.spec, labels.build
    if login is None:
        raise BuildRefused(
            "Specster's bot login is unknown, so its own spec cannot be told from a forged one",
            "Set identity.bot_login to your Specster bot's login, e.g. specster-endika[bot].",
        )
    comment = latest_spec_comment(comments, login)
    if comment is None:
        raise BuildRefused(
            "There is no approved spec on this issue",
            f"Add `{spec_label}` and wait for a spec before adding `{build_label}`.",
        )
    for later in sorted(comments, key=_order):
        if _order(later) <= _order(comment) or not is_specster(later, login):
            continue
        marker = last_marker(later.body)
        if marker is not None and marker.phase == "spec":
            raise BuildRefused(
                f"Specster posted a newer {marker.outcome} comment after the spec",
                f"Answer it and add `{spec_label}` again; add `{build_label}` once a new spec "
                "is posted.",
            )
    if label_at is not None and label_at < comment.created_at:
        raise BuildRefused(
            f"The `{build_label}` label was added before the spec was posted",
            f"Review the spec, then add `{build_label}` again.",
        )
    if body_edited_at is not None and body_edited_at > comment.created_at:
        raise BuildRefused(
            "The issue body was edited after the spec",
            f"Add `{spec_label}` again so the spec covers the edit.",
        )
    if comment.updated_at > comment.created_at:
        raise BuildRefused(
            "The spec comment was edited after Specster posted it",
            f"Add `{spec_label}` again for a fresh spec.",
        )
    try:
        spec = load_spec(comment)
    except BuildRefused as e:
        raise BuildRefused(e.message, FRESH_SPEC.format(spec=spec_label)) from e
    workflows = sorted({f for t in spec.tasks for f in t.files if is_workflow_path(f)})
    if workflows and not build.allow_workflow_changes:
        raise BuildRefused(
            f"{CANNOT_BUILD}{workflows[0]} is a workflow file, and a pushed workflow runs with "
            "the repository's secrets",
            "Set build.allow_workflow_changes: true to build it anyway, or add "
            f"`{spec_label}` again for a plan that leaves workflows alone.",
        )
    after = comments_after(comments, comment.created_at, issue, trust, login=login)
    if after and not build.allow_comments_after_spec:
        raise BuildRefused(
            "There are requested changes the spec does not include",
            f"Add `{spec_label}` again to fold them into the spec, then `{build_label}`.",
            after,
        )
    return spec, after
