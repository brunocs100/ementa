"""Every model call goes through the `ModelRegistry`: agents ask for a role, never a provider."""

from ementa_kit.llm.pricing import Price, cost_usd, price_for
from ementa_kit.llm.registry import ModelRegistry, ModelRole, ModelSpec, ModelUnavailableError

__all__ = [
    "ModelRegistry",
    "ModelRole",
    "ModelSpec",
    "ModelUnavailableError",
    "Price",
    "cost_usd",
    "price_for",
]
