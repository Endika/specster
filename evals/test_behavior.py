import math
from dataclasses import asdict
from pathlib import Path

import pytest

from evals.graders import Case, Check, grade, load_cases
from evals.harness import (
    MAX_TURNS,
    AgentRun,
    CostMeter,
    RecordingModel,
    ResultLog,
    RunRecord,
    add_costs,
    copy_fixture,
    model_cost,
    run_spec_agent,
)
from evals.judge import Verdict, judge
from specster.config import TrustConfig
from specster.github import Issue

pytestmark = pytest.mark.eval


def required_passes(k: int) -> int:
    return math.ceil(2 * k / 3)


def _judge(case: Case, run: AgentRun, model: RecordingModel) -> tuple[Check, Verdict | None]:
    try:
        verdict = judge(model, case.judge, run.thread.text, run.output_json())
    except RuntimeError as e:
        return Check("judge", False, str(e)), None
    return Check("judge", verdict.passed, verdict.reasons), verdict


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_behavior(
    case: Case,
    eval_model: RecordingModel,
    budget: CostMeter,
    k: int,
    results: ResultLog,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    judge_model: RecordingModel | None = (
        request.getfixturevalue("judge_model") if case.judge else None
    )
    issue = Issue(1, case.title, case.body, "reporter", "OWNER", ("ai-spec",))
    passes = 0
    for i in range(k):
        repo = copy_fixture(tmp_path / f"run-{i}")
        run = run_spec_agent(
            eval_model, repo, issue, (), TrustConfig(comments="all"), case.language
        )
        checks: list[Check] = []
        verdict: Verdict | None = None
        judge_cost: float | None = 0.0
        if run.outcome is None:
            checks.append(Check("agent_submitted", False, run.error or ""))
        else:
            checks += grade(case, run.outcome, run.ws, run.tool_names)
            checks.append(Check("turn_cap", run.turns <= MAX_TURNS, f"{run.turns} turns"))
            if judge_model is not None:
                check, verdict = _judge(case, run, judge_model)
                checks.append(check)
                judge_cost = model_cost(judge_model)
        passed = all(c.ok for c in checks)
        passes += passed
        cost = add_costs(run.cost, judge_cost)
        results.write(
            RunRecord(
                "behavior",
                case.id,
                i,
                passed,
                [asdict(c) for c in checks],
                verdict.scores if verdict else None,
                asdict(run.usage),
                cost,
                run.turns,
                run.tool_names,
                run.error,
            ).to_dict()
        )
        budget.add(cost)
    need = required_passes(k)
    assert passes >= need, f"{case.id}: passed {passes}/{k} runs, needs {need}"
