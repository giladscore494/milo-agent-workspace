import argparse
import os
from typing import Any
from datetime import UTC, datetime
from uuid import UUID, uuid4
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.runtime import CancellationRequested, RunEventRecord, SupabaseEventSink
from backend.supervisor import SupervisorInput, apply_event_to_blackboard, build_evaluation_report, initial_blackboard, make_shadow_decision, route_event_message
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
    shadow_blackboard = initial_blackboard(str((run.get("input") or {}).get("content") or "MILO vehicle catalog run"))

    def shadow_observe(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal shadow_blackboard
        try:
            shadow_blackboard = apply_event_to_blackboard(shadow_blackboard, event_type, payload)
            if hasattr(repo, "upsert_run_blackboard"):
                repo.upsert_run_blackboard(run_id, shadow_blackboard.model_dump(mode="json"))
            message = route_event_message(run_id, event_type, payload)
            if message and hasattr(repo, "create_agent_message"):
                repo.create_agent_message(message.model_dump(mode="json"))
            if event_type in {"checkpoint_saved", "chunk_failed", "run_failed", "run_completed", "run_partial_success"} and hasattr(repo, "create_supervisor_decision"):
                previous = repo.list_supervisor_decisions(run_id) if hasattr(repo, "list_supervisor_decisions") else []
                decision = make_shadow_decision(SupervisorInput(goal=shadow_blackboard.goal, compiled_workflow=shadow_blackboard.approved_plan, blackboard=shadow_blackboard, unread_messages=[message] if message else [], open_conflicts=shadow_blackboard.claims_conflict_summaries, budget=shadow_blackboard.remaining_budget), previous_decisions=previous)
                report = build_evaluation_report(decision, [event_type])
                repo.create_supervisor_decision(run_id, {"input": {"goal": shadow_blackboard.goal, "compiled_workflow": shadow_blackboard.approved_plan}, "assessment": decision.assessment, "proposed_commands": [c.model_dump(mode="json") for c in decision.proposed_commands], "next_wake_condition": decision.next_wake_condition.model_dump(mode="json"), "rationale_summary": decision.rationale_summary, "evaluation_report": report.model_dump(mode="json")})
        except Exception as exc:
            sink.emit(RunEventRecord(run_id=run_id, type="agent_failed", message="Supervisor shadow observation failed without altering execution", payload={"code": "SUPERVISOR_SHADOW_FAILED", "message": str(exc)}))

    sink.emit(RunEventRecord(run_id=run_id, type="run_started", message="Run started", payload={"worker_id": worker_id, "attempt": run.get("attempt", 1)}))
    shadow_observe("run_started", {"worker_id": worker_id, "attempt": run.get("attempt", 1)})
    latest_checkpoint = repo.latest_checkpoint(run_id, getattr(engine, "workflow_key", "vehicle_catalog_v1")) if hasattr(repo, "latest_checkpoint") else None
    if latest_checkpoint:
        sink.emit(RunEventRecord(run_id=run_id, type="run_resumed", message="Run resumed from latest compatible checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")}))
        shadow_observe("run_resumed", {"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")})
        artifacts = latest_checkpoint.get("artifacts") or {}
        if latest_checkpoint.get("phase") == "summary" and artifacts.get("final_builder"):
            final = artifacts["final_builder"].get("parsed", {})
            result = {"status": final.get("status", "success"), "result": final, "summary": (artifacts.get("hebrew_summary") or {}).get("parsed", {}).get("summary"), "results": artifacts, **(latest_checkpoint.get("token_usage") or {})}
            sink.emit(RunEventRecord(run_id=run_id, type="run_completed", message="Run completed from checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", ""))}))
            shadow_observe("run_completed", {"checkpoint_id": str(latest_checkpoint.get("id", ""))})
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
            shadow_observe("checkpoint_saved", checkpoint)
    def is_cancelled():
        return repo.get_run(run_id).get("status") == "cancellation_requested"
    selected_engine = engine or VehicleCatalogV1Adapter(
        event_sink=lambda t, p: (sink.emit(RunEventRecord(run_id=run_id, type=t, message=p.get("message", t), payload=p, phase=p.get("phase"), agent=p.get("agent"), progress=p.get("progress"))), shadow_observe(t, p)),
        checkpoint_sink=save_checkpoint,
        cancellation_checker=is_cancelled,
    )
    try:
        result = selected_engine.run(run)
    except CancellationRequested:
        sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled", payload={}))
        shadow_observe("run_cancelled", {})
        if hasattr(repo, "transition_run"):
            repo.transition_run(run_id, "cancelled", finished_at=datetime.now(UTC).isoformat())
        return 0
    if result.get("status") in {"complete", "partial_success", "success"} or (result.get("status") != "failed" and result.get("result")):
        status = "partial_success" if result.get("status") == "partial_success" else "completed"
        sink.emit(RunEventRecord(run_id=run_id, type="run_partial_success" if status == "partial_success" else "run_completed", message=f"Run {status}", payload={"status": result.get("status")}))
        shadow_observe("run_partial_success" if status == "partial_success" else "run_completed", {"status": result.get("status")})
        if hasattr(repo, "transition_run") and status == "partial_success":
            repo.transition_run(run_id, "partial_success", output=result, error=None, finished_at=datetime.now(UTC).isoformat())
        else:
            repo.mark_run_complete(run_id, result)
        return 0
    error = result.get("error", {}) if isinstance(result, dict) else {}
    code = error.get("code", "ENGINE_FAILED")
    message = error.get("message", "vehicle_catalog_v1 engine failed")
    sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=message, payload={"code": code}))
    shadow_observe("run_failed", {"code": code})
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
