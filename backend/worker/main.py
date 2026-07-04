import argparse
import os
from uuid import UUID
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.worker.engine import Engine, EngineNotIntegratedError, VehicleCatalogV1Adapter


def resolve_run_id(cli_run_id: str | None) -> UUID:
    value = cli_run_id or os.getenv("RUN_ID")
    if not value:
        raise AppError("MISSING_RUN_ID", "RUN_ID must be provided by environment or --run-id", 2)
    return UUID(value)


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None) -> int:
    run = repo.get_run(run_id)
    selected_engine = engine or VehicleCatalogV1Adapter()
    try:
        selected_engine.run(run)
    except EngineNotIntegratedError as exc:
        repo.append_run_event(run_id, exc.code, {"message": exc.message})
        repo.mark_run_failed(run_id, exc.code, exc.message)
        return 1
    return 0


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
