"""The build phase's comment and its pull request body."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from specster.build import BuildReport, TaskRecord
from specster.closing import rewrite_references
from specster.render.common import (
    _NONE,
    _OVER,
    TEST_OUTPUT_TAIL,
    RenderContext,
    _cell,
    _code,
    _Cuts,
    _fit,
    _flat,
    _kept,
    _l,
    _links,
    _no_mentions,
    _prose,
    fence,
)
from specster.render.evidence import evidence_section
from specster.render.spec import spec_objective
from specster.sandbox import RunResult
from specster.schemas import Finding


@dataclass(frozen=True)
class BuildView:
    report: BuildReport
    spec_text: str
    spec_url: str
    branch: str
    branch_url: str | None
    pr_url: str | None
    unapplied: Sequence[tuple[str, str]]
    issue_number: int
    close_issue: bool
    evidence_links: Mapping[str, str] | None = None
    evidence_note: str | None = None


def _status(record: TaskRecord, lab: Mapping[str, str]) -> str:
    key = "not_started" if record.status == "pending" else record.status
    status = lab[f"st_{key}"]
    if record.escalated_to:
        status += f" ({lab['escalated'].format(model=f'`{_code(record.escalated_to)}`')})"
    return (
        f"{status}: {_cell(record.reason)}" if record.status != "done" and record.reason else status
    )


def _task_table(report: BuildReport, lab: Mapping[str, str], rows: int | None) -> list[str]:
    table = [f"| {lab['task']} | {lab['status']} | {lab['commit']} |", "|---|---|---|"]
    for r in report.tasks[:rows]:
        commits = "<br>".join(f"`{_code(c.sha[:7])}` {_cell(_code(c.subject))}" for c in r.commits)
        table.append(f"| `{_code(r.task.id)}` | {_status(r, lab)} | {commits or _NONE} |")
    return table


def _tests(
    report: BuildReport, lab: Mapping[str, str], output: bool
) -> tuple[list[str], str | None]:
    if not report.has_tests:
        return [lab["no_tests"]], None
    res = report.final_tests
    if res is None:
        return [], None
    if res.ok:
        return [lab["tests_ok"].format(runs=report.test_runs)], None
    if res.timed_out or res.exit_code is None:
        lines = [lab["tests_timeout"]]
    else:
        lines = [lab["tests_bad"].format(code=res.exit_code)]
    return _failed_run(res, lines, output, "test output")


def _failed_run(
    res: RunResult, lines: list[str], output: bool, what: str
) -> tuple[list[str], str | None]:
    if not output:
        return lines, f"{what} left out: {_OVER}"
    tail, note = res.output, None
    if len(tail) > TEST_OUTPUT_TAIL:
        note = f"{what} cut to the last {TEST_OUTPUT_TAIL:,} of {len(tail):,} characters"
        tail = tail[-TEST_OUTPUT_TAIL:]
    lines += ["", fence(rewrite_references(tail))]
    return lines, note


def _task_tests(
    report: BuildReport, lab: Mapping[str, str], output: bool
) -> tuple[list[str], list[str]]:
    """Each failed task's last test run, when the branch itself was never tested."""
    if report.final_tests is not None:
        return [], []
    body: list[str] = []
    notes: list[str] = []
    for r in report.tasks:
        res = r.last_tests
        if r.status != "failed" or res is None or res.ok:
            continue
        task = _code(r.task.id)
        if res.timed_out or res.exit_code is None:
            head = lab["task_tests_timeout"].format(task=task)
        else:
            head = lab["task_tests_bad"].format(task=task, code=res.exit_code)
        lines, note = _failed_run(res, [head], output, f"test output of {r.task.id}")
        body += [*lines, ""]
        notes += [note] if note else []
    return body, notes


def _findings(findings: Sequence[Finding]) -> list[str]:
    return [
        f"- `{_code(f.task_id)}` {f.severity} `{_code(f.file)}`: {_flat(f.description)}"
        for f in findings
    ]


