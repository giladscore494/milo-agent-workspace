from __future__ import annotations

import os
from typing import Any

from . import core
from .engine import VehicleCatalogEngine, VehicleCatalogRunConfig


def worker_provider_api_key() -> str:
    """Provider credentials come ONLY from worker-scoped environment
    variables (populated by Secret Manager mappings in production). Run
    input, browser requests, messages, metadata and database payloads are
    never a credential source."""
    return (os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY") or "").strip()


class VehicleCatalogV1Adapter:
    workflow_key = "vehicle_catalog_v1"

    def __init__(self, *, model_client_factory=None, sleep_fn=None, event_sink=None, checkpoint_sink=None, cancellation_checker=None, agent_step_callback=None, retry_callback=None):
        self.engine = VehicleCatalogEngine(model_client_factory=model_client_factory, sleep_fn=sleep_fn, event_sink=event_sink, checkpoint_sink=checkpoint_sink, cancellation_checker=cancellation_checker, agent_step_callback=agent_step_callback, retry_callback=retry_callback)

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_input = run.get("input", {}) or {}
        config = VehicleCatalogRunConfig(
            api_key=worker_provider_api_key(),
            manufacturer=run_input.get("manufacturer", run_input.get("make", core.DEFAULT_MANUFACTURER)),
            market=run_input.get("market", core.DEFAULT_MARKET),
            period=run_input.get("period", core.DEFAULT_PERIOD),
        )
        return self.engine.run(config)
