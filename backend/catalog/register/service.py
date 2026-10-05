"""The API's side of register capture (PR-D1, D1-3 / D1-4).

`register_view`       the Register page: the latest directory version, every
                      tozar with its expected rows and state, the totals, and
                      the capacity bar.
`request_capture`     Capture one tozar or a group, through the EXISTING Cloud
                      Run capture-job trigger (`prepare_trigger.py`), release
                      refusal first (both jobs on the current release).
`request_directory_refresh`  the same trigger, directory mode.
`request_sync`        PR-SYNC-1: the same trigger, sync mode (`register/sync.py`):
                      one at a time, never beside a live capture, directory
                      refresh or sync (`CATALOG_REGISTER_BUSY`).
`normalization_view` / `request_normalization` / `approve_normalization`
                      PR-D3: manufacturer normalisation -- the canonical
                      names, the deterministic groups and the latest model
                      proposal; the ONE guarded K3 call (the same trigger,
                      normalisation mode, behind its own flag); the owner's
                      approval.

Capture is $0 and is NOT a product run: it depends on the register stage flag
(`MILO_ENABLE_REGISTER_CAPTURE`) and nothing else -- never on
`GATEWAY_ALLOW_RUN_START_ROUTES`, never on Arm. The capture job uses an
operator capture run only as the lease its snapshot writes require.

Idempotent per (tozar, register_version) (`request_register_capture`): a
second request is answered with the existing request or snapshot and starts
no job. A group covers at most `MILO_REGISTER_GROUP_MAX_ROWS` expected rows;
one tozar larger than that is captured alone. The capacity guard runs in the
same database call, before anything is written.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import UUID

from backend.capture_invocation import (InvocationError, manufacturer_normalisation, register_capture,
                                       register_directory, register_sync)
from backend.catalog.register import config as register_config
from backend.catalog.register import normalization
from backend.catalog.scope import prepare_trigger as trig
from backend.errors import AppError
from backend.production_config import TRUE_VALUES

REGISTER_FLAG = "MILO_ENABLE_REGISTER_CAPTURE"
START_GRACE_SECONDS = 900
MAX_TOZARS_PER_REQUEST = 1000
_LOG = logging.getLogger("milo.catalog.register")
_ENDED = ("completed", "partial_success", "failed", "cancelled", "timed_out", "budget_exhausted")
_STATIC_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_CAPACITY = re.compile(r"current=(\d+)\s+projected=(\d+)\s+limit=(\d+)")

#: The API's refusals, each with its HTTP status and static message.
REQUEST_REASONS: Mapping[str, tuple[int, str]] = {
    "CATALOG_REGISTER_DISABLED": (404, "register capture is not enabled"),
    "CATALOG_REGISTER_UNAVAILABLE": (503, "register capture has no capture job on this server"),
    "CATALOG_REGISTER_NO_DIRECTORY": (409, "the register directory has not been read yet"),
    "CATALOG_REGISTER_VERSION_STALE": (409, "that is not the current register directory version"),
    "CATALOG_REGISTER_UNIT_UNKNOWN": (422, "a requested tozar is not in the register directory"),
    "CATALOG_REGISTER_REQUEST_INVALID": (422, "the register capture request is not valid"),
    "CATALOG_REGISTER_GROUP_TOO_LARGE": (422, "one request may cover at most the group cap of expected rows"),
    "CATALOG_CAPACITY_THRESHOLD_EXCEEDED":
        (409, "the capture would take the database above its capacity threshold"),
    "CATALOG_REGISTER_JOB_NOT_RELEASE": (409, "the capture jobs are not on the current release"),
    "CATALOG_REGISTER_JOB_UNREADABLE": (409, "the capture jobs could not be read"),
    "CATALOG_REGISTER_TRIGGER_FAILED": (502, "the capture job could not be started"),
    "CATALOG_REGISTER_CONVERSATION_UNAVAILABLE":
        (404, "that conversation does not belong to this project"),
    "CATALOG_REGISTER_WORKFLOW_UNSUPPORTED":
        (409, "this project's engine does not read the Government catalog"),
    # PR-SYNC-1: single flight.
    "CATALOG_REGISTER_BUSY": (409, "a register capture, directory refresh or sync is already running"),
    # PR-D3.
    "CATALOG_NORMALIZATION_DISABLED": (404, "manufacturer normalisation is not enabled"),
    "CATALOG_NORMALIZATION_NOTHING_UNMAPPED": (409, "every source manufacturer name is already mapped"),
    "CATALOG_NORMALIZATION_OWNER_ONLY": (403, "only a project owner starts, approves or rejects a normalisation"),
    "CATALOG_NORMALIZATION_APPROVAL_INVALID": (422, "that is not a pending group, or it must be approved alone"),
    "CATALOG_NORMALIZATION_REJECTION_INVALID": (422, "that is not a pending group"),
    "CATALOG_NORMALIZATION_VERSION_STALE": (409, "the active normalisation changed; reload"),
    "CATALOG_NORMALIZATION_COOLDOWN": (429, "a normalisation was requested moments ago; try again later"),
    "CATALOG_NORMALIZATION_INPUT_TOO_LARGE":
        (422, "more unmapped manufacturer names than one normalisation may carry"),
}

_TRIGGER_CODES = {trig.JOB_NOT_RELEASE: "CATALOG_REGISTER_JOB_NOT_RELEASE",
                  trig.JOB_UNREADABLE: "CATALOG_REGISTER_JOB_UNREADABLE"}


class CapacityRefusal(AppError):
    """The capacity refusal, with the numbers the page shows."""

    def __init__(self, current: int, projected: int, limit: int) -> None:
        status, message = REQUEST_REASONS["CATALOG_CAPACITY_THRESHOLD_EXCEEDED"]
        super().__init__("CATALOG_CAPACITY_THRESHOLD_EXCEEDED",
                         f"{message}: current={current} projected={projected} limit={limit} bytes", status)
        self.capacity = {"current_bytes": current, "projected_bytes": projected, "limit_bytes": limit}


def _refusal(code: str) -> AppError:
    status, message = REQUEST_REASONS[code]
    return AppError(code, message, status)


def _log_refusal(project_id: Any, code: Any) -> None:
    try:
        project = str(UUID(str(project_id)))
    except ValueError:
        project = "invalid"
    static = code if isinstance(code, str) and _STATIC_CODE.fullmatch(code) else "UNCLASSIFIED"
    _LOG.warning("event=register_capture_refused project_id=%s code=%s", project, static)


def register_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get(REGISTER_FLAG) or "").strip().lower() in TRUE_VALUES


def require_enabled(env: Mapping[str, str] | None = None) -> None:
    """A disabled register surface does not exist: 404."""
    if not register_enabled(env):
        raise _refusal("CATALOG_REGISTER_DISABLED")


def server_can_capture(trigger: Any, env: Mapping[str, str] | None = None) -> bool:
    return register_enabled(env) and trigger is not None


# -- the page ------------------------------------------------------------------

def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stalled(group: Mapping[str, Any], run: Mapping[str, Any], now: datetime) -> bool:
    """The database's retry rule (request_register_capture), read for the page:
    a claim that never got a run, a run no worker ever claimed, a running
    capture whose lease expired, or a run left in any other live status
    (launching, waiting, cancellation_requested) -- each longer ago than the
    grace (public.catalog_register_group_stale)."""
    limit = now - timedelta(seconds=START_GRACE_SECONDS)
    claimed = _time(group.get("claimed_at"))
    started = _time(group.get("triggered_at")) or claimed
    lease = _time(run.get("lease_expires_at")) if run.get("lease_expires_at") else None
    return bool(
        (group.get("trigger_state") == "claimed" and not group.get("run_id")
         and claimed is not None and claimed < limit)
        or (run.get("status") == "queued" and not run.get("worker_id") and started is not None and started < limit)
        or (run.get("status") in ("starting", "running") and lease is not None and lease < limit)
        or ((run.get("status") in ("launching", "waiting", "cancellation_requested")
             or (run.get("status") == "queued" and run.get("worker_id")))
            and (lease or started) is not None and (lease or started) < limit))


def _state(unit: Mapping[str, Any] | None, runs: Mapping[str, Mapping[str, Any]],
           groups: Mapping[str, Mapping[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    if unit is None:
        return {"state": "not_captured"}
    status = str(unit.get("status"))
    view = {"state": status, "snapshot_key": unit.get("snapshot_key") or None,
            "captured_rows": unit.get("captured_rows"), "api_total": unit.get("api_total"),
            "verified": unit.get("count_verified"), "failure_code": unit.get("failure_code") or None,
            "measured_bytes": unit.get("measured_bytes"), "register_version": unit.get("register_version")}
    rows = unit.get("captured_rows")
    measured = unit.get("measured_bytes")
    view["measured_bytes_per_row"] = (round(int(measured) / int(rows))
                                      if isinstance(rows, int) and rows > 0 and isinstance(measured, int)
                                      else None)
    if status in ("requested", "capturing"):
        group = groups.get(str(unit.get("group_id"))) or {}
        run = runs.get(str(group.get("run_id"))) or {}
        if group.get("trigger_state") == trig.TRIGGER_FAILED:
            view.update(state="failed", failure_code="CATALOG_REGISTER_TRIGGER_FAILED")
        elif run.get("status") in _ENDED:
            # The job ended without recording this unit's outcome.
            view.update(state="failed", failure_code="CATALOG_REGISTER_CAPTURE_INTERRUPTED")
        elif _stalled(group, run, now or datetime.now(UTC)):
            # The database would take a new request for it: say so, so the
            # page offers "Capture again".
            view.update(state="failed", failure_code="CATALOG_REGISTER_CAPTURE_STALLED")
        else:
            view["state"] = "capturing"
    if view["state"] == "requested":
        view["state"] = "capturing"
    return view


def _supported(project: Mapping[str, Any]) -> bool:
    from backend.catalog.scope.service import WORK_SCOPE_WORKFLOWS

    return project.get("workflow_key") in WORK_SCOPE_WORKFLOWS


def unit_views(repo: Any) -> dict[str, dict[str, Any]]:
    """Per tozar, its newest request row as the page shows it (`_state`)."""
    latest_by_tozar: dict[str, Mapping[str, Any]] = {}
    for unit in sorted(repo.register_capture_units(), key=lambda row: str(row.get("updated_at") or "")):
        latest_by_tozar[str(unit["tozar"])] = unit
    groups = {str(group["id"]): group for group in repo.register_capture_groups(
        sorted({str(unit["group_id"]) for unit in latest_by_tozar.values()
                if unit.get("status") in ("requested", "capturing")}))}
    runs = {key: run for key, run in ((str(g["run_id"]), _run(repo, g)) for g in groups.values()) if run}
    return {tozar: dict(_state(unit, runs, groups), updated_at=unit.get("updated_at"))
            for tozar, unit in latest_by_tozar.items()}


def _run(repo: Any, group: Mapping[str, Any]) -> Mapping[str, Any] | None:
    try:
        return repo.get_run(UUID(str(group["run_id"]))) if group.get("run_id") else None
    except Exception:
        return None


def _group_live(repo: Any, group: Mapping[str, Any]) -> bool:
    """Not `public.catalog_register_group_stale`, read for the API."""
    run = _run(repo, group) or {}
    return not (group.get("trigger_state") == trig.TRIGGER_FAILED or run.get("status") in _ENDED
                or _stalled(group, run, datetime.now(UTC)))


def register_busy(repo: Any) -> bool:
    """A live capture unit, or a live directory refresh or sync (PR-SYNC-1)."""
    return (any(view["state"] == "capturing" for view in unit_views(repo).values())
            or any(_group_live(repo, group) for group in repo.register_directory_groups(1)))


#: The newest directory groups read for the last sync (a sync's run keeps
#: its summary in its output; a plain refresh's run has none).
SYNC_LOOKBACK = 10


def last_sync(repo: Any, *, before_run: Any = None) -> dict[str, Any] | None:
    """The newest finished sync's document (its summary fields and basis)."""
    for group in repo.register_directory_groups(SYNC_LOOKBACK):
        run = _run(repo, group) if str(group.get("run_id")) != str(before_run) else None
        sync = ((run or {}).get("output") or {}).get("sync")
        if isinstance(sync, Mapping) and sync.get("summary"):
            return {key: sync.get(key) for key in ("summary", "stop", "basis")} | {
                "finished_at": run.get("finished_at")}
    return None


