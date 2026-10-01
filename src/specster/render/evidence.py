"""The before/after evidence section of the pull request body."""

from collections.abc import Mapping

from specster.closing import rewrite_references
from specster.evidence import Capture, EvidenceRun
from specster.render.common import _NONE, _code, _kept, _prose, fence

DIFF_INLINE_MAX = 4_000


_TO = " \u2192 "


def _status(capture: Capture | None) -> str:
    if capture is None:
        return _NONE
    return "error" if capture.status is None else str(capture.status)


def evidence_section(
    run: EvidenceRun,
    lab: Mapping[str, str],
    links: Mapping[str, str] | None,
    keep: int | None,
    notes: list[str],
) -> list[str]:
    out = [f"**{lab['evidence_pr']}**", ""]
    if links and "folder" in links:
        out += [lab["evidence_files"].format(url=links["folder"]), ""]
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
    out += [
        f"| {lab['evidence_request']} | | {lab['status']} | {lab['evidence_changed']} |",
        "|---|---|---|---|",
    ]
    for item in run.items:
        r = item.request
        out.append(
            f"| `{_code(r.name)}` | `{r.method} {_code(r.path)}` | "
            f"{_status(item.base)}{_TO}{_status(item.head)} | {yes if item.changed else no} |"
        )
    out.append("")
    cut = [
        lab["evidence_cut"].format(name=_code(item.request.name), side=side)
        for item in run.items
        for side, got in (("base", item.base), ("head", item.head))
        if got is not None and got.truncated
    ]
    if cut:
        out += [*(f"- {c}" for c in cut), ""]
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
