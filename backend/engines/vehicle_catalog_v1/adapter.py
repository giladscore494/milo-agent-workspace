from __future__ import annotations

from typing import Any

from . import core
from .engine import VehicleCatalogEngine, VehicleCatalogRunConfig


class VehicleCatalogV1Adapter:
    workflow_key = "vehicle_catalog_v1"

    def __init__(self, *, model_client_factory=None, sleep_fn=None, event_sink=None, checkpoint_sink=None, cancellation_checker=None):
        self.engine = VehicleCatalogEngine(model_client_factory=model_client_factory, sleep_fn=sleep_fn, event_sink=event_sink, checkpoint_sink=checkpoint_sink, cancellation_checker=cancellation_checker)

    def run(self, run: dict[str, Any]) -> dict[str, Any]:
        run_input = run.get("input", {}) or {}
        config = VehicleCatalogRunConfig(
            api_key=run_input.get("api_key", ""),
            manufacturer=run_input.get("manufacturer", run_input.get("make", core.DEFAULT_MANUFACTURER)),
            market=run_input.get("market", core.DEFAULT_MARKET),
            period=run_input.get("period", core.DEFAULT_PERIOD),
        )
        return self.engine.run(config)