def _by_task(findings: Sequence[Finding]) -> list[str]:
    grouped: dict[str, list[Finding]] = {}
    for f in findings:
        grouped.setdefault(f.task_id, []).append(f)
    lines: list[str] = []
    for task_id, items in grouped.items():
        lines.append(f"- `{_code(task_id)}`")
        lines += [f"  - {f.severity} `{_code(f.file)}`: {_flat(f.description)}" for f in items]
    return lines


def _unapplied(view: BuildView, ctx: RenderContext, cuts: _Cuts, notes: list[str]) -> list[str]:
    links = _kept(view.unapplied, cuts.unapplied, "unapplied comments", notes)
    if not links:
        return []
    return [f"**{_l(ctx)['unapplied'].format(label=ctx.spec_label)}**", *_links(links), ""]


def render_pr_body(view: BuildView, ctx: RenderContext) -> str:
    lab = _l(ctx)
    report = view.report
    objective = spec_objective(view.spec_text)

    def make(cuts: _Cuts, notes: list[str]) -> list[str]:
        if objective is not None:
            # Already escaped when the spec comment was rendered.
            body = [f"**{lab['objective']}.** {_no_mentions(rewrite_references(objective))}", ""]
        else:
            body = [f"**{lab['spec_link']}:** {view.spec_url}", ""]
        tests, note = _tests(report, lab, cuts.output)
        notes += [note] if note else []
        body += [*_task_table(report, lab, cuts.rows), ""]
        _kept(report.tasks, cuts.rows, "task rows", notes)
        if tests:
            body += [f"**{lab['test_result']}:** {tests[0]}", *tests[1:], ""]
        ev = report.evidence
        if ev is not None and (ev.items or ev.pages):
            body += evidence_section(ev, lab, view.evidence_links, cuts.evidence, notes, cuts.pages)
        minor = _kept(report.minor, cuts.findings, "minor findings", notes)
        if minor:
            body += [f"**{lab['minor']}**", *_findings(minor), ""]
        body += _unapplied(view, ctx, cuts, notes)
        if view.close_issue:
            body += [f"Closes #{view.issue_number}", ""]
        return body

    sizes = {
        "findings": len(report.minor),
        "unapplied": len(view.unapplied),
        "evidence": sum(1 for i in report.evidence.items if i.changed) if report.evidence else 0,
        "pages": len(report.evidence.pages) if report.evidence else 0,
        "rows": len(report.tasks),
    }
    return _fit(ctx, make, sizes)


_HEADLINES = {
    "approved": "pr_opened",
    "not_approved": "not_approved",
    "failed": "build_failed",
    "budget_exhausted": "build_budget",
}


def render_build(view: BuildView, ctx: RenderContext) -> str:
    lab = _l(ctx)
    report = view.report
    headline = "pr_opened" if view.pr_url else _HEADLINES[report.status]
    if headline == "build_budget" and report.out_of_time:
        headline = "build_time"
    pending = report.pending if report.status == "not_approved" else []

    def make(cuts: _Cuts, notes: list[str]) -> list[str]:
        body = [f"**{lab[headline]}**", ""]
        if view.pr_url:
            body += [view.pr_url, ""]
        elif view.branch_url:
            body += [f"**{lab['branch']}:** [`{_code(view.branch)}`]({view.branch_url})", ""]
        else:
            body += [lab["no_branch"], ""]
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
        if report.reason:
            body += [_prose(report.reason), ""]
        body += _unapplied(view, ctx, cuts, notes)
        if view.evidence_note:
            body += [_prose(view.evidence_note), ""]
        body.append(lab["next_pr"] if view.pr_url else lab["next_human"])
        return body

    sizes = {"findings": len(pending), "unapplied": len(view.unapplied), "rows": len(report.tasks)}
    return _fit(ctx, make, sizes)
