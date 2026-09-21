"""One finalizer, one product outcome, one terminal decision per run.

The properties under test, and why each one exists:

A. There is exactly ONE finalization path. `backend/worker/main.py` used to
   close a run from eight places, each writing its own status; the AST checks
   in section A prove none of them is left.
B. A V1 run's real semantic outcome survives the summary phase, the final
   checkpoint and a resume. A summary checkpoint used to resume straight into
   durable `completed` whatever the final builder had recorded.
C. Terminalization is idempotent and race-safe: a decision already won by a
   legitimate path is never overwritten, a repeat is a no-op, and `completed`
   -- the weakest claim any branch can make -- loses to every other terminal.
D. Stage D distinguishes "the worker executed successfully" from "the product
   result was semantically acceptable", and refuses the second on a
   technically clean run whose outcome is unusable, refused or unrecorded.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from backend import finalization as finalization_module
from backend.errors import AppError
from backend.budget import BudgetExceeded
from backend.finalization import (TERMINAL_AUTHORITY, FinalizationUnavailable,
                                  RunFinalizer, TerminalClaim,
                                  TerminalEvidenceUnavailable)
from backend.product_outcome import (BLOCKING_CODES, SEMANTIC_STATUSES,
                                     ProductOutcomeError, acceptance_problems,
                                     derive_product_outcome, not_produced_outcome,
                                     outcome_from_record, refused_outcome,
                                     safe_payload_reference)
from backend.runtime import SupabaseEventSink
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs
from backend.worker.main import execute_run

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE_D = REPO_ROOT / "scripts" / "release" / "stage-d"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def seeded_run(workflow_key: str = "vehicle_catalog_v1"):
    return seeded_run_with(MemoryRepository, workflow_key)


def catalog_document(*, status="complete", settled=2, review=0, rejected=0,
                     failed_agents=0, verifier="success", technical="success"):
    """A document shaped exactly like `build_final_json_python` emits."""
    models = [{"canonical_model_name": f"M{i}", "verification_status": "verified"}
              for i in range(settled)]
    models += [{"canonical_model_name": f"R{i}", "verification_status": "needs_review"}
               for i in range(review)]
    models += [{"canonical_model_name": f"X{i}", "verification_status": "rejected"}
               for i in range(rejected)]
    return {
        "manufacturer": "Hyundai", "market": "Israel", "period": "2024-2025",
        "status": status, "models": models,
        "needs_review": [m for m in models if m["verification_status"] == "needs_review"],
        "rejected": [m for m in models if m["verification_status"] == "rejected"],
        "failed_agents": [{"agent": f"a{i}", "error": "E"} for i in range(failed_agents)],
        "pipeline_quality": {"discovery": "success", "normalizer": "success",
                             "technical_enrichment": technical, "verifier": verifier,
                             "final_builder": "success", "data_depth": "full_technical"},
        "token_usage": {},
    }


def v1_envelope(document, *, declared=None):
    return {"status": declared if declared is not None else document["status"],
            "result": document, "summary": "sum", "results": {},
            "input_tokens": 1, "output_tokens": 1}


def v2_payload(*, kind="partial_result"):
    field = {"value": "1.6T", "provenance": {"claim_id": "c1", "source_id": "gov:1",
                                             "run_id": "r", "task_id": "t", "scope": {}}}
    if kind == "usable_result":
        return {"status": "complete", "result_kind": "usable_result",
                "fields": {"engine": [field]}, "needs_review": []}
    if kind == "partial_result":
        return {"status": "partial_success", "result_kind": "partial_result",
                "fields": {"engine": [field]},
                "needs_review": [{"field": "power", "reason": "UNVERIFIED"}]}
    return {"status": "partial_success", "result_kind": "no_usable_result",
            "fields": {}, "needs_review": [{"code": "NO_USABLE_RESULT"}]}


class Engine:
    def __init__(self, workflow_key, result):
        self.workflow_key = workflow_key
        self._result = result

    def run(self, run):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def finalizer_for(repo, run_id, engine="vehicle_catalog_v1"):
    run = repo.claim_run(run_id, "worker-1", lease_seconds=300)
    lease = {"worker_id": "worker-1", "attempt": run.get("attempt"),
             "lease_token": run.get("lease_token")}
    repo.transition_run(run_id, "running", expected_worker_id="worker-1",
                        expected_attempt=lease["attempt"],
                        expected_lease_token=lease["lease_token"])
    return RunFinalizer(repo=repo, run_id=run_id, engine=engine, lease_ctx=lease)


class Stop(BudgetExceeded):
    def __init__(self, code="RUN_DURATION_EXCEEDED", terminal="timed_out"):
        super().__init__(code, "limit reached", "run_timed_out", terminal)


TERMINAL_EVENT_TYPES = ("run_completed", "run_partial_success", "run_failed",
                        "run_cancelled")
PRODUCT_EVENT_TYPES = ("run_completed", "run_partial_success")


def terminal_events(repo):
    return [e for e in repo.run_events if e["event_type"] in TERMINAL_EVENT_TYPES]


class LegacyRepository(MemoryRepository):
    """A repository from before the atomic primitive existed.

    The finalizer must be correct against this too: it is the shape of every
    harness double, and of any deployment whose migrations trail its images.
    """

    finalize_run = None  # type: ignore[assignment]


def seeded_run_with(repo_class, workflow_key="swarm_v2"):
    repo = repo_class()
    project_id, user_id = uuid4(), uuid4()
    repo.seed_user(str(user_id))
    repo.seed_project(str(project_id), "finalization", "Finalization", [str(user_id)])
    repo.projects[str(project_id)]["workflow_key"] = workflow_key
    conversation = repo.create_conversation(project_id, "finalization")
    created = repo.create_message_and_run(
        conversation["id"], "finalize me", {}, user_id, f"key-{uuid4()}", "fingerprint",
        **identity_kwargs(repo, conversation["id"]))
    return repo, created["run"]["id"]


def inject_cancellation_after_the_decision_read(repo, run_id):
    """Land a cancellation request AFTER the finalizer reads the run's state
    and BEFORE it writes: exactly the window the blocker named."""
    original = repo.get_run
    injected = []

    def racing_get_run(candidate, user_id=None):
        row = original(candidate, user_id)
        if not injected:
            injected.append(row["status"])
            repo.request_cancellation(run_id)
        return row

    repo.get_run = racing_get_run
    return injected


def probe_style_record(repo, run_id):
    """The evidence record exactly as probe_db.py builds it: the run row, and
    the ProductOutcome copied from the LAST product terminal event."""
    outcome = None
    for event in repo.run_events:
        if event["event_type"] in PRODUCT_EVENT_TYPES and isinstance(
                event["payload"].get("product_outcome"), dict):
            outcome = event["payload"]["product_outcome"]
    record = {"stage_d_probe": "evidence", "ok": True, "run_id": str(run_id),
              "run": {"id": str(run_id), "status": repo.get_run(run_id)["status"]}}
    if outcome is not None:
        record["product_outcome"] = outcome
    return record


# ===========================================================================
# A. ONE finalizer: no terminal branch writes its own status
# ===========================================================================


def worker_source_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "backend" / "worker" / "main.py").read_text())


def test_the_worker_never_writes_a_terminal_status_itself():
    """Checked over the parsed code, so a comment cannot pass or fail it.

    Every terminal write in the worker used to be a direct repository call.
    They are all the finalizer's now -- if one comes back, this fails.
    """
    forbidden = {"mark_run_complete", "mark_run_failed"}
    offenders = [node.func.attr for node in ast.walk(worker_source_tree())
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr in forbidden]
    assert offenders == [], f"the worker still closes runs itself: {offenders}"


def test_the_worker_only_ever_transitions_a_run_to_a_non_terminal_state():
    from backend.runtime import TERMINAL_STATES

    terminal_writes = []
    for node in ast.walk(worker_source_tree()):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "transition_run"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and arg.value in TERMINAL_STATES:
                terminal_writes.append(arg.value)
    assert terminal_writes == [], (
        f"the worker writes terminal states directly: {terminal_writes}")


@pytest.mark.parametrize("workflow_key,result,expected", [
    ("vehicle_catalog_v1", v1_envelope(catalog_document()), "completed"),
    ("vehicle_catalog_v1", v1_envelope(catalog_document(status="partial_success",
                                                        review=1)), "partial_success"),
    ("swarm_v2", v2_payload(kind="usable_result"), "completed"),
    ("swarm_v2", v2_payload(kind="partial_result"), "partial_success"),
    ("swarm_v2", v2_payload(kind="no_usable_result"), "partial_success"),
])
def test_both_engines_terminalize_through_the_canonical_finalizer(
        monkeypatch, workflow_key, result, expected):
    """V1 and V2 reach their durable status by the SAME mechanism.

    The finalizer is monkeypatched to record every claim it is handed: if
    either engine's terminal path still wrote its own status, the run would
    reach a terminal state with no claim recorded here.
    """
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    seen: list[TerminalClaim] = []
    original = RunFinalizer.finalize

    def recording(self, claim):
        seen.append(claim)
        return original(self, claim)

    monkeypatch.setattr(RunFinalizer, "finalize", recording)
    repo, run_id = seeded_run(workflow_key)

    assert execute_run(run_id, repo, Engine(workflow_key, result)) == 0
    assert repo.get_run(run_id)["status"] == expected
    assert [claim.durable_status for claim in seen] == [expected]
    assert seen[0].reason == "product"
    assert seen[0].outcome.engine == workflow_key


@pytest.mark.parametrize("workflow_key", ["vehicle_catalog_v1", "swarm_v2"])
def test_every_non_product_terminal_path_also_goes_through_the_finalizer(
        monkeypatch, workflow_key):
    from backend.runtime import CancellationRequested

    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    reasons: list[str] = []
    original = RunFinalizer.finalize
    monkeypatch.setattr(RunFinalizer, "finalize",
                        lambda self, claim: (reasons.append(claim.reason),
                                             original(self, claim))[1])

    repo, run_id = seeded_run(workflow_key)
    repo.request_cancellation(run_id)
    assert execute_run(run_id, repo, Engine(workflow_key,
                                            CancellationRequested("RUN_CANCELLED"))) == 0
    assert reasons == ["cancelled"]
    assert repo.get_run(run_id)["status"] == "cancelled"


# ===========================================================================
# B. the V1 semantic outcome survives summary, checkpoint and resume
# ===========================================================================


def summary_checkpoint(run_id, document, *, failures=()):
    return {"id": "checkpoint-1", "run_id": str(run_id), "attempt": 1,
            "workflow_key": "vehicle_catalog_v1",
            "engine_version": "vehicle_catalog_v1.stage3", "phase": "summary",
            "completed_tasks": ["final_builder", "hebrew_summary"],
            "artifacts": {"final_builder": {"parsed": document},
                          "hebrew_summary": {"parsed": {"summary": "s"}}},
            "failures": list(failures),
            "token_usage": {"input_tokens": 10, "output_tokens": 5}}


def test_a_v1_partial_result_survives_the_summary_phase():
    """The engine's own report is the first place the outcome can be lost.

    `_apply_source_policy` recomputes needs_review AFTER the deterministic
    builder decided `status`, and the summary phase runs after that. The
    engine must report what the record says, not what the status field said
    before the record changed.
    """
    from backend.engines.vehicle_catalog_v1.engine import VehicleCatalogEngine

    downgraded = catalog_document(status="complete", settled=1, review=1)
    assert VehicleCatalogEngine._semantic_status(downgraded, []) == "partial_success"
    # An absorbed agent failure is enough on its own.
    assert VehicleCatalogEngine._semantic_status(
        catalog_document(status="complete"), [{"agent": "a", "error": "E"}]) == "partial_success"
    # A final document with no status at all is NOT a success.
    assert VehicleCatalogEngine._semantic_status({"models": []}, []) == "partial_success"
    # And a genuinely clean run still completes.
    assert VehicleCatalogEngine._semantic_status(catalog_document(), []) == "complete"


def test_a_v1_partial_result_survives_a_resume_from_the_summary_checkpoint(monkeypatch):
    """THE regression: this path called mark_run_complete unconditionally."""
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id = seeded_run("vehicle_catalog_v1")
    document = catalog_document(status="partial_success", settled=1, review=2,
                                verifier="partial")
    repo.checkpoints.append(summary_checkpoint(run_id, document))

    executed = []
    assert execute_run(run_id, repo, Engine("vehicle_catalog_v1",
                                            v1_envelope(catalog_document()))) == 0
    assert executed == [], "the fast path must not re-run the engine"

    run = repo.get_run(run_id)
    assert run["status"] == "partial_success"
    assert run["output"]["result"] is not None
    types = [event["event_type"] for event in repo.run_events]
    assert "run_partial_success" in types and "run_completed" not in types


def test_a_final_checkpoint_cannot_upgrade_the_product_quality_by_itself(monkeypatch):
    """A checkpoint carrying a CLEAN document but recorded failures stays partial.

    The checkpoint is evidence about a crashed attempt. Its final document can
    be clean while the attempt itself absorbed failures, and a resume must not
    be able to buy a better outcome than the run it resumes.
    """
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id = seeded_run("vehicle_catalog_v1")
    repo.checkpoints.append(summary_checkpoint(
        run_id, catalog_document(status="complete"),
        failures=[{"agent": "trims_years_agent", "error": "MODEL_OUTPUT_TRUNCATED"}]))

    assert execute_run(run_id, repo, Engine("vehicle_catalog_v1", {})) == 0
    assert repo.get_run(run_id)["status"] == "partial_success"

    terminal = [e for e in repo.run_events
                if e["event_type"] in ("run_completed", "run_partial_success")]
    assert len(terminal) == 1
    outcome = terminal[0]["payload"]["product_outcome"]
    assert outcome["semantic_status"] == "partial"
    assert {"code": "FAILED_AGENTS", "count": 1} in outcome["blocking"]


def test_a_checkpoint_with_no_recorded_status_is_not_read_as_success(monkeypatch):
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id = seeded_run("vehicle_catalog_v1")
    document = catalog_document()
    document.pop("status")
    repo.checkpoints.append(summary_checkpoint(run_id, document))

    assert execute_run(run_id, repo, Engine("vehicle_catalog_v1", {})) == 0
    assert repo.get_run(run_id)["status"] == "partial_success"


# ===========================================================================
# C. terminal races and idempotence
# ===========================================================================


def test_the_authority_order_makes_completed_the_weakest_claim():
    assert TERMINAL_AUTHORITY["cancelled"] > TERMINAL_AUTHORITY["budget_exhausted"]
    assert TERMINAL_AUTHORITY["budget_exhausted"] == TERMINAL_AUTHORITY["timed_out"]
    assert TERMINAL_AUTHORITY["timed_out"] > TERMINAL_AUTHORITY["failed"]
    assert TERMINAL_AUTHORITY["failed"] > TERMINAL_AUTHORITY["partial_success"]
    assert TERMINAL_AUTHORITY["partial_success"] > TERMINAL_AUTHORITY["completed"]
    assert min(TERMINAL_AUTHORITY, key=TERMINAL_AUTHORITY.get) == "completed"


@pytest.mark.parametrize("winner,claim", [
    ("budget_exhausted", lambda: TerminalClaim.budget_stop(
        "swarm_v2", Stop("MODEL_CALL_LIMIT_REACHED", "budget_exhausted"))),
    ("timed_out", lambda: TerminalClaim.budget_stop("swarm_v2", Stop())),
])
def test_a_noted_safety_stop_outranks_a_later_completion(winner, claim):
    """A rail that tripped first decides, whichever branch reaches the writer."""
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    finalizer.note(claim())

    result = finalizer.finalize(TerminalClaim.product("swarm_v2",
                                                      v2_payload(kind="usable_result")))

    assert result.status == winner
    assert repo.get_run(run_id)["status"] == winner


def test_a_live_rail_outranks_a_completion_even_if_it_trips_after_the_check():
    """The claim source is consulted INSIDE the decision lock.

    This is the window the old `if tracker.stop is not None:` check could not
    close: a rail tripping on a worker thread after that line and before the
    write.
    """
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    tripped: list[Stop] = []
    finalizer.add_claim_source(
        lambda: TerminalClaim.budget_stop("swarm_v2", tripped[0]) if tripped else None)

    tripped.append(Stop("COST_LIMIT_REACHED", "budget_exhausted"))
    result = finalizer.finalize(TerminalClaim.product("swarm_v2",
                                                      v2_payload(kind="usable_result")))

    assert result.status == "budget_exhausted"
    assert repo.get_run(run_id)["status"] == "budget_exhausted"


def test_a_claim_source_that_raises_never_ends_the_run():
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")

    def broken():
        raise RuntimeError("the net itself failed")

    finalizer.add_claim_source(broken)
    result = finalizer.finalize(TerminalClaim.product("swarm_v2",
                                                      v2_payload(kind="usable_result")))
    assert result.status == "completed"


def test_completed_cannot_overwrite_a_cancellation_already_accepted():
    """`cancellation_requested` admits only `cancelled` and `failed`.

    The product is not thrown away -- it stays as the run's output -- but the
    decision to stop is honoured, and the run is not recorded as a success.
    This used to raise INVALID_RUN_TRANSITION out of execute_run, which Cloud
    Run reads as a failed task and relaunches.
    """
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    repo.request_cancellation(run_id)

    payload = v2_payload(kind="usable_result")
    result = finalizer.finalize(TerminalClaim.product("swarm_v2", payload))

    assert result.status == "cancelled"
    run = repo.get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["output"] == payload, "the work produced before cancellation was lost"
    assert run["error"]["code"] == "RUN_CANCELLED_AFTER_RESULT"


def test_a_terminal_state_another_path_already_won_is_adopted_not_overwritten():
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    # Another legitimate path (a previous attempt, the operator) got there.
    repo.transition_run(run_id, "budget_exhausted",
                        expected_worker_id="worker-1",
                        expected_attempt=repo.get_run(run_id)["attempt"],
                        expected_lease_token=repo.get_run(run_id)["lease_token"],
                        error={"code": "COST_LIMIT_REACHED", "message": "x"})

    result = finalizer.finalize(TerminalClaim.product("swarm_v2",
                                                      v2_payload(kind="usable_result")))

    assert result.already_terminal and not result.wrote
    assert result.status == "budget_exhausted"
    assert repo.get_run(run_id)["status"] == "budget_exhausted"


def test_duplicate_finalization_is_idempotent():
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    payload = v2_payload(kind="partial_result")
    claim = TerminalClaim.product("swarm_v2", payload)

    first = finalizer.finalize(claim)
    second = finalizer.finalize(TerminalClaim.product("swarm_v2", dict(payload)))

    assert first.wrote and first.status == "partial_success"
    assert second.duplicate and not second.wrote and second.idempotent
    assert second.status == "partial_success"
    terminal = [e for e in repo.run_events
                if e["event_type"] in ("run_completed", "run_partial_success")]
    assert len(terminal) == 1, "a duplicate finalization emitted a second claim"


def test_a_different_later_claim_never_overwrites_the_decision_in_force():
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    finalizer.finalize(TerminalClaim.budget_stop("swarm_v2", Stop()))

    late = finalizer.finalize(TerminalClaim.product("swarm_v2",
                                                    v2_payload(kind="usable_result")))

    assert late.superseded and not late.wrote and late.status == "timed_out"
    assert repo.get_run(run_id)["status"] == "timed_out"


def test_a_stale_worker_whose_write_is_rejected_never_reports_an_outcome():
    """A rejected write on a NON-terminal run means this worker lost the run."""
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    finalizer.lease_ctx = {**finalizer.lease_ctx, "lease_token": "not-the-lease"}

    with pytest.raises(AppError):
        finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload()))
    assert repo.get_run(run_id)["status"] == "running"
    # The claim lost, so it never got to claim anything: no terminal event.
    assert terminal_events(repo) == []


def test_a_repository_that_cannot_express_the_decision_is_an_infrastructure_failure():
    """Never silently downgrade a partial, cancelled or stopped run to complete."""

    class NoTransition:
        def get_run(self, run_id, user_id=None):
            return {"id": run_id, "status": "running"}

        def mark_run_complete(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("a partial run was reported as completed")

    finalizer = RunFinalizer(repo=NoTransition(), run_id=uuid4(),
                             engine="swarm_v2", lease_ctx={})
    with pytest.raises(FinalizationUnavailable):
        finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload()))


def test_concurrent_finalizations_produce_exactly_one_terminal_decision():
    import threading

    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    start = threading.Barrier(6)
    results: list = []
    lock = threading.Lock()

    def close(claim):
        start.wait(timeout=10)
        outcome = finalizer.finalize(claim)
        with lock:
            results.append(outcome)

    claims = [TerminalClaim.product("swarm_v2", v2_payload(kind="usable_result")),
              TerminalClaim.product("swarm_v2", v2_payload(kind="partial_result")),
              TerminalClaim.budget_stop("swarm_v2", Stop()),
              TerminalClaim.failure("swarm_v2", "X", "y"),
              TerminalClaim.cancelled("swarm_v2"),
              TerminalClaim.product("swarm_v2", v2_payload(kind="no_usable_result"))]
    threads = [threading.Thread(target=close, args=(claim,)) for claim in claims]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len([r for r in results if r.wrote]) == 1
    assert len({r.status for r in results}) == 1
    assert repo.get_run(run_id)["status"] == results[0].status
    terminal = [e for e in repo.run_events if e["event_type"].startswith("run_")
                and e["event_type"] in ("run_completed", "run_partial_success",
                                        "run_failed", "run_cancelled")]
    assert len(terminal) <= 1


def test_a_budget_terminal_does_not_emit_a_second_terminal_event():
    """The tracker already emitted the CAUSE event when the rail tripped."""
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    finalizer.finalize(TerminalClaim.budget_stop("swarm_v2", Stop()))
    assert [e["event_type"] for e in repo.run_events
            if e["event_type"].startswith("run_")] == []


# ===========================================================================
# the canonical ProductOutcome itself
# ===========================================================================


def test_technical_completion_is_never_read_as_semantic_success():
    every_stage_returned = v1_envelope(
        catalog_document(status="complete", settled=3, review=2, failed_agents=1))
    outcome = derive_product_outcome("vehicle_catalog_v1", every_stage_returned)
    assert outcome.semantic_status == "partial"
    assert outcome.usability == "partial"
    assert set(outcome.blocking_codes) == {"OUTSTANDING_REVIEW_ITEMS", "FAILED_AGENTS"}
    assert outcome.durable_product_status() == "partial_success"


def test_a_catalog_with_no_settled_model_is_unusable_not_complete():
    outcome = derive_product_outcome(
        "vehicle_catalog_v1", v1_envelope(catalog_document(status="complete", settled=0)))
    assert outcome.semantic_status == "unusable"
    assert not outcome.is_usable
    assert "NO_USABLE_RESULT" in outcome.blocking_codes
    assert outcome.durable_product_status() == "partial_success"


def test_a_degraded_stage_blocks_a_complete_outcome():
    for quality in ({"verifier": "partial"}, {"technical": "partial"}):
        outcome = derive_product_outcome(
            "vehicle_catalog_v1", v1_envelope(catalog_document(**quality)))
        assert outcome.semantic_status == "partial", quality


def test_coverage_is_a_real_ratio_and_never_invents_one():
    outcome = derive_product_outcome(
        "vehicle_catalog_v1", v1_envelope(catalog_document(settled=3, review=1)))
    assert outcome.coverage.produced == 3 and outcome.coverage.outstanding == 1
    assert outcome.coverage.ratio == 0.75
    assert not_produced_outcome("swarm_v2").coverage.ratio is None


def test_a_swarm_v2_payload_the_contract_refuses_is_refused_here_too():
    outcome = derive_product_outcome("swarm_v2", {"status": "complete",
                                                  "result_kind": "usable_result",
                                                  "fields": {}, "needs_review": []})
    assert outcome.semantic_status == "refused"
    assert outcome.blocking_codes == ("OUTCOME_CONTRACT_VIOLATION",)
    assert outcome.durable_product_status() == "failed"


def test_the_payload_reference_describes_without_reproducing():
    secret_ish = {"status": "complete", "result": {"models": ["a private value"]}}
    reference = safe_payload_reference(secret_ish)
    rendered = json.dumps(reference.as_record())
    assert reference.present and len(reference.digest) == 64
    assert "a private value" not in rendered
    assert reference.shape == ("result", "status")
    # The same payload always references identically; a different one does not.
    assert reference.digest == safe_payload_reference(dict(secret_ish)).digest
    assert reference.digest != safe_payload_reference({"status": "complete"}).digest


def test_every_outcome_record_round_trips_through_the_reader():
    for outcome in (derive_product_outcome("vehicle_catalog_v1",
                                           v1_envelope(catalog_document())),
                    derive_product_outcome("swarm_v2", v2_payload()),
                    refused_outcome("swarm_v2"), not_produced_outcome("swarm_v2")):
        assert outcome_from_record(outcome.as_record()) == outcome


@pytest.mark.parametrize("record", [
    {"semantic_status": "amazing"},
    {"semantic_status": "complete", "blocking": [{"code": "MADE_UP", "count": 1}]},
    {"semantic_status": "complete", "coverage": {"produced": "lots"}},
    "not a mapping",
])
def test_an_outcome_record_this_contract_did_not_produce_is_refused(record):
    with pytest.raises(ProductOutcomeError):
        outcome_from_record(record)


def test_the_blocking_vocabulary_and_semantic_vocabulary_stay_static():
    assert "OUTSTANDING_REVIEW_ITEMS" in BLOCKING_CODES
    assert set(SEMANTIC_STATUSES) == {"complete", "partial", "unusable", "refused",
                                      "not_produced"}
    outcome = derive_product_outcome("vehicle_catalog_v1", v1_envelope(catalog_document()))
    assert set(outcome.blocking_codes) <= BLOCKING_CODES


def test_the_v2_useful_table_and_the_canonical_reader_agree():
    """Two answers to "is this useful?" is exactly the drift being removed."""
    from backend.engines.swarm_v2.outcome import is_useful_outcome

    for kind in ("usable_result", "partial_result", "no_usable_result"):
        outcome = derive_product_outcome("swarm_v2", v2_payload(kind=kind))
        assert is_useful_outcome(outcome.durable_product_status(),
                                 outcome.result_kind) is outcome.is_usable, kind


def test_the_canonical_module_performs_no_io():
    """Checked over the parsed code: an outcome is a pure reading of a payload."""
    tree = ast.parse((REPO_ROOT / "backend" / "product_outcome.py").read_text())
    imported = {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}
    assert imported <= {"hashlib", "json", "re", "collections", "dataclasses",
                        "typing", "__future__", "backend"}


def test_finalization_never_imports_an_engine_at_module_scope():
    tree = ast.parse((REPO_ROOT / "backend" / "finalization.py").read_text())
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    modules = {getattr(node, "module", "") or "" for node in top_level}
    assert not any(module.startswith("backend.engines") for module in modules)
    assert finalization_module.TERMINAL_AUTHORITY  # the module imports cleanly


# ===========================================================================
# D. Stage D: technical success is not semantic acceptance
# ===========================================================================


def run_semantic_gate(record, *, args=()):
    lines = "" if record is None else json.dumps(record) + "\n"
    return subprocess.run(
        [sys.executable, str(STAGE_D / "semantic_acceptance.py"), *args],
        input="noise line\n" + lines, capture_output=True, text=True, timeout=120)


def evidence_record(status="completed", outcome=None):
    record = {"stage_d_probe": "evidence", "ok": True, "run_id": "r-1",
              "run": {"id": "r-1", "status": status}}
    if outcome is not None:
        record["product_outcome"] = outcome
    return record


def test_a_technically_successful_run_with_a_partial_product_is_accepted():
    """A partial product with real verified content is the expected shape."""
    outcome = derive_product_outcome("swarm_v2", v2_payload(kind="partial_result"))
    done = run_semantic_gate(evidence_record("partial_success", outcome.as_record()))
    assert done.returncode == 0, done.stdout
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert verdict["ok"] is True and verdict["usability"] == "partial"


@pytest.mark.parametrize("kind,status", [
    ("no_usable_result", "partial_success"),
])
def test_a_technically_successful_run_with_an_unusable_product_is_refused(kind, status):
    outcome = derive_product_outcome("swarm_v2", v2_payload(kind=kind))
    done = run_semantic_gate(evidence_record(status, outcome.as_record()))
    assert done.returncode == 1
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert verdict["ok"] is False
    assert "not semantically acceptable" in verdict["reason"]
    assert verdict["usability"] == "unusable"


def test_a_completed_run_whose_outcome_is_refused_does_not_pass():
    outcome = refused_outcome("swarm_v2")
    done = run_semantic_gate(evidence_record("completed", outcome.as_record()))
    assert done.returncode == 1
    assert json.loads(done.stdout.strip().splitlines()[-1])["ok"] is False


def test_a_completed_run_with_no_recorded_outcome_fails_closed():
    done = run_semantic_gate(evidence_record("completed", None))
    assert done.returncode == 1
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert "recorded NO canonical ProductOutcome" in verdict["reason"]


def test_a_missing_or_unreadable_evidence_record_fails_closed():
    for record in (None, {"stage_d_probe": "preflight", "ok": True}):
        done = run_semantic_gate(record)
        assert done.returncode == 1
        assert "failing closed" in json.loads(done.stdout.strip().splitlines()[-1])["reason"]


def test_a_forged_outcome_record_is_refused_rather_than_believed():
    done = run_semantic_gate(evidence_record(
        "completed", {"semantic_status": "perfect", "usability": "usable"}))
    assert done.returncode == 1
    assert "not one this repository" in json.loads(
        done.stdout.strip().splitlines()[-1])["reason"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out",
                                    "budget_exhausted"])
def test_a_non_product_terminal_state_is_never_semantically_accepted(status):
    done = run_semantic_gate(evidence_record(status, None))
    assert done.returncode == 1
    assert "is not a product outcome" in json.loads(
        done.stdout.strip().splitlines()[-1])["reason"]


def test_require_complete_refuses_a_truthful_partial():
    outcome = derive_product_outcome("swarm_v2", v2_payload(kind="partial_result"))
    done = run_semantic_gate(evidence_record("partial_success", outcome.as_record()),
                             args=("--require-complete",))
    assert done.returncode == 1
    assert "requires a complete product result" in json.loads(
        done.stdout.strip().splitlines()[-1])["reason"]


def test_the_semantic_gate_is_wired_into_the_evidence_gate_and_is_not_optional():
    text = (STAGE_D / "06-collect-evidence.sh").read_text()
    assert "semantic_acceptance.py" in text
    gate = [line for line in text.splitlines() if "semantic_acceptance.py" in line
            and not line.lstrip().startswith("#")]
    assert gate, "the semantic gate is only mentioned in a comment"
    assert not any("|| true" in line or "|| echo" in line for line in gate)


def test_the_semantic_gate_does_not_re_derive_the_outcome_in_the_probe():
    """The probe COPIES what the finalizer recorded; it never judges.

    A probe that derived its own outcome would be a second implementation of
    the semantic rule, free to disagree with the one that actually decided the
    run's terminal status.
    """
    probe = (STAGE_D / "probe_db.py").read_text()
    assert '"product_outcome"' in probe
    assert "acceptance_problems" not in probe
    assert "semantic_status" not in probe


def test_the_worker_records_the_canonical_outcome_the_stage_d_gate_reads(monkeypatch):
    """End to end: what the worker writes is what the gate accepts."""
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id = seeded_run("vehicle_catalog_v1")
    result = v1_envelope(catalog_document(status="partial_success", settled=0))

    assert execute_run(run_id, repo, Engine("vehicle_catalog_v1", result)) == 0
    assert repo.get_run(run_id)["status"] == "partial_success"

    terminal = [e for e in repo.run_events
                if e["event_type"] == "run_partial_success"][-1]
    recorded = terminal["payload"]["product_outcome"]
    assert acceptance_problems(outcome_from_record(recorded)), (
        "a run that verified nothing passed semantic acceptance")

    done = run_semantic_gate(evidence_record("partial_success", recorded))
    assert done.returncode == 1


# ===========================================================================
# E. the terminal event exists only for the decision that durably won
# ===========================================================================
#
# Every test here runs against the atomic path, because it is the only path
# there is: the repository offers `finalize_run` (production does, through
# migration 20260920000200) or the finalizer refuses. A repository without it
# is covered on its own, below.

PATHS = [pytest.param(MemoryRepository, id="atomic")]


@pytest.mark.parametrize("repo_class", PATHS)
def test_a_cancellation_between_the_decision_and_the_commit_wins(repo_class):
    """The blocker, replayed deterministically.

    1. the finalizer reads `running` and decides `completed`;
    2. a cancellation request lands;
    3. the `completed` write is rejected -- and because the event follows the
       write, no `run_completed` was ever recorded;
    4. the run is re-read once and decided again under the state that holds.
    """
    repo, run_id = seeded_run_with(repo_class)
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    injected = inject_cancellation_after_the_decision_read(repo, run_id)
    payload = v2_payload(kind="usable_result")

    result = finalizer.finalize(TerminalClaim.product("swarm_v2", payload))

    assert injected == ["running"], "the cancellation must land after the decision read"
    # 2/3. cancellation wins, and the durable event and status AGREE.
    run = repo.get_run(run_id)
    assert result.status == run["status"] == "cancelled"
    assert result.wrote and result.evidence_recorded
    assert [e["event_type"] for e in terminal_events(repo)] == ["run_cancelled"]
    assert terminal_events(repo)[0]["payload"]["code"] == "RUN_CANCELLED_AFTER_RESULT"
    # the product was not thrown away, and it never claimed to have won.
    assert run["output"] == payload
    assert not any(e["event_type"] in PRODUCT_EVENT_TYPES for e in repo.run_events)
    assert not any("product_outcome" in e["payload"] for e in repo.run_events)


@pytest.mark.parametrize("repo_class", PATHS)
def test_a_losing_product_claim_leaves_no_product_terminal_event(repo_class):
    """A claim that loses to a state it cannot legally follow records nothing.

    Here the write is rejected because the lease is gone; there is no legal
    re-decision, the error escapes, and the event stream holds NO terminal
    claim -- which is the whole difference from emitting first.
    """
    repo, run_id = seeded_run_with(repo_class)
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    finalizer.lease_ctx = {**finalizer.lease_ctx, "lease_token": "reclaimed"}

    with pytest.raises(AppError):
        finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="usable_result")))

    assert repo.get_run(run_id)["status"] == "running"
    assert terminal_events(repo) == []
    assert not finalizer.decided


@pytest.mark.parametrize("repo_class", PATHS)
def test_the_race_is_closed_end_to_end_through_the_worker(monkeypatch, repo_class):
    """Through execute_run: the worker exits 0, the run is cancelled, and the
    event stream carries exactly one terminal claim that agrees with it."""
    monkeypatch.delenv("MILO_ENABLE_PAID_EXECUTION", raising=False)
    repo, run_id = seeded_run_with(repo_class)
    original_get_run = repo.get_run
    armed = {"after_engine": False}

    def racing_get_run(candidate, user_id=None):
        row = original_get_run(candidate, user_id)
        if armed["after_engine"] and row["status"] == "running":
            armed["after_engine"] = False
            repo.request_cancellation(run_id)
        return row

    repo.get_run = racing_get_run

    class RacingEngine:
        workflow_key = "swarm_v2"

        def run(self, run):
            armed["after_engine"] = True   # the next read is the decision read
            return v2_payload(kind="usable_result")

    assert execute_run(run_id, repo, RacingEngine()) == 0
    run = repo.get_run(run_id)
    assert run["status"] == "cancelled"
    assert [e["event_type"] for e in terminal_events(repo)] == ["run_cancelled"]
    assert run["output"] == v2_payload(kind="usable_result")


@pytest.mark.parametrize("repo_class", PATHS)
def test_stage_d_cannot_consume_a_superseded_product_outcome(repo_class):
    """The probe copies the ProductOutcome from the last product terminal
    event, and the host gate judges it. After the race there IS no such
    event, the run is `cancelled`, and the gate refuses -- there is nothing
    superseded left to consume."""
    repo, run_id = seeded_run_with(repo_class)
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    inject_cancellation_after_the_decision_read(repo, run_id)
    finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="usable_result")))

    record = probe_style_record(repo, run_id)
    assert "product_outcome" not in record
    done = run_semantic_gate(record)
    assert done.returncode == 1
    verdict = json.loads(done.stdout.strip().splitlines()[-1])
    assert verdict["terminal_status"] == "cancelled"
    assert "is not a product outcome" in verdict["reason"]
    # The mirror above must match the probe's own extraction rule.
    probe = (STAGE_D / "probe_db.py").read_text()
    assert 'event.get("event_type") not in ("run_completed", "run_partial_success")' in probe


def test_the_atomic_primitive_commits_the_status_and_its_event_in_one_call():
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    atomic_calls = []
    original = repo.finalize_run

    def spying(*args, **kwargs):
        atomic_calls.append((args[1], args[2], (args[3] or {}).get("type")))
        return original(*args, **kwargs)

    repo.finalize_run = spying

    result = finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="partial_result")))

    # The status, the expected status and the terminal event all travel in ONE
    # call to the primitive: there is no second write to lose.
    assert atomic_calls == [("partial_success", "running", "run_partial_success")]
    assert result.evidence_recorded and result.wrote
    assert [e["event_type"] for e in terminal_events(repo)] == ["run_partial_success"]


def test_an_atomic_finalization_that_fails_leaves_nothing_behind():
    """The primitive commits both or neither: a failure inside it is not a
    half-terminal run and not a dangling event."""
    repo, run_id = seeded_run("swarm_v2")
    finalizer = finalizer_for(repo, run_id, "swarm_v2")

    def broken(*args, **kwargs):
        raise AppError("REPOSITORY_ERROR", "guarded persistence operation failed", 502)

    repo.finalize_run = broken
    with pytest.raises(AppError) as excinfo:
        finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="usable_result")))
    assert excinfo.value.code == "REPOSITORY_ERROR"
    assert repo.get_run(run_id)["status"] == "running"
    assert terminal_events(repo) == []
    assert not finalizer.decided


def test_a_repository_without_the_atomic_primitive_cannot_terminalize_at_all():
    """There is no fallback, and that is the point.

    Terminal status and terminal evidence are ONE write. A repository whose
    migrations trail its images cannot make that write, and the finalizer
    refuses rather than splitting it into a transition plus a separate event --
    which is exactly the split-brain terminalization Console 3 removed. The
    refusal is a 503-class signal, and nothing durable moves.
    """
    repo, run_id = seeded_run_with(LegacyRepository)
    finalizer = finalizer_for(repo, run_id, "swarm_v2")

    for claim in (TerminalClaim.product("swarm_v2", v2_payload(kind="partial_result")),
                  TerminalClaim.failure("swarm_v2", "X_FAILED", "x failed")):
        with pytest.raises(FinalizationUnavailable) as raised:
            finalizer.finalize(claim)
        assert raised.value.code == "RUN_FINALIZATION_UNAVAILABLE"

    from backend.runtime import TERMINAL_STATES

    assert repo.get_run(run_id)["status"] not in TERMINAL_STATES
    assert terminal_events(repo) == []


def test_the_finalizer_carries_no_second_event_path_to_fall_back_to():
    """Structural: the sink the fallback used is gone from the source.

    A dead `event_sink` left on the finalizer is an invitation to reinstate the
    split write, so it does not exist -- the terminal event travels with the
    status through the atomic primitive, or not at all.
    """
    source = inspect.getsource(finalization_module)
    assert "event_sink" not in source
    assert not hasattr(RunFinalizer(repo=None, run_id=None, engine="", lease_ctx={}),
                       "event_sink")


def test_the_supervisor_shadow_observes_only_durable_terminal_events():
    repo, run_id = seeded_run_with(MemoryRepository)
    finalizer = finalizer_for(repo, run_id, "swarm_v2")
    observed = []
    finalizer.observer = lambda kind, payload: observed.append(kind)

    # The terminal event is committed by the atomic primitive or not at all, so
    # a finalization that does not commit has nothing for the shadow to see.
    def refusing_finalize(*args, **kwargs):
        raise AppError("RUN_LEASE_LOST", "terminal write rejected", 409)

    finalizer.repo.finalize_run = refusing_finalize
    with pytest.raises(AppError):
        finalizer.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="partial_result")))
    assert observed == [], "an event that never became durable was observed"
    assert terminal_events(repo) == []

    repo2, run_id2 = seeded_run_with(MemoryRepository)
    finalizer2 = finalizer_for(repo2, run_id2, "swarm_v2")
    observed2 = []
    finalizer2.observer = lambda kind, payload: observed2.append(kind)
    finalizer2.finalize(TerminalClaim.product("swarm_v2", v2_payload(kind="partial_result")))
    assert observed2 == ["run_partial_success"]


def test_the_memory_primitive_matches_the_database_contract():
    """Parity checks for the in-memory `finalize_run`, so the atomic path the
    suite exercises is the one production runs."""
    repo, run_id = seeded_run("swarm_v2")
    run = repo.claim_run(run_id, "worker-1", lease_seconds=300)
    lease = {"worker_id": "worker-1", "attempt": run["attempt"], "lease_token": run["lease_token"]}
    repo.transition_run(run_id, "running", expected_worker_id="worker-1",
                        expected_attempt=lease["attempt"], expected_lease_token=lease["lease_token"])
    # a non-terminal target is refused outright
    with pytest.raises(AppError, match="not a terminal"):
        repo.finalize_run(run_id, "waiting", "running", None, **lease)
    # a moved status is a conflict, and NOTHING is written
    repo.request_cancellation(run_id)
    with pytest.raises(AppError) as excinfo:
        repo.finalize_run(run_id, "completed", "running",
                          {"type": "run_completed", "message": "m", "payload": {}}, **lease,
                          output={"x": 1})
    assert excinfo.value.code == "RUN_TRANSITION_CONFLICT"
    assert repo.get_run(run_id)["status"] == "cancellation_requested"
    assert repo.get_run(run_id).get("output") is None and terminal_events(repo) == []
    # under the right expectation both land together
    repo.finalize_run(run_id, "cancelled", "cancellation_requested",
                      {"type": "run_cancelled", "message": "m", "payload": {"code": "C"}}, **lease,
                      output={"x": 1}, error={"code": "C", "message": "c"})
    assert repo.get_run(run_id)["status"] == "cancelled"
    assert [e["event_type"] for e in terminal_events(repo)] == ["run_cancelled"]
    assert terminal_events(repo)[0]["payload"] == {"code": "C"}
