from specster.config import PriceEntry
from specster.llm.base import Usage
from specster.metrics import RunMetrics, encode_marker, extract_markers, spent, strip_markers
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


def test_corrupt_marker_is_ignored_not_fatal() -> None:
    assert extract_markers("<!-- specster:metrics {not json} -->") == []
