"""The spec phase's comments: questions, specs, refusals, errors and budget stops."""

import json
from collections.abc import Mapping, Sequence

from specster.closing import rewrite_references
from specster.plan import levels, mermaid, plan_payload
from specster.render.common import (
    _DOT,
    _NONE,
    RenderContext,
    _bullets,
    _cell,
    _code,
    _indent,
    _l,
    _links,
    _no_closing,
    _prose,
    _unescaped,
    _wrap,
    fence,
)
from specster.render.labels import LABELS
from specster.schemas import EvidencePage, EvidenceRequest, PlanTask, QuestionsResult, SpecResult


def render_questions(result: QuestionsResult, ctx: RenderContext) -> str:
    lab = _l(ctx)
    body = [f"**{lab['questions']}**", "", _prose(result.summary), ""]
    for i, q in enumerate(result.questions, 1):
        body.append(f"{i}. **{_prose(q.question)}**")
        body.append(f"   {_prose(q.why)}")
        if q.options:
            body.append("   " + _DOT.join(f"`{_code(o)}`" for o in q.options))
    body += ["", lab["next_questions"].format(label=ctx.spec_label)]
    return _wrap(ctx, body, result.closing_line)


def _evidence(
    evidence: Sequence[EvidenceRequest], pages: Sequence[EvidencePage], lab: Mapping[str, str]
) -> list[str]:
    out = [
        "",
        f"**{lab['evidence_with_pages' if pages else 'evidence']}**",
        "",
        f"| {lab['evidence_request']} | | {lab['evidence_why']} |",
        "|---|---|---|",
    ]
    for e in evidence:
        json_body = " + JSON body" if e.body is not None else ""
        out.append(
            f"| `{_code(e.name)}` | `{e.method} {_code(e.path)}`{json_body} | {_cell(e.why)} |"
        )
    out += [f"| `{_code(g.name)}` | `PAGE {_code(g.path)}` | {_cell(g.why)} |" for g in pages]
    for e in evidence:
        if e.body is not None:
            out += [
                "",
                f"`{_code(e.name)}`:",
                fence(json.dumps(e.body, indent=1, sort_keys=True), "json"),
            ]
    return out


def render_spec(
    result: SpecResult, tasks: Sequence[PlanTask], fixes: Sequence[str], ctx: RenderContext
) -> str:
    lab = _l(ctx)
    lv = levels(tasks)
    body = [f"### {_prose(' '.join(result.title.split()))}", ""]
    if result.changes:
        body += [f"**{lab['changes']}**", *_bullets(result.changes), ""]
    body += [
        f"**{lab['objective']}.** {_prose(result.objective)}",
        "",
        f"**{lab['in_scope']}**",
        *_bullets(result.in_scope),
        "",
        f"**{lab['out_of_scope']}**",
        *_bullets(result.out_of_scope),
        "",
        f"**{lab['files']}**",
        *([f"- `{_code(f)}`" for f in result.files] or [f"- {_NONE}"]),
        "",
        f"**{lab['approach']}**",
        "",
        _prose(result.approach),
        "",
        f"**{lab['risks']}**",
        *_bullets(result.risks),
        "",
        f"**{lab['tests']}**",
        "",
        _prose(result.test_strategy),
        "",
        f"#### {lab['plan']}",
        "",
        f"| {lab['task']} | {lab['files']} | {lab['depends']} | {lab['level']} |",
        "|---|---|---|---|",
    ]
    for t in tasks:
        files = ", ".join(f"`{_code(f)}`" for f in t.files)
        deps = ", ".join(f"`{_code(d)}`" for d in t.depends_on) or _NONE
        body.append(f"| `{_code(t.id)}` | {files} | {deps} | {lv[t.id]} |")
    if fixes:
        body += ["", f"**{lab['reordered']}:**", *_bullets(fixes)]
    one_line = [t.model_copy(update={"title": " ".join(t.title.split())}) for t in tasks]
    body += ["", fence(mermaid(one_line), "mermaid"), "", f"**{lab['acceptance']}**"]
    for t in tasks:
        body.append(f"- `{_code(t.id)}` {_prose(' '.join(t.title.split()))}")
        body += _indent(t.description)
        body += [f"  - {_prose(a)}" for a in t.acceptance]
    if result.evidence or result.pages:
        body += _evidence(result.evidence, result.pages, lab)
    body += ["", lab["next_spec"].format(label=ctx.build_label)]
    payload, digest = plan_payload(tasks, result.evidence, result.pages)
    safe = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    plan_marker = f"<!-- specster:plan {safe} sha256={digest} -->"
    return _wrap(ctx, body, result.closing_line) + "\n" + plan_marker


def render_error(message: str, hint: str, ctx: RenderContext) -> str:
    lab = _l(ctx)
    body = [
        f"**{lab['error']}**",
        "",
        fence(rewrite_references(message)),
        "",
        f"**{lab['fix']}:** {hint}",
    ]
    return _wrap(_no_closing(ctx), body, "")


def render_budget(spent_usd: float, unknown_runs: int, cap: float, ctx: RenderContext) -> str:
    lab = _l(ctx)
    extra = f" (+{unknown_runs} runs with unknown cost)" if unknown_runs else ""
    body = [f"**{lab['budget']}**", "", f"${spent_usd:.2f}{extra} of ${cap:.2f}."]
    return _wrap(_no_closing(ctx), body, "")


def spec_objective(spec_text: str) -> str | None:
    for lab in LABELS.values():
        prefix = f"**{lab['objective']}.** "
        start = spec_text.find(prefix)
        if start != -1:
            paragraph = spec_text[start + len(prefix) :].split("\n\n", 1)[0].strip()
            return paragraph or None
    return None


def spec_title(spec_text: str) -> str | None:
    """The spec's title as plain one-line text, for a pull request title (not HTML-rendered)."""
    for line in spec_text.splitlines():
        if line.startswith("### "):
            text = _unescaped(line[4:])
            return rewrite_references(" ".join(text.split())) or None
    return None


def render_refused(
    message: str, hint: str, ctx: RenderContext, comments: Sequence[tuple[str, str]] = ()
) -> str:
    lab = _l(ctx)
    body = [
        f"**{lab['refused']}**",
        "",
        fence(rewrite_references(message)),
        "",
        f"**{lab['fix']}:** {hint}",
    ]
    if comments:
        body += ["", *_links(comments)]
    return _wrap(_no_closing(ctx), body, "")
