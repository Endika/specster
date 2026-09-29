import os

import pytest

from evals.build_harness import BuildCase, load_build_cases, run_build_case
from evals.harness import CostMeter, ResultLog, RunRecord
from specster.config import ModelConfig

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(os.geteuid() != 0, reason="builds run sandbox slots: root in the image"),
]


@pytest.mark.parametrize("case", load_build_cases(), ids=lambda c: c.id)
def test_build(
    case: BuildCase, eval_model_cfg: ModelConfig, budget: CostMeter, k: int, results: ResultLog
) -> None:
    with_skills = os.environ.get("SPECSTER_EVAL_SKILLS", "on") == "on"
    label = f"{case.id} [{eval_model_cfg.model}, skills {'on' if with_skills else 'off'}]"
    for i in range(k):
        out = run_build_case(case, eval_model_cfg, with_skills)
        report = out.report
        review = report.review
        severities = [f.severity for f in review.findings] if review is not None else []
        checks = [
            {"name": "approved", "ok": report.status == "approved", "detail": report.reason},
            {"name": "hidden_check", "ok": out.hidden.ok, "detail": out.hidden.output[-1500:]},
        ]
        results.write(
            RunRecord(
                "build",
                label,
                i,
                all(c["ok"] for c in checks),
                checks,
                {
                    "review_rounds": report.review_rounds,
                    "test_runs": report.test_runs,
                    "critical": severities.count("critical"),
                    "important": severities.count("important"),
                    "minor": severities.count("minor"),
                    "seconds": int(out.seconds),
                },
                {},
                out.cost,
                out.turns,
            ).to_dict()
        )
        budget.add(out.cost)
