import argparse
import os
import threading
import time
from typing import Any
from datetime import UTC, datetime
from uuid import UUID, uuid4
from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, ModelCallReservation, build_guarded_client_factory, paid_execution_enabled
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.runtime import CancellationRequested, RunEventRecord, SupabaseEventSink
from backend.supervisor import SupervisorInput, apply_event_to_blackboard, build_evaluation_report, initial_blackboard, make_shadow_decision, route_event_message
from backend.engines.vehicle_catalog_v1 import VehicleCatalogV1Adapter
from backend.worker.engine import Engine, EngineRegistry, EngineResolver


def resolve_run_id(cli_run_id: str | None) -> UUID:
    value = cli_run_id or os.getenv("RUN_ID")
    if not value:
        raise AppError("MISSING_RUN_ID", "RUN_ID must be provided by environment or --run-id", 2)
    return UUID(value)


def _persist_budget_terminal(repo: Repository, run_id: UUID,
                             stop: BudgetExceeded, tracker: BudgetTracker,
                             lease_ctx: dict[str, Any]) -> None:
    """Persist a budget stop or fail so the job remains retryable.

    Returning from this helper means the terminal transition completed under
    the active lease. A missing repository capability is an infrastructure
    failure, not a handled run outcome.
    """
    transition = getattr(repo, "transition_run", None)
    if not callable(transition):
        raise AppError(
            "RUN_FINALIZATION_UNAVAILABLE",
            "terminal run transition is unavailable",
            503,
        )
    transition(
        run_id,
        stop.terminal_status,
        expected_worker_id=lease_ctx["worker_id"],
        expected_attempt=lease_ctx["attempt"],
        expected_lease_token=lease_ctx["lease_token"],
        error={"code": stop.code, "message": stop.message},
        finished_at=datetime.now(UTC).isoformat(),
        usage=tracker.snapshot(),
    )


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None, budget_tracker: "BudgetTracker | None" = None, engine_registry: EngineRegistry | None = None) -> int:
    worker_id = os.getenv("WORKER_ID", f"worker-{uuid4()}")
    lease_seconds = int(os.getenv("MILO_WORKER_LEASE_SECONDS", "300"))
    heartbeat_interval = max(1.0, min(float(os.getenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "30")), lease_seconds / 3))
    if hasattr(repo, "claim_run"):
        run = repo.claim_run(run_id, worker_id, lease_seconds=lease_seconds)
    else:
        run = repo.get_run(run_id)
    # The active lease travels with every durable write this worker makes:
    # once the lease is reclaimed, each of these writes is rejected
    # atomically at the database boundary.
    lease_ctx = {"worker_id": worker_id, "attempt": run.get("attempt"), "lease_token": run.get("lease_token")}
    sink = SupabaseEventSink(repo, **lease_ctx)
    lease_lost = threading.Event()
    stop_heartbeat = threading.Event()
    heartbeat_thread: threading.Thread | None = None

    def heartbeat_once() -> bool:
        if not hasattr(repo, "heartbeat"):
            return True
        try:
            repo.heartbeat(run_id, worker_id, lease_seconds=lease_seconds, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
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
                repo.transition_run(run_id, "cancelled", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), finished_at=datetime.now(UTC).isoformat())
            return 0
        # Routing is resolved from server-owned relations only. Do this before
        # checkpoint access and before invoking any engine factory.
        engine_builder = None
        swarm_engine_builder = None

        def build_default_engine():
            if engine_builder is None:
                raise RuntimeError("engine factory invoked before worker dependencies were ready")
            return engine_builder()

        def build_swarm_engine():
            if swarm_engine_builder is None:
                raise RuntimeError("swarm engine factory invoked before worker dependencies were ready")
            return swarm_engine_builder()

        registry = engine_registry or EngineRegistry(
            {engine.workflow_key: lambda: engine} if engine is not None else
            {"vehicle_catalog_v1": build_default_engine, "swarm_v2": build_swarm_engine})
        try:
            resolved_engine = EngineResolver(repo, registry).resolve(run)
        except AppError as exc:
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=exc.message, payload={"code": exc.code}))
            repo.mark_run_failed(run_id, exc.code, exc.message, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 1
        workflow_key = resolved_engine.workflow_key

        shadow_blackboard = initial_blackboard(str((run.get("input") or {}).get("content") or "MILO vehicle catalog run"))

        def shadow_observe(event_type: str, payload: dict[str, Any]) -> None:
            nonlocal shadow_blackboard
            try:
                shadow_blackboard = apply_event_to_blackboard(shadow_blackboard, event_type, payload)
                if hasattr(repo, "upsert_run_blackboard"):
                    repo.upsert_run_blackboard(run_id, shadow_blackboard.model_dump(mode="json"), **lease_ctx)
                message = route_event_message(run_id, event_type, payload)
                if message and hasattr(repo, "create_agent_message"):
                    repo.create_agent_message(message.model_dump(mode="json"), **lease_ctx)
                if event_type in {"checkpoint_saved", "chunk_failed", "run_failed", "run_completed", "run_partial_success"} and hasattr(repo, "create_supervisor_decision"):
                    previous = repo.list_supervisor_decisions(run_id) if hasattr(repo, "list_supervisor_decisions") else []
                    decision = make_shadow_decision(SupervisorInput(goal=shadow_blackboard.goal, compiled_workflow=shadow_blackboard.approved_plan, blackboard=shadow_blackboard, unread_messages=[message] if message else [], open_conflicts=shadow_blackboard.claims_conflict_summaries, budget=shadow_blackboard.remaining_budget), previous_decisions=previous)
                    report = build_evaluation_report(decision, [event_type])
                    repo.create_supervisor_decision(run_id, {"input": {"goal": shadow_blackboard.goal, "compiled_workflow": shadow_blackboard.approved_plan}, "assessment": decision.assessment, "proposed_commands": [c.model_dump(mode="json") for c in decision.proposed_commands], "next_wake_condition": decision.next_wake_condition.model_dump(mode="json"), "rationale_summary": decision.rationale_summary, "evaluation_report": report.model_dump(mode="json")}, **lease_ctx)
            except Exception as exc:
                sink.emit(RunEventRecord(run_id=run_id, type="supervisor_shadow_failed", message="Supervisor shadow observation failed without altering execution", payload={"code": "SUPERVISOR_SHADOW_FAILED", "message": str(exc)}))

        sink.emit(RunEventRecord(run_id=run_id, type="run_started", message="Run started", payload={"worker_id": worker_id, "attempt": run.get("attempt", 1)}))
        shadow_observe("run_started", {"worker_id": worker_id, "attempt": run.get("attempt", 1)})
        latest_checkpoint = repo.latest_checkpoint(run_id, workflow_key) if hasattr(repo, "latest_checkpoint") else None
        if latest_checkpoint:
            sink.emit(RunEventRecord(run_id=run_id, type="run_resumed", message="Run resumed from latest compatible checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")}))
            shadow_observe("run_resumed", {"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")})
            artifacts = latest_checkpoint.get("artifacts") or {}
            if latest_checkpoint.get("phase") == "summary" and artifacts.get("final_builder"):
                final = artifacts["final_builder"].get("parsed", {})
                result = {"status": final.get("status", "success"), "result": final, "summary": (artifacts.get("hebrew_summary") or {}).get("parsed", {}).get("summary"), "results": artifacts, **(latest_checkpoint.get("token_usage") or {})}
                sink.emit(RunEventRecord(run_id=run_id, type="run_completed", message="Run completed from checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", ""))}))
                shadow_observe("run_completed", {"checkpoint_id": str(latest_checkpoint.get("id", ""))})
                repo.mark_run_complete(run_id, result, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
                return 0
        if hasattr(repo, "transition_run"):
            repo.transition_run(run_id, "running", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), started_at=run.get("started_at") or datetime.now(UTC).isoformat())
        if hasattr(repo, "heartbeat"):
            repo.heartbeat(run_id, worker_id, lease_seconds=lease_seconds, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
        def save_checkpoint(_phase, checkpoint):
            if hasattr(repo, "save_checkpoint"):
                checkpoint = {**checkpoint, "run_id": str(run_id), "attempt": run.get("attempt", 1), "workflow_key": workflow_key}
                repo.save_checkpoint(checkpoint, **lease_ctx)
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
            repo.mark_run_failed(run_id, "BUDGET_CONFIG_INVALID", f"mandatory budget settings missing: {missing}", worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0 if workflow_key == "swarm_v2" else 1
        # Provider credentials are worker-only (env/Secret Manager). Paid
        # execution fails closed when the key is absent; the key value itself is
        # never logged, persisted or echoed into events.
        from backend.engines.vehicle_catalog_v1.adapter import worker_provider_api_key

        if paid_execution_enabled() and not worker_provider_api_key():
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Provider API key not configured for this worker; refusing paid execution", payload={"code": "PROVIDER_KEY_MISSING"}))
            repo.mark_run_failed(run_id, "PROVIDER_KEY_MISSING", "worker provider API key (KIMI_API_KEY/MOONSHOT_API_KEY) is not configured", worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0 if workflow_key == "swarm_v2" else 1

        # Provider-side scheduling limits (concurrency/RPM/TPM/backpressure
        # bounds) are numeric deployment configuration validated fail-closed:
        # an invalid value refuses the run instead of degrading into
        # unlimited capacity.
        from backend.provider_scheduler import ProviderLimitsConfig

        engine_mode = (os.getenv("MILO_WORKER_ENGINE") or "").strip().lower()
        provider_limits = None
        if engine is None and engine_mode != "mock":
            try:
                provider_limits = ProviderLimitsConfig.from_env()
            except ValueError as exc:
                sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Provider limit configuration invalid; refusing execution", payload={"code": "PROVIDER_LIMITS_CONFIG_INVALID", "message": str(exc)}))
                repo.mark_run_failed(run_id, "PROVIDER_LIMITS_CONFIG_INVALID", str(exc), worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
                return 0 if workflow_key == "swarm_v2" else 1

        def emit_budget_event(event_type, payload):
            sink.emit(RunEventRecord(run_id=run_id, type=event_type, message=payload.get("message", event_type), payload=payload.get("payload", payload)))
            shadow_observe(event_type, payload)

        def record_usage(usage):
            if hasattr(repo, "update_run_usage"):
                repo.update_run_usage(run_id, usage, **lease_ctx)

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

        # MILO_WORKER_ENGINE=mock (forbidden in production by
        # backend/production_config.py) runs the zero-cost staging engine: no
        # provider client exists, so the tracker's kill switch is satisfied
        # locally and simulated calls exercise the real reservation lifecycle
        # with mock costs only.
        tracker = budget_tracker or BudgetTracker(
            budget_config,
            kill_switch=(lambda: True) if engine_mode == "mock" else paid_execution_enabled,
            cancellation_checker=is_cancelled,
            event_emitter=emit_budget_event,
            usage_recorder=record_usage,
            ledger_recorder=record_ledger,
            lease_checker=holds_lease,
            daily_user_cost_provider=(lambda: repo.sum_daily_ledger_cost(user_id=run.get("requested_by"))) if hasattr(repo, "sum_daily_ledger_cost") and run.get("requested_by") else None,
            daily_project_cost_provider=(lambda: repo.sum_daily_ledger_cost(project_id=str(ledger_project_id))) if hasattr(repo, "sum_daily_ledger_cost") and ledger_project_id else None,
            daily_user_reserver=(lambda amount, call_seq: repo.reserve_model_call_budget(run_id, call_seq, run.get("requested_by"), str(ledger_project_id) if ledger_project_id else None, amount, budget_config.daily_user_budget, budget_config.daily_project_budget, **lease_ctx)) if hasattr(repo, "reserve_model_call_budget") and (run.get("requested_by") or ledger_project_id) and (budget_config.daily_user_budget or budget_config.daily_project_budget) else None,
            daily_project_reserver=None,
            daily_settler=(lambda reservation, actual_cost, status, reason: repo.settle_model_call_budget(reservation.id if isinstance(reservation, ModelCallReservation) else str(reservation), actual_cost, status, reason, run_id=run_id, **lease_ctx)) if hasattr(repo, "settle_model_call_budget") else None,
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

        def record_provider_backpressure(agent: str, phase: str, reason: str, wait_seconds: float) -> None:
            """Provider backpressure wait telemetry: distinct from semantic
            retries and never consumes the retry allowance. The tracker's
            provider_backpressure_events counter is incremented once per 429
            by the guarded client, so this callback only records the event."""
            forward_event("provider_backpressure_wait", {"agent": agent, "phase": phase, "reason": reason, "wait_seconds": wait_seconds, "message": f"Provider backpressure for {agent}/{phase}: waiting {wait_seconds}s ({reason})"})

        if engine is None and engine_registry is None and engine_mode == "mock":
            from backend.worker.mock_engine import MockLifecycleEngine

            engine_builder = lambda: MockLifecycleEngine(
                event_sink=forward_event, checkpoint_sink=save_checkpoint,
                cancellation_checker=is_cancelled, agent_step_callback=record_agent_step,
                retry_callback=record_retry, budget_tracker=tracker,
            )
        elif engine is None and engine_registry is None:
            engine_builder = lambda: VehicleCatalogV1Adapter(
                model_client_factory=build_guarded_client_factory(tracker), event_sink=forward_event,
                checkpoint_sink=save_checkpoint, cancellation_checker=is_cancelled,
                agent_step_callback=record_agent_step, retry_callback=record_retry,
                provider_limits=provider_limits, provider_backpressure_callback=record_provider_backpressure,
            )
            def make_swarm_engine():
                from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander,
                    CommanderModelResolver, EvidenceReference, GenericWorker, ModelGateway,
                    PlanLimits, PlanValidator, RemainingBudget, SwarmV2Adapter, Verifier)
                from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
                from backend.provider_scheduler import ProviderScheduler
                from backend.tools import ToolContext, ToolRegistry

                allowed = tuple(filter(None, (item.strip() for item in
                    os.getenv("MILO_COMMANDER_MODEL_ALLOWLIST", "").split(","))))
                commander_model = os.getenv("MILO_COMMANDER_MODEL", "").strip()
                worker_model = os.getenv("MILO_SWARM_WORKER_MODEL", "").strip()
                if not allowed or not commander_model or not worker_model or commander_model not in allowed:
                    raise ValueError("Swarm V2 model configuration is incomplete or not allowlisted")
                tools = ToolRegistry()  # real tools are registered explicitly; mocks never enter this path
                scheduler = ProviderScheduler(provider_limits,
                    cancellation_checker=is_cancelled,
                    backpressure_callback=record_provider_backpressure)
                gateway = ModelGateway(guarded_client_factory=build_guarded_client_factory(tracker),
                    scheduler=scheduler, api_key=worker_provider_api_key(),
                    base_url=os.getenv("MILO_MODEL_BASE_URL", "https://api.moonshot.ai/v1"),
                    allowed_tool_names=tools.allowed_names,
                    cancellation_checker=is_cancelled, agent_step_callback=record_agent_step)
                limits = PlanLimits()
                validator = PlanValidator(allowed_tools=tools.allowed_names, limits=limits)
                commander = Commander(client=gateway,
                    resolver=CommanderModelResolver(allowed, set(allowed)), validator=validator)
                tool_context = ToolContext(cancellation_checker=is_cancelled)
                executor = BoundedTaskExecutor(worker_factory=lambda: GenericWorker(
                    gateway=gateway, tools=tools, model=worker_model, tool_context=tool_context,
                    cancellation_checker=is_cancelled, event_sink=forward_event),
                    max_active_workers=BoundedTaskExecutor.configured_limit(),
                    cancellation_checker=is_cancelled)
                board = EvidenceBoard(repo, WorkerLease(run_id, worker_id,
                    int(run.get("attempt") or 1), str(run.get("lease_token") or "")))
                def remaining():
                    cfg = tracker.config
                    model_calls = max(
                        0, (cfg.max_model_calls_per_run or
                            (limits.max_tasks + 2)) - tracker.model_calls
                    )
                    return RemainingBudget(
                        cost_units=limits.max_cost_units,
                        tool_calls=limits.max_tool_calls,
                        tasks=limits.max_tasks,
                        model_calls=model_calls,
                    )
                return SwarmV2Adapter(commander=commander, executor=executor,
                    verifier=Verifier(gateway=gateway, model=commander_model),
                    evidence_loader=lambda _: [EvidenceReference.model_validate(item)
                                               for item in board.references()],
                    checkpoint_sink=save_checkpoint, event_sink=forward_event,
                    usage_snapshot=tracker.snapshot, remaining_budget=remaining)
            swarm_engine_builder = make_swarm_engine
        try:
            # Restore cumulative V2 usage before constructing any model path.
            # A restarted worker must not regain per-run budget capacity.
            if workflow_key == "swarm_v2" and latest_checkpoint:
                checkpoint_usage = latest_checkpoint.get("token_usage") or {}
                if not checkpoint_usage:
                    checkpoint_usage = (((latest_checkpoint.get("artifacts") or {})
                                         .get("swarm_state") or {})
                                        .get("usage_snapshot") or {})
                tracker.restore_snapshot(dict(checkpoint_usage))
            selected_engine = resolved_engine.factory()
            # V2 owns its versioned checkpoint compatibility checks. V1 keeps
            # its existing artifact-based resume path above unchanged.
            engine_run = ({**run, "checkpoint": latest_checkpoint}
                          if workflow_key == "swarm_v2" and latest_checkpoint else run)
            result = selected_engine.run(engine_run)
        except CancellationRequested:
            sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled", payload={}))
            shadow_observe("run_cancelled", {})
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, "cancelled", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), finished_at=datetime.now(UTC).isoformat())
            return 0
        except BudgetExceeded as exc:
            _persist_budget_terminal(repo, run_id, exc, tracker, lease_ctx)
            return 0 if workflow_key == "swarm_v2" else 1
        except Exception as exc:
            # Preserve V1 behavior. V2 validation/factory/provider failures are
            # terminal and sanitized, but a stale worker is never allowed to
            # write a failure after losing its lease.
            if workflow_key != "swarm_v2" or not holds_lease():
                raise
            from backend.engines.swarm_v2 import CommanderPlanFailure
            if isinstance(exc, CommanderPlanFailure):
                code, message = exc.code, exc.safe_message
            else:
                code, message = "SWARM_V2_EXECUTION_FAILED", "Swarm V2 execution failed"
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed",
                                     message=message, payload={"code": code}))
            shadow_observe("run_failed", {"code": code})
            repo.mark_run_failed(run_id, code, message, worker_id=worker_id,
                                 attempt=run.get("attempt"),
                                 lease_token=run.get("lease_token"))
            # A terminal failure committed under this lease is a handled job
            # outcome. Non-zero is reserved for exceptions above (lease loss
            # or inability to durably write the terminal state).
            return 0
        if tracker.stop is not None:
            # The engine absorbed per-agent failures, but a hard limit tripped:
            # never report success and record the terminal budget status.
            stop = tracker.stop
            _persist_budget_terminal(repo, run_id, stop, tracker, lease_ctx)
            return 0 if workflow_key == "swarm_v2" else 1
        if result.get("status") in {"complete", "partial_success", "success"} or (result.get("status") != "failed" and result.get("result")):
            status = "partial_success" if result.get("status") == "partial_success" else "completed"
            sink.emit(RunEventRecord(run_id=run_id, type="run_partial_success" if status == "partial_success" else "run_completed", message=f"Run {status}", payload={"status": result.get("status")}))
            shadow_observe("run_partial_success" if status == "partial_success" else "run_completed", {"status": result.get("status")})
            if hasattr(repo, "transition_run") and status == "partial_success":
                repo.transition_run(run_id, "partial_success", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), output=result, error=None, finished_at=datetime.now(UTC).isoformat())
            else:
                repo.mark_run_complete(run_id, result, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0
        error = result.get("error", {}) if isinstance(result, dict) else {}
        code = error.get("code", "ENGINE_FAILED")
        message = error.get("message", "vehicle_catalog_v1 engine failed")
        sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=message, payload={"code": code}))
        shadow_observe("run_failed", {"code": code})
        repo.mark_run_failed(run_id, code, message, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
        return 0 if workflow_key == "swarm_v2" else 1
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
