"""Compatibility surface over :mod:`backend.model_profiles`.

The price table that used to live here failed OPEN (an unknown model cost
0.0) and carried a stale kimi-k2.6 price. Both are fixed at the source: the
prices are now DERIVED from the fail-closed profile registry, and
:func:`calculate_model_cost` refuses a model the registry does not name
instead of pricing it at zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.model_profiles import PROFILES, UnknownModelProfile, get_profile


@dataclass(frozen=True)
class ModelPricing:
    provider: str
    model: str
    input_per_million: float
    output_per_million: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return float(get_profile(self.model).usage_cost(
            input_tokens=input_tokens, output_tokens=output_tokens))


#: Read-only view: cache-miss input and output rates of every registered model.
PRICING: dict[str, ModelPricing] = {
    name: ModelPricing(profile.provider, name,
                       input_per_million=float(profile.price_input_miss),
                       output_per_million=float(profile.price_output))
    for name, profile in PROFILES.items()
}


def calculate_model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Price a call with no cache breakdown. Raises for an unknown model."""
    return float(get_profile(model).usage_cost(
        input_tokens=input_tokens, output_tokens=output_tokens))


__all__ = ["ModelPricing", "PRICING", "UnknownModelProfile", "calculate_model_cost"]
