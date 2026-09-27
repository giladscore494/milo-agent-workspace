"""E': prepare ONE Mapping Plan revision from the website.

The route, in order
-------------------

`POST /work-scopes/{id}/preparations` names the revision and digest the person
was shown. The server then:

1.  authorizes -- the same non-disclosing membership check every plan route
    performs (`service._authorized`);
2.  refuses a stale request (409 `WORK_SCOPE_STALE`) and a project whose
    engine reads no plan;
3.  refuses, before anything is written, unless the capture job runs the
    deployed release image (`prepare_trigger`);
4.  CLAIMS the revision's ONE preparation request under the plan's row lock
    (`request_work_scope_preparation`). Only the caller answered `claimed`
    goes on; every other caller -- the second of two concurrent clicks, a
    reload, a second member -- is answered with the existing preparation or
    the attempt in flight, and nothing else starts;
5.  makes the operator capture run through the entrypoint's own
    `prepare_capture_run` (or reuses the attempt's run that no capture ever
    claimed), records it, and executes the capture job with the invocation
    `backend/capture_invocation.py` builds -- the same one the operator
    script runs -- then records what the trigger answered.

It never starts a batch, never creates a product run, never calls a model and
never enables paid execution or promotion: the only execution it can start is
the capture job's scoped preparation, and the capture entrypoint refuses paid
execution and promotion on its own.

The status is derived, never observed
-------------------------------------

`GET /work-scopes/{id}/preparation` reads ONE bounded set of durable facts
(`work_scope_preparation_state`) and `derive_status` turns them into
Preparing / Prepared / Failed / Stale -- from the preparation row, the
request row and the capture run's durable status. Never from an exit code, an
execution document or a log (plan 7.1.5: a capture is judged by the database
and the prepared gate, never by what a process printed). A failure is shown as
a static reason code.
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from uuid import UUID

from backend.capture_invocation import InvocationError, work_scope_preparation
from backend.errors import AppError, NotFoundError
from backend.execution_guard import is_stage_enabled

from . import batches as work_scope_batches
from . import prepare_trigger as trig
from . import service as work_scopes

#: The server flag behind the Prepare route. Default off, pinned off at Stage
#: A, and enforced by `ExecutionSurfaceGuardMiddleware` before a body is read.
PREPARATION_REQUESTS_FLAG = "MILO_ENABLE_WORK_SCOPE_PREPARATION_REQUESTS"

#: How long an attempt may go without a capture claiming its run before it is
#: judged never to have started (and may be retried). The same quiet period
#: `reconcile_lost_launch` requires; the database enforces it on the claim.
START_GRACE_SECONDS = 900

#: The four states the website shows, plus the one before any request.
STATES = ("not_requested", "preparing", "prepared", "failed", "stale")

#: Static reason codes for a failed preparation. The capture entrypoint's own
#: static codes (`operator_capture.CAPTURE_REASONS` and the vocabularies it
#: reports) pass through as themselves; nothing else ever does.
FAILURE_REASONS: Mapping[str, str] = {
    "PREPARATION_TRIGGER_FAILED": "the capture job could not be started",
    "PREPARATION_NOT_STARTED": "the capture job never started this preparation",
    "PREPARATION_INTERRUPTED": "the capture stopped without finishing; an operator reconciles it",
    "PREPARATION_CANCELLED": "the preparation was cancelled",
    "PREPARATION_INCOMPLETE": "the capture finished without preparing this revision",
    "PREPARATION_FAILED": "the preparation failed",
}

#: Why Prepare is not offered for a revision right now.
BLOCKERS: Mapping[str, str] = {
    "preparation_disabled": "preparing from the website is not enabled on this server",
    "workflow_not_supported": "this project's engine does not read a mapping plan",
    "stale": "the plan changed since this revision was read",
    "prepared": "this revision is already prepared",
    "in_flight": "this revision is being prepared",
    "needs_operator": "the last preparation stopped part way; an operator reconciles it",
}

REQUEST_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_PREPARATION_DISABLED": BLOCKERS["preparation_disabled"],
    trig.JOB_NOT_RELEASE: "the capture job does not run the deployed release image",
    trig.JOB_UNREADABLE: "the capture job could not be read, so it was not started",
    "WORK_SCOPE_PREPARATION_TRIGGER_FAILED": FAILURE_REASONS["PREPARATION_TRIGGER_FAILED"],
    "WORK_SCOPE_PREPARATION_NEEDS_OPERATOR": BLOCKERS["needs_operator"],
}

_TERMINAL = frozenset({"completed", "partial_success", "failed", "cancelled", "timed_out",
                       "budget_exhausted"})


def _refusal(code: str, status: int) -> AppError:
    return AppError(code, REQUEST_REASONS[code], status)


def _capture_reason(code: Any) -> str | None:
    """A capture's own static code, only when the entrypoint's vocabulary owns it."""
    if not isinstance(code, str):
        return None
    from backend.catalog.operator_capture import CAPTURE_REASONS, safe_message

    known = code in CAPTURE_REASONS or safe_message(code) != CAPTURE_REASONS[
        "CAPTURE_UNEXPECTED_FAILURE"]
    return code if known else None


def derive_status(facts: Mapping[str, Any], *,
                  grace_seconds: int = START_GRACE_SECONDS) -> dict[str, Any]:
    """Pure: the state of one revision's preparation, from durable facts only.

    Returns `state`, `reason_code` (Failed only) and `retryable` (whether a
    new request may start another attempt).
    """
    def result(state: str, reason: str | None = None, retryable: bool = False) -> dict[str, Any]:
        return {"state": state, "reason_code": reason, "retryable": retryable}

    if facts.get("closed") is True or facts.get("head_revision") != facts.get("revision") \
            or facts.get("head_digest") != facts.get("digest"):
        return result("stale")
    if isinstance(facts.get("preparation"), Mapping):
        return result("prepared")
    request = facts.get("request")
    if not isinstance(request, Mapping):
        return result("not_requested")
    trigger = request.get("trigger_state")
    if trigger == "trigger_failed":
        return result("failed", "PREPARATION_TRIGGER_FAILED", True)
    if trigger == "claimed":
        if int(request.get("claimed_seconds") or 0) > grace_seconds:
            return result("failed", "PREPARATION_NOT_STARTED", True)
        return result("preparing")
    if trigger not in ("triggered", "trigger_unknown"):
        raise ValueError("unknown trigger state")
    run = facts.get("run")
    if not isinstance(run, Mapping):
        return result("preparing")
    status = run.get("status")
    if status in _TERMINAL:
        if status == "cancelled":
            return result("failed", "PREPARATION_CANCELLED", True)
        if status in ("completed", "partial_success"):
            return result("failed", "PREPARATION_INCOMPLETE", True)
        return result("failed", _capture_reason(run.get("error_code")) or "PREPARATION_FAILED",
                      True)
    if run.get("claimed") is not True:
        waited = request.get("triggered_seconds")
        if waited is None:
            waited = request.get("claimed_seconds")
        if status == "queued" and int(waited or 0) > grace_seconds:
            return result("failed", "PREPARATION_NOT_STARTED", True)
        return result("preparing")
    expired = run.get("lease_expired_seconds")
    if isinstance(expired, int) and not isinstance(expired, bool) and expired > grace_seconds:
        return result("failed", "PREPARATION_INTERRUPTED", False)
    return result("preparing")


def _enabled() -> bool:
    return is_stage_enabled(PREPARATION_REQUESTS_FLAG)


def server_can_prepare(trigger: Any) -> bool:
    """Whether THIS server can prepare anything at all: the flag and a job."""
    return _enabled() and trigger is not None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("not a count")
    return value


def _units(repo: Any, facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The prepared revision's per-unit counts (PR-Z's enriched / ambiguous /
    pending / queued), through the progress read that already derives them."""
    try:
        view = work_scope_batches._progress_view(
            work_scope_batches._read_progress(repo, UUID(str(facts["work_scope_id"]))))
    except AppError:
        return []
    preparation = view.get("preparation")
    if not isinstance(preparation, Mapping) or preparation.get("revision") != facts.get("revision"):
        return []
    return [{"unit_key": unit["unit_key"], "name": unit["name"], "state": unit["state"],
             "queued_count": unit["queued_count"], "coverage": unit.get("coverage")}
            for unit in preparation.get("units") or []]


