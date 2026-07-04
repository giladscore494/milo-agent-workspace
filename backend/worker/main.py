import argparse
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.runtime import CancellationRequested, RunEventRecord, SupabaseEventSink
from backend.worker.engine import Engine, VehicleCatalogV1Adapter


def resolve_run_id(cli_run_id: str | None) -> UUID:
    value = cli_run_id or os.getenv("RUN_ID")
    if not value:
        raise AppError("MISSING_RUN_ID", "RUN_ID must be provided by environment or --run-id", 2)
    return UUID(value)


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None) -> int:
    worker_id = os.getenv("WORKER_ID", f"worker-{uuid4()}")
    if hasattr(repo, "claim_run"):
        run = repo.claim_run(run_id, worker_id)
    else:
        run = repo.get_run(run_id)
    sink = SupabaseEventSink(repo)
    sink.emit(RunEventRecord(run_id=run_id, type="run_started", message="Run started", payload={"worker_id": worker_id, "attempt": run.get("attempt", 1)}))
    latest_checkpoint = repo.latest_checkpoint(run_id, getattr(engine, "workflow_key", "vehicle_catalog_v1")) if hasattr(repo, "latest_checkpoint") else None
    if latest_checkpoint:
        sink.emit(RunEventRecord(run_id=run_id, type="run_resumed", message="Run resumed from latest compatible checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")}))
        artifacts = latest_checkpoint.get("artifacts") or {}
        if latest_checkpoint.get("phase") == "summary" and artifacts.get("final_builder"):
            final = artifacts["final_builder"].get("parsed", {})
            result = {"status": final.get("status", "success"), "result": final, "summary": (artifacts.get("hebrew_summary") or {}).get("parsed", {}).get("summary"), "results": artifacts, **(latest_checkpoint.get("token_usage") or {})}
            sink.emit(RunEventRecord(run_id=run_id, type="run_completed", message="Run completed from checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", ""))}))
            repo.mark_run_complete(run_id, result)
            return 0
    if hasattr(repo, "transition_run"):
        repo.transition_run(run_id, "running", started_at=run.get("started_at") or datetime.now(UTC).isoformat())
    if hasattr(repo, "heartbeat"):
        repo.heartbeat(run_id, worker_id)
    def save_checkpoint(_phase, checkpoint):
        if hasattr(repo, "save_checkpoint"):
            checkpoint = {**checkpoint, "run_id": str(run_id), "attempt": run.get("attempt", 1)}
            repo.save_checkpoint(checkpoint)
    def is_cancelled():
        return repo.get_run(run_id).get("status") == "cancellation_requested"
    selected_engine = engine or VehicleCatalogV1Adapter(
        event_sink=lambda t, p: sink.emit(RunEventRecord(run_id=run_id, type=t, message=p.get("message", t), payload=p, phase=p.get("phase"), agent=p.get("agent"), progress=p.get("progress"))),
        checkpoint_sink=save_checkpoint,
        cancellation_checker=is_cancelled,
    )
    try:
        result = selected_engine.run(run)
    except CancellationRequested:
        sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled", payload={}))
        if hasattr(repo, "transition_run"):
            repo.transition_run(run_id, "cancelled", finished_at=datetime.now(UTC).isoformat())
        return 0
    if result.get("status") in {"complete", "partial_success", "success"} or (result.get("status") != "failed" and result.get("result")):
        status = "partial_success" if result.get("status") == "partial_success" else "completed"
        sink.emit(RunEventRecord(run_id=run_id, type="run_partial_success" if status == "partial_success" else "run_completed", message=f"Run {status}", payload={"status": result.get("status")}))
        if hasattr(repo, "transition_run") and status == "partial_success":
            repo.transition_run(run_id, "partial_success", output=result, error=None, finished_at=datetime.now(UTC).isoformat())
        else:
            repo.mark_run_complete(run_id, result)
        return 0
    error = result.get("error", {}) if isinstance(result, dict) else {}
    code = error.get("code", "ENGINE_FAILED")
    message = error.get("message", "vehicle_catalog_v1 engine failed")
    sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=message, payload={"code": code}))
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
