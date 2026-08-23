"""Fail-closed Commander model selection without provider discovery calls."""

from dataclasses import dataclass
from typing import Collection, Mapping, Sequence


class CommanderModelError(ValueError):
    pass


@dataclass(frozen=True)
class CommanderModelResolver:
    allowed_models: Sequence[str]
    available_models: Collection[str]
    fallback_model: str | None = None

    def __post_init__(self) -> None:
        if not self.allowed_models or len(set(self.allowed_models)) != len(self.allowed_models):
            raise CommanderModelError("Commander allowlist must be non-empty and unique")
        if self.fallback_model is not None and self.fallback_model not in self.allowed_models:
            raise CommanderModelError("fallback model is not allowlisted")

    def resolve(self, requested: str) -> str:
        available = set(self.available_models)
        if requested == "auto_best_available":
            for model in self.allowed_models:  # priority order is configuration, never inventory order
                if model in available:
                    return model
            if self.fallback_model is not None and self.fallback_model in available:
                return self.fallback_model
            raise CommanderModelError("no allowlisted Commander model is available")
        if requested not in self.allowed_models:
            raise CommanderModelError("unknown or unapproved Commander model")
        if requested in available:
            return requested
        if self.fallback_model is not None and self.fallback_model in available:
            return self.fallback_model
        raise CommanderModelError("requested Commander model is unavailable")
