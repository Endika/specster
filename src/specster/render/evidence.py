"""The before/after evidence section of the pull request body."""

import html
from collections.abc import Mapping, Sequence

from specster.closing import rewrite_references
from specster.evidence import Capture, EvidenceRun, PageItem, Shot
from specster.render.common import _NONE, _cell, _code, _code_cell, _kept, _prose, fence

DIFF_INLINE_MAX = 4_000
# Display widths in the pull request, for a 1280 and a 390 pixel wide viewport.
SHOT_WIDTH = {"desktop": 400, "mobile": 200}


_TO = " \u2192 "


def _status(capture: Capture | None) -> str:
    if capture is None:
        return _NONE
    return "error" if capture.status is None else str(capture.status)


def _shot(
    page: PageItem, side: str, shot: Shot, lab: Mapping[str, str], links: Mapping[str, str]
) -> str:
    cell = _picture(page, side, shot, lab, links)
    if not shot.http_error:
        return cell
    if shot.no_response:
        said = lab["evidence_no_response"]
    else:
        said = lab["evidence_http_status"].format(status=shot.http_status)
    return f"{cell}<br>{said}"


def _picture(
    page: PageItem, side: str, shot: Shot, lab: Mapping[str, str], links: Mapping[str, str]
) -> str:
    if shot.png is None:
        return _cell(shot.note)
    name = f"{page.page.name}.{side}.{shot.viewport}.png"
    url = links.get(name)
    if url is None:
        return lab["evidence_not_uploaded"]
    alt = html.escape(f"{page.page.name} {side} {shot.viewport}")
    return f'<img src="{html.escape(url)}" alt="{alt}" width="{SHOT_WIDTH[shot.viewport]}">'


def _pages(
    pages: Sequence[PageItem], lab: Mapping[str, str], links: Mapping[str, str]
) -> list[str]:
    out: list[str] = []
    for page in pages:
        compared = any(
            b.png is not None and h.png is not None
            for b, h in zip(page.base, page.head, strict=True)
        )
        changed = lab["evidence_yes" if page.changed else "evidence_no"] if compared else _NONE
        out += [
            f"**`{_code(page.page.name)}`** `{_code(page.page.path)}`"
            f" \u00b7 {lab['evidence_changed']}: {changed}",
            "",
            f"| | {lab['evidence_before']} | {lab['evidence_after']} |",
            "|---|---|---|",
        ]
        for b, h in zip(page.base, page.head, strict=True):
            out.append(
                f"| {lab['evidence_' + b.viewport]} | {_shot(page, 'base', b, lab, links)} "
                f"| {_shot(page, 'head', h, lab, links)} |"
            )
        out.append("")
    return out


def evidence_section(
    run: EvidenceRun,
    lab: Mapping[str, str],
    links: Mapping[str, str] | None,
    keep: int | None,
    notes: list[str],
    keep_pages: int | None = None,
) -> list[str]:
    out = [f"**{lab['evidence_pr']}**", ""]
    if links and "folder" in links:
        files = "evidence_files_pages" if run.pages else "evidence_files"
        out += [lab[files].format(url=links["folder"]), ""]
    for p in run.problems:
        out.append(f"- {_prose(f'{p.side}: {p.reason}')}")
        if p.log_tail:
            out += [
                f"  <details><summary><code>server-{p.side}.log</code></summary>",
                "",
                *(
                    f"  {line}"
                    for line in fence(rewrite_references(p.log_tail.rstrip("\n"))).splitlines()
                ),
                "",
                "  </details>",
            ]
    if run.problems:
        out.append("")
    yes, no = lab["evidence_yes"], lab["evidence_no"]
    if run.items:
        out += [
            f"| {lab['evidence_request']} | | {lab['status']} | {lab['evidence_changed']} |",
            "|---|---|---|---|",
        ]
        for item in run.items:
            r = item.request
            out.append(
                f"| `{_code_cell(r.name)}` | `{r.method} {_code_cell(r.path)}` | "
                f"{_status(item.base)}{_TO}{_status(item.head)} | {yes if item.changed else no} |"
            )
        out.append("")
    said: list[str] = []
    for item in run.items:
        name = _code(item.request.name)
        for side, got in (("base", item.base), ("head", item.head)):
            if got is None:
                continue
            if got.status is None:
                why = _code(got.failure)
                said.append(lab["evidence_failed"].format(name=name, side=side, why=why))
            elif got.truncated:
                cut = "evidence_cut_time" if got.late else "evidence_cut"
                said.append(lab[cut].format(name=name, side=side))
    if said:
        out += [*(f"- {s}" for s in said), ""]
    out += _pages(_kept(run.pages, keep_pages, "pages", notes), lab, links or {})
    changed = [item for item in run.items if item.changed]
    for item in _kept(changed, keep, "evidence requests", notes):
        diff = item.diff
        if len(diff) > DIFF_INLINE_MAX:
            notes.append(
                f"diff cut to its first {DIFF_INLINE_MAX:,} of {len(diff):,} characters: "
                "full diff in the evidence files"
            )
            diff = diff[:DIFF_INLINE_MAX]
        out += [
            f"<details><summary><code>{_code(item.request.name)}</code></summary>",
            "",
            fence(rewrite_references(diff.rstrip("\n")), "diff"),
            "",
            "</details>",
            "",
        ]
    return out
