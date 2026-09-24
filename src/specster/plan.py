import hashlib
import json
import posixpath
from collections.abc import Sequence

from specster.schemas import PlanTask


class PlanError(Exception):
    pass


def norm_path(path: str) -> str:
    """The one spelling of a task path: TaskWorkspace writes by it, the plan orders by it."""
    return posixpath.normpath(path)


def _check(tasks: Sequence[PlanTask]) -> None:
    ids = [task.id for task in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise PlanError(f"duplicate task ids: {', '.join(dupes)}")
    known = set(ids)
    for task in tasks:
        for dep in task.depends_on:
            if dep not in known:
                raise PlanError(f"task '{task.id}' depends on unknown task '{dep}'")
    levels(tasks)


def _reaches(deps: dict[str, list[str]], start: str, target: str) -> bool:
    stack, seen = [start], set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(deps[node])
    return False


def normalize_plan(tasks: Sequence[PlanTask]) -> tuple[list[PlanTask], list[str]]:
    _check(tasks)
    deps = {task.id: list(task.depends_on) for task in tasks}
    fixes = []
    for i, earlier in enumerate(tasks):
        for later in tasks[i + 1 :]:
            shared = sorted(
                {norm_path(f) for f in earlier.files} & {norm_path(f) for f in later.files}
            )
            if not shared:
                continue
            ordered = _reaches(deps, later.id, earlier.id) or _reaches(deps, earlier.id, later.id)
            if not ordered:
                deps[later.id].append(earlier.id)
                fixes.append(f"{later.id} now runs after {earlier.id}: both touch {shared[0]}")
    return [task.model_copy(update={"depends_on": deps[task.id]}) for task in tasks], fixes


def levels(tasks: Sequence[PlanTask]) -> dict[str, int]:
    deps = {task.id: task.depends_on for task in tasks}
    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def visit(node: str) -> int:
        if node in depth:
            return depth[node]
        if node in visiting:
            raise PlanError(f"dependency cycle through '{node}'")
        visiting.add(node)
        depth[node] = 1 + max((visit(d) for d in deps[node]), default=-1)
        visiting.discard(node)
        return depth[node]

    for task in tasks:
        visit(task.id)
    return {task.id: depth[task.id] for task in tasks}


def max_parallel(tasks: Sequence[PlanTask]) -> int:
    counts: dict[int, int] = {}
    for level in levels(tasks).values():
        counts[level] = counts.get(level, 0) + 1
    return max(counts.values(), default=0)


def _node(task_id: str) -> str:
    # Bare ids like "end" or "graph" are Mermaid keywords, and "--" in an id reads as an edge.
    return "t_" + task_id.replace("-", "_")


def mermaid(tasks: Sequence[PlanTask]) -> str:
    lines = ["graph TD"]
    for task in tasks:
        label = task.title.replace('"', "'")
        lines.append(f'  {_node(task.id)}["{label}"]')
    for task in tasks:
        lines.extend(f"  {_node(dep)} --> {_node(task.id)}" for dep in task.depends_on)
    return "\n".join(lines)


def plan_payload(tasks: Sequence[PlanTask]) -> tuple[str, str]:
    text = json.dumps([task.model_dump() for task in tasks], sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()
