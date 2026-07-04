from typing import Any, Protocol
from backend.errors import AppError


class Engine(Protocol):
    workflow_key: str
    def run(self, run: dict[str, Any]) -> dict[str, Any]: ...


class EngineNotIntegratedError(AppError):
    def __init__(self):
        super().__init__("ENGINE_NOT_INTEGRATED", "vehicle_catalog_v1 engine adapter is not integrated yet", 501)


class VehicleCatalogV1Adapter:
    workflow_key = "vehicle_catalog_v1"

    def run(self, run: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise EngineNotIntegratedError()
