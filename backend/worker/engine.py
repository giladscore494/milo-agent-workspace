from typing import Any, Protocol

from backend.engines.vehicle_catalog_v1 import VehicleCatalogV1Adapter


class Engine(Protocol):
    workflow_key: str
    def run(self, run: dict[str, Any]) -> dict[str, Any]: ...


__all__ = ["Engine", "VehicleCatalogV1Adapter"]
