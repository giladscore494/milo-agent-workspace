"""Batch runs of a Mapping Plan: start, pause / resume, and progress.

Scoped catalog PR3. A prepared plan revision (`backend/catalog/scope/
preparation.py`, run by the operator capture job) holds a deterministic queue
cut into bounded batches. This module is how a person turns ONE of those
batches into ONE product run, and how the plan's progress is read back.

One launch path
---------------

A batch run is an ordinary Swarm V2 run. It is created by
`create_work_scope_batch_run`, which wraps the one run creator
(`create_message_and_run_v3`) and the batch binding in ONE transaction, and it
is launched by the API's existing launch step (`backend/main.py`), with the
same identity gate, launch compare-and-set, launcher and failure handling as
any other run. Nothing here launches anything by itself, and there is no
automatic next batch: every batch run is one person's request.

What the browser may say, and what it may not
---------------------------------------------

A request names the plan's head revision and digest it was made against, and
the batch it means to start. All three are PRECONDITIONS, never choices: the
database decides which batch is next and refuses any other, refuses a stale
revision, and refuses a second live batch. The run's work -- its snapshot, its
candidates and their order -- comes from the binding table alone, which the
worker reads for itself. Browser state is never execution authority.

Progress is derived
-------------------

`work_scope_progress` derives every count from the bound runs' own durable
status and their promotion events. This module projects that answer onto a
closed, browser-safe shape and adds what the server allows next (start, pause,
resume, cancel), from its own flags and the same durable state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping
from uuid import UUID

from backend.errors import AppError, NotFoundError
from backend.execution_guard import is_stage_enabled

from . import directory as mdir
from . import service as work_scopes

#: The server flag behind starting a batch and pausing / resuming a plan.
#: Default off, pinned off in every deployment contract, and enforced by
#: `ExecutionSurfaceGuardMiddleware` before a request body is read. Starting a
#: batch ALSO needs `MILO_ENABLE_RUN_CREATION`: a batch run is a run.
WORK_SCOPE_BATCHES_FLAG = work_scopes.WORK_SCOPE_BATCHES_FLAG
RUN_CREATION_FLAG = work_scopes.RUN_CREATION_FLAG
RUN_CANCELLATION_FLAG = "MILO_ENABLE_RUN_CANCELLATION"

#: The workflow a batch run executes as. Only Swarm V2 reads the catalog.
BATCH_RUN_WORKFLOW = "swarm_v2"

#: Why a batch cannot start right now. Static codes, each with one sentence.
START_BLOCKERS: Mapping[str, str] = {
    "batches_disabled": "starting batches is not enabled on this server",
    "closed": "the mapping plan is closed",
    "paused": "the mapping plan is paused",
    "not_prepared": "the plan's current revision has not been prepared yet",
    "nothing_queued": "preparing the plan queued no candidates",
    "batch_running": "a batch of this plan is still running",
    "complete": "every batch of the plan has finished",
}

BATCH_REASONS: Mapping[str, str] = {
    "WORK_SCOPE_BATCH_NOT_NEXT": "only the next batch of the plan can start",
    "WORK_SCOPE_PROGRESS_UNAVAILABLE": "the mapping plan's progress could not be read",
}

#: Batch states `work_scope_progress` derives; anything else is unreadable.
BATCH_STATES = frozenset({"pending", "active", "completed", "partial", "interrupted"})
#: The unit states a preparation records (`20260923000100`).
UNIT_STATES = frozenset({"prepared", "register_unverified", "snapshot_unusable",
                         "vocabulary_insufficient"})
#: Run statuses a live batch run may be in (not terminal).
LIVE_RUN_STATUSES = frozenset({"queued", "launching", "starting", "running", "waiting",
                               "cancellation_requested"})
#: The most recent batch runs a progress read lists.
MAX_RECENT_BATCHES = 5


def _refusal(code: str, status: int) -> AppError:
    return AppError(code, BATCH_REASONS[code], status)


# ---------------------------------------------------------------------------
# Reads.
# ---------------------------------------------------------------------------

def progress(repo: Any, user_id: UUID, work_scope_id: UUID) -> dict[str, Any]:
    """The plan's progress, for a member of its conversation's project. 404 otherwise."""
    work_scopes._authorized(repo, user_id, work_scope_id)
    return _progress_view(_read_progress(repo, work_scope_id))


def _read_progress(repo: Any, work_scope_id: UUID) -> Mapping[str, Any]:
    read = getattr(repo, "work_scope_progress", None)
    if not callable(read):
        raise _refusal("WORK_SCOPE_PROGRESS_UNAVAILABLE", 503)
    raw = work_scopes._repository(lambda: read(work_scope_id))
    if raw is None:
        raise NotFoundError("work_scope", "requested")
    if not isinstance(raw, Mapping):
        raise _refusal("WORK_SCOPE_PROGRESS_UNAVAILABLE", 502)
    return raw


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------

def set_paused(repo: Any, user_id: UUID, work_scope_id: UUID, paused: bool) -> dict[str, Any]:
    """Pause or resume one plan. A pause stops the NEXT batch from starting;
    a batch already running is stopped only by cancelling its run."""
    work_scopes._authorized(repo, user_id, work_scope_id)
    result = work_scopes._repository(lambda: repo.set_work_scope_paused(work_scope_id, paused,
                                                                        user_id))
    return {"changed": bool(result.get("changed")), "paused": bool(result.get("paused")),
            "progress": _progress_view(_read_progress(repo, work_scope_id))}


def batch_request(repo: Any, user_id: UUID, work_scope_id: UUID, *, expected_revision: int,
                  expected_digest: str, batch_id: UUID) -> dict[str, Any]:
    """Everything the run creator needs for ONE batch start, decided by the server.

    Authorizes first, then reads the durable progress to find the batch the
    request names. Only the plan's NEXT batch, or the batch already running (a
    double submission, answered with its run), can be named; any other is
    refused here and again by the database under the plan's row lock.

    The run's instruction and the request fingerprint are composed from server
    state and the request's own preconditions, never from browser text.
    """
    plan = work_scopes._authorized(repo, user_id, work_scope_id)
    project = repo.get_project(UUID(str(plan["project_id"])))
    if project.get("workflow_key") != BATCH_RUN_WORKFLOW:
        raise AppError("WORK_SCOPE_WORKFLOW_UNSUPPORTED",
                       work_scopes.REQUEST_REASONS["WORK_SCOPE_WORKFLOW_UNSUPPORTED"], 409)
    if plan.get("head_revision") != expected_revision or plan.get("head_digest") != expected_digest:
        raise AppError("WORK_SCOPE_STALE", work_scopes.REQUEST_REASONS["WORK_SCOPE_STALE"], 409)
    raw = _read_progress(repo, work_scope_id)
    preparation = raw.get("preparation") if isinstance(raw.get("preparation"), Mapping) else None
    live = raw.get("live") if isinstance(raw.get("live"), Mapping) else None
    wanted = str(batch_id)
    target: Mapping[str, Any] | None = None
    if live is not None and str(live.get("batch_id")) == wanted:
        target = live
    elif preparation is not None and isinstance(preparation.get("next"), Mapping) \
            and str(preparation["next"].get("batch_id")) == wanted:
        target = preparation["next"]
    if target is None:
        raise _refusal("WORK_SCOPE_BATCH_NOT_NEXT", 409)
    batch_count = int(preparation.get("batch_count") or 0) if preparation is not None else 0
    return {
        "project_id": str(plan["project_id"]),
        "content": batch_instruction(unit_key=str(target.get("unit_key") or ""),
                                     batch_number=int(target.get("batch_number") or 0),
                                     batch_count=batch_count,
                                     item_count=int(target.get("item_count") or 0),
                                     revision=int(expected_revision)),
        "fingerprint": batch_fingerprint(work_scope_id, expected_revision, expected_digest,
                                         batch_id),
    }


def create_batch_run(repo: Any, user_id: UUID, work_scope_id: UUID, *, expected_revision: int,
                     expected_digest: str, batch_id: UUID, idempotency_key: str,
                     content: str, fingerprint: str, run_id: UUID,
                     run_identity: Mapping[str, Any], max_user_active: int | None,
                     max_project_active: int | None) -> dict[str, Any]:
    """Create (or replay) ONE batch run through the database's one transaction."""
    metadata = {"requested_by": str(user_id), "idempotency_key": idempotency_key}
    try:
        return repo.create_work_scope_batch_run(
            work_scope_id, batch_id, expected_revision, expected_digest,
            run_id=run_id, run_identity=dict(run_identity), content=content, metadata=metadata,
            requested_by=user_id, idempotency_key=idempotency_key,
            request_fingerprint=fingerprint, max_user_active=max_user_active,
            max_project_active=max_project_active)
    except NotFoundError:
        raise NotFoundError("work_scope", "requested") from None