def _view(repo: Any, facts: Mapping[str, Any], *, supported: bool, trigger: Any) -> dict[str, Any]:
    """The browser-safe status of one revision, built key by key."""
    try:
        derived = derive_status(facts)
        preparation = facts.get("preparation")
        prepared = None
        if isinstance(preparation, Mapping):
            prepared = {key: _count(preparation.get(key)) for key in (
                "unit_count", "prepared_unit_count", "queued_item_count", "batch_count")}
            created = preparation.get("created_at")
            prepared["prepared_at"] = str(created)[:64] if created else None
        known = facts.get("known_unresolved")
        unresolved = None
        if isinstance(known, Mapping):
            unresolved = {"revision": _count(known.get("revision")),
                          "count": _count(known.get("count"))}
        request = facts.get("request")
        attempt = _count(request.get("attempt")) if isinstance(request, Mapping) else 0
    except (KeyError, TypeError, ValueError):
        raise AppError("WORK_SCOPE_PREPARATION_STATUS_UNAVAILABLE",
                       "the preparation status could not be read", 502) from None
    state = derived["state"]
    blocker = None
    if not server_can_prepare(trigger):
        blocker = "preparation_disabled"
    elif not supported:
        blocker = "workflow_not_supported"
    elif state == "stale":
        blocker = "stale"
    elif state == "prepared":
        blocker = "prepared"
    elif state == "preparing":
        blocker = "in_flight"
    elif state == "failed" and not derived["retryable"]:
        blocker = "needs_operator"
    return {
        "work_scope_id": str(facts["work_scope_id"]),
        "revision": _count(facts.get("revision")),
        "digest": str(facts.get("digest")),
        "state": state,
        "reason_code": derived["reason_code"],
        "attempt": attempt,
        "can_prepare": blocker is None,
        "blocked_by": blocker,
        "preparation": prepared,
        "units": _units(repo, facts) if state == "prepared" else [],
        "known_unresolved": unresolved,
    }


