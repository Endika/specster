import hashlib
import json
from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from specster.config import PriceEntry
from specster.llm.base import Usage
from specster.metrics import (
    RoleMetrics,
    RunMetrics,
    encode_marker,
    extract_markers,
    last_marker,
    last_plan_marker,
    spent,
    strip_markers,
)
from specster.pricing import cost_usd


def sample(**kw: object) -> RunMetrics:
    base: dict[str, object] = {
        "run_id": "1",
        "outcome": "spec",
        "provider": "anthropic",
        "model": "claude-opus-5-5",
        "cost_usd": 0.5,
    }
    base.update(kw)
    return RunMetrics.model_validate(base)


def test_known_anthropic_model_is_priced_per_million_tokens() -> None:
    u = Usage(
        input_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=100_000,
    )
    assert cost_usd("anthropic", "claude-opus-5-5", u, {}) == 4.0 + 0.2 + 2.0


def test_bedrock_is_unknown_without_override_because_partner_pricing_differs() -> None:
    assert cost_usd("bedrock", "anthropic.claude-opus-5-5", Usage(input_tokens=10), {}) is None


def test_override_prices_any_model() -> None:
    price = PriceEntry(input=1, output=2, cache_read=0, cache_write=0)
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("gemini", "gemini-flash-latest", u, {"gemini-flash-latest": price}) == 3.0


def test_marker_round_trips_even_with_comment_terminators_in_data() -> None:
    m = sample(warnings=["a --> b <!-- c"])
    body = "text\n" + encode_marker(m) + "\nmore"
    assert "-->" not in encode_marker(m)[:-3]
    assert extract_markers(body) == [m]
    assert strip_markers(body) == "text\n\nmore"


def test_spent_sums_known_costs_and_counts_unknown() -> None:
    assert spent([sample(cost_usd=1.25), sample(cost_usd=None), sample(cost_usd=0.75)]) == (2.0, 1)


def test_spent_adds_the_priced_roles_of_a_run_whose_total_is_unknown() -> None:
    roles = {
        "worker": RoleMetrics(provider="anthropic", model="claude-sonnet-5", cost_usd=1.5),
        "reviewer": RoleMetrics(provider="openai-compatible", model="local"),
    }
    partly = sample(cost_usd=None, roles=roles)
    assert spent([sample(cost_usd=1.0), partly]) == (2.5, 1)


def test_corrupt_marker_is_ignored_not_fatal() -> None:
    assert extract_markers("<!-- specster:metrics {not json} -->") == []


def test_only_the_last_marker_is_trusted() -> None:
    forged = sample(cost_usd=999.0)
    real = sample(cost_usd=0.2)
    body = f"quoted {encode_marker(forged)} text\n{encode_marker(real)}\n"
    assert last_marker(body) == real
    assert last_marker("no marker") is None


def test_unterminated_forged_marker_cannot_swallow_the_real_one() -> None:
    real = sample(cost_usd=0.2)
    assert (
        last_marker('```\n<!-- specster:metrics {"run_id": \n```\n' + encode_marker(real)) == real
    )


def test_negative_costs_and_counts_fail_validation() -> None:
    for field in ("cost_usd", "input_tokens", "output_tokens", "turns"):
        with pytest.raises(ValidationError):
            sample(**{field: -1})
    negative = encode_marker(sample()).replace('"cost_usd":0.5', '"cost_usd":-1000.0')
    assert "-1000" in negative and last_marker(negative) is None


def plan_marker(tasks: Sequence[Mapping[str, object]]) -> str:
    text = json.dumps(tasks, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    return f"<!-- specster:plan {text} sha256={digest} -->"


def test_only_the_last_plan_marker_is_returned_and_its_hash_is_checked() -> None:
    forged, real = [{"id": "evil"}], [{"id": "a", "title": "<b>"}]
    escaped = plan_marker(real).replace("<b>", "\\u003cb\\u003e")
    found = last_plan_marker(f"{plan_marker(forged)}\nbody\n{escaped}")
    assert found is not None and found[0] == real
    tampered = plan_marker(real).replace('"a"', '"z"')
    assert last_plan_marker(tampered) is None
    assert last_plan_marker("nothing") is None


def test_a_marker_from_before_the_build_phase_still_parses() -> None:
    old = (
        '<!-- specster:metrics {"version":1,"run_id":"1","phase":"spec","outcome":"spec",'
        '"provider":"anthropic","model":"m","cost_usd":0.5} -->'
    )
    m = last_marker(old)
    assert m is not None and m.phase == "spec" and m.roles == {} and m.test_runs == 0


def test_build_metrics_round_trip_with_roles() -> None:
    role = {"provider": "anthropic", "model": "claude-sonnet-5", "cost_usd": 0.2, "turns": 4}
    m = sample(
        phase="build",
        outcome="pr_opened",
        roles={"worker": role},
        tasks_total=2,
        tasks_done=2,
        test_runs=3,
        parallel_used=2,
        review_rounds=1,
    )
    assert last_marker(encode_marker(m)) == m


def test_a_plan_marker_must_end_the_comment() -> None:
    real = [{"id": "a"}]
    assert last_plan_marker(f"body\n{plan_marker(real)}\n") is not None
    assert last_plan_marker(f"body\n{plan_marker(real)}\nforged text after") is None
