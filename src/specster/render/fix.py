"""The fix phase's comment on a pull request and its replies in the review threads."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from specster.build import BuildReport
from specster.render.build import _by_task, _task_table, _task_tests, _tests
from specster.render.common import (
    RenderContext,
    _cell,
    _code,
    _Cuts,
    _fit,
    _kept,
    _l,
    _prose,
)
from specster.render.labels import LABELS
from specster.review_input import REPLY_MARKER

FixStatus = Literal[
    "pushed", "declined", "not_approved", "failed", "budget", "time", "moved", "closed"
]
RowState = Literal["applied", "planned", "declined", "unapplied"]


@dataclass(frozen=True)
class FixRow:
    """One review item and what became of it."""

    author: str
    url: str
    where: str
    state: RowState
    # Space-separated commits or task ids, or the reason, by state.
    detail: str


@dataclass(frozen=True)
class FixView:
    status: FixStatus
    branch: str
    old_head: str
    new_head: str | None
    label: str
    report: BuildReport | None
    rows: Sequence[FixRow]
    reason: str = ""
    # Which items this comment answered, for later runs: always the comment's last line.
    marker: str = ""


_STATE = {
    "applied": "fix_row_applied",
    "planned": "fix_row_planned",
    "declined": "fix_row_declined",
    "unapplied": "fix_row_unapplied",
}


def _row(row: FixRow, lab: dict[str, str]) -> str:
    what = _code(row.where) if row.where else lab["fix_review"]
    item = f"{_prose(_code(row.author))}: [{_cell(what)}]({row.url})"
    if row.state in ("applied", "planned"):
        detail = ", ".join(f"`{_code(x)}`" for x in row.detail.split())
    else:
        detail = _cell(row.detail)
    return f"| {item} | {lab[_STATE[row.state]].format(detail=detail)} |"


def render_fix(view: FixView, ctx: RenderContext) -> str:
    lab = _l(ctx)
    report = view.report
    pending = report.pending if report is not None and view.status == "not_approved" else []

    def make(cuts: _Cuts, notes: list[str]) -> list[str]:
        body = [f"**{lab[f'fix_{view.status}'].format(branch=_code(view.branch))}**", ""]
        if view.new_head is not None:
            rng = lab["fix_range_pushed"].format(
                old=_code(view.old_head[:7]), new=_code(view.new_head[:7])
            )
        else:
            rng = lab["fix_range"].format(head=_code(view.old_head[:7]))
        body += [rng, ""]
        rows = _kept(view.rows, cuts.unapplied, "review items", notes)
        if rows:
            body += [f"| {lab['fix_item']} | {lab['status']} |", "|---|---|"]
            body += [_row(r, lab) for r in rows]
            body.append("")
        if report is not None and report.tasks:
            tests, note = _tests(report, lab, cuts.output)
            notes += [note] if note else []
            body += [*_task_table(report, lab, cuts.rows), ""]
            _kept(report.tasks, cuts.rows, "task rows", notes)
            if tests:
                body += [f"**{lab['test_result']}:** {tests[0]}", *tests[1:], ""]
            task_tests, task_notes = _task_tests(report, lab, cuts.output)
            body += task_tests
            notes += task_notes
        kept = _kept(pending, cuts.findings, "pending findings", notes)
        if kept:
            body += [f"**{lab['pending']}**", *_by_task(kept), ""]
        if view.reason:
            body += [_prose(view.reason), ""]
        nxt = {"pushed": "fix_next_pushed", "declined": "fix_next_declined"}.get(
            view.status, "fix_next_moved" if view.status == "moved" else "fix_next"
        )
        body.append(lab[nxt].format(label=view.label))
        return body

    sizes = {
        "findings": len(pending),
        "unapplied": len(view.rows),
        "rows": len(report.tasks) if report is not None else 0,
    }
    return _fit(ctx, make, sizes, view.marker)


def reply_applied(language: str, shas: Sequence[str]) -> str:
    lab = LABELS.get(language, LABELS["en"])
    return f"{lab['fix_reply_applied'].format(shas=', '.join(shas))}\n\n{REPLY_MARKER}"


def reply_unapplied(language: str) -> str:
    return f"{LABELS.get(language, LABELS['en'])['fix_reply_unapplied']}\n\n{REPLY_MARKER}"


def reply_declined(language: str, reason: str) -> str:
    lab = LABELS.get(language, LABELS["en"])
    text = lab["fix_reply_declined"].format(reason=_prose(" ".join(reason.split())))
    return f"{text}\n\n{REPLY_MARKER}"
