import argparse
import os
from uuid import UUID
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.worker.engine import Engine, VehicleCatalogV1Adapter


def resolve_run_id(cli_run_id: str | None) -> UUID:
    value = cli_run_id or os.getenv("RUN_ID")
    if not value:
        raise AppError("MISSING_RUN_ID", "RUN_ID must be provided by environment or --run-id", 2)
    return UUID(value)


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None) -> int:
    run = repo.get_run(run_id)
    selected_engine = engine or VehicleCatalogV1Adapter()
    result = selected_engine.run(run)
    repo.append_run_event(run_id, "ENGINE_COMPLETED", {"status": result.get("status")})
    if result.get("status") in {"complete", "partial_success", "success"} or (result.get("status") != "failed" and result.get("result")):
        repo.mark_run_complete(run_id, result)
        return 0
    error = result.get("error", {}) if isinstance(result, dict) else {}
    code = error.get("code", "ENGINE_FAILED")
    message = error.get("message", "vehicle_catalog_v1 engine failed")
    repo.mark_run_failed(run_id, code, message)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    try:
        run_id = resolve_run_id(args.run_id)
        return execute_run(run_id, SupabaseRepository(get_settings()))
    except AppError as exc:
        print(f"{exc.code}: {exc.message}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