def batch_instruction(*, unit_key: str, batch_number: int, batch_count: int, item_count: int,
                      revision: int) -> str:
    """The run's instruction: composed by the server, never typed by anyone.

    The candidates themselves are NOT in it: the worker hands them to the engine
    from the batch binding, through the run's server-owned work context.
    """
    entry = mdir.entry_for(unit_key)
    name = entry.name if entry is not None else unit_key
    noun = "candidate" if item_count == 1 else "candidates"
    of = f" of {batch_count}" if batch_count > 0 else ""
    return (f"Mapping plan batch {batch_number}{of} (plan revision {revision}): research the "
            f"{item_count} {name} vehicle variant {noun} in this run's Government work queue "
            "and record verified evidence for each one.")


def batch_fingerprint(work_scope_id: UUID, expected_revision: int, expected_digest: str,
                      batch_id: UUID) -> str:
    """The logical request, fingerprinted: the same start is the same request."""
    canonical = json.dumps({"work_scope_batch_run": {
        "work_scope_id": str(work_scope_id), "revision": int(expected_revision),
        "digest": str(expected_digest), "batch_id": str(batch_id)}},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# The projection.
# ---------------------------------------------------------------------------

def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("not a count")
    number = int(value)
    if number < 0:
        raise ValueError("negative count")
    return number


def _text(value: Any, limit: int = 80) -> str | None:
    return str(value)[:limit] if isinstance(value, str) and value else None


def _unit_view(unit: Mapping[str, Any]) -> dict[str, Any]:
    state = unit.get("state")
    if state not in UNIT_STATES:
        raise ValueError("unknown unit state")
    batch_count = _int(unit.get("batch_count"))
    settled = _int(unit.get("settled_batches"))
    active = unit.get("active") is True
    if state != "prepared":
        progress_state = "not_queued"
    elif batch_count == 0:
        progress_state = "nothing_queued"
    elif settled == batch_count:
        progress_state = "completed"
    elif active:
        progress_state = "active"
    elif settled > 0:
        progress_state = "in_progress"
    else:
        progress_state = "pending"
    entry = mdir.entry_for(str(unit.get("unit_key") or ""))
    return {
        "unit_key": str(unit.get("unit_key")),
        "name": entry.name if entry is not None else str(unit.get("unit_key")),
        "priority": _int(unit.get("priority")),
        "state": state,
        "reason_code": _text(unit.get("reason_code")),
        "progress": progress_state,
        "readable_count": _int(unit.get("readable_count")),
        "ambiguous_count": _int(unit.get("ambiguous_count")),
        "eligible_count": _int(unit.get("eligible_count")),
        "queued_count": _int(unit.get("queued_count")),
        "batch_count": batch_count,
        "settled_batches": settled,
        "active": active,
        "promoted": _int(unit.get("promoted")),
        "refused": _int(unit.get("refused")),
        "unresolved": _int(unit.get("unresolved")),
    }


def _batch_view(batch: Mapping[str, Any], *, with_outcome: bool) -> dict[str, Any]:
    state = batch.get("state")
    if state not in BATCH_STATES:
        raise ValueError("unknown batch state")
    view = {
        "batch_id": str(UUID(str(batch.get("batch_id")))),
        "batch_number": _int(batch.get("batch_number")),
        "unit_key": str(batch.get("unit_key")),
        "item_count": _int(batch.get("item_count")),
        "state": state,
        "attempts": _int(batch.get("attempts")),
    }
    if with_outcome:
        view.update({"run_id": str(UUID(str(batch.get("run_id")))),
                     "run_status": _text(batch.get("run_status"), 40),
                     "promoted": _int(batch.get("promoted")),
                     "refused": _int(batch.get("refused")),
                     "unresolved": _int(batch.get("unresolved"))})
    return view


def _live_view(live: Mapping[str, Any]) -> dict[str, Any]:
    status = live.get("run_status")
    if status not in LIVE_RUN_STATUSES:
        raise ValueError("a live batch run must not be terminal")
    return {
        "batch_id": str(UUID(str(live.get("batch_id")))),
        "batch_number": _int(live.get("batch_number")),
        "revision": _int(live.get("revision")),
        "unit_key": str(live.get("unit_key")),
        "item_count": _int(live.get("item_count")),
        "attempt": _int(live.get("attempt")),
        "run_id": str(UUID(str(live.get("run_id")))),
        "run_status": status,
        "launch_state": _text(live.get("launch_state"), 40),
    }


def _progress_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The browser-safe progress of one plan, built key by key.

    Anything the projection cannot read is refused whole (502) rather than
    rendered in part: a count shown wrong is worse than a count not shown.
    """
    try:
        live = _live_view(raw["live"]) if isinstance(raw.get("live"), Mapping) else None
        preparation_raw = raw.get("preparation")
        preparation = None
        if isinstance(preparation_raw, Mapping):
            items = preparation_raw.get("items") or {}
            batches = preparation_raw.get("batches") or {}
            following = preparation_raw.get("next")
            preparation = {
                "revision": _int(preparation_raw.get("revision")),
                "prepared_at": _text(preparation_raw.get("created_at"), 64),
                "unit_count": _int(preparation_raw.get("unit_count")),
                "prepared_unit_count": _int(preparation_raw.get("prepared_unit_count")),
                "units": [_unit_view(unit) for unit in preparation_raw.get("units") or []],
                "next": (_batch_view(following, with_outcome=False)
                         if isinstance(following, Mapping) else None),
                "recent": [_batch_view(batch, with_outcome=True)
                           for batch in (preparation_raw.get("recent") or [])[:MAX_RECENT_BATCHES]],
                "batches": {"total": _int(batches.get("total")),
                            "settled": _int(batches.get("settled")),
                            "active": _int(batches.get("active")),
                            "interrupted": _int(batches.get("interrupted"))},
                "items": {"total": _int(items.get("total")),
                          "promoted": _int(items.get("promoted")),
                          "refused": _int(items.get("refused")),
                          "unresolved": _int(items.get("unresolved"))},
            }
            done = (preparation["items"]["promoted"] + preparation["items"]["refused"]
                    + preparation["items"]["unresolved"])
            preparation["items"]["completed"] = done
            preparation["items"]["remaining"] = max(preparation["items"]["total"] - done, 0)
            preparation["batches"]["remaining"] = max(
                preparation["batches"]["total"] - preparation["batches"]["settled"], 0)
        view = {
            "work_scope_id": str(UUID(str(raw.get("work_scope_id")))),
            "revision": _int(raw.get("revision")),
            "digest": str(raw.get("digest")),
            "closed": raw.get("closed") is True,
            "paused": raw.get("paused") is True,
            "live": live,
            "preparation": preparation,
        }
    except (KeyError, TypeError, ValueError):
        raise _refusal("WORK_SCOPE_PROGRESS_UNAVAILABLE", 502) from None
    view["status"] = _status(view)
    view["controls"] = _controls(view)
    return view


def _status(view: Mapping[str, Any]) -> str:
    """One word for where the plan stands, derived from the fields above."""
    preparation = view["preparation"]
    if view["live"] is not None:
        return "running"
    if preparation is None:
        return "not_prepared"
    if preparation["batches"]["total"] == 0:
        return "nothing_queued"
    if preparation["next"] is None:
        return "complete"
    return "ready"


def _start_blocker(view: Mapping[str, Any]) -> str | None:
    if not (is_stage_enabled(WORK_SCOPE_BATCHES_FLAG) and is_stage_enabled(RUN_CREATION_FLAG)):
        return "batches_disabled"
    if view["closed"]:
        return "closed"
    if view["paused"]:
        return "paused"
    status = view["status"]
    if status == "running":
        return "batch_running"
    if status in ("not_prepared", "nothing_queued", "complete"):
        return status
    return None


def _controls(view: Mapping[str, Any]) -> dict[str, Any]:
    """What the server allows next. The database re-checks every one of them.

    `start.batch` is the batch a start would run -- the plan's next batch, or,
    when no worker was ever started for the running batch (its launch never
    happened, or definitely failed), that batch again, so the same run is
    launched rather than a second one created.
    """
    batches_on = is_stage_enabled(WORK_SCOPE_BATCHES_FLAG)
    blocker = _start_blocker(view)
    preparation = view["preparation"]
    live = view["live"]
    batch = preparation["next"] if preparation is not None else None
    # No worker was ever started for this run, and none will be unless it is
    # launched: the launch compare-and-set takes only these two states.
    unlaunched = (live is not None and live["run_status"] == "queued"
                  and live["launch_state"] in ("pending", "launch_failed"))
    # Only a batch of the HEAD revision can be named by a start: a stale
    # revision never launches, and the database refuses it too.
    relaunch = (unlaunched and live["revision"] == view["revision"]
                and batches_on and is_stage_enabled(RUN_CREATION_FLAG)
                and not view["paused"] and not view["closed"])
    if relaunch:
        blocker = None
        batch = {"batch_id": live["batch_id"], "batch_number": live["batch_number"],
                 "unit_key": live["unit_key"], "item_count": live["item_count"],
                 "state": "active", "attempts": live["attempt"]}
    return {
        "start": {"available": blocker is None, "blocked_by": blocker,
                  "batch": batch if blocker is None else None,
                  "retry": bool(batch is not None and batch.get("state") == "interrupted"
                                and blocker is None),
                  "relaunch": relaunch},
        "pause": {"available": batches_on and not view["closed"] and not view["paused"]},
        "resume": {"available": batches_on and not view["closed"] and view["paused"]},
        # A run no worker was started for has nobody to finalize its
        # cancellation: cancelling it would leave it `cancellation_requested`,
        # holding the plan, until an operator resolves it. It is not offered.
        "cancel": {"available": (live is not None and not unlaunched
                                 and live["run_status"] != "cancellation_requested"
                                 and is_stage_enabled(RUN_CANCELLATION_FLAG)),
                   "run_id": live["run_id"] if live is not None else None},
    }


__all__ = ["BATCH_REASONS", "BATCH_RUN_WORKFLOW", "START_BLOCKERS", "WORK_SCOPE_BATCHES_FLAG",
           "batch_fingerprint", "batch_instruction", "batch_request", "create_batch_run",
           "progress", "set_paused"]
