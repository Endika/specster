import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from specster.config import PersonaConfig
from specster.metrics import RunMetrics, encode_marker
from specster.plan import levels, mermaid, plan_payload
from specster.schemas import PlanTask, QuestionsResult, SpecResult
from specster.thread import FOOTER_OPEN, HIDDEN_OPEN, HiddenItem

_DOT = " \u00b7 "
_NONE = "\u2014"

LABELS: dict[str, dict[str, str]] = {
    "en": {
        "questions": "Before I write the spec, I need a few answers",
        "hidden": "Hidden content removed",
        "untrusted": "Comments ignored by the trust filter",
        "objective": "Objective",
        "in_scope": "In scope",
        "out_of_scope": "Out of scope",
        "files": "Files",
        "approach": "Approach",
        "risks": "Risks",
        "tests": "Test strategy",
        "plan": "Plan",
        "task": "Task",
        "depends": "Depends on",
        "level": "Level",
        "reordered": "Specster reordered",
        "acceptance": "Acceptance criteria",
        "error": "Specster could not finish this run",
        "fix": "How to fix it",
        "budget": "Budget for this issue is spent",
        "next_questions": "Answer below, then add the `{label}` label again.",
        "next_spec": "Review the spec. The `{label}` label will build it (v0.2).",
    },
    "es": {
        "questions": "Antes de escribir la spec necesito algunas respuestas",
        "hidden": "Contenido oculto eliminado",
        "untrusted": "Comentarios ignorados por el filtro de confianza",
        "objective": "Objetivo",
        "in_scope": "Dentro del alcance",
        "out_of_scope": "Fuera del alcance",
        "files": "Ficheros",
        "approach": "Enfoque",
        "risks": "Riesgos",
        "tests": "Estrategia de tests",
        "plan": "Plan",
        "task": "Tarea",
        "depends": "Depende de",
        "level": "Nivel",
        "reordered": "Specster ha reordenado",
        "acceptance": "Criterios de aceptaci\u00f3n",
        "error": "Specster no ha podido terminar esta ejecuci\u00f3n",
        "fix": "C\u00f3mo arreglarlo",
        "budget": "El presupuesto de esta issue est\u00e1 agotado",
        "next_questions": "Responde abajo y vuelve a poner la etiqueta `{label}`.",
        "next_spec": "Revisa la spec. La etiqueta `{label}` la construir\u00e1 (v0.2).",
    },
}


@dataclass(frozen=True)
class RenderContext:
    persona: PersonaConfig
    metrics: RunMetrics
    hidden: Sequence[HiddenItem]
    untrusted: Sequence[str]
    spec_label: str = "ai-spec"
    build_label: str = "ai-build"


def _l(ctx: RenderContext) -> dict[str, str]:
    return LABELS.get(ctx.persona.language, LABELS["en"])


def fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def _header(ctx: RenderContext) -> list[str]:
    if not ctx.persona.header:
        return []
    img = (
        f'<img src="{ctx.persona.avatar_url}" width="20" height="20" align="top"> '
        if ctx.persona.avatar_url
        else ""
    )
    return [f"{img}**{ctx.persona.name}**", ""]


def _closing(ctx: RenderContext, generated: str) -> list[str]:
    mode = ctx.persona.closing_line
    line = ctx.persona.closing_text if mode == "fixed" else generated if mode == "generated" else ""
    return [f"_{line.strip()}_", ""] if line.strip() else []


def _hidden(ctx: RenderContext) -> list[str]:
    out: list[str] = []
    if ctx.hidden:
        title = f"{_l(ctx)['hidden']} ({len(ctx.hidden)})"
        out += [f"{HIDDEN_OPEN}<summary>{title}</summary>", ""]
        for item in ctx.hidden:
            out += [f"**{item.where}**", "", fence(item.content), ""]
        out += ["</details>", ""]
    if ctx.untrusted:
        names = ", ".join(sorted(set(ctx.untrusted)))
        out += [f"{_l(ctx)['untrusted']}: {names}", ""]
    return out


