"""A finished run's numbers as OTLP gauges: one point per run, so no temporality to choose."""

from collections.abc import Callable

from opentelemetry import metrics

from specster.metrics import RunMetrics

_TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")


def record_run(repo: str, phase: str, final: tuple[str, RunMetrics | None] | None) -> None:
    if final is None:
        return
    outcome, m = final
    meter = metrics.get_meter("specster")
    base = {"specster.repo": repo, "specster.phase": phase, "specster.outcome": outcome}
    gauges: dict[str, Callable[[float, dict[str, str]], None]] = {}

    def put(name: str, value: float | None, unit: str = "1", **extra: str) -> None:
        if value is None:
            return
        if name not in gauges:
            gauges[name] = meter.create_gauge(name, unit=unit).set
        gauges[name](value, base | extra)

    put("specster.runs", 1)
    if m is None:
        return
    put("specster.run.cost", m.cost_usd, "USD")
    put("specster.run.duration", m.duration_s, "s")
    put("specster.run.turns", m.turns)
    for t in _TOKEN_TYPES:
        put("specster.run.tokens", getattr(m, f"{t}_tokens"), **{"specster.token.type": t})
    put("specster.run.truncations", len(m.truncations))
    put("specster.run.warnings", len(m.warnings))
    for role, r in m.roles.items():
        who = {
            "specster.role": role,
            "gen_ai.provider.name": r.provider,
            "gen_ai.request.model": r.model,
        }
        put("specster.role.cost", r.cost_usd, "USD", **who)
        put("specster.role.turns", r.turns, **who)
        for t in _TOKEN_TYPES:
            put(
                "specster.role.tokens",
                getattr(r, f"{t}_tokens"),
                **who,
                **{"specster.token.type": t},
            )
    if phase == "spec":
        put("specster.spec.files_read", len(m.files_read))
        for kind, v in (
            ("included", m.comments_included),
            ("untrusted", m.comments_untrusted),
            ("after_label", m.comments_after_label),
            ("edited_after_label", m.comments_edited_after_label),
        ):
            put("specster.spec.comments", v, **{"specster.kind": kind})
        put("specster.spec.hidden_removed", m.hidden_removed)
        for kind, n in (
            ("available", len(m.skills_available)),
            ("read", len(m.skills_read)),
            ("inlined", len(m.skills_inlined)),
        ):
            put("specster.spec.skills", n, **{"specster.kind": kind})
    if phase in ("build", "fix"):
        for state, v in (
            ("total", m.tasks_total),
            ("done", m.tasks_done),
            ("escalated", m.tasks_escalated),
        ):
            put("specster.build.tasks", v, **{"specster.state": state})
        put("specster.build.test_runs", m.test_runs)
        put("specster.build.parallel", m.parallel_used)
        put("specster.build.review_rounds", m.review_rounds)
    if phase in ("build", "evidence"):
        for state, v in (
            ("items", m.evidence_items),
            ("pages", m.evidence_pages),
            ("problems", m.evidence_problems),
        ):
            put("specster.build.evidence", v, **{"specster.state": state})