def claim_bytes_per_row(repo: Any, config: Any) -> int:
    """The estimate counts COMPACTED rows. While a finished capture's snapshot
    is not compacted (a refused or failed compaction: its rows are full
    size), the incoming rows are priced as they land, uncompacted -- never a
    refusal with no way out (its re-capture supersedes and archives it)."""
    if repo.catalog_register_uncompacted_captures() > 0:
        return max(config.bytes_per_row, register_config.UNCOMPACTED_BYTES_PER_ROW)
    return config.bytes_per_row


def register_view(repo: Any, user_id: UUID, project_id: UUID, *, trigger: Any,
                  env: Mapping[str, str] | None = None) -> dict[str, Any]:
    require_enabled(env)
    # A project whose engine does not read the catalog has no Register page.
    if not _supported(repo.get_project(project_id, user_id)):
        raise _refusal("CATALOG_REGISTER_DISABLED")
    config = register_config.load(env)
    directory = repo.latest_register_directory()
    views = unit_views(repo)
    rows: list[dict[str, Any]] = []
    captured_units = captured_rows = 0
    # A label beside the exact tozar: unreadable, the page shows the source names.
    canonical = normalization.current_map_or_empty(repo) if directory is not None else {}
    if directory is not None:
        for entry in directory["units"]:
            view = {"tozar": entry["tozar"], "expected_rows": int(entry["expected_rows"]),
                    # PR-D3: the approved canonical manufacturer (the tozar stays exact).
                    "canonical_manufacturer": canonical.get(str(entry["tozar"])),
                    **views.get(str(entry["tozar"]), {"state": "not_captured"})}
            if view["state"] == "captured":
                captured_units += 1
                captured_rows += int(view.get("captured_rows") or 0)
            rows.append(view)
    current = int(repo.catalog_database_bytes())
    version = directory["version"] if directory is not None else None
    return {
        "available": True,
        "can_capture": server_can_capture(trigger, env) and directory is not None,
        "can_refresh_directory": server_can_capture(trigger, env),
        "can_sync": server_can_capture(trigger, env) and directory is not None,
        "last_sync": last_sync(repo),
        "directory": None if version is None else {
            "register_version": version["register_version"], "resource_id": version["resource_id"],
            "fetched_at": str(version["fetched_at"]), "unit_count": int(version["unit_count"]),
            "total_rows": int(version["total_rows"])},
        "units": rows,
        "totals": {"units_total": len(rows), "units_captured": captured_units,
                   "rows_total": sum(row["expected_rows"] for row in rows), "rows_captured": captured_rows},
        "capacity": {**config.as_view(), "current_bytes": current,
                     "over_threshold": current > config.capacity_limit_bytes},
        "group_max_rows": config.group_max_rows,
    }


