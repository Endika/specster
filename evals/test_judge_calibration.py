import json
from dataclasses import asdict

import pytest

from evals.harness import CostMeter, RecordingModel, ResultLog, RunRecord, model_cost
from evals.judge import judge, load_calibration

pytestmark = pytest.mark.eval


def test_judge_agrees_with_hand_labels(
    judge_model: RecordingModel, budget: CostMeter, results: ResultLog
) -> None:
    disagreements: list[str] = []
    for i, sample in enumerate(load_calibration()):
        output = json.dumps(sample.output, ensure_ascii=False)
        error: str | None = None
        scores: dict[str, int] | None = None
        try:
            verdict = judge(judge_model, sample.rubric, sample.thread, output)
            scores = verdict.scores
            got = "good" if verdict.passed else "bad"
        except RuntimeError as e:
            error, got = str(e), "no grade"
        agreed = got == sample.label
        if not agreed:
            disagreements.append(f"{sample.id}: labeled {sample.label}, judge said {got}")
        cost = model_cost(judge_model)
        results.write(
            RunRecord(
                "calibration",
                sample.id,
                i,
                agreed,
                [{"name": "agrees_with_label", "ok": agreed, "detail": f"judge said {got}"}],
                scores,
                asdict(judge_model.usage),
                cost,
                0,
                judge_model.tool_names,
                error,
            ).to_dict()
        )
        budget.add(cost)
    assert not disagreements, (
        "the judge disagrees with the hand labels, so its scores in the behavior results are "
        f"not to be trusted: {disagreements}"
    )
