"""Token prices (USD per 1M tokens) used for budget accounting.

Anthropic prices as published for current models; cache_write assumes the
standard 5-minute cache (1.25x input). Local/self-hosted models cost 0.
Override or add models via [llm.prices."<model>"] in config.toml.
"""

from __future__ import annotations

from app.core.config import ModelPrice, get_settings
from app.llm.base import TokenUsage

DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-fable-5-1": ModelPrice(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5),
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    "claude-haiku-4-5": ModelPrice(input=1.0, output=5.0, cache_read=0.10, cache_write=1.25),
}


def price_for(model: str) -> ModelPrice | None:
    custom = get_settings().llm.prices
    return custom.get(model) or DEFAULT_PRICES.get(model)


def cost_usd(model: str, usage: TokenUsage) -> float:
    price = price_for(model)
    if price is None:
        return 0.0
    cache_read = price.cache_read if price.cache_read is not None else price.input
    cache_write = price.cache_write if price.cache_write is not None else price.input
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
    )
    return round(total / 1_000_000, 6)
