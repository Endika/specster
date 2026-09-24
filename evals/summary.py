import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def render(records: list[dict[str, Any]]) -> str:
    if not records:
        return "No eval results.\n"
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault((r["suite"], r["case"]), []).append(r)
    lines = [
        "| suite | case | pass rate | mean cost | mean turns | failing checks |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for (suite, case), runs in groups.items():
        passed = sum(bool(r["passed"]) for r in runs)
        costs = [r["cost_usd"] for r in runs]
        known = [c for c in costs if c is not None]
        cost = _mean(known)
        cost_text = "unknown" if cost is None else f"${cost:.4f}"
        if cost is not None and len(known) < len(costs):
            cost_text += f" ({len(costs) - len(known)} unknown)"
        turns = sum(r["turns"] for r in runs) / len(runs)
        failing = Counter(c["name"] for r in runs for c in r["checks"] if not c["ok"])
        failing_text = ", ".join(f"{name} x{n}" for name, n in sorted(failing.items())) or "-"
        lines.append(
            f"| {suite} | {case} | {passed}/{len(runs)} ({passed / len(runs):.0%}) | {cost_text} "
            f"| {turns:.1f} | {failing_text} |"
        )
    return "\n".join(lines) + "\n"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m evals.summary RESULTS.jsonl", file=sys.stderr)
        return 2
    print("## Specster evals\n")
    print(render(load(Path(argv[0]))), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
