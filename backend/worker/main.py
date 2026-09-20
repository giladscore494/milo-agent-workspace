import argparse
import os
import threading
import time
from typing import Any
from datetime import UTC, datetime
from uuid import UUID, uuid4
from backend.budget import BudgetConfig, BudgetExceeded, BudgetTracker, ModelCallReservation, build_guarded_client_factory, paid_execution_enabled
from backend.execution_usage import merge_usage_snapshots, public_usage_projection
from backend.runtime_policy import RuntimePolicyError, policy_failure_code, resolve_runtime_policy
from backend.config import get_settings
from backend.errors import AppError
from backend.repository import Repository, SupabaseRepository
from backend.runtime import TERMINAL_STATES, CancellationRequested, RunEventRecord, SupabaseEventSink
from backend.supervisor import SupervisorInput, apply_event_to_blackboard, build_evaluation_report, initial_blackboard, make_shadow_decision, route_event_message
from backend.engines.vehicle_catalog_v1 import VehicleCatalogV1Adapter
from backend.worker.engine import Engine, EngineRegistry, EngineResolver


def resolve_run_id(cli_run_id: str | None) -> UUID:
    value = cli_run_id or os.getenv("RUN_ID")
    if not value:
        raise AppError("MISSING_RUN_ID", "RUN_ID must be provided by environment or --run-id", 2)
    return UUID(value)


#: Repository error codes that PROVE the lease is no longer this worker's:
#: the guarded RPCs' STALE_WORKER_WRITE (surfaced as RUN_LEASE_LOST), the
#: in-memory repository's conflict, and a run that no longer exists.
DEFINITIVE_LEASE_LOSS_CODES = frozenset({
    "RUN_LEASE_LOST", "RUN_TRANSITION_CONFLICT", "STALE_WORKER_WRITE", "RUN_NOT_FOUND",
})


def _is_definitive_lease_loss(exc: BaseException) -> bool:
    """Whether a heartbeat failure proves the lease is gone.

    Only an answer FROM the database about ownership counts: a lease/attempt/
    token mismatch or a missing run. A transport failure, a 5xx or an unknown
    exception says nothing about ownership and is treated as transient; the
    lease is then considered lost only once the last proven extension lapses.
    """
    if isinstance(exc, AppError):
        return exc.code in DEFINITIVE_LEASE_LOSS_CODES or exc.status_code in {404, 409}
    return False


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


def _claim_run_with_recovery(repo: Repository, run_id: UUID, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    """Claim the run lease; a retry against a finalized run is a no-op.

    Cloud Run retries a task whose previous attempt exited non-zero. When
    that retry finds the run already in a durable terminal state, exiting
    non-zero again would only burn the retry budget on RUN_ALREADY_CLAIMED
    (the recorded production failure mode), so it returns None and the
    caller exits 0 without touching the run. When another worker still
    holds an unexpired lease, the retry waits it out (bounded by the lease
    duration itself) and re-claims through the same atomic CAS; if the
    holder keeps heartbeating past the bound, the conflict escapes
    unchanged so this stale retry never writes anything.
    """
    deadline = time.monotonic() + max(
        0.0, float(os.getenv("MILO_WORKER_CLAIM_WAIT_SECONDS", str(lease_seconds + 30)))
    )
    while True:
        try:
            return repo.claim_run(run_id, worker_id, lease_seconds=lease_seconds)
        except AppError as exc:
            if exc.code != "RUN_ALREADY_CLAIMED":
                raise
            if repo.get_run(run_id).get("status") in TERMINAL_STATES:
                return None
            if time.monotonic() >= deadline:
                raise
        time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))


def evidence_of_completed_tasks(board: Any, results: Any) -> list[Any]:
    """The run's evidence, as the Swarm V2 engine defines it: completed tasks only.

    The Evidence Board records a claim the moment a trusted tool result is
    mapped, which is BEFORE that task's model call and, under the bounded
    executor, while sibling tasks are running on other threads. So the board
    holds claims for tasks that are in flight, and keeps claims for tasks that
    later failed. The engine's evidence merge is stated over the tasks that
    COMPLETED, and it is handed exactly the results it should read evidence
    for -- this loader used to ignore that argument and hand back every claim
    on the board, which made the merge refuse the run (and every resume of it)
    as soon as one register-read task completed before another, or one failed
    after its tool call. Evidence of a task that has not completed is not lost:
    it is durable, and it enters the run on that task's own completion.
    """
    from backend.engines.swarm_v2 import EvidenceReference

    completed = {str(task_id) for task_id, result in dict(results or {}).items()
                 if getattr(result, "status", None) == "completed"}
    return [EvidenceReference.model_validate(item)
            for item in board.references(task_ids=completed)]