def _facts(repo: Any, work_scope_id: UUID, revision: int, digest: str) -> Mapping[str, Any]:
    read = getattr(repo, "work_scope_preparation_state", None)
    if not callable(read):
        raise AppError("WORK_SCOPE_PREPARATION_STATUS_UNAVAILABLE",
                       "the preparation status could not be read", 503)
    facts = work_scopes._repository(lambda: read(work_scope_id, revision, digest))
    if facts is None:
        raise NotFoundError("work_scope", "requested")
    return facts


def status(repo: Any, user_id: UUID, work_scope_id: UUID, revision: int, digest: str, *,
           trigger: Any) -> dict[str, Any]:
    """One revision's preparation status, for a member. 404 otherwise."""
    plan = work_scopes._authorized(repo, user_id, work_scope_id)
    supported = repo.get_project(UUID(str(plan["project_id"]))).get("workflow_key") \
        in work_scopes.WORK_SCOPE_WORKFLOWS
    return _view(repo, _facts(repo, work_scope_id, revision, digest), supported=supported,
                 trigger=trigger)


def request_preparation(repo: Any, user_id: UUID, work_scope_id: UUID, *, expected_revision: int,
                        expected_digest: str, trigger: Any,
                        env: Mapping[str, str] | None = None) -> tuple[dict[str, Any], bool]:
    """Prepare ONE revision, or answer with the preparation that exists.

    Returns (status view, started) -- `started` is True only for the ONE
    caller whose claim executed the capture job.
    """
    environment = os.environ if env is None else env
    plan = work_scopes._authorized(repo, user_id, work_scope_id)
    work_scopes._require_supported(repo.get_project(UUID(str(plan["project_id"]))))
    if not server_can_prepare(trigger):
        raise _refusal("WORK_SCOPE_PREPARATION_DISABLED", 503)
    if plan.get("head_revision") != expected_revision or plan.get("head_digest") != expected_digest:
        raise AppError("WORK_SCOPE_STALE", work_scopes.REQUEST_REASONS["WORK_SCOPE_STALE"], 409)

    def current() -> dict[str, Any]:
        return _view(repo, _facts(repo, work_scope_id, expected_revision, expected_digest),
                     supported=True, trigger=trigger)

    before = current()
    if before["state"] in ("prepared", "preparing") or before["blocked_by"] == "needs_operator":
        if before["blocked_by"] == "needs_operator":
            raise _refusal("WORK_SCOPE_PREPARATION_NEEDS_OPERATOR", 409)
        return before, False
    # Fail closed on the image BEFORE anything durable is written.
    refusal = trigger.release_refusal()
    if refusal is not None:
        raise _refusal(refusal, 409)
    claim = work_scopes._repository(lambda: repo.request_work_scope_preparation(
        work_scope_id, expected_revision, expected_digest, user_id,
        grace_seconds=START_GRACE_SECONDS))
    if claim["decision"] == "stale":
        raise AppError("WORK_SCOPE_STALE", work_scopes.REQUEST_REASONS["WORK_SCOPE_STALE"], 409)
    if claim["decision"] != "claimed":
        return current(), False
    request = claim["request"]
    request_id, attempt = request["id"], int(request["attempt"])

    def record(state: str, run_id: Any, execution: str | None = None) -> None:
        work_scopes._repository(lambda: repo.record_work_scope_preparation_trigger(
            request_id, attempt, run_id=run_id, trigger_state=state, execution_name=execution))

    run_id = request.get("run_id")
    if not run_id:
        from backend.catalog.operator_capture import EXIT_OK, prepare_capture_run

        code, document = prepare_capture_run(
            repo, conversation_id=UUID(str(plan["conversation_id"])), requested_by=user_id,
            # One key per attempt of this request: a replay of the same
            # attempt returns the same run, never a second one.
            idempotency_key=f"work-scope-prepare-{request_id}-{attempt}", env=environment)
        prepared = document.get("preparation") if isinstance(document, Mapping) else None
        if code != EXIT_OK or not isinstance(prepared, Mapping) or not prepared.get("run_id"):
            record(trig.TRIGGER_FAILED, None)
            raise _refusal("WORK_SCOPE_PREPARATION_TRIGGER_FAILED", 502)
        run_id = str(prepared["run_id"])
        record("claimed", run_id)
    try:
        invocation = work_scope_preparation(
            project_ref=str(environment.get("MILO_EXPECTED_SUPABASE_PROJECT_REF") or ""),
            run_id=str(run_id), work_scope_id=str(work_scope_id), revision=expected_revision,
            digest=expected_digest)
    except InvocationError:
        record(trig.TRIGGER_FAILED, run_id)
        raise _refusal("WORK_SCOPE_PREPARATION_TRIGGER_FAILED", 502) from None
    outcome = trigger.run(invocation)
    if outcome.state not in (trig.TRIGGERED, trig.TRIGGER_FAILED, trig.TRIGGER_UNKNOWN):
        outcome = trig.TriggerOutcome(trig.TRIGGER_UNKNOWN)
    record(outcome.state, run_id, outcome.execution_name)
    if outcome.state == trig.TRIGGER_FAILED:
        raise _refusal("WORK_SCOPE_PREPARATION_TRIGGER_FAILED", 502)
    return current(), True


__all__ = ["BLOCKERS", "FAILURE_REASONS", "PREPARATION_REQUESTS_FLAG", "REQUEST_REASONS",
           "START_GRACE_SECONDS", "STATES", "derive_status", "request_preparation",
           "server_can_prepare", "status"]
