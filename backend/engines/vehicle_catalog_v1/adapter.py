from __future__ import annotations

import os
from typing import Any

from backend.vehicle_catalog_scope import VehicleCatalogScope, VehicleCatalogScopeError

from .engine import VehicleCatalogEngine, VehicleCatalogRunConfig


def worker_provider_api_key() -> str:
    """Provider credentials come ONLY from worker-scoped environment
    variables (populated by Secret Manager mappings in production). Run
    input, browser requests, messages, metadata and database payloads are
    never a credential source."""
    return (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "").strip()


class VehicleCatalogV1Adapter:
    """The product wrapper around the preserved V1 engine.

    WHAT a run maps -- manufacturer, market, period -- is handed to this
    adapter by trusted wiring as a `VehicleCatalogScope`, and from nowhere
    else. It used to be read from top-level `run.input` keys that no run
    creator ever wrote, with a silent fallback to the engine defaults, which
    made every website V1 run a Hyundai run. Run input is now never a scope
    source, and an adapter with no scope refuses rather than defaulting.

    Direct, non-website callers of `VehicleCatalogEngine` are unaffected:
    `VehicleCatalogRunConfig` keeps its defaults.
    """

    workflow_key = "vehicle_catalog_v1"

    def __init__(self, *, model_client_factory=None, sleep_fn=None, event_sink=None, checkpoint_sink=None, cancellation_checker=None, agent_step_callback=None, retry_callback=None, provider_limits=None, provider_backpressure_callback=None, provider_coordinator=None, provider_adapter=None, evidence_authority=None, scope: VehicleCatalogScope | None = None):
        self.engine = VehicleCatalogEngine(model_client_factory=model_client_factory, sleep_fn=sleep_fn, event_sink=event_sink, checkpoint_sink=checkpoint_sink, cancellation_checker=cancellation_checker, agent_step_callback=agent_step_callback, retry_callback=retry_callback, provider_limits=provider_limits, provider_backpressure_callback=provider_backpressure_callback,
                                             provider_adapter=provider_adapter,
            provider_coordinator=provider_coordinator, evidence_authority=evidence_authority)
        self.scope = scope

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(self.scope, VehicleCatalogScope):
            # Never a default: a scope nobody bound is not a scope to guess.
            raise VehicleCatalogScopeError("VEHICLE_CATALOG_SCOPE_MISSING")
        config = VehicleCatalogRunConfig(
            api_key=worker_provider_api_key(),
            manufacturer=self.scope.manufacturer,
            market=self.scope.market,
            period=self.scope.period,
        )
        return self.engine.run(config)
