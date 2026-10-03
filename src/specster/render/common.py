"""Escaping, the metrics footer and the fitting every Specster comment shares."""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from specster.closing import rewrite_references
from specster.config import PersonaConfig
from specster.metrics import RunMetrics, encode_marker
from specster.render.labels import LABELS
from specster.thread import FOOTER_OPEN, HIDDEN_OPEN, HiddenItem

HIDDEN_ITEM_MAX = 2_000


HIDDEN_SECTION_MAX = 20_000


TEST_OUTPUT_TAIL = 3_000


# GitHub refuses bodies over 65,536 characters; the rest is room for the footer.
BODY_MAX = 60_000


_OVER = f"the body would pass {BODY_MAX:,} characters"


_DOT = " \u00b7 "


_NONE = "\u2014"


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


# A matched code span on one line: GitHub shows it verbatim, entities included, and never pings
# or opens HTML inside it. Across lines it would not protect: a line starting "<!--" is an HTML
# block before any span is parsed.
_SPAN = re.compile(r"(?<!`)(`+)(?!`)[^\n]*?[^`\n]\1(?!`)")


def _outside_spans(text: str, fn: Callable[[str], str]) -> str:
    out: list[str] = []
    last = 0
    for m in _SPAN.finditer(text):
        out += [fn(text[last : m.start()]), m[0]]
        last = m.end()
    return "".join([*out, fn(text[last:])])


def _no_mentions(escaped: str) -> str:
    return _outside_spans(escaped, lambda s: _MENTION.sub("&#64;", s))


def _escaped(text: str) -> str:
    # A "<!--" would hide the footer and "<details" could forge our sections; "&" first, so no
    # entity the text carries (like "&#32;") ever decodes.
    return _outside_spans(
        rewrite_references(text), lambda s: s.replace("&", "&amp;").replace("<", "&lt;")
    )


def _unescaped(text: str) -> str:
    """Undo _prose's escapes, "&" last, so only what the model wrote comes back."""
    return _outside_spans(
        text, lambda s: s.replace("&#64;", "@").replace("&lt;", "<").replace("&amp;", "&")
    )


def _prose(text: str) -> str:
    """Model-written text, which must never ping a person or team either."""
    return _no_mentions(_escaped(text))


def _code(text: str) -> str:
    # Backticks first: "fixes `#3`" only becomes a closing reference once they are gone.
    return " ".join(rewrite_references(text.replace("`", "")).split())


def _code_cell(text: str) -> str:
    """_code for a table cell: GFM splits a cell on "|" even inside a code span."""
    return _code(text).replace("|", "\\|")


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


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {_prose(i)}" for i in items] or [f"- {_NONE}"]


def _flat(text: str) -> str:
    return _prose(" ".join(text.split()))


def _cell(text: str) -> str:
    return _flat(text).replace("|", "\\|")


@dataclass(frozen=True)
class _Cuts:
    """How much of each cuttable section a build body keeps; None keeps all of it."""

    output: bool = True
    findings: int | None = None
    unapplied: int | None = None
    evidence: int | None = None
    pages: int | None = None
    rows: int | None = None


def _kept[T](items: Sequence[T], keep: int | None, what: str, notes: list[str]) -> Sequence[T]:
    if keep is None or keep >= len(items):
        return items
    notes.append(f"{len(items) - keep} of {len(items)} {what} left out: {_OVER}")
    return items[:keep]


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
        ("evidence", lambda c, k: replace(c, evidence=k)),
        ("pages", lambda c, k: replace(c, pages=k)),
        ("rows", lambda c, k: replace(c, rows=k)),
    )
    for name, cut in steps:
        if len(out) <= BODY_MAX:
            break
        if name not in sizes:
            continue
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


def _links(comments: Sequence[tuple[str, str]]) -> list[str]:
    return [f"- {_prose(_code(author))}: {url}" for author, url in comments]


def _build_wrap(ctx: RenderContext, body: list[str], notes: Sequence[str]) -> str:
    if notes:
        m = ctx.metrics
        ctx = replace(ctx, metrics=m.model_copy(update={"truncations": [*m.truncations, *notes]}))
    return _wrap(_no_closing(ctx), body, "")