def _footer(ctx: RenderContext) -> list[str]:
    m = ctx.metrics
    cost = f"${m.cost_usd:.3f}" if m.cost_usd is not None else "cost unknown"
    rows = [
        "| Provider | Model | Input | Cache read | Cache write | Output | Turns |",
        "|---|---|---|---|---|---|---|",
        f"| {m.provider} | {m.model} | {m.input_tokens} | {m.cache_read_tokens} | "
        f"{m.cache_write_tokens} | {m.output_tokens} | {m.turns} |",
    ]
    shown = f" ({', '.join(m.files_read[:10])})" if m.files_read else ""
    read = ", ".join(m.skills_read) or "none"
    facts = [
        f"- Files read: {len(m.files_read)}{shown}",
        f"- Comments: {m.comments_included} read, {m.comments_untrusted} untrusted, "
        f"{m.comments_after_label} after the label, "
        f"{m.comments_edited_after_label} edited after it",
        f"- Hidden content removed: {m.hidden_removed}",
        f"- Skills: {len(m.skills_available)} available, read: {read}",
    ]
    if m.plan_max_parallel is not None:
        facts.append(f"- Plan parallelism: up to {m.plan_max_parallel} tasks at once")
    facts += [f"- Cut: {t}" for t in m.truncations]
    facts += [f"- Warning: {w}" for w in m.warnings]
    summary = f"{cost}{_DOT}{m.duration_s:.0f}s{_DOT}{m.model}"
    return [
        f"{FOOTER_OPEN}<summary>{summary}</summary>",
        "",
        *rows,
        "",
        *facts,
        "",
        "</details>",
        encode_marker(m),
    ]


def _wrap(ctx: RenderContext, body: list[str], closing: str) -> str:
    lines = _header(ctx) + body + [""] + _hidden(ctx) + _closing(ctx, closing) + _footer(ctx)
    return "\n".join(lines)


def _no_closing(ctx: RenderContext) -> RenderContext:
    return replace(ctx, persona=ctx.persona.model_copy(update={"closing_line": "off"}))


def render_questions(result: QuestionsResult, ctx: RenderContext) -> str:
    lab = _l(ctx)
    body = [f"**{lab['questions']}**", "", result.summary, ""]
    for i, q in enumerate(result.questions, 1):
        body.append(f"{i}. **{q.question}**")
        body.append(f"   {q.why}")
        if q.options:
            body.append("   " + _DOT.join(f"`{o}`" for o in q.options))
    body += ["", lab["next_questions"].format(label=ctx.spec_label)]
    return _wrap(ctx, body, result.closing_line)


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {i}" for i in items] or [f"- {_NONE}"]


def render_spec(
    result: SpecResult, tasks: Sequence[PlanTask], fixes: Sequence[str], ctx: RenderContext
) -> str:
    lab = _l(ctx)
    lv = levels(tasks)
    body = [
        f"### {result.title}",
        "",
        f"**{lab['objective']}.** {result.objective}",
        "",
        f"**{lab['in_scope']}**",
        *_bullets(result.in_scope),
        "",
        f"**{lab['out_of_scope']}**",
        *_bullets(result.out_of_scope),
        "",
        f"**{lab['files']}**",
        *_bullets([f"`{f}`" for f in result.files]),
        "",
        f"**{lab['approach']}**",
        "",
        result.approach,
        "",
        f"**{lab['risks']}**",
        *_bullets(result.risks),
        "",
        f"**{lab['tests']}**",
        "",
        result.test_strategy,
        "",
        f"#### {lab['plan']}",
        "",
        f"| {lab['task']} | {lab['files']} | {lab['depends']} | {lab['level']} |",
        "|---|---|---|---|",
    ]
    for t in tasks:
        files = ", ".join(f"`{f}`" for f in t.files)
        deps = ", ".join(f"`{d}`" for d in t.depends_on) or _NONE
        body.append(f"| `{t.id}` | {files} | {deps} | {lv[t.id]} |")
    if fixes:
        body += ["", f"**{lab['reordered']}:**", *_bullets(fixes)]
    body += ["", "```mermaid", mermaid(tasks), "```", "", f"**{lab['acceptance']}**"]
    for t in tasks:
        body.append(f"- `{t.id}` {t.title}")
        body += [f"  - {a}" for a in t.acceptance]
    body += ["", lab["next_spec"].format(label=ctx.build_label)]
    payload, digest = plan_payload(tasks)
    safe = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    plan_marker = f"<!-- specster:plan {safe} sha256={digest} -->"
    return _wrap(ctx, body, result.closing_line) + "\n" + plan_marker


def render_error(message: str, hint: str, ctx: RenderContext) -> str:
    lab = _l(ctx)
    body = [f"**{lab['error']}**", "", fence(message), "", f"**{lab['fix']}:** {hint}"]
    return _wrap(_no_closing(ctx), body, "")


def render_budget(spent_usd: float, unknown_runs: int, cap: float, ctx: RenderContext) -> str:
    lab = _l(ctx)
    extra = f" (+{unknown_runs} runs with unknown cost)" if unknown_runs else ""
    body = [f"**{lab['budget']}**", "", f"${spent_usd:.2f}{extra} of ${cap:.2f}."]
    return _wrap(_no_closing(ctx), body, "")