def execute_run(run_id: UUID, repo: Repository, engine: Engine | None = None, budget_tracker: "BudgetTracker | None" = None, engine_registry: EngineRegistry | None = None) -> int:
    worker_id = os.getenv("WORKER_ID", f"worker-{uuid4()}")
    lease_seconds = int(os.getenv("MILO_WORKER_LEASE_SECONDS", "300"))
    heartbeat_interval = max(1.0, min(float(os.getenv("MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS", "30")), lease_seconds / 3))
    if hasattr(repo, "claim_run"):
        claimed = _claim_run_with_recovery(repo, run_id, worker_id, lease_seconds)
        if claimed is None:
            # The run already reached a durable terminal state: nothing to
            # execute, nothing to write, and the retry chain ends here.
            return 0
        run = claimed
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
    # The local view of when the lease this worker LAST PROVABLY extended
    # lapses. A heartbeat that fails for a transient reason (network, 5xx)
    # does not by itself mean the lease is gone: the database still holds the
    # extension the previous heartbeat obtained. Ownership is never widened by
    # this -- every durable write is still fenced at the database boundary,
    # and `holds_lease` still re-reads the run row -- it only stops a
    # transient blip from ending a paid run as `failed` when it could resume.
    # A DEFINITIVE rejection (the lease was reclaimed or the run is gone)
    # marks the lease lost at once, and so does the lease lapsing locally
    # without a successful extension.
    lease_deadline = [time.monotonic() + lease_seconds]
    heartbeat_degraded = threading.Event()

    def heartbeat_once() -> bool:
        if not hasattr(repo, "heartbeat"):
            return True
        try:
            repo.heartbeat(run_id, worker_id, lease_seconds=lease_seconds, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
        except Exception as exc:
            if _is_definitive_lease_loss(exc) or time.monotonic() >= lease_deadline[0]:
                lease_lost.set()
                return False
            heartbeat_degraded.set()
            return True
        lease_deadline[0] = time.monotonic() + lease_seconds
        heartbeat_degraded.clear()
        return True

    def heartbeat_loop() -> None:
        while True:
            # Retry sooner while degraded, so a transient failure is retried
            # well inside the lease instead of once per full interval.
            wait = min(heartbeat_interval, 5.0) if heartbeat_degraded.is_set() else heartbeat_interval
            if stop_heartbeat.wait(wait):
                return
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
        # Catalog PR3's trusted promotion path, populated by the Swarm V2
        # wiring below and read once the engine has settled its verdicts. A
        # plain dict rather than a closure variable because the wiring runs
        # inside a nested factory; it holds server objects only.
        catalog_promotion: dict[str, Any] = {}

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
                # Only the exception class name is persisted: raw exception
                # text can carry provider/database details into run_events.
                sink.emit(RunEventRecord(run_id=run_id, type="supervisor_shadow_failed", message="Supervisor shadow observation failed without altering execution", payload={"code": "SUPERVISOR_SHADOW_FAILED", "error_type": type(exc).__name__}))

        sink.emit(RunEventRecord(run_id=run_id, type="run_started", message="Run started", payload={"worker_id": worker_id, "attempt": run.get("attempt", 1)}))
        shadow_observe("run_started", {"worker_id": worker_id, "attempt": run.get("attempt", 1)})
        latest_checkpoint = repo.latest_checkpoint(run_id, workflow_key) if hasattr(repo, "latest_checkpoint") else None
        if latest_checkpoint:
            sink.emit(RunEventRecord(run_id=run_id, type="run_resumed", message="Run resumed from latest compatible checkpoint", payload={"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")}))
            shadow_observe("run_resumed", {"checkpoint_id": str(latest_checkpoint.get("id", "")), "phase": latest_checkpoint.get("phase")})
            artifacts = latest_checkpoint.get("artifacts") or {}
            if latest_checkpoint.get("phase") == "summary" and artifacts.get("final_builder"):
                final = artifacts["final_builder"].get("parsed", {})
                # The V1 result contract carries the two token counts; the
                # checkpoint's token_usage may also hold the full ledger
                # record (the worker consolidates it there), which belongs
                # to runs.usage / run_execution_usage, not to the output.
                checkpoint_tokens = latest_checkpoint.get("token_usage") or {}
                result = {"status": final.get("status", "success"), "result": final, "summary": (artifacts.get("hebrew_summary") or {}).get("parsed", {}).get("summary"), "results": artifacts, **{k: checkpoint_tokens[k] for k in ("input_tokens", "output_tokens") if k in checkpoint_tokens}}
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
                # A checkpoint's token_usage is CONSOLIDATED from the ledger
                # rather than trusted as the engine wrote it. Engines count
                # process-locally (V1 restarts its own token counters on
                # every replay, the mock engine derives them from its phase
                # count); the tracker is cumulative across attempts, and the
                # merge is never lower than either -- so a checkpoint can
                # never carry less usage than the run has durably spent.
                checkpoint = {**checkpoint, "run_id": str(run_id), "attempt": run.get("attempt", 1), "workflow_key": workflow_key,
                              "token_usage": merge_usage_snapshots(checkpoint.get("token_usage"), tracker.ledger_snapshot())}
                repo.save_checkpoint(checkpoint, **lease_ctx)
                shadow_observe("checkpoint_saved", checkpoint)
        def is_cancelled():
            return repo.get_run(run_id).get("status") == "cancellation_requested"

        # Hard budget/cost gate, checked first so the established
        # BUDGET_CONFIG_INVALID code still names an incomplete budget. The
        # mandatory set it checks is now DERIVED from the canonical runtime
        # policy, so it covers every dimension the reviewed first-run profile
        # advertises rather than the five it used to name.
        budget_config = BudgetConfig.from_env()
        if paid_execution_enabled() and budget_config.missing_mandatory():
            missing = ", ".join(budget_config.missing_mandatory())
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Budget configuration incomplete; refusing paid execution", payload={"code": "BUDGET_CONFIG_INVALID", "missing": missing}))
            repo.mark_run_failed(run_id, "BUDGET_CONFIG_INVALID", f"mandatory budget settings missing: {missing}", worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0 if workflow_key == "swarm_v2" else 1

        # The ONE canonical runtime policy for this DEPLOYMENT. It is resolved
        # ONCE, here, and every enforcement surface below is derived from it:
        # the
        # budget tracker, the provider scheduler, the Swarm V2 plan firewall,
        # the provider-visible planning policy, the feasibility gate and the
        # executor width. Two surfaces cannot describe different effective
        # safety envelopes because there is only one envelope to describe.
        #
        # Fail closed: in the paid posture an absent, unparseable, wider-than-
        # reviewed or self-contradictory dimension refuses the run rather than
        # resolving to a generic default. The tracker additionally blocks every
        # call while MILO_ENABLE_PAID_EXECUTION is off.
        try:
            policy = resolve_runtime_policy()
        except RuntimePolicyError as exc:
            code = policy_failure_code(exc)
            detail = "; ".join(f"{v.dimension}: {v.message}" for v in exc.violations)
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Runtime policy incomplete or wider than the reviewed envelope; refusing execution", payload={"code": code, "codes": ", ".join(exc.codes), "detail": detail}))
            repo.mark_run_failed(run_id, code, detail, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0 if workflow_key == "swarm_v2" else 1
        budget_config = policy.budget_config()
        # Provider credentials are worker-only (env/Secret Manager). Paid
        # execution fails closed when the key is absent; the key value itself is
        # never logged, persisted or echoed into events.
        from backend.engines.vehicle_catalog_v1.adapter import worker_provider_api_key

        if paid_execution_enabled() and not worker_provider_api_key():
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Provider API key not configured for this worker; refusing paid execution", payload={"code": "PROVIDER_KEY_MISSING"}))
            repo.mark_run_failed(run_id, "PROVIDER_KEY_MISSING", "worker provider API key (KIMI_API_KEY/MOONSHOT_API_KEY) is not configured", worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0 if workflow_key == "swarm_v2" else 1

        # Provider-side scheduling limits (concurrency/RPM/TPM/backpressure
        # bounds) come from the canonical runtime policy resolved above, which
        # validated them fail-closed: an invalid value refuses the run instead
        # of degrading into unlimited capacity.
        engine_mode = (os.getenv("MILO_WORKER_ENGINE") or "").strip().lower()
        provider_limits = None
        provider_coordinator = None
        # Imported before the try: a ValueError from limit parsing must not
        # leave the handler unable to name its own exception type.
        from backend.provider_quota import ProviderQuotaUnavailable, resolve_coordinator

        if engine is None and engine_mode != "mock":
            try:
                # Same policy object, so the provider envelope a paid run
                # actually admits against cannot differ from the one the
                # reviewed profile, Stage D and configuration validation all
                # name.
                provider_limits = policy.provider_limits()
                # ONE coordinator per worker process, shared by whichever engine
                # runs. The Kimi allowance is account-wide, so this is the only
                # thing that can see the other Cloud Run executions, processes
                # and replicas drawing on it. Fails closed in production when
                # the shared store is unconfigured: an unmetered fallback there
                # would let each execution admit a full ceiling of its own.
                # The client deadline comes from the SAME validated
                # configuration that owns the permits, so the timeout a request
                # actually gets and the permit it runs under can never be
                # resolved from two different places.
                provider_coordinator = resolve_coordinator(
                    diagnostic_sink=lambda kind, payload: sink.emit(RunEventRecord(
                        run_id=run_id, type=kind,
                        message="provider quota signal", payload=payload)))
            except (ValueError, ProviderQuotaUnavailable) as exc:
                code = ("PROVIDER_QUOTA_UNAVAILABLE"
                        if isinstance(exc, ProviderQuotaUnavailable)
                        else "PROVIDER_LIMITS_CONFIG_INVALID")
                message = exc.message if isinstance(exc, ProviderQuotaUnavailable) else str(exc)
                sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message="Provider limit configuration invalid; refusing execution", payload={"code": code, "message": message}))
                repo.mark_run_failed(run_id, code, message, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
                return 0 if workflow_key == "swarm_v2" else 1

        # Resolved once, from the coordinator that owns the permits.
        provider_request_deadline = (provider_coordinator.config.request_deadline_seconds
                                     if provider_coordinator is not None else None)

        def emit_budget_event(event_type, payload):
            sink.emit(RunEventRecord(run_id=run_id, type=event_type, message=payload.get("message", event_type), payload=payload.get("payload", payload)))
            shadow_observe(event_type, payload)

        def record_usage(ledger):
            """Make the tracker's ledger durable under the active lease.

            The canonical path is `record_run_usage` (migration
            20260920000100): a merging, versioned, lease-guarded write that
            also projects the public aggregate into runs.usage. A repository
            without it still receives the public projection through the
            (now monotonic) `update_run_usage`. Either way a stale lease is
            rejected at the database boundary and the failure propagates:
            a worker that cannot record consumption must not keep consuming.
            """
            if hasattr(repo, "record_run_usage"):
                row = repo.record_run_usage(run_id, ledger, **lease_ctx)
                return row.get("version") if isinstance(row, dict) else None
            if hasattr(repo, "update_run_usage"):
                repo.update_run_usage(run_id, public_usage_projection(ledger), **lease_ctx)
            return None

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
        if tracker.usage_recorder is None:
            # An injected tracker (tests, harnesses) still records through
            # THIS worker's lease: durable accounting is a property of the
            # run, not of whoever constructed the tracker.
            tracker.usage_recorder = record_usage

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

        def record_ledger_consumption(kind: str) -> None:
            """Swarm V2's non-provider consumption, made durable as it happens.

            The engine names WHAT was consumed with a static kind; the ledger
            is the only place it is counted. Nothing here is derived from the
            plan, the checkpoint or the event stream afterwards.
            """
            if kind == "task_completed":
                tracker.record_task_result("completed")
            elif kind == "task_failed":
                tracker.record_task_result("failed")
            elif kind == "replan":
                tracker.record_replan()
            elif kind == "correction_round":
                tracker.record_replan(correction=True)
            else:
                raise ValueError(f"unknown ledger consumption kind {kind!r}")

        if engine is None and engine_registry is None and engine_mode == "mock":
            from backend.worker.mock_engine import MockLifecycleEngine

            engine_builder = lambda: MockLifecycleEngine(
                event_sink=forward_event, checkpoint_sink=save_checkpoint,
                cancellation_checker=is_cancelled, agent_step_callback=record_agent_step,
                retry_callback=record_retry, budget_tracker=tracker,
            )
        elif engine is None and engine_registry is None:
            engine_builder = lambda: VehicleCatalogV1Adapter(
                model_client_factory=build_guarded_client_factory(tracker, request_deadline_seconds=provider_request_deadline), event_sink=forward_event,
                checkpoint_sink=save_checkpoint, cancellation_checker=is_cancelled,
                agent_step_callback=record_agent_step, retry_callback=record_retry,
                provider_limits=provider_limits, provider_backpressure_callback=record_provider_backpressure,
                # The SAME coordinator instance the Swarm V2 wiring uses: the
                # Kimi allowance is one organization-wide pool, so V1 and V2
                # must draw from one gate, not two that each believe they own
                # the account.
                provider_coordinator=provider_coordinator,
            )
            def make_swarm_engine():
                from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander,
                    CommanderModelResolver, GenericWorker, ModelGateway,
                    PlanValidator, RemainingBudget, SwarmV2Adapter, Verifier)
                from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
                from backend.engines.swarm_v2.evidence_mapping import (
                    EvidenceMapperRegistry, RegisteredOperationEvidenceSink,
                    TrustedEvidenceAcquisition, production_evidence_mappers)
                from backend.engines.swarm_v2.grounding import RepositoryEvidenceResolver
                from backend.provider_scheduler import ProviderScheduler
                from backend.tools import ToolContext, ToolRegistry
                from backend.tools.government_vehicle import (GOVERNMENT_TOOL_SCOPE,
                                                              GovernmentVehicleTool)
                from backend.catalog.execution import catalog_posture
                from backend.catalog.pipeline import CatalogPromotionPipeline

                # The catalog posture, read ONCE here from the process
                # environment. Every value is default off, and off for any
                # value the repository's shared convention does not recognise.
                #
                # It is read ONCE, at construction, so every decision below
                # comes from the same answer: a registry, a scope, a mapper and
                # a pipeline that disagreed about which capabilities this
                # process has would be a worse posture than having no switch.
                #
                # Reading the register and promoting into the canonical catalog
                # are now SEPARATE capabilities. They used to be one flag, so
                # the only way to let a run read government data was to arm
                # canonical writes at the same time -- which meant there was no
                # configuration at all for a genuinely read-only first run.
                # Promotion still requires read; read never implies promotion.
                posture = catalog_posture()
                government_read_enabled = posture["government_read"]
                promotion_enabled = posture["promotion"]

                allowed = tuple(filter(None, (item.strip() for item in
                    os.getenv("MILO_COMMANDER_MODEL_ALLOWLIST", "").split(","))))
                commander_model = os.getenv("MILO_COMMANDER_MODEL", "").strip()
                worker_model = os.getenv("MILO_SWARM_WORKER_MODEL", "").strip()
                if not allowed or not commander_model or not worker_model or commander_model not in allowed:
                    raise ValueError("Swarm V2 model configuration is incomplete or not allowlisted")
                # Catalog PR3 registers the FIRST real production tool: the
                # bounded, read-only Israeli vehicle register. It reads durable
                # catalog rows through this worker's own repository and holds
                # no transport and no credential, so a chat run cannot reach
                # `data.gov.il` through it. Its scope is granted on the
                # ToolContext below -- in this trusted wiring, never by a plan.
                #
                # No Yeda, CKAN or web tool is registered, and no WRITE tool is
                # registered at all: canonical promotion is a lease-guarded
                # repository RPC that trusted server code calls, not a
                # capability a model can request.
                #
                # CODE-2 gates the REGISTRATION itself rather than hiding a
                # registered tool. With the catalog off the registry is empty,
                # so there is no descriptor for Commander to see, nothing for
                # the plan firewall to admit, and no Government name in the
                # provider-visible policy -- the capability is ABSENT, not
                # merely unreachable.
                tools = ToolRegistry([GovernmentVehicleTool(repo)] if government_read_enabled else [])
                scheduler = ProviderScheduler(provider_limits,
                    cancellation_checker=is_cancelled,
                    backpressure_callback=record_provider_backpressure,
                    coordinator=provider_coordinator)
                # ONE PlanLimits instance feeds both the provider-visible
                # policy (ModelGateway) and the deterministic firewall
                # (PlanValidator): contract parity cannot drift silently.
                #
                # Derived from the canonical runtime policy, not from the broad
                # `PlanLimits()` defaults. Wiring those unconditionally let the
                # firewall admit a 64-task plan inside a 56-agent-step budget,
                # and admit 3 replans and 100 tool calls into a reviewed
                # profile that authorized 1 and 24. The ceiling is now the
                # reviewed plan shape, narrowed again to what this run's
                # agent-step and model-call envelope can actually pay for.
                limits = policy.plan_limits()
                gateway = ModelGateway(guarded_client_factory=build_guarded_client_factory(tracker, request_deadline_seconds=provider_request_deadline),
                    scheduler=scheduler, api_key=worker_provider_api_key(),
                    base_url=os.getenv("MILO_MODEL_BASE_URL", "https://api.moonshot.ai/v1"),
                    # Sanitized, server-owned descriptors: Commander sees each
                    # registered tool's operations and schemas, and the SAME
                    # descriptors are the firewall's only tool authority.
                    tool_descriptors=tools.descriptors(),
                    cancellation_checker=is_cancelled, agent_step_callback=record_agent_step,
                    plan_limits=limits)
                validator = PlanValidator(allowed_tools=tools.descriptors(), limits=limits)
                commander = Commander(client=gateway,
                    resolver=CommanderModelResolver(allowed, set(allowed)), validator=validator,
                    retry_callback=record_retry)
                # Exactly ONE read scope is granted, from trusted server
                # state. `write_approved` stays False and no
                # `tool:write:<name>` capability is granted, so a write tool
                # would still be impossible even if one were registered. A
                # plan can request a registered capability; it can never
                # authorize one.
                #
                # With the catalog off NO scope is granted at all. That is
                # belt-and-braces on top of the empty registry above -- the
                # Registry refuses an unregistered tool before scope is even
                # consulted -- but a granted scope with nothing to unlock is
                # exactly the kind of leftover that survives a later refactor.
                tool_context = ToolContext(
                    scopes=frozenset({GOVERNMENT_TOOL_SCOPE}) if government_read_enabled
                           else frozenset(),
                    cancellation_checker=is_cancelled)
                # The run's lease-guarded Evidence Board, built BEFORE the
                # executor because the worker's trusted tool-result sink writes
                # through it. Same board the Verifier's verdicts and the
                # conflict decisions go through, so there is one evidence
                # writer for the whole run.
                board = EvidenceBoard(repo, WorkerLease(run_id, worker_id,
                    int(run.get("attempt") or 1), str(run.get("lease_token") or "")))
                # The R3 seam, wired for the first time. Routed so that ONLY
                # the registered Government operation becomes evidence: the
                # tool's seven other reads record nothing at all rather than
                # failing the task that called them, and the pre-R3 generic
                # text extractor stays unreachable either way.
                #
                # CODE-2: with the catalog off the sink is built over an EMPTY
                # mapper registry -- `production_evidence_mappers()` is not
                # even called, so the Government mapper is never constructed
                # and no operation can become durable evidence. The sink itself
                # stays wired so its refusal of anything that is not a
                # `ToolCallRecord` keeps holding for every other caller.
                evidence_sink = RegisteredOperationEvidenceSink(
                    TrustedEvidenceAcquisition(
                        board=board,
                        mappers=production_evidence_mappers() if government_read_enabled
                                else EvidenceMapperRegistry()))
                # Catalog PR3: the trusted promotion path. It observes NOTHING
                # here and holds no state: when it runs it asks the database
                # which candidates this run still owes, deriving the
                # association from rows the server itself wrote. That is what
                # makes it behave identically in a worker that gathered the
                # evidence and in one that replaced a worker which did.
                #
                # CODE-2: with the catalog off it is never CONSTRUCTED, which
                # is what the call site below reads. Not constructed means no
                # pending-promotion read, no canonical write and no catalog
                # event -- and a database that already holds a usable snapshot
                # stays inert for a chat run instead of being live by accident.
                if promotion_enabled:
                    catalog_promotion["pipeline"] = CatalogPromotionPipeline(repo, board.lease)
                executor = BoundedTaskExecutor(worker_factory=lambda: GenericWorker(
                    gateway=gateway, tools=tools, model=worker_model, tool_context=tool_context,
                    cancellation_checker=is_cancelled, event_sink=forward_event,
                    # Every planned Tool invocation is a durable, cumulative
                    # consumption BEFORE it runs: a failed call is still a
                    # call, and a resumed run does not get it back.
                    tool_call_callback=tracker.record_tool_call,
                    # The trusted post-execution seam, wired. It is reached
                    # only with a Registry-validated result and server-resolved
                    # identity; the worker model cannot call it, cannot choose
                    # what it writes, and cannot turn its own completion into
                    # evidence.
                    tool_result_sink=evidence_sink,
                    # A bounded worker-output repair is a semantic retry and
                    # consumes the SAME run-level retry allowance the
                    # Commander repair does. Provider 429 backpressure is
                    # absorbed by the scheduler and never reaches here.
                    retry_callback=record_retry),
                    # Bounded three ways and widened by none: the deployment's
                    # own setting, the canonical policy's reviewed width, and
                    # what the organization will really admit concurrently.
                    # Extra logical workers beyond that only queue, consuming
                    # run duration and lease time.
                    max_active_workers=min(
                        BoundedTaskExecutor.configured_limit(
                            provider_capacity=provider_limits.max_concurrency),
                        policy.swarm_max_active_workers),
                    cancellation_checker=is_cancelled)
                def remaining():
                    cfg = tracker.config
                    model_calls = max(
                        0, (cfg.max_model_calls_per_run or
                            (limits.max_tasks + 2)) - tracker.model_calls
                    )
                    # R4: the remaining SEMANTIC retry allowance, so the one
                    # bounded correction round is refused when the run has no
                    # retries left. A deployment with no configured limit keeps
                    # the permissive contract default and is not given one here.
                    retries = (RemainingBudget.model_fields["retries"].default
                               if cfg.max_retries is None else
                               max(0, cfg.max_retries - tracker.retries))
                    # Every guarded gateway call records one agent step, so a
                    # plan whose worst case needs more steps than remain cannot
                    # finish no matter how much call/token/cost budget it has.
                    # This dimension was missing, which is how a 54+ task plan
                    # passed preflight against a 56-step ceiling.
                    agent_steps = (RemainingBudget.model_fields["agent_steps"].default
                                   if cfg.max_agent_steps is None else
                                   max(0, cfg.max_agent_steps - tracker.agent_steps))
                    # Tool calls and task executions are CUMULATIVE ledger
                    # dimensions, like model calls and agent steps: what a
                    # failed task spent, what an earlier attempt spent and
                    # what a superseded plan spent all stay spent. The
                    # remaining capacity is therefore the reviewed plan-shape
                    # ceiling minus what the ledger shows, never a value
                    # rebuilt from the current plan's completed tasks.
                    tool_calls = max(0, limits.max_tool_calls - tracker.tool_calls)
                    tasks = max(0, limits.max_tasks - (tracker.tasks_completed + tracker.tasks_failed))
                    return RemainingBudget(
                        cost_units=limits.max_cost_units,
                        tool_calls=tool_calls,
                        tasks=tasks,
                        model_calls=model_calls,
                        retries=retries,
                        agent_steps=agent_steps,
                    )
                return SwarmV2Adapter(commander=commander, executor=executor,
                    # The resolver is the Verifier's ONLY route to durable
                    # evidence: it is constructed here, in the trusted worker
                    # wiring that already holds the repository and the run, so
                    # the Verifier itself never gains database, web or tool
                    # access of its own.
                    verifier=Verifier(gateway=gateway, model=commander_model,
                                      resolver=RepositoryEvidenceResolver(repo, run_id=run_id)),
                    # Completed tasks only: see `evidence_of_completed_tasks`.
                    evidence_loader=lambda results: evidence_of_completed_tasks(board, results),
                    checkpoint_sink=save_checkpoint, event_sink=forward_event,
                    # The checkpoint carries the FULL ledger, not only the
                    # public aggregate, so a resume restores every dimension.
                    usage_snapshot=tracker.ledger_snapshot, remaining_budget=remaining,
                    ledger_sink=record_ledger_consumption,
                    # R4 durable provenance. The engine still holds no
                    # repository handle: it hands each settled verdict and each
                    # conflict decision to the run's own lease-guarded Evidence
                    # Board, which writes them through the same idempotent,
                    # append-only guarded RPCs as every other evidence write.
                    verdict_sink=board.record_verification_verdict,
                    resolution_sink=board.record_conflict_resolution)
            swarm_engine_builder = make_swarm_engine
        try:
            # Restore the run's cumulative usage BEFORE constructing any model
            # path -- for EVERY engine. A restarted worker must not regain
            # per-run capacity, so the restore takes the MOST ADVANCED durable
            # record rather than any single one:
            #
            #   * the run_execution_usage ledger row, written after every
            #     recorded consumption under the lease (the canonical record);
            #   * runs.usage, the public projection of it (and the only record
            #     a run that predates the ledger row carries);
            #   * the latest checkpoint's token_usage (and, for Swarm V2, the
            #     usage_snapshot inside the checkpointed state).
            #
            # They advance at different rates and a crash can leave any of
            # them staler than the others; the component-wise maximum holds
            # whichever is ahead on each dimension, so nothing durably spent is
            # ever refunded.
            #
            # V1 has no partial-phase resume: unless its checkpoint is the
            # final one (the fast path above), it deliberately REPLAYS the
            # pipeline from the start. That replay is now charged ON TOP of
            # everything the earlier attempts consumed -- the tracker starts
            # from the restored record, never from zero -- so a relaunched V1
            # run cannot spend a second full budget.
            checkpoint_usage: dict[str, Any] = {}
            swarm_state_usage: dict[str, Any] = {}
            if latest_checkpoint:
                checkpoint_usage = latest_checkpoint.get("token_usage") or {}
                swarm_state_usage = (((latest_checkpoint.get("artifacts") or {})
                                      .get("swarm_state") or {})
                                     .get("usage_snapshot") or {})
            ledger_row = repo.get_run_usage_ledger(run_id) if hasattr(repo, "get_run_usage_ledger") else None
            durable_ledger = (ledger_row or {}).get("ledger") or {}
            # A run that never spent anything stores {} everywhere, so a
            # first attempt merges to nothing and restores nothing.
            restored = merge_usage_snapshots(run.get("usage"), checkpoint_usage,
                                             swarm_state_usage, durable_ledger)
            if restored:
                tracker.restore_snapshot(restored)
            selected_engine = resolved_engine.factory()
            # V2 owns its versioned checkpoint compatibility checks. V1 keeps
            # its existing artifact-based resume path above unchanged.
            engine_run = ({**run, "checkpoint": latest_checkpoint}
                          if workflow_key == "swarm_v2" and latest_checkpoint else run)
            result = selected_engine.run(engine_run)
            # Catalog PR3: the trusted promotion path, AFTER the engine has
            # settled every verdict and BEFORE the run is finalized, so it
            # still holds the lease every write it performs is guarded by.
            #
            # It is deliberately not a Tool and not an engine step: a model can
            # cause a Government READ and nothing beyond it. It asks the
            # database what this run still owes -- so a RESUMED worker, which
            # restored the completed tasks and never re-executed the tool,
            # promotes exactly what the crashed one would have -- promotes at
            # most `MAX_PROMOTIONS_PER_RUN`, starts no capture and schedules
            # nothing.
            #
            # A REFUSAL is a legitimate outcome and is emitted as a run event
            # rather than failing the run. An INFRASTRUCTURE failure is not a
            # refusal and is never reported as one: a LOST LEASE and a
            # PENDING-PROMOTION READ that could not run both raise `AppError`
            # from here into the handler below, which re-raises it. That is
            # deliberate and load-bearing -- a failed read is not "this run
            # owes no promotion", and finalizing the run on one would strand a
            # run whose verified evidence is durable and whose canonical
            # promotion never happened, with no later scheduler to revisit it.
            #
            # CODE-2: the `is not None` guard is now load-bearing rather than
            # defensive. With `MILO_ENABLE_CATALOG_EXECUTION` off the wiring
            # above never puts a pipeline here, so `promote()` is not called,
            # no candidate is read, nothing is written and neither catalog
            # event is emitted. The catalog path is skipped whole; the run's
            # own outcome and finalization are untouched by the skip.
            if workflow_key == "swarm_v2" and catalog_promotion.get("pipeline") is not None:
                for attempt in catalog_promotion["pipeline"].promote():
                    sink.emit(RunEventRecord(
                        run_id=run_id,
                        type="catalog_variant_promoted" if attempt.promoted
                             else "catalog_promotion_refused",
                        message=("Canonical catalog variant promoted."
                                 if attempt.promoted else attempt.safe_message),
                        payload=attempt.as_event()))
        except CancellationRequested:
            sink.emit(RunEventRecord(run_id=run_id, type="run_cancelled", message="Run cancelled", payload={}))
            shadow_observe("run_cancelled", {})
            if hasattr(repo, "transition_run"):
                repo.transition_run(run_id, "cancelled", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), finished_at=datetime.now(UTC).isoformat())
            return 0
        except BudgetExceeded as exc:
            # `_persist_budget_terminal` returning means the terminal state is
            # durable under this lease, so the run is FINISHED -- a timeout, a
            # budget stop or a cost stop is an answer, not a crash.
            #
            # This used to exit 1 for V1, which Cloud Run reads as a failed
            # task and relaunches (maxRetries=1). Run
            # 3772fc84-420c-4a66-9e79-d58649d4e9b4 timed out, exited 1, and the
            # relaunched task found the run already finalized and exited 0 --
            # so the execution reported "completed successfully in 33m42s"
            # while the product outcome was `timed_out`. A second paid
            # execution that raced the finalization instead would have been
            # worse than misleading.
            _persist_budget_terminal(repo, run_id, exc, tracker, lease_ctx)
            return 0
        except Exception as exc:
            # Preserve V1 behavior. V2 validation/factory/provider failures are
            # terminal and sanitized, but a stale worker is never allowed to
            # write a failure after losing its lease, and a persistence/lease
            # failure surfacing as AppError from the repository boundary is an
            # infrastructure outcome that must escape: it can never be
            # reported as a handled Swarm run failure.
            if workflow_key != "swarm_v2" or isinstance(exc, AppError) or not holds_lease():
                raise
            from backend.engines.swarm_v2 import VALIDATION_REASONS, CommanderPlanFailure
            failure_payload: dict[str, Any]
            if isinstance(exc, CommanderPlanFailure):
                code, message = exc.code, exc.safe_message
                failure_payload = {"code": code}
                # Bounded diagnostic classification for telemetry only:
                # exclusively a static allowlisted reason code, never raw
                # model/validation/provider text. run.error stays unchanged.
                reason = getattr(exc, "validation_reason", None)
                if reason in VALIDATION_REASONS:
                    failure_payload["validation_reason"] = reason
            else:
                code, message = "SWARM_V2_EXECUTION_FAILED", "Swarm V2 execution failed"
                failure_payload = {"code": code}
            sink.emit(RunEventRecord(run_id=run_id, type="run_failed",
                                     message=message, payload=failure_payload))
            shadow_observe("run_failed", dict(failure_payload))
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
            # Zero for the same reason as the BudgetExceeded handler above: the
            # terminal state is durable, so the task is done and Cloud Run must
            # not relaunch it into a second paid execution.
            stop = tracker.stop
            _persist_budget_terminal(repo, run_id, stop, tracker, lease_ctx)
            return 0
        # Product outcome -> durable run status.
        #
        # Swarm V2 owns a validated product-outcome contract, so the worker
        # LOOKS IT UP instead of guessing usefulness from dictionary
        # truthiness: `status` and `result_kind` must be allowlisted, must
        # agree with each other and must agree with the verified fields, or
        # the run is a contract violation rather than a quiet success. In
        # particular `no_usable_result` can only ever reach `partial_success`
        # and can never emit run_completed.
        #
        # V1 (and the mock lifecycle engine) keep their existing mapping
        # unchanged, including the generic `result` fallback they rely on.
        if workflow_key == "swarm_v2":
            from backend.engines.swarm_v2 import ProductOutcomeError, durable_run_status
            try:
                status = durable_run_status(result)
            except ProductOutcomeError:
                # Static classification only: the offending payload never
                # reaches an event, run.error or the browser.
                code, message = "SWARM_V2_OUTCOME_INVALID", "Swarm V2 product outcome is invalid"
                sink.emit(RunEventRecord(run_id=run_id, type="run_failed", message=message,
                                         payload={"code": code}))
                shadow_observe("run_failed", {"code": code})
                repo.mark_run_failed(run_id, code, message, worker_id=worker_id,
                                     attempt=run.get("attempt"), lease_token=run.get("lease_token"))
                return 0
            sink.emit(RunEventRecord(run_id=run_id, type="run_partial_success" if status == "partial_success" else "run_completed", message=f"Run {status}", payload={"status": result.get("status"), "result_kind": result.get("result_kind")}))
            shadow_observe("run_partial_success" if status == "partial_success" else "run_completed", {"status": result.get("status"), "result_kind": result.get("result_kind")})
            if status == "partial_success":
                # Never silently downgrade to mark_run_complete when the
                # repository cannot express partial_success: reporting an
                # unusable result as `completed` is the exact defect this
                # contract exists to prevent, so a missing capability is an
                # infrastructure failure (as it already is for budget stops).
                if not callable(getattr(repo, "transition_run", None)):
                    raise AppError("RUN_FINALIZATION_UNAVAILABLE",
                                   "terminal run transition is unavailable", 503)
                repo.transition_run(run_id, "partial_success", expected_worker_id=worker_id, expected_attempt=run.get("attempt"), expected_lease_token=run.get("lease_token"), output=result, error=None, finished_at=datetime.now(UTC).isoformat())
            else:
                repo.mark_run_complete(run_id, result, worker_id=worker_id, attempt=run.get("attempt"), lease_token=run.get("lease_token"))
            return 0
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