# -- Capture ---------------------------------------------------------------------

def _authorized_conversation(repo: Any, user_id: UUID, project_id: UUID, conversation_id: UUID) -> None:
    if not _supported(repo.get_project(project_id, user_id)):
        raise _refusal("CATALOG_REGISTER_WORKFLOW_UNSUPPORTED")
    try:
        conversation = repo.get_conversation(conversation_id, user_id)
    except Exception:
        raise _refusal("CATALOG_REGISTER_CONVERSATION_UNAVAILABLE") from None
    if str(conversation.get("project_id")) != str(project_id):
        raise _refusal("CATALOG_REGISTER_CONVERSATION_UNAVAILABLE")


def _release_gate(trigger: Any) -> None:
    refusal = trigger.release_refusal()
    if refusal is not None:
        raise _refusal(_TRIGGER_CODES.get(refusal, "CATALOG_REGISTER_JOB_UNREADABLE"))


def _run_for(repo: Any, *, conversation_id: UUID, user_id: UUID, key: str,
             environment: Mapping[str, str]) -> str | None:
    from backend.catalog.operator_capture import EXIT_OK, prepare_capture_run

    code, document = prepare_capture_run(repo, conversation_id=conversation_id, requested_by=user_id,
                                         idempotency_key=key, env=environment)
    prepared = document.get("preparation") if isinstance(document, Mapping) else None
    if code != EXIT_OK or not isinstance(prepared, Mapping) or not prepared.get("run_id"):
        return None
    return str(prepared["run_id"])


