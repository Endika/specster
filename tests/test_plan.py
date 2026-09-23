import pytest

from specster.plan import PlanError, levels, max_parallel, mermaid, normalize_plan, plan_payload
from specster.schemas import PlanTask


def t(id: str, files: list[str], deps: list[str] | None = None) -> PlanTask:
    return PlanTask(
        id=id, title=id, description=id, files=files, depends_on=deps or [], acceptance=["ok"]
    )


def test_independent_tasks_on_the_same_file_are_chained_in_listed_order() -> None:
    tasks, fixes = normalize_plan([t("a", ["x.py"]), t("b", ["x.py", "y.py"]), t("c", ["z.py"])])
    by_id = {task.id: task for task in tasks}
    assert by_id["b"].depends_on == ["a"]
    assert by_id["c"].depends_on == []
    assert fixes == ["b now runs after a: both touch x.py"]


def test_already_ordered_tasks_are_left_alone() -> None:
    tasks, fixes = normalize_plan(
        [t("a", ["x.py"]), t("b", ["y.py"], ["a"]), t("c", ["x.py"], ["b"])]
    )
    assert fixes == []
    assert [task.depends_on for task in tasks] == [[], ["a"], ["b"]]


def test_cycle_is_an_error() -> None:
    with pytest.raises(PlanError, match="cycle"):
        normalize_plan([t("a", ["x"], ["b"]), t("b", ["y"], ["a"])])


def test_unknown_dependency_is_an_error() -> None:
    with pytest.raises(PlanError, match="unknown task 'zzz'"):
        normalize_plan([t("a", ["x"], ["zzz"])])


def test_duplicate_id_is_an_error() -> None:
    with pytest.raises(PlanError, match="duplicate"):
        normalize_plan([t("a", ["x"]), t("a", ["y"])])


def test_levels_and_parallelism() -> None:
    tasks = [t("a", ["1"]), t("b", ["2"]), t("c", ["3"], ["a", "b"]), t("d", ["4"])]
    assert levels(tasks) == {"a": 0, "b": 0, "c": 1, "d": 0}
    assert max_parallel(tasks) == 3


def test_fully_sequential_plan_has_parallelism_one() -> None:
    assert max_parallel([t("a", ["1"]), t("b", ["2"], ["a"]), t("c", ["3"], ["b"])]) == 1


def test_mermaid_lists_every_edge() -> None:
    out = mermaid([t("a", ["1"]), t("b", ["2"], ["a"])])
    assert out.splitlines()[0] == "graph TD"
    assert "  a --> b" in out


def test_payload_hash_is_stable_across_calls() -> None:
    tasks = [t("a", ["1"])]
    assert plan_payload(tasks) == plan_payload(list(tasks))
