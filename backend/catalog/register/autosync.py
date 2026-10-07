"""PR-SYNC-2: WHEN the register sync (PR-SYNC-1, unchanged) starts by itself.

Once per Cloud Scheduler tick, `decide` (pure; the rules in REGISTER_CAPTURE.md)
says whether to start one through the existing single-flight
`service.request_sync`, as the user who turned the Register page's Auto sync
switch on. The register is global and the schedule one row per project: the
earliest switch that is on drives the sync, every row gets the tick, and only
the owner's Resume leaves a pause.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID

from backend import observability
from backend.catalog.register import config as register_config
from backend.catalog.register import service
from backend.errors import AppError

START, SKIP, PAUSE = "start", "skip", "pause"
BACKLOG_INTERVAL = timedelta(hours=1)
DAILY_INTERVAL = timedelta(hours=24)
THROTTLE_COOLDOWN = timedelta(hours=6)
THROTTLE_COOLDOWN_CAP = timedelta(hours=72)
#: By throttles in a row: 6, 12, 24 h, then the cap for the 4th and every later one.
THROTTLE_COOLDOWNS = (THROTTLE_COOLDOWN, 2 * THROTTLE_COOLDOWN, 4 * THROTTLE_COOLDOWN, THROTTLE_COOLDOWN_CAP)
FAILURES_BEFORE_PAUSE = 2
#: The non-blocking warning: 90% of the capacity limit (360 MB of 400 MB).
WARNING_FRACTION = 0.9
#: A switch that is on with no tick for this long: "Scheduler not ticking".
STALE_TICK = timedelta(hours=2)

PAUSED_CAPACITY, PAUSED_FAILING = "SYNC_PAUSED_CAPACITY", "SYNC_PAUSED_FAILING"
NEXT_STEP = {
    PAUSED_CAPACITY: ("The database reached its capacity threshold. Run the Register retention workflow "
                      "with mode vacuum-full (reviewer-gated), then press Resume."),
    PAUSED_FAILING: ("Two syncs in a row failed. Open the last sync's run, fix its failure code, "
                     "then press Resume."),
}
_SUCCEEDED = ("completed", "partial_success")
_BACKLOG = re.compile(r"\|backlog=(\d+)/(\d+)(?:\||$)")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,79}$")
_TICK_FIELDS = ("at", "decision", "reason", "backlog", "db_mb", "warning")
#: The register is global: every row carries the same pause and counters (a
#: second project's switch can neither hide a pause nor leave it).
_SHARED = ("paused_reason", "paused_at", "resumed_at", "consecutive_throttles", "consecutive_failures",
           "counted_run_id")


def _time(value: Any) -> datetime | None:
    return service._time(value) if value else None


def backlog(summary: Any) -> tuple[int, int] | None:
    """(tozars, rows) left, from a `SYNC_SUMMARY|...` line."""
    match = _BACKLOG.search(str(summary or ""))
    return (int(match.group(1)), int(match.group(2))) if match else None


def last_attempt(repo: Any) -> dict[str, Any] | None:
    """The newest FINISHED sync, whatever its ending -- a failed sync with no
    summary too (`service.last_sync` reads only those with one)."""
    for group in repo.register_directory_groups(service.SYNC_LOOKBACK):
        run = service._run(repo, group) or {}
        if run.get("status") not in service._ENDED:
            continue
        sync = (run.get("output") or {}).get("sync")
        if not isinstance(sync, Mapping) and not str(run.get("idempotency_key") or "").startswith("register-sync-"):
            continue  # a plain directory refresh
        sync = sync if isinstance(sync, Mapping) else {}
        left = backlog(sync.get("summary"))
        return {"run_id": str(run.get("id")), "status": run.get("status"), "stop": sync.get("stop"),
                "summary": sync.get("summary"), "backlog": None if left is None else left[0],
                "backlog_rows": None if left is None else left[1],
                "finished_at": _time(run.get("finished_at")) or _time(run.get("updated_at"))}
    return None


def observe(schedule: Mapping[str, Any], last: Mapping[str, Any] | None) -> dict[str, Any]:
    """Count a newly finished sync, once: a throttle adds one throttle; any other
    finish resets the throttles; a success resets the failures, anything else
    adds one."""
    out = dict(schedule)
    if last is None or str(last["run_id"]) == str(schedule.get("counted_run_id") or ""):
        return out
    out["counted_run_id"] = str(last["run_id"])
    if last.get("stop") == "throttled":
        out["consecutive_throttles"] = int(schedule.get("consecutive_throttles") or 0) + 1
        return out
    out["consecutive_throttles"] = 0
    out["consecutive_failures"] = (0 if last.get("status") in _SUCCEEDED
                                   else int(schedule.get("consecutive_failures") or 0) + 1)
    return out


def cooldown(throttles: int) -> timedelta:
    """6, 12, 24, then 72 h (a fixed table: no 48 h step); 0 throttles reads as 1."""
    return THROTTLE_COOLDOWNS[min(max(1, throttles), len(THROTTLE_COOLDOWNS)) - 1]


def decide(schedule: Mapping[str, Any] | None, last_sync: Mapping[str, Any] | None, busy: bool, db_bytes: int,
           now: datetime, *, register_on: bool = True,
           limit_bytes: int = register_config.load({}).capacity_limit_bytes) -> tuple[str, str]:
    """(action, reason): the first rule that applies (REGISTER_CAPTURE.md)."""
    if not register_on:
        return SKIP, "SYNC_REGISTER_DISABLED"
    if not schedule or not schedule.get("enabled"):
        return SKIP, "SYNC_AUTO_OFF"
    if schedule.get("paused_reason"):
        return SKIP, str(schedule["paused_reason"])
    if busy:
        return SKIP, "SYNC_BUSY"
    finished = last_sync.get("finished_at") if last_sync else None
    if last_sync and last_sync.get("stop") == "throttled" and finished is not None \
            and now < finished + cooldown(int(schedule.get("consecutive_throttles") or 0)):
        return SKIP, "SYNC_COOLING_DOWN"
    resumed = _time(schedule.get("resumed_at"))
    fresh = last_sync is not None and (resumed is None or finished is None or finished > resumed)
    if (fresh and last_sync.get("stop") == "capacity") or db_bytes >= limit_bytes:
        return PAUSE, PAUSED_CAPACITY
    if int(schedule.get("consecutive_failures") or 0) >= FAILURES_BEFORE_PAUSE:
        return PAUSE, PAUSED_FAILING
    if last_sync is None or finished is None:
        return START, "SYNC_FIRST"
    left = last_sync.get("backlog")
    if (left is None or left > 0) and now - finished >= BACKLOG_INTERVAL:
        return START, "SYNC_BACKLOG"
    if left == 0 and now - finished >= DAILY_INTERVAL:
        return START, "SYNC_DAILY_CHECK"
    return SKIP, "SYNC_NOT_DUE"


def _driving(rows: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    on = [row for row in rows if row.get("enabled")]
    return min(on, key=lambda row: str(row.get("enabled_at") or "")) if on else None


def _shared(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """The global state: a paused row's if any, else the newest-ticked row's."""
    source = next((row for row in rows if row.get("paused_reason")), None) or max(
        rows, key=lambda row: str((row.get("last_tick") or {}).get("at") or ""), default={})
    return {key: source.get(key) for key in _SHARED if source.get(key) is not None}


def _db_mb(db_bytes: int | None) -> str:
    return "-" if db_bytes is None else f"{db_bytes / 1_000_000:.1f}"


def tick(repo: Any, *, trigger: Any, env: Mapping[str, str] | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """One scheduled tick. Starts at most one sync; returns the tick record."""
    environment = os.environ if env is None else env
    now = now or datetime.now(UTC)
    register_on = service.register_enabled(environment)
    rows = repo.register_sync_schedules()
    driving = _driving(rows)
    state: dict[str, Any] = {}
    last, db_bytes, warning = None, None, ""
    if not register_on or driving is None:
        action, reason = decide(driving, None, False, 0, now, register_on=register_on)
    else:
        limit = register_config.load(environment).capacity_limit_bytes
        last = last_attempt(repo)
        before = dict(driving)
        schedule = observe(driving, last)
        db_bytes = int(repo.catalog_database_bytes())
        warning = "SYNC_DB_NEAR_CAPACITY" if db_bytes >= limit * WARNING_FRACTION else ""
        action, reason = decide(schedule, last, service.register_busy(repo), db_bytes, now, limit_bytes=limit)
        if action == PAUSE:
            schedule.update(paused_reason=reason, paused_at=now.isoformat())
            observability.report_sync_paused(reason)  # a transition: a paused row skips before this
        elif action == START:
            current = repo.register_sync_schedule(UUID(str(driving["project_id"])))
            if not current or not current.get("enabled") or current.get("paused_reason"):
                action, reason = SKIP, "SYNC_AUTO_OFF"  # the owner turned it off meanwhile
            else:
                try:
                    service.request_sync(repo, UUID(str(driving["enabled_by"])), UUID(str(driving["project_id"])),
                                         conversation_id=UUID(str(driving["conversation_id"])),
                                         trigger=trigger, env=environment)
                except AppError as refused:
                    action = SKIP
                    reason = ("SYNC_BUSY" if refused.code == "CATALOG_REGISTER_BUSY"
                              else refused.code if _CODE.fullmatch(str(refused.code)) else "SYNC_START_REFUSED")
                    if refused.code == "CATALOG_REGISTER_TRIGGER_FAILED":  # a start that never ran: a failure
                        schedule["consecutive_failures"] = int(schedule.get("consecutive_failures") or 0) + 1
        state = {key: schedule.get(key) for key in _SHARED}
        current = repo.register_sync_schedule(UUID(str(driving["project_id"]))) or {}
        if current.get("resumed_at") != before.get("resumed_at"):
            state = {}  # the owner pressed Resume during this tick: theirs wins
    left = "unknown" if last is None or last.get("backlog") is None else f"{last['backlog']}/{last['backlog_rows']}"
    record = {"at": now.isoformat(), "decision": START if action == START else SKIP, "reason": reason,
              "backlog": left, "db_mb": _db_mb(db_bytes), "warning": warning}
    for row in rows:
        repo.update_register_sync_schedule(UUID(str(row["project_id"])), {**state, "last_tick": record})
    print("SYNC_TICK|" + "|".join(f"{key}={record[key]}" for key in _TICK_FIELDS[1:5])
          + (f"|warning={warning}" if warning else ""), flush=True)
    return record


# -- the owner's switch and the page ---------------------------------------------

def set_switch(repo: Any, user_id: UUID, project_id: UUID, *, action: str, conversation_id: UUID,
               env: Mapping[str, str] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """On / off / resume, authorised exactly like `request_sync`."""
    environment = os.environ if env is None else env
    now = now or datetime.now(UTC)
    service.require_enabled(environment)
    service._authorized_conversation(repo, user_id, project_id, conversation_id)
    if action == "on":
        repo.upsert_register_sync_schedule(project_id, {
            **_shared(repo.register_sync_schedules()), "enabled": True, "enabled_by": str(user_id),
            "conversation_id": str(conversation_id), "enabled_at": now.isoformat()})
    elif action == "off":
        if repo.register_sync_schedule(project_id) is not None:
            repo.update_register_sync_schedule(project_id, {"enabled": False})
    elif action == "resume":
        row = repo.register_sync_schedule(project_id) or {}
        if row.get("paused_reason"):
            if row["paused_reason"] == PAUSED_CAPACITY and int(repo.catalog_database_bytes()) \
                    >= register_config.load(environment).capacity_limit_bytes:
                raise AppError("SYNC_RESUME_OVER_CAPACITY", "the database is still above its capacity "
                               "threshold: run Register retention with mode vacuum-full first", 409)
            last = last_attempt(repo)
            for other in repo.register_sync_schedules():  # the pause is global, and so is Resume
                repo.update_register_sync_schedule(UUID(str(other["project_id"])), {
                    "paused_reason": None, "paused_at": None, "resumed_at": now.isoformat(),
                    "consecutive_failures": 0, **({"counted_run_id": last["run_id"]} if last else {})})
    else:
        raise AppError("SYNC_SWITCH_INVALID", "the action must be on, off or resume", 422)
    return view(repo, project_id, env=environment, now=now)


def view(repo: Any, project_id: UUID, *, env: Mapping[str, str] | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """The page's Auto sync block: the switch, the pause and its next step, the
    last tick, what the next tick would decide, and the two warnings."""
    environment = os.environ if env is None else env
    now = now or datetime.now(UTC)
    row = repo.register_sync_schedule(project_id) or {}
    limit = register_config.load(environment).capacity_limit_bytes
    db_bytes = int(repo.catalog_database_bytes())
    last = last_attempt(repo)
    action, reason = decide(observe(row, last), last, service.register_busy(repo), db_bytes, now,
                            register_on=service.register_enabled(environment), limit_bytes=limit)
    tick_record = row.get("last_tick") if isinstance(row.get("last_tick"), Mapping) else None
    seen = max(filter(None, (_time((tick_record or {}).get("at")), _time(row.get("enabled_at")))), default=None)
    paused = row.get("paused_reason") or None
    return {
        "enabled": bool(row.get("enabled")),
        "paused_reason": paused, "paused_at": row.get("paused_at") if paused else None,
        "next_step": NEXT_STEP.get(str(paused)) if paused else None,
        "consecutive_throttles": int(row.get("consecutive_throttles") or 0),
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "last_tick": {key: tick_record.get(key) for key in _TICK_FIELDS} if tick_record else None,
        "next": {"decision": START if action == START else SKIP, "reason": reason},
        "scheduler_stale": bool(row.get("enabled")) and (seen is None or now - seen > STALE_TICK),
        "db_warning": db_bytes >= limit * WARNING_FRACTION,
    }


__all__ = ["NEXT_STEP", "PAUSE", "SKIP", "START", "decide", "last_attempt", "observe", "set_switch", "tick",
           "view"]