def _map_claim_refusal(refused: AppError) -> AppError:
    if refused.code == "CATALOG_CAPACITY_THRESHOLD_EXCEEDED":
        match = _CAPACITY.search(refused.message or "")
        if match:
            return CapacityRefusal(*(int(value) for value in match.groups()))
        return _refusal("CATALOG_CAPACITY_THRESHOLD_EXCEEDED")
    if refused.code in REQUEST_REASONS:
        return _refusal(refused.code)
    return refused


def request_capture(repo: Any, user_id: UUID, project_id: UUID, *, register_version: str,
                    tozars: Sequence[str], conversation_id: UUID, trigger: Any,
                    env: Mapping[str, str] | None = None) -> tuple[dict[str, Any], bool]:
    """Capture the given tozars of the given directory version, or answer with
    what already exists. Returns (answer, started)."""
    try:
        return _request_capture(repo, user_id, project_id, register_version=register_version,
                                tozars=tozars, conversation_id=conversation_id, trigger=trigger, env=env)
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


def _request_capture(repo: Any, user_id: UUID, project_id: UUID, *, register_version: str,
                     tozars: Sequence[str], conversation_id: UUID, trigger: Any,
                     env: Mapping[str, str] | None) -> tuple[dict[str, Any], bool]:
    environment = os.environ if env is None else env
    require_enabled(environment)
    _authorized_conversation(repo, user_id, project_id, conversation_id)
    if trigger is None:
        raise _refusal("CATALOG_REGISTER_UNAVAILABLE")
    names = [str(name) for name in tozars]
    if not names or len(names) > MAX_TOZARS_PER_REQUEST or len(set(names)) != len(names):
        raise _refusal("CATALOG_REGISTER_REQUEST_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", str(register_version)):
        raise _refusal("CATALOG_REGISTER_REQUEST_INVALID")
    # Fail closed on the image BEFORE anything durable is written.
    _release_gate(trigger)
    # PR-SYNC-1: never beside a live directory refresh or sync.
    if any(_group_live(repo, group) for group in repo.register_directory_groups(1)):
        raise _refusal("CATALOG_REGISTER_BUSY")
    config = register_config.load(environment)
    per_row = claim_bytes_per_row(repo, config)
    try:
        claim = repo.request_register_capture(
            register_version, names, user_id, group_max_rows=config.group_max_rows,
            capacity_limit_bytes=config.capacity_limit_bytes, bytes_per_row=per_row,
            grace_seconds=START_GRACE_SECONDS)
    except AppError as refused:
        raise _map_claim_refusal(refused) from None
    if claim["decision"] != "claimed":
        return {"decision": "existing", "units": claim.get("units") or []}, False
    group = claim["group"]

    def record(state: str, run_id: str | None, execution: str | None = None) -> None:
        repo.record_register_capture_trigger(group["id"], run_id=run_id, trigger_state=state,
                                             execution_name=execution)

    run_id = _run_for(repo, conversation_id=conversation_id, user_id=user_id,
                      key=f"register-capture-{group['id']}", environment=environment)
    if run_id is None:
        record(trig.TRIGGER_FAILED, None)
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
    record("claimed", run_id)
    try:
        invocation = register_capture(
            project_ref=str(environment.get("MILO_EXPECTED_SUPABASE_PROJECT_REF") or ""),
            run_id=run_id, group_id=str(group["id"]))
    except InvocationError:
        record(trig.TRIGGER_FAILED, run_id)
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED") from None
    outcome = trigger.run(invocation)
    if outcome.state not in (trig.TRIGGERED, trig.TRIGGER_FAILED, trig.TRIGGER_UNKNOWN):
        outcome = trig.TriggerOutcome(trig.TRIGGER_UNKNOWN)
    record(outcome.state, run_id, outcome.execution_name)
    if outcome.state == trig.TRIGGER_FAILED:
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
    return {"decision": "claimed", "group_id": str(group["id"]), "units": claim.get("units") or []}, True


