"""Typed model pricing used by guarded production model calls."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    provider: str
    model: str
    input_per_million: float
    output_per_million: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            (max(0, input_tokens) / 1_000_000 * self.input_per_million)
            + (max(0, output_tokens) / 1_000_000 * self.output_per_million),
            8,
        )


PRICING: dict[str, ModelPricing] = {
    # Moonshot/Kimi production model used by the preserved MILO pipeline.
    # Values are explicit configuration for enforcement tests; provider-sent
    # trusted cost fields take precedence when available.
    "kimi-k2.6": ModelPricing("moonshot", "kimi-k2.6", input_per_million=0.60, output_per_million=2.50),
    "kimi": ModelPricing("moonshot", "kimi", input_per_million=0.60, output_per_million=2.50),
}


def calculate_model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = PRICING.get((model or "").strip())
    if pricing is None:
        return 0.0
    return pricing.cost(input_tokens, output_tokens)
