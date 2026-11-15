"""Model prices (versioned in `prices.toml`) and the cost of one call."""

import tomllib
from collections.abc import Mapping
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


@dataclass(frozen=True)
class CallUsage:
    """Tokens of one model call, as the GenAI instrumentation records them on its span."""

    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    @property
    def cost_usd(self) -> float | None:
        return cost_usd(self.model, self.input_tokens, self.output_tokens, self.cached_input_tokens)


def _int(value: object) -> int:
    return int(value) if isinstance(value, int | float) else 0


def usage_from_attributes(attributes: Mapping[str, object]) -> CallUsage | None:
    """The usage on a model call span (`gen_ai.usage.*`); None for any other span."""
    if "gen_ai.usage.input_tokens" not in attributes:
        return None
    model = attributes.get("gen_ai.response.model") or attributes.get("gen_ai.request.model")
    return CallUsage(
        model=str(model or ""),
        input_tokens=_int(attributes.get("gen_ai.usage.input_tokens")),
        output_tokens=_int(attributes.get("gen_ai.usage.output_tokens")),
        cached_input_tokens=_int(attributes.get("gen_ai.usage.cache_read.input_tokens")),
    )