def request_directory_refresh(repo: Any, user_id: UUID, project_id: UUID, *, conversation_id: UUID,
                              trigger: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Start one directory refresh (metadata reads only), or answer with the
    one still live -- one at a time (`request_register_directory_refresh`).
    A refresh of an unchanged register lands on the same version."""
    environment = os.environ if env is None else env
    try:
        require_enabled(environment)
        _authorized_conversation(repo, user_id, project_id, conversation_id)
        if trigger is None:
            raise _refusal("CATALOG_REGISTER_UNAVAILABLE")
        _release_gate(trigger)
        claim = repo.request_register_directory_refresh(user_id, grace_seconds=START_GRACE_SECONDS)
        group = claim["group"]
        if claim["decision"] != "claimed":
            # The register is global; its run lives in the requester's
            # conversation, so only they are told which run it is.
            mine = str(group.get("requested_by")) == str(user_id)
            return {"started": False, "group_id": str(group["id"]),
                    "run_id": group.get("run_id") if mine else None}
        return _start_directory_group(repo, group, user_id=user_id, conversation_id=conversation_id,
                                      trigger=trigger, environment=environment, mode="directory")
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


def _start_directory_group(repo: Any, group: Mapping[str, Any], *, user_id: UUID, conversation_id: UUID,
                           trigger: Any, environment: Mapping[str, str], mode: str) -> dict[str, Any]:
    """Prepare the claimed directory group's run and execute the job in `mode`."""
    def record(state: str, run_id: str | None, execution: str | None = None) -> None:
        repo.record_register_capture_trigger(group["id"], run_id=run_id, trigger_state=state,
                                             execution_name=execution)

    run_id = _run_for(repo, conversation_id=conversation_id, user_id=user_id,
                      key=f"register-{mode}-{group['id']}", environment=environment)
    if run_id is None:
        record(trig.TRIGGER_FAILED, None)
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
    record("claimed", run_id)
    try:
        invocation = (register_sync if mode == "sync" else register_directory)(
            project_ref=str(environment.get("MILO_EXPECTED_SUPABASE_PROJECT_REF") or ""), run_id=run_id)
    except InvocationError:
        record(trig.TRIGGER_FAILED, run_id)
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED") from None
    outcome = trigger.run(invocation)
    if outcome.state not in (trig.TRIGGERED, trig.TRIGGER_FAILED, trig.TRIGGER_UNKNOWN):
        outcome = trig.TriggerOutcome(trig.TRIGGER_UNKNOWN)
    record(outcome.state, run_id, outcome.execution_name)
    if outcome.state == trig.TRIGGER_FAILED:
        raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
    return {"started": True, "group_id": str(group["id"]), "run_id": run_id}


def request_sync(repo: Any, user_id: UUID, project_id: UUID, *, conversation_id: UUID,
                 trigger: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """PR-SYNC-1: start ONE register sync (`register/sync.py`) in the capture
    job. Single flight: refused (`CATALOG_REGISTER_BUSY`, never a second job)
    while any capture, directory refresh or sync is live. The sync holds the
    directory refresh's own claim (`request_register_directory_refresh`, a
    `directory` group), so a refresh and a sync exclude each other."""
    environment = os.environ if env is None else env
    try:
        require_enabled(environment)
        _authorized_conversation(repo, user_id, project_id, conversation_id)
        if trigger is None:
            raise _refusal("CATALOG_REGISTER_UNAVAILABLE")
        if repo.latest_register_directory() is None:
            raise _refusal("CATALOG_REGISTER_NO_DIRECTORY")
        _release_gate(trigger)
        if register_busy(repo):
            raise _refusal("CATALOG_REGISTER_BUSY")
        claim = repo.request_register_directory_refresh(user_id, grace_seconds=START_GRACE_SECONDS)
        if claim["decision"] != "claimed":
            raise _refusal("CATALOG_REGISTER_BUSY")
        return _start_directory_group(repo, claim["group"], user_id=user_id, conversation_id=conversation_id,
                                      trigger=trigger, environment=environment, mode="sync")
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


# -- PR-D3: manufacturer normalisation ---------------------------------------------

def _normalization_inputs(repo: Any) -> tuple[dict[str, int], dict[str, Any], dict[str, str]]:
    """(every directory tozar -> rows, per captured tozar its evidence, the active map)."""
    directory = repo.latest_register_directory()
    if directory is None:
        raise _refusal("CATALOG_REGISTER_NO_DIRECTORY")
    names = {str(unit["tozar"]): int(unit["expected_rows"]) for unit in directory["units"]}
    evidence = {str(row["tozar"]): row for row in repo.catalog_manufacturer_evidence()}
    return names, evidence, normalization.current_map(repo)


def _pending(repo: Any) -> dict[str, Any]:
    names, evidence, active = _normalization_inputs(repo)
    rules = normalization.deterministic_groups(names, evidence, active)
    # The newest ANSWERED proposal: a later request, refused or still running,
    # never hides the groups waiting for approval.
    answered = repo.latest_manufacturer_normalization_proposal("proposed")
    model = [dict(group, proposal_id=str(answered["id"]))
             for group in (answered or {}).get("groups") or []
             if any(active.get(m) != group["canonical"] for m in group["members"])]
    proposal = repo.latest_manufacturer_normalization_proposal()
    # A group the owner rejected, exactly as it was proposed, is not pending.
    rejected = {(row["canonical_name"], tuple(sorted(row["members"], key=str.encode)), row.get("rule_id"),
                 str(row["proposal_id"]) if row.get("proposal_id") else None)
                for row in repo.manufacturer_normalization_rejections()}
    pending = [group for group in rules + model
               if (group["canonical"], tuple(sorted(group["members"], key=str.encode)), group.get("rule_id"),
                   group.get("proposal_id")) not in rejected]
    for group in pending:
        group["conflicting"] = normalization.conflicts(group, active, pending)
        # A single-member group re-names one source: always approved on its own.
        group["bulk_approvable"] = (group["confidence"] == "high" and not group["conflicting"]
                                    and len(group["members"]) >= 2)
    current = repo.manufacturer_normalization_current() or {}
    return {"version": int(current.get("version") or 0), "active": active, "pending": pending,
            "proposal": proposal, "unmapped": len(set(names) - set(active))}


def normalization_view(repo: Any, user_id: UUID, project_id: UUID, *, trigger: Any,
                       env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The approval screen: the active version (canonical -> its exact
    sources), the pending groups (rules, then the latest model proposal) and
    whether the "Normalise manufacturers" button may be pressed."""
    environment = os.environ if env is None else env
    require_enabled(environment)
    if not _supported(repo.get_project(project_id, user_id)):
        raise _refusal("CATALOG_REGISTER_DISABLED")
    state = _pending(repo)
    owner = repo.project_member_role(project_id, user_id) == "owner"
    canonical: dict[str, list[str]] = {}
    for source, name in sorted(state["active"].items(), key=lambda item: item[0].encode("utf-8")):
        canonical.setdefault(name, []).append(source)
    proposal = state["proposal"] or None
    return {
        "version": state["version"], "unmapped": state["unmapped"],
        "canonical": [{"canonical": name, "sources": sources} for name, sources in sorted(
            canonical.items(), key=lambda item: item[0].encode("utf-8"))],
        "pending": state["pending"],
        "proposal": None if proposal is None else {
            key: proposal.get(key) for key in ("id", "status", "reason_code", "model", "created_at")},
        "can_normalise": (normalization.enabled(environment) and trigger is not None and state["unmapped"] > 0
                          and owner),
        "can_approve": owner,
    }


def request_normalization(repo: Any, user_id: UUID, project_id: UUID, *, conversation_id: UUID,
                          trigger: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Start the ONE guarded K3 call over every unmapped name (the capture job,
    normalisation mode), or answer with the one still live."""
    environment = os.environ if env is None else env
    try:
        require_enabled(environment)
        if not normalization.enabled(environment):
            raise _refusal("CATALOG_NORMALIZATION_DISABLED")
        _authorized_conversation(repo, user_id, project_id, conversation_id)
        if repo.project_member_role(project_id, user_id) != "owner":
            raise _refusal("CATALOG_NORMALIZATION_OWNER_ONLY")  # it spends the owner's daily budget
        if trigger is None:
            raise _refusal("CATALOG_REGISTER_UNAVAILABLE")
        _release_gate(trigger)
        names, evidence, active = _normalization_inputs(repo)
        try:
            rows = normalization.model_input(names, evidence, active)
        except normalization.NormalizationRefused:
            raise _refusal("CATALOG_NORMALIZATION_INPUT_TOO_LARGE") from None
        if not rows:
            raise _refusal("CATALOG_NORMALIZATION_NOTHING_UNMAPPED")
        claim = repo.request_manufacturer_normalization(user_id, rows, grace_seconds=START_GRACE_SECONDS)
        group, proposal = claim["group"], claim["proposal"]
        if claim["decision"] == "cooldown":
            raise _refusal("CATALOG_NORMALIZATION_COOLDOWN")
        if claim["decision"] != "claimed":
            # `existing` (one is live) or `reused` (this exact input already has
            # an answer): nothing starts, nothing is spent.
            return {"started": False, "proposal_id": str(proposal["id"]), "decision": claim["decision"]}

        def record(state: str, run_id: str | None, execution: str | None = None) -> None:
            repo.record_register_capture_trigger(group["id"], run_id=run_id, trigger_state=state,
                                                 execution_name=execution)

        run_id = _run_for(repo, conversation_id=conversation_id, user_id=user_id,
                          key=f"manufacturer-normalisation-{group['id']}", environment=environment)
        if run_id is None:
            record(trig.TRIGGER_FAILED, None)
            raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
        # The run is recorded on the group BEFORE the job starts: the job
        # claims this proposal and its run from the database, since it is
        # executed exactly as defined (no overrides, no id passed).
        record("claimed", run_id)
        outcome = trigger.run(manufacturer_normalisation())
        if outcome.state not in (trig.TRIGGERED, trig.TRIGGER_FAILED, trig.TRIGGER_UNKNOWN):
            outcome = trig.TriggerOutcome(trig.TRIGGER_UNKNOWN)
        record(outcome.state, run_id, outcome.execution_name)
        if outcome.state == trig.TRIGGER_FAILED:
            raise _refusal("CATALOG_REGISTER_TRIGGER_FAILED")
        return {"started": True, "proposal_id": str(proposal["id"]), "run_id": run_id}
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


def approve_normalization(repo: Any, user_id: UUID, project_id: UUID, *, expected_version: int,
                          groups: Sequence[Mapping[str, Any]], env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The owner approves pending groups exactly as proposed: several at once
    only when every one is high-confidence and conflicts with nothing; a
    low-confidence or conflicting group alone. Creates the next version."""
    try:
        require_enabled(env)
        if not _supported(repo.get_project(project_id, user_id)):
            raise _refusal("CATALOG_REGISTER_DISABLED")
        if repo.project_member_role(project_id, user_id) != "owner":
            raise _refusal("CATALOG_NORMALIZATION_OWNER_ONLY")
        pending = _pending(repo)["pending"]
        chosen = []
        for asked in groups:
            match = next((group for group in pending if _same_group(asked, group)), None)
            if match is None or match in chosen:
                raise _refusal("CATALOG_NORMALIZATION_APPROVAL_INVALID")
            chosen.append(match)
        if not chosen or (len(chosen) > 1 and not all(group["bulk_approvable"] for group in chosen)):
            raise _refusal("CATALOG_NORMALIZATION_APPROVAL_INVALID")
        entries = [{"source_tozar": member, "canonical_name": group["canonical"],
                    **({"provenance": "rule", "rule_id": group["rule_id"]} if group.get("rule_id")
                       else {"provenance": "model", "proposal_id": group["proposal_id"]})}
                   for group in chosen for member in group["members"]]
        try:
            return repo.approve_manufacturer_normalization(user_id, int(expected_version), entries)
        except AppError as refused:
            if refused.code in REQUEST_REASONS:
                raise _refusal(refused.code) from None
            raise
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


def _same_group(asked: Mapping[str, Any], group: Mapping[str, Any]) -> bool:
    """The group exactly as the server proposed it (provenance included)."""
    return (asked.get("canonical") == group["canonical"]
            and list(asked.get("members") or []) == list(group["members"])
            and asked.get("rule_id") == group.get("rule_id")
            and asked.get("proposal_id") == group.get("proposal_id"))


def reject_normalization(repo: Any, user_id: UUID, project_id: UUID, *, group: Mapping[str, Any],
                         env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The owner rejects ONE pending group, exactly as proposed: an
    append-only record; the group is no longer pending. Nothing is mapped."""
    try:
        require_enabled(env)
        if not _supported(repo.get_project(project_id, user_id)):
            raise _refusal("CATALOG_REGISTER_DISABLED")
        if repo.project_member_role(project_id, user_id) != "owner":
            raise _refusal("CATALOG_NORMALIZATION_OWNER_ONLY")
        match = next((pending for pending in _pending(repo)["pending"] if _same_group(group, pending)), None)
        if match is None:
            raise _refusal("CATALOG_NORMALIZATION_REJECTION_INVALID")
        body = {"canonical": match["canonical"], "members": list(match["members"]),
                **({"rule_id": match["rule_id"]} if match.get("rule_id") else {"proposal_id": match["proposal_id"]})}
        try:
            repo.reject_manufacturer_normalization_group(user_id, body)
        except AppError as refused:
            if refused.code in REQUEST_REASONS:
                raise _refusal(refused.code) from None
            raise
        return {"rejected": True}
    except AppError as refused:
        _log_refusal(project_id, refused.code)
        raise


__all__ = ["CapacityRefusal", "REGISTER_FLAG", "REQUEST_REASONS", "START_GRACE_SECONDS", "register_enabled",
           "approve_normalization", "normalization_view", "register_view", "request_capture",
           "request_directory_refresh", "request_normalization", "request_sync", "require_enabled",
           "server_can_capture", "reject_normalization", "claim_bytes_per_row", "last_sync", "register_busy",
           "unit_views"]
