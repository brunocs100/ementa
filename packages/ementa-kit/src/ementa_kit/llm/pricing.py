"""Model prices (versioned in `prices.toml`) and the cost of one call."""

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PRICES_FILE = Path(__file__).with_name("prices.toml")


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    input: float
    cached_input: float
    output: float


@lru_cache
def load_prices(path: Path = PRICES_FILE) -> dict[str, Price]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {model: Price(**values) for model, values in data["models"].items()}


def price_for(model: str) -> Price | None:
    """Exact id, or the longest known id that prefixes a dated snapshot (gpt-6-luna-2026-10-01)."""
    prices = load_prices()
    if model in prices:
        return prices[model]
    known = [m for m in prices if model.startswith(f"{m}-")]
    return prices[max(known, key=len)] if known else None


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> float | None:
    """Cost of one call; `input_tokens` includes the cached ones, as the OpenAI usage reports."""
    price = price_for(model)
    if price is None:
        return None
    cached = min(cached_input_tokens, input_tokens)
    return (
        (input_tokens - cached) * price.input
        + cached * price.cached_input
        + output_tokens * price.output
    ) / 1_000_000
