from collections.abc import Callable
from pathlib import Path

import pytest
from opentelemetry import metrics

from specster.config import ModelConfig
from specster.llm.base import ChatModel, ToolCall
from specster.run import main
from tests.conftest import Point
from tests.fakes import FakeTracker, ScriptBook, ScriptedModel
from tests.test_run import SPEC, env, run, tracker
from tests.test_run_build import approve, book, is_reviewer, unprivileged, world
from tests.test_run_cleanup import go as cleanup
from tests.test_run_cleanup import tracker as cleanup_tracker
from tests.test_run_cleanup import world as cleanup_world
from tests.test_run_evidence import go as evidence_go
from tests.test_run_evidence import tracker as evidence_tracker
from tests.test_run_evidence import world as evidence_world

Points = Callable[[], list[Point]]


def named(points: list[Point], name: str, attrs: dict[str, str] | None = None) -> list[Point]:
    want = (attrs or {}).items()
    return [p for p in points if p.name == name and want <= p.attributes.items()]


@pytest.mark.usefixtures("metric_reader")
def test_a_gauge_set_in_one_test_is_left_behind_unread() -> None:
    metrics.get_meter("specster").create_gauge("specster.test.leftover").set(1)


def test_the_next_test_starts_without_the_last_tests_points(metric_points: Points) -> None:
    assert named(metric_points(), "specster.test.leftover") == []
    metrics.get_meter("specster").create_gauge("specster.test.leftover").set(2)
    assert [p.value for p in named(metric_points(), "specster.test.leftover")] == [2]
    assert metric_points() == []


def test_a_spec_run_records_its_gauges(tmp_path: Path, metric_points: Points) -> None:
    assert run(env(tmp_path), tracker(), ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])) == 0
    points = metric_points()
    runs = named(points, "specster.runs")
    assert [(p.value, p.attributes) for p in runs] == [
        (1, {"specster.repo": "o/r", "specster.phase": "spec", "specster.outcome": "spec"})
    ]
    tokens = named(points, "specster.run.tokens")
    assert {p.attributes["specster.token.type"] for p in tokens} == {
        "input",
        "output",
        "cache_read",
        "cache_write",
    }
    assert [p.unit for p in named(points, "specster.run.cost")] == ["USD"]
    kinds = {p.attributes["specster.kind"] for p in named(points, "specster.spec.comments")}
    assert kinds == {"included", "untrusted", "after_label", "edited_after_label"}
    assert named(points, "specster.build.tasks") == []
    assert not [k for p in points for k in p.attributes if "issue" in k]


def test_a_build_records_tasks_roles_and_escalation(tmp_path: Path, metric_points: Points) -> None:
    config = "models:\n  escalation: {provider: anthropic, model: claude-opus-5}\n"
    e, tr, _ = world(tmp_path, config)
    stuck = ScriptBook({'id="a"': [["thinking", "still thinking"]]})
    reviewer = approve()

    def make(cfg: ModelConfig) -> ChatModel:
        if cfg.model == "claude-opus-5":
            return book()
        return reviewer if is_reviewer(cfg) else stuck

    assert main(e, tr, make, lambda *_: b"", timer=lambda: 0.0, identity=unprivileged) == 0
    points = metric_points()
    runs = named(
        points, "specster.runs", {"specster.phase": "build", "specster.outcome": "pr_opened"}
    )
    assert len(runs) == 1
    escalated = named(points, "specster.build.tasks", {"specster.state": "escalated"})
    assert [p.value for p in escalated] == [1]
    roles = named(
        points,
        "specster.role.cost",
        {"specster.role": "worker-escalated", "gen_ai.request.model": "claude-opus-5"},
    )
    assert len(roles) == 1 and roles[0].attributes["gen_ai.provider.name"] == "anthropic"
    assert named(points, "specster.spec.comments") == []


class FailsOnce(FakeTracker):
    failed = False

    def post_comment(self, number: int, body: str) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("GitHub is down")
        super().post_comment(number, body)


def test_a_failed_publish_records_one_error_run(tmp_path: Path, metric_points: Points) -> None:
    base = tracker()
    tr = FailsOnce(issue=base.issue, label_events=base.label_events)
    assert run(env(tmp_path), tr, ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])) == 1
    runs = named(metric_points(), "specster.runs")
    assert [p.attributes["specster.outcome"] for p in runs] == ["error"]


def test_an_unknown_price_skips_only_the_cost_gauge(tmp_path: Path, metric_points: Points) -> None:
    config = "models:\n  planner: {provider: anthropic, model: claude-unpriced}\n"
    model = ScriptedModel([[ToolCall("1", "submit_spec", SPEC)]])
    assert run(env(tmp_path, config=config), tracker(), model) == 0
    points = metric_points()
    assert named(points, "specster.run.cost") == []
    assert len(named(points, "specster.runs", {"specster.outcome": "spec"})) == 1


def test_cleanup_records_only_the_run(tmp_path: Path, metric_points: Points) -> None:
    e, _, _ = cleanup_world(tmp_path, "specster/issue-3")
    assert cleanup(e, cleanup_tracker()) == 0
    assert [(p.name, p.attributes) for p in metric_points()] == [
        (
            "specster.runs",
            {"specster.repo": "o/r", "specster.phase": "cleanup", "specster.outcome": "skipped"},
        )
    ]


def test_an_evidence_run_records_its_phase_and_evidence(
    tmp_path: Path, metric_points: Points
) -> None:
    e, pull, _ = evidence_world(tmp_path, "{preview}")
    model = ScriptedModel([[ToolCall("s", "submit_evidence", {"why": "Only docs change."})]])
    assert evidence_go(e, evidence_tracker(pull), model) == 0
    points = metric_points()
    base = {"specster.phase": "evidence", "specster.outcome": "evidence_posted"}
    assert len(named(points, "specster.runs", base)) == 1
    assert len(named(points, "specster.role.turns", base | {"specster.role": "planner"})) == 1
    states = {p.attributes["specster.state"] for p in named(points, "specster.build.evidence")}
    assert states == {"items", "problems"} and named(points, "specster.build.tasks") == []
