import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from specster.build import BuildReport, TaskRecord
from specster.closing import rewrite_references
from specster.config import PersonaConfig
from specster.metrics import RunMetrics, encode_marker
from specster.plan import levels, mermaid, plan_payload
from specster.schemas import Finding, PlanTask, QuestionsResult, SpecResult
from specster.thread import FOOTER_OPEN, HIDDEN_OPEN, HiddenItem

HIDDEN_ITEM_MAX = 2_000
HIDDEN_SECTION_MAX = 20_000
TEST_OUTPUT_TAIL = 3_000
# GitHub refuses bodies over 65,536 characters; the rest is room for the footer.
BODY_MAX = 60_000
_OVER = f"the body would pass {BODY_MAX:,} characters"
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
        "changes": "Changes from the previous spec",
        "error": "Specster could not finish this run",
        "fix": "How to fix it",
        "budget": "Budget for this issue is spent",
        "next_questions": "Answer below, then add the `{label}` label again.",
        "next_spec": "Review the spec. The `{label}` label will build it.",
        "refused": "Specster will not build this issue",
        "pr_opened": "Pull request opened",
        "build_failed": "The build failed",
        "not_approved": "The reviewer did not approve the build",
        "build_budget": "The build stopped: its budget is spent",
        "build_time": "The build stopped: it reached its time limit (`build.max_minutes`)",
        "test_result": "Tests",
        "commit": "Commit",
        "status": "Status",
        "no_tests": "No tests were run: `build.test_command` is not set.",
        "tests_ok": "Tests pass on the branch ({runs} test runs in total).",
        "tests_bad": "Tests fail on the branch (exit {code}).",
        "tests_timeout": "Tests timed out on the branch.",
        "minor": "Minor findings",
        "pending": "Findings still open",
        "unapplied": "Comments after the spec, not applied: add `{label}` again to fold them in",
        "branch": "Branch",
        "no_branch": "Nothing was committed, so no branch was pushed.",
        "spec_link": "Spec",
        "next_pr": "Review the pull request.",
        "next_human": "A person needs to take it from here.",
        "st_done": "done",
        "st_failed": "failed",
        "st_skipped": "skipped",
        "st_not_started": "not started",
        "hint_default_branch": "Run the build from the default branch.",
        "hint_branch_exists": (
            "Delete the branch (or merge its pull request), then add the `{label}` label "
            "again. Specster never overwrites a branch."
        ),
        "hint_root": (
            "Run the build phase with the Docker action (it starts as root); see the "
            "README's build section."
        ),
        "hint_pull_403": (
            "The branch was pushed. With GITHUB_TOKEN, enable 'Allow GitHub Actions to "
            "create and approve pull requests' in the repository settings, or pass a "
            "GitHub App token."
        ),
        "hint_pull_other": (
            "The branch was pushed, but GitHub refused the pull request (HTTP {status}). "
            "Open it from the branch by hand, or fix the cause, delete the branch and add "
            "the `{label}` label again."
        ),
        "hint_sandbox": (
            "Nothing was pushed. See the workflow log, fix the cause, then add the "
            "`{label}` label again."
        ),
        "hint_push": (
            "Check that the token has contents: write and that the branch does not exist "
            "on the remote, then add the `{label}` label again."
        ),
        "hint_role_model": "Check the provider credentials and the model id in {key}.",
        "hint_checkout": "Add actions/checkout before Specster.",
        "hint_docker_socket": (
            "Nothing ran. Remove the Docker socket mount or restrict its mode, then add the "
            "`{label}` label again."
        ),
        "hint_head": (
            "Check out the default branch at the commit the event saw (actions/checkout "
            "without a ref), then add the `{label}` label again."
        ),
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
        "changes": "Cambios respecto a la spec anterior",
        "error": "Specster no ha podido terminar esta ejecuci\u00f3n",
        "fix": "C\u00f3mo arreglarlo",
        "budget": "El presupuesto de esta issue est\u00e1 agotado",
        "next_questions": "Responde abajo y vuelve a poner la etiqueta `{label}`.",
        "next_spec": "Revisa la spec. La etiqueta `{label}` la construir\u00e1.",
        "refused": "Specster no va a construir esta issue",
        "pr_opened": "Pull request abierta",
        "build_failed": "La construcci\u00f3n ha fallado",
        "not_approved": "El revisor no ha aprobado la construcci\u00f3n",
        "build_budget": "La construcci\u00f3n se ha parado: presupuesto agotado",
        "build_time": (
            "La construcci\u00f3n se ha parado: ha llegado a su tiempo m\u00e1ximo "
            "(`build.max_minutes`)"
        ),
        "test_result": "Tests",
        "commit": "Commit",
        "status": "Estado",
        "no_tests": "Sin tests ejecutados: `build.test_command` no est\u00e1 configurado.",
        "tests_ok": "Los tests pasan en la rama ({runs} ejecuciones en total).",
        "tests_bad": "Los tests fallan en la rama (salida {code}).",
        "tests_timeout": "Los tests han agotado el tiempo en la rama.",
        "minor": "Hallazgos menores",
        "pending": "Hallazgos pendientes",
        "unapplied": (
            "Comentarios posteriores a la spec, no aplicados: vuelve a "
            "poner `{label}` para incorporarlos"
        ),
        "branch": "Rama",
        "no_branch": "No hay ning\u00fan commit, as\u00ed que no se ha subido ninguna rama.",
        "spec_link": "Spec",
        "next_pr": "Revisa la pull request.",
        "next_human": "Ahora le toca a una persona.",
        "st_done": "hecha",
        "st_failed": "fallida",
        "st_skipped": "omitida",
        "st_not_started": "sin empezar",
        "hint_default_branch": "Lanza la construcci\u00f3n desde la rama por defecto.",
        "hint_branch_exists": (
            "Borra la rama (o fusiona su pull request) y vuelve a poner la etiqueta "
            "`{label}`. Specster nunca sobrescribe una rama."
        ),
        "hint_root": (
            "Lanza la fase de construcci\u00f3n con la acci\u00f3n de Docker (arranca "
            "como root); mira la secci\u00f3n de construcci\u00f3n del README."
        ),
        "hint_pull_403": (
            "La rama se ha subido. Con GITHUB_TOKEN, activa 'Allow GitHub Actions to "
            "create and approve pull requests' en los ajustes del repositorio, o pasa un "
            "token de GitHub App."
        ),
        "hint_pull_other": (
            "La rama se ha subido, pero GitHub ha rechazado la pull request (HTTP "
            "{status}). \u00c1brela a mano desde la rama, o arregla la causa, borra la "
            "rama y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_sandbox": (
            "No se ha subido nada. Mira el log del workflow, arregla la causa y vuelve a "
            "poner la etiqueta `{label}`."
        ),
        "hint_push": (
            "Comprueba que el token tiene contents: write y que la rama no existe en el "
            "remoto, y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_role_model": "Revisa las credenciales del proveedor y el id del modelo en {key}.",
        "hint_checkout": "A\u00f1ade actions/checkout antes de Specster.",
        "hint_docker_socket": (
            "No se ha ejecutado nada. Quita el montaje del socket de Docker o restringe su "
            "modo y vuelve a poner la etiqueta `{label}`."
        ),
        "hint_head": (
            "Haz checkout de la rama por defecto en el commit que vio el evento "
            "(actions/checkout sin ref) y vuelve a poner la etiqueta `{label}`."
        ),
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


def hint(language: str, name: str, /, **values: object) -> str:
    return LABELS.get(language, LABELS["en"])[name].format(**values)


def fence(text: str, info: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{info}\n{text}\n{ticks}"


_MENTION = re.compile(r"(?<!\w)@(?=[A-Za-z0-9])")


def _no_mentions(escaped: str) -> str:
    return _MENTION.sub("&#64;", escaped)


def _escaped(text: str) -> str:
    # A "<!--" would hide the footer and "<details" could forge our sections; "&" first, so no
    # entity the text carries (like "&#32;") ever decodes.
    return rewrite_references(text).replace("&", "&amp;").replace("<", "&lt;")


def _prose(text: str) -> str:
    """Model-written text, which must never ping a person or team either."""
    return _no_mentions(_escaped(text))


def _code(text: str) -> str:
    # Backticks first: "fixes `#3`" only becomes a closing reference once they are gone.
    return " ".join(rewrite_references(text.replace("`", "")).split())


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
    if mode == "fixed":
        # The owner's own words from the config: mentions in it are meant.
        text = _escaped(ctx.persona.closing_text.strip())
    else:
        text = _prose(generated.strip()) if mode == "generated" else ""
    return [f"_{text}_", ""] if text else []


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


def _role_rows(ctx: RenderContext) -> list[str]:
    m = ctx.metrics
    rows = [
        "| Role | Provider | Model | Input | Cache read | Cache write | Output | Turns | Cost |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for role, r in m.roles.items():
        cost = f"${r.cost_usd:.3f}" if r.cost_usd is not None else "cost unknown"
        rows.append(
            f"| {role} | {r.provider} | {r.model} | {r.input_tokens} | {r.cache_read_tokens} | "
            f"{r.cache_write_tokens} | {r.output_tokens} | {r.turns} | {cost} |"
        )
    return rows


def _footer(ctx: RenderContext) -> list[str]:
    m = ctx.metrics
    cost = f"${m.cost_usd:.3f}" if m.cost_usd is not None else "cost unknown"
    if m.roles:
        rows = _role_rows(ctx)
    else:
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
    # The thread and the planner's file reads are the spec phase's; a build has neither.
    facts = (
        []
        if m.phase == "build"
        else [
            f"- Files read: {len(m.files_read)}{shown}",
            f"- Comments: {m.comments_included} read, {m.comments_untrusted} untrusted, "
            f"{m.comments_after_label} after the label, "
            f"{m.comments_edited_after_label} edited after it",
            f"- Hidden content removed: {m.hidden_removed}",
        ]
    )
    facts.append(
        f"- Skills: {len(m.skills_available)} available; loaded: {inlined}; "
        f"read by the model: {read}"
    )
    if m.plan_max_parallel is not None:
        facts.append(f"- Plan parallelism: up to {m.plan_max_parallel} tasks at once")
    if m.roles:
        facts.append(
            f"- Build: {m.tasks_done} of {m.tasks_total} tasks, {m.test_runs} test runs, "
            f"up to {m.parallel_used} workers at once, {m.review_rounds} review rounds"
        )
    if m.revision:
        facts.append("- Revision of the previous spec")
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
    body += ["", lab["next_spec"].format(label=ctx.build_label)]
    payload, digest = plan_payload(tasks)
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
            # Undo _prose's escapes, "&" last, so only what the model wrote comes back.
            text = line[4:].replace("&#64;", "@").replace("&lt;", "<").replace("&amp;", "&")
            return rewrite_references(" ".join(text.split())) or None
    return None


def _links(comments: Sequence[tuple[str, str]]) -> list[str]:
    return [f"- {_prose(_code(author))}: {url}" for author, url in comments]


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


def _flat(text: str) -> str:
    return _prose(" ".join(text.split()))


def _cell(text: str) -> str:
    return _flat(text).replace("|", "\\|")


def _status(record: TaskRecord, lab: Mapping[str, str]) -> str:
    key = "not_started" if record.status == "pending" else record.status
    status = lab[f"st_{key}"]
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
    if not output:
        return lines, f"test output left out: {_OVER}"
    tail, note = res.output, None
    if len(tail) > TEST_OUTPUT_TAIL:
        note = f"test output cut to the last {TEST_OUTPUT_TAIL:,} of {len(tail):,} characters"
        tail = tail[-TEST_OUTPUT_TAIL:]
    lines += ["", fence(rewrite_references(tail))]
    return lines, note


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


@dataclass(frozen=True)
class _Cuts:
    """How much of each cuttable section a build body keeps; None keeps all of it."""

    output: bool = True
    findings: int | None = None
    unapplied: int | None = None
    rows: int | None = None


def _kept[T](items: Sequence[T], keep: int | None, what: str, notes: list[str]) -> Sequence[T]:
    if keep is None or keep >= len(items):
        return items
    notes.append(f"{len(items) - keep} of {len(items)} {what} left out: {_OVER}")
    return items[:keep]


def _unapplied(view: BuildView, ctx: RenderContext, cuts: _Cuts, notes: list[str]) -> list[str]:
    links = _kept(view.unapplied, cuts.unapplied, "unapplied comments", notes)
    if not links:
        return []
    return [f"**{_l(ctx)['unapplied'].format(label=ctx.spec_label)}**", *_links(links), ""]


def _build_wrap(ctx: RenderContext, body: list[str], notes: Sequence[str]) -> str:
    if notes:
        m = ctx.metrics
        ctx = replace(ctx, metrics=m.model_copy(update={"truncations": [*m.truncations, *notes]}))
    return _wrap(_no_closing(ctx), body, "")


def _fit(
    ctx: RenderContext,
    make: Callable[[_Cuts, list[str]], list[str]],
    sizes: Mapping[str, int],
) -> str:
    def render(cuts: _Cuts) -> str:
        notes: list[str] = []
        return _build_wrap(ctx, make(cuts, notes), notes)

    cuts = _Cuts()
    out = render(cuts)
    if len(out) <= BODY_MAX:
        return out
    cuts = replace(cuts, output=False)
    out = render(cuts)
    steps: tuple[tuple[str, Callable[[_Cuts, int], _Cuts]], ...] = (
        ("findings", lambda c, k: replace(c, findings=k)),
        ("unapplied", lambda c, k: replace(c, unapplied=k)),
        ("rows", lambda c, k: replace(c, rows=k)),
    )
    for name, cut in steps:
        if len(out) <= BODY_MAX:
            break
        low, high = 0, sizes[name]
        while low < high:
            mid = (low + high + 1) // 2
            if len(render(cut(cuts, mid))) <= BODY_MAX:
                low = mid
            else:
                high = mid - 1
        cuts = cut(cuts, low)
        out = render(cuts)
    if len(out) <= BODY_MAX:
        return out
    notes: list[str] = []
    body = make(cuts, notes)
    # The closing lines (the next step, `Closes #n`) always stay.
    text, tail = "\n".join(body[:-3]), body[-3:]
    keep = max(0, len(text) - (len(out) - BODY_MAX) - 1_000)
    notes.append(f"body cut to its first {keep:,} of {len(text):,} characters: {_OVER}")
    return _build_wrap(ctx, [text[:keep], "", *tail], notes)


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
        kept = _kept(pending, cuts.findings, "pending findings", notes)
        if kept:
            body += [f"**{lab['pending']}**", *_by_task(kept), ""]
        if report.reason:
            body += [_prose(report.reason), ""]
        body += _unapplied(view, ctx, cuts, notes)
        body.append(lab["next_pr"] if view.pr_url else lab["next_human"])
        return body

    sizes = {"findings": len(pending), "unapplied": len(view.unapplied), "rows": len(report.tasks)}
    return _fit(ctx, make, sizes)
