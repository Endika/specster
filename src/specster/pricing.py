from collections.abc import Mapping

from specster.config import PriceEntry
from specster.llm.base import Usage

# First-party Anthropic API list prices, USD per million tokens. Partner platforms
# (Bedrock, Vertex) bill differently, so they are only priced through config overrides.
DEFAULT_PRICES: dict[str, PriceEntry] = {
    "claude-fable-5-1": PriceEntry(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5),
    "claude-opus-5-5": PriceEntry(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-opus-5": PriceEntry(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
    "claude-sonnet-5-5": PriceEntry(input=2.0, output=10.0, cache_read=0.10, cache_write=2.5),
    "claude-sonnet-5": PriceEntry(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    # Haiku 5.5 bills prompts over 100K tokens at five times the rate; usage is summed across
    # turns, so the cap prices every prompt at that rate rather than undercount.
    "claude-haiku-5-5": PriceEntry(input=0.50, output=2.50, cache_read=0.05, cache_write=0.625),
    "claude-haiku-4-5": PriceEntry(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
}


def cost_usd(
    provider: str, model: str, usage: Usage, overrides: Mapping[str, PriceEntry]
) -> float | None:
    price = overrides.get(model)
    if price is None and provider == "anthropic":
        price = DEFAULT_PRICES.get(model)
    if price is None:
        return None
    total = (
        usage.input_tokens * price.input
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
        + usage.output_tokens * price.output
    )
    return round(total / 1_000_000, 6)
