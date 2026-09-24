import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from specster.config import PersonaConfig
from specster.metrics import RunMetrics, encode_marker
from specster.plan import levels, mermaid, plan_payload
from specster.schemas import PlanTask, QuestionsResult, SpecResult
from specster.thread import FOOTER_OPEN, HIDDEN_OPEN, HiddenItem

HIDDEN_ITEM_MAX = 2_000
HIDDEN_SECTION_MAX = 20_000
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


def fence(text: str, info: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{info}\n{text}\n{ticks}"


def _prose(text: str) -> str:
    # Model-written text: a "<!--" would hide the footer and "<details" could forge our sections.
    return text.replace("<", "&lt;")


def _code(text: str) -> str:
    return " ".join(text.replace("`", "").split())


def _indent(text: str) -> list[str]:
    return [f"  {line}" if line else "" for line in _prose(text).splitlines()]


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
    return [f"_{_prose(line.strip())}_", ""] if line.strip() else []


def _hidden(ctx: RenderContext) -> tuple[list[str], int]:
    out: list[str] = []
    cut = 0
    if ctx.hidden:
        title = f"{_l(ctx)['hidden']} ({len(ctx.hidden)})"
        out += [f"{HIDDEN_OPEN}<summary>{title}</summary>", ""]
        room = HIDDEN_SECTION_MAX
        for item in ctx.hidden:
            keep = min(len(item.content), HIDDEN_ITEM_MAX, room)
            cut += len(item.content) - keep
            room -= keep
            if keep:
                out += [f"**{item.where}**", "", fence(item.content[:keep]), ""]
        if cut:
            out += [f"(cut: {cut} characters not shown)", ""]
        out += ["</details>", ""]
    if ctx.untrusted:
        names = ", ".join(sorted(set(ctx.untrusted)))
        out += [f"{_l(ctx)['untrusted']}: {names}", ""]
    return out, cut


def _footer(ctx: RenderContext) -> list[str]:
    m = ctx.metrics
    cost = f"${m.cost_usd:.3f}" if m.cost_usd is not None else "cost unknown"
    rows = [
        "| Provider | Model | Input | Cache read | Cache write | Output | Turns |",
        "|---|---|---|---|---|---|---|",
        f"| {m.provider} | {m.model} | {m.input_tokens} | {m.cache_read_tokens} | "
        f"{m.cache_write_tokens} | {m.output_tokens} | {m.turns} |",
    ]
    listed = [_prose(f) for f in m.files_read[:10]] + (["..."] if len(m.files_read) > 10 else [])
    shown = f" ({', '.join(listed)})" if listed else ""
    inlined = ", ".join(_prose(n) for n in m.skills_inlined) or "none"
    read = ", ".join(_prose(n) for n in m.skills_read) or "none"
    facts = [
        f"- Files read: {len(m.files_read)}{shown}",
        f"- Comments: {m.comments_included} read, {m.comments_untrusted} untrusted, "
        f"{m.comments_after_label} after the label, "
        f"{m.comments_edited_after_label} edited after it",
        f"- Hidden content removed: {m.hidden_removed}",
        f"- Skills: {len(m.skills_available)} available; loaded: {inlined}; "
        f"read by the model: {read}",
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
    hidden, cut = _hidden(ctx)
    if cut:
        note = f"hidden content: {cut} characters not shown"
        m = ctx.metrics
        ctx = replace(ctx, metrics=m.model_copy(update={"truncations": [*m.truncations, note]}))
    lines = _header(ctx) + body + [""] + hidden + _closing(ctx, closing) + _footer(ctx)
    return "\n".join(lines)


def _no_closing(ctx: RenderContext) -> RenderContext:
    return replace(ctx, persona=ctx.persona.model_copy(update={"closing_line": "off"}))


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


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {_prose(i)}" for i in items] or [f"- {_NONE}"]


def render_spec(
    result: SpecResult, tasks: Sequence[PlanTask], fixes: Sequence[str], ctx: RenderContext
) -> str:
    lab = _l(ctx)
    lv = levels(tasks)
    body = [
        f"### {_prose(' '.join(result.title.split()))}",
        "",
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
