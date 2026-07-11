import argparse
import os
import threading
import time
from typing import Any
from datetime import UTC, datetime
from uuid import UUID, uuid4
from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, build_guarded_client_factory, paid_execution_enabled
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


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None, budget_tracker: "BudgetTracker | None" = None) -> int:
    worker_id = os.getenv("WORKER_ID", f"worker-{uuid4()}")
    lease_seconds = int(os.getenv("MILO_WORKER_LEASE_SECONDS", "300"))
    heartbeat_interval = max(1.0, min(float(os.getenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "30")), lease_seconds / 3))
    if hasattr(repo, "claim_run"):
        run = repo.claim_run(run_id, worker_id, lease_seconds=lease_seconds)
    else:
        run = repo.get_run(run_id)
    sink = SupabaseEventSink(repo)
    lease_lost = threading.Event()
    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    def heartbeat_once() -> bool:
        if not hasattr(repo, "heartbeat"):
            return True
        try:
            repo.heartbeat(run_id, worker_id, lease_seconds=lease_seconds)
            return True
        except Exception:
            lease_lost.set()
            return False

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(heartbeat_interval):
            if not heartbeat_once():
                return

    def start_heartbeat() -> None:
        nonlocal heartbeat_thread
        if heartbeat_thread is None:
            heartbeat_thread = threading.Thread(target=heartbeat_loop, name=f"milo-heartbeat-{run_id}", daemon=True)
            heartbeat_thread.start()

    def cleanup_heartbeat() -> None:
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=max(2.0, heartbeat_interval + 1.0))

    start_heartbeat()
    try:
        if run.get("status") == "cancellation_requested":
            sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled before worker execution", payload={"code": "RUN_CANCELLED_BEFORE_START"}))
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, "cancelled", expected_worker_id=worker_id, finished_at=datetime.now(UTC).isoformat())
            return 0
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
                repo.mark_run_complete(run_id, result, worker_id=worker_id)
                return 0
        if hasattr(repo, "transition_run"):
            repo.transition_run(run_id, "running", expected_worker_id=worker_id, started_at=run.get("started_at") or datetime.now(UTC).isoformat())
        if hasattr(repo, "heartbeat"):
            repo.heartbeat(run_id, worker_id, lease_seconds=lease_seconds)
        def save_checkpoint(_phase, checkpoint):
            if hasattr(repo, "save_checkpoint"):
                checkpoint = {**checkpoint, "run_id": str(run_id), "attempt": run.get("attempt", 1)}
                repo.save_checkpoint(checkpoint)
                shadow_observe("checkpoint_saved", checkpoint)
        def is_cancelled():
            return repo.get_run(run_id).get("status") == "cancellation_requested"

        # Hard budget/cost gate. Fail closed: paid execution requires both the
        # global kill switch and complete mandatory budget configuration; the
        # tracker also blocks every call while MILO_ENABLE_PAID_EXECUTION is off.
        budget_config = BudgetConfig.from_env()
        if paid_execution_enabled() and budget_config.missing_mandatory():
            missing = ", ".join(budget_config.missing_mandatory())
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Budget configuration incomplete; refusing paid execution", payload={"code": "BUDGET_CONFIG_INVALID", "missing": missing}))
            repo.mark_run_failed(run_id, "BUDGET_CONFIG_INVALID", f"mandatory budget settings missing: {missing}")
            return 1
        # Provider credentials are worker-only (env/Secret Manager). Paid
        # execution fails closed when the key is absent; the key value itself is
        # never logged, persisted or echoed into events.
        from backend.engines.vehicle_catalog_v1.adapter import worker_provider_api_key

        if paid_execution_enabled() and not worker_provider_api_key():
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Provider API key not configured for this worker; refusing paid execution", payload={"code": "PROVIDER_KEY_MISSING"}))
            repo.mark_run_failed(run_id, "PROVIDER_KEY_MISSING", "worker provider API key (KIMI_API_KEY/MOONSHOT_API_KEY) is not configured")
            return 1

        def emit_budget_event(event_type, payload):
            sink.emit(RunEventRecord(run_id=run_id, type=event_type, message=payload.get("message", event_type), payload=payload.get("payload", payload)))
            shadow_observe(event_type, payload)

        def record_usage(usage):
            if hasattr(repo, "update_run_usage"):
                repo.update_run_usage(run_id, usage)

        def holds_lease():
            if lease_lost.is_set():
                return False
            current = repo.get_run(run_id)
            expires = current.get("lease_expires_at")
            if expires:
                try:
                    if datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= datetime.now(UTC):
                        return False
                except ValueError:
                    return False
            return (not current.get("worker_id") or current.get("worker_id") == worker_id) and current.get("status") not in {"completed", "failed", "cancelled", "timed_out", "budget_exhausted"}

        ledger_project_id = None
        try:
            if run.get("conversation_id"):
                ledger_project_id = repo.get_conversation(run["conversation_id"]).get("project_id")
        except Exception:
            ledger_project_id = None

        def record_ledger(entry):
            if hasattr(repo, "append_usage_ledger"):
                repo.append_usage_ledger({
                    "run_id": str(run_id),
                    "project_id": str(ledger_project_id) if ledger_project_id else None,
                    "user_id": run.get("requested_by"),
                    "provider": "moonshot",
                    "model": "kimi",
                    **entry,
                })

        tracker = budget_tracker or BudgetTracker(
            budget_config,
            cancellation_checker=is_cancelled,
            event_emitter=emit_budget_event,
            usage_recorder=record_usage,
            ledger_recorder=record_ledger,
            lease_checker=holds_lease,
            daily_user_cost_provider=(lambda: repo.sum_daily_ledger_cost(user_id=run.get("requested_by"))) if hasattr(repo, "sum_daily_ledger_cost") and run.get("requested_by") else None,
            daily_project_cost_provider=(lambda: repo.sum_daily_ledger_cost(project_id=str(ledger_project_id))) if hasattr(repo, "sum_daily_ledger_cost") and ledger_project_id else None,
            daily_user_reserver=(lambda amount, call_seq: repo.reserve_model_call_budget(run_id, call_seq, run.get("requested_by"), str(ledger_project_id) if ledger_project_id else None, amount, budget_config.daily_user_budget, budget_config.daily_project_budget)) if hasattr(repo, "reserve_model_call_budget") and (run.get("requested_by") or ledger_project_id) and (budget_config.daily_user_budget or budget_config.daily_project_budget) else None,
            daily_project_reserver=None,
        )

        def forward_event(t, p):
            sink.emit(RunEventRecord(run_id=run_id, type=t, message=p.get("message", t), payload=p, phase=p.get("phase"), agent=p.get("agent"), progress=p.get("progress")))
            shadow_observe(t, p)

        def record_agent_step(agent: str, phase: str) -> None:
            """Count immediately before each real model-backed agent task.

            Policy: one step for each discovery agent, normalizer call, each
            technical agent/chunk, verifier call, final-builder call, Hebrew
            summary call, and fallback prompt when it performs an additional
            provider attempt.
            """
            tracker.record_agent_step()
            forward_event("agent_started", {"agent": agent, "phase": phase, "message": f"Agent task started: {agent}/{phase}"})

        def record_retry(agent: str, phase: str, reason: str) -> None:
            tracker.record_retry()
            forward_event("retry_limit_checked", {"agent": agent, "phase": phase, "reason": reason, "message": f"Retry allowance consumed for {agent}/{phase}"})

        selected_engine = engine or VehicleCatalogV1Adapter(
            model_client_factory=build_guarded_client_factory(tracker),
            event_sink=forward_event,
            checkpoint_sink=save_checkpoint,
            cancellation_checker=is_cancelled,
            agent_step_callback=record_agent_step,
            retry_callback=record_retry,
        )
        try:
            result = selected_engine.run(run)
        except CancellationRequested:
            sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled", payload={}))
            shadow_observe("run_cancelled", {})
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, "cancelled", expected_worker_id=worker_id, finished_at=datetime.now(UTC).isoformat())
            return 0
        except BudgetExceeded as exc:
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, exc.terminal_status, expected_worker_id=worker_id, error={"code": exc.code, "message": exc.message}, finished_at=datetime.now(UTC).isoformat(), usage=tracker.snapshot())
            return 1
        if tracker.stop is not None:
            # The engine absorbed per-agent failures, but a hard limit tripped:
            # never report success and record the terminal budget status.
            stop = tracker.stop
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, stop.terminal_status, expected_worker_id=worker_id, error={"code": stop.code, "message": stop.message}, finished_at=datetime.now(UTC).isoformat(), usage=tracker.snapshot())
            return 1
        if result.get("status") in {"complete", "partial_success", "success"} or (result.get("status") != "failed" and result.get("result")):
            status = "partial_success" if result.get("status") == "partial_success" else "completed"
            sink.emit(RunEventRecord(run_id=run_id, type="run_partial_success" if status == "partial_success" else "run_completed", message=f"Run {status}", payload={"status": result.get("status")}))
            shadow_observe("run_partial_success" if status == "partial_success" else "run_completed", {"status": result.get("status")})
            if hasattr(repo, "transition_run") and status == "partial_success":
                repo.transition_run(run_id, "partial_success", expected_worker_id=worker_id, output=result, error=None, finished_at=datetime.now(UTC).isoformat())
            else:
                repo.mark_run_complete(run_id, result, worker_id=worker_id)
            return 0
        error = result.get("error", {}) if isinstance(result, dict) else {}
        code = error.get("code", "ENGINE_FAILED")
        message = error.get("message", "vehicle_catalog_v1 engine failed")
        sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=message, payload={"code": code}))
        shadow_observe("run_failed", {"code": code})
        repo.mark_run_failed(run_id, code, message, worker_id=worker_id)
        return 1
    finally:
        cleanup_heartbeat()


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
