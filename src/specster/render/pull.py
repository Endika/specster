"""The evidence phase's comment on a pull request."""

from collections.abc import Mapping
from dataclasses import dataclass

from specster.evidence import EvidenceRun
from specster.render.common import RenderContext, _code, _Cuts, _fit, _l, _prose
from specster.render.evidence import evidence_section
from specster.render.spec import _evidence
from specster.schemas import EvidencePlan


@dataclass(frozen=True)
class PullEvidenceView:
    base: str
    head: str
    label: str
    # None when the planner never submitted.
    plan: EvidencePlan | None
    run: EvidenceRun | None
    links: Mapping[str, str] | None = None
    note: str | None = None
    # Why the run stopped short, said after everything it got.
    reason: str = ""
    budget_spent: bool = False
    out_of_time: bool = False


def render_pull_evidence(view: PullEvidenceView, ctx: RenderContext) -> str:
    lab = _l(ctx)
    plan, run = view.plan, view.run
    captured = run is not None and bool(run.items or run.pages)

    def make(cuts: _Cuts, notes: list[str]) -> list[str]:
        headline = "evidence_pull"
        if view.budget_spent:
            headline = "evidence_pull_time" if view.out_of_time else "evidence_pull_budget"
        rng = lab["evidence_pull_range"].format(
            base=_code(view.base[:7]), head=_code(view.head[:7])
        )
        body = [f"**{lab[headline]}**", "", rng, ""]
        if plan is not None:
            body += [f"**{lab['evidence_pull_why']}**", "", _prose(plan.why), ""]
            if plan.evidence or plan.pages:
                body += [*_evidence(plan.evidence, plan.pages, lab)[1:], ""]
            else:
                body += [lab["evidence_pull_none"], ""]
        if run is not None and captured:
            body += evidence_section(run, lab, view.links, cuts.evidence, notes, cuts.pages)
        elif plan is not None and (plan.evidence or plan.pages) and not view.reason:
            body += [lab["evidence_pull_not_captured"], ""]
        if view.reason:
            body += [_prose(view.reason), ""]
        if view.note:
            body += [_prose(view.note), ""]
        body.append(lab["evidence_pull_next"].format(label=view.label))
        return body

    sizes = {
        "evidence": sum(1 for i in run.items if i.changed) if run is not None else 0,
        "pages": len(run.pages) if run is not None else 0,
    }
    return _fit(ctx, make, sizes)
