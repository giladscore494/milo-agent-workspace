"""PR-SYNC-2: the register sync runs itself -- offline.

1. `decide()`: every rule and boundary, in a table (exactly 1 h / 24 h, the
   6 -> 12 -> 24 -> 24 h throttle backoff, the counter resets, a pause before
   any start).
2. The tick's authentication: only the dedicated scheduler identity; the
   gateway's own token, a wrong audience or issuer, an unverified email or no
   token is 401; missing or partial configuration is 503.
3. A due tick starts exactly one sync, under the recorded user, project and
   conversation; two concurrent ticks start at most one.
4. The switch is authorised like `request_sync`; Resume over capacity is refused.
5. Pauses: a capacity stop and two failures in a row pause; one Sentry event
   per transition, never on the skips that follow.
6. A replay of production (5.10): the 83-tozar backlog, throttles on runs 2
   and 3, hourly starts, the 6 h then 12 h cooldowns, convergence, then one
   start per 24 h.
And the kill switch: register capture off, every tick skips and starts nothing.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from backend import observability
from backend.catalog.register import autosync
from backend.catalog.register import service as register_service
from backend.gateway_auth import get_gateway_token_verifier
from backend.main import app
from backend.testing.memory_repository import MemoryRepository
from tests.test_register_capture import (OUTSIDER, USER, FakeTrigger, api_env, as_user, client,  # noqa: F401
                                         directory, no_sockets, world)

T0 = datetime(2026, 10, 5, 16, 29, tzinfo=UTC)
H = timedelta(hours=1)
MB = 1_000_000
AUDIENCE = "https://milo-agent-api.example.run.app"
SCHEDULER = "milo-register-scheduler@test-project.iam.gserviceaccount.com"
GATEWAY = "milo-gateway@test-project.iam.gserviceaccount.com"


def on(**fields: Any) -> dict[str, Any]:
    return {"enabled": True, "enabled_by": str(USER), "conversation_id": str(uuid4()),
            "consecutive_throttles": 0, "consecutive_failures": 0, **fields}


def finished(stop: str = "budget", left: int | None = 83, *, at: datetime = T0, status: str = "completed",
             run: str = "r1") -> dict[str, Any]:
    return {"run_id": run, "status": status, "stop": stop, "backlog": left, "finished_at": at}


# =============================================================================
# 1. decide(): the rules table
# =============================================================================

CASES = [
    # (label, schedule, last, busy, db_mb, now, register_on) -> (action, reason)
    ("flag off beats everything", on(paused_reason="SYNC_PAUSED_CAPACITY"), None, True, 500, T0, False,
     ("skip", "SYNC_REGISTER_DISABLED")),
    ("no schedule", None, None, False, 300, T0, True, ("skip", "SYNC_AUTO_OFF")),
    ("switch off", on(enabled=False), finished(), False, 300, T0 + 5 * H, True, ("skip", "SYNC_AUTO_OFF")),
    ("paused keeps its reason", on(paused_reason="SYNC_PAUSED_FAILING"), finished(), False, 300, T0 + 5 * H,
     True, ("skip", "SYNC_PAUSED_FAILING")),
    ("busy", on(), finished(), True, 300, T0 + 5 * H, True, ("skip", "SYNC_BUSY")),
    ("no sync yet", on(), None, False, 300, T0, True, ("start", "SYNC_FIRST")),
    ("backlog, 59 min", on(), finished(), False, 300, T0 + 59 * timedelta(minutes=1), True,
     ("skip", "SYNC_NOT_DUE")),
    ("backlog, exactly 1 h", on(), finished(), False, 300, T0 + H, True, ("start", "SYNC_BACKLOG")),
    ("unknown backlog (a crash) counts as backlog", on(), finished("", None, status="failed"), False, 300,
     T0 + H, True, ("start", "SYNC_BACKLOG")),
    ("complete, 23 h 59", on(), finished("complete", 0), False, 300, T0 + 24 * H - timedelta(minutes=1), True,
     ("skip", "SYNC_NOT_DUE")),
    ("complete, exactly 24 h", on(), finished("complete", 0), False, 300, T0 + 24 * H, True,
     ("start", "SYNC_DAILY_CHECK")),
    ("throttle 1: 6 h cooldown", on(consecutive_throttles=1), finished("throttled", status="failed"), False, 300,
     T0 + 6 * H - timedelta(seconds=1), True, ("skip", "SYNC_COOLING_DOWN")),
    ("throttle 1: at 6 h", on(consecutive_throttles=1), finished("throttled", status="failed"), False, 300,
     T0 + 6 * H, True, ("start", "SYNC_BACKLOG")),
    ("throttle 2: 12 h", on(consecutive_throttles=2), finished("throttled", status="failed"), False, 300,
     T0 + 12 * H - timedelta(seconds=1), True, ("skip", "SYNC_COOLING_DOWN")),
    ("throttle 2: at 12 h", on(consecutive_throttles=2), finished("throttled", status="failed"), False, 300,
     T0 + 12 * H, True, ("start", "SYNC_BACKLOG")),
    ("throttle 3: 24 h", on(consecutive_throttles=3), finished("throttled", status="failed"), False, 300,
     T0 + 24 * H - timedelta(seconds=1), True, ("skip", "SYNC_COOLING_DOWN")),
    ("throttle 4: still 24 h", on(consecutive_throttles=4), finished("throttled", status="failed"), False, 300,
     T0 + 24 * H, True, ("start", "SYNC_BACKLOG")),
    ("capacity stop pauses", on(), finished("capacity"), False, 300, T0 + 5 * H, True,
     ("pause", "SYNC_PAUSED_CAPACITY")),
    ("db at the threshold pauses", on(), finished(), False, 400, T0 + 5 * H, True,
     ("pause", "SYNC_PAUSED_CAPACITY")),
    ("db just under it does not", on(), finished(), False, 399.999999, T0 + 5 * H, True,
     ("start", "SYNC_BACKLOG")),
    ("a capacity stop before Resume is settled", on(resumed_at=(T0 + H).isoformat()), finished("capacity"),
     False, 300, T0 + 5 * H, True, ("start", "SYNC_BACKLOG")),
    ("two failures pause", on(consecutive_failures=2), finished(status="failed"), False, 300, T0 + 5 * H, True,
     ("pause", "SYNC_PAUSED_FAILING")),
    ("one failure does not", on(consecutive_failures=1), finished(status="failed"), False, 300, T0 + 5 * H,
     True, ("start", "SYNC_BACKLOG")),
    ("a pause beats a due start", on(consecutive_failures=2), None, False, 300, T0, True,
     ("pause", "SYNC_PAUSED_FAILING")),
]


@pytest.mark.parametrize("label,schedule,last,busy,db_mb,now,register_on,expected", CASES,
                         ids=[case[0] for case in CASES])
def test_decide(label, schedule, last, busy, db_mb, now, register_on, expected):
    assert autosync.decide(schedule, last, busy, int(db_mb * MB), now, register_on=register_on,
                           limit_bytes=400 * MB) == expected


def test_the_backoff_is_6_12_24_24_hours():
    assert [autosync.cooldown(n) for n in (1, 2, 3, 4, 9)] == [6 * H, 12 * H, 24 * H, 24 * H, 24 * H]


def test_counters_count_each_finished_sync_once_and_reset():
    state = on()
    throttled = finished("throttled", status="failed", run="a")
    state = autosync.observe(state, throttled)
    assert (state["consecutive_throttles"], state["consecutive_failures"]) == (1, 0)
    assert autosync.observe(state, throttled) == state  # the same run is never counted twice
    state = autosync.observe(state, finished("throttled", status="failed", run="b"))
    assert state["consecutive_throttles"] == 2
    state = autosync.observe(state, finished(status="failed", run="c"))  # a non-throttled finish
    assert (state["consecutive_throttles"], state["consecutive_failures"]) == (0, 1)
    state = autosync.observe(state, finished("throttled", status="failed", run="d"))  # a throttle keeps failures
    assert (state["consecutive_throttles"], state["consecutive_failures"]) == (1, 1)
    state = autosync.observe(state, finished(status="failed", run="e"))
    assert (state["consecutive_throttles"], state["consecutive_failures"]) == (0, 2)
    state = autosync.observe(state, finished(status="completed", run="f"))  # a success resets both
    assert (state["consecutive_throttles"], state["consecutive_failures"], state["counted_run_id"]) == (0, 0, "f")


def test_the_warning_is_360_mb_of_the_default_400_mb_limit():
    assert autosync.decide.__kwdefaults__["limit_bytes"] == 400 * MB
    assert 400 * MB * autosync.WARNING_FRACTION == 360 * MB


# =============================================================================
# a world: the API, the switch, a tick, and syncs finished by hand
# =============================================================================

class World:
    def __init__(self, *, rows: int = 41137) -> None:
        self.repo, self.w = world()
        directory(self.repo)
        self.trigger = FakeTrigger()
        self.api = client(self.repo, self.trigger)
        self.project = UUID(self.w["project"])

    def switch(self, action: str, *, user: UUID = USER, conversation: str | None = None):
        return self.api.post(f"/projects/{self.project}/register/auto-sync", headers=as_user(user),
                             json={"action": action, "conversation_id": conversation or self.w["conversation"]})

    def enable(self) -> None:
        """The switch, without the browser gateway (the tick tests configure the real one)."""
        autosync.set_switch(self.repo, USER, self.project, action="on", conversation_id=UUID(self.w["conversation"]))

    def tick(self, now: datetime, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        import os
        return autosync.tick(self.repo, trigger=self.trigger, env=dict(os.environ, **(env or {})), now=now)

    def started(self) -> list[str]:
        out = []
        for invocation in self.trigger.calls:
            args = list(invocation.entrypoint_args)
            out.append(args[args.index("--run-id") + 1])
        return out

    def finish(self, run_id: str, *, at: datetime, stop: str = "budget", left: int = 83, rows: int = 41137,
               status: str = "completed", summary: bool = True) -> None:
        output = {"sync": {"summary": (f"SYNC_SUMMARY|changed=false|directory_version=abcdefabcdef|work={left}|"
                                       f"captured=0|reused=0|failed=0|deferred=0|requests=78/80|stop={stop}|"
                                       f"backlog={left}/{rows}|coverage=98930/101714"),
                           "stop": stop, "basis": {}}} if summary else None
        self.repo.runs[run_id].update(status=status, output=output, finished_at=at.isoformat())


@pytest.fixture
def sentry(monkeypatch) -> list[str]:
    sent: list[str] = []
    monkeypatch.setattr(observability, "report_sync_paused", lambda code: sent.append(code) or True)
    return sent


# =============================================================================
# 2. authentication of the tick
# =============================================================================

def claims(**overrides: Any) -> dict[str, Any]:
    return {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": SCHEDULER, "email_verified": True,
            **overrides}


class FakeVerifier:
    TOKENS = {"scheduler": claims(), "gateway": claims(email=GATEWAY), "issuer": claims(iss="https://evil.test"),
              "unverified": claims(email_verified=False), "aud-claim": claims(aud="https://other.test")}

    def verify(self, token: str, audience: str) -> dict[str, Any]:
        assert audience == AUDIENCE
        if token == "wrong-audience":
            raise ValueError("Token has wrong audience")
        if token not in self.TOKENS:
            raise ValueError("Could not verify token signature")
        return dict(self.TOKENS[token])


@pytest.fixture
def scheduler_env(monkeypatch):
    monkeypatch.setenv("MILO_GATEWAY_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("MILO_REGISTER_SCHEDULER_IDENTITY", SCHEDULER)
    monkeypatch.setenv("MILO_APPROVED_GATEWAY_IDENTITIES", GATEWAY)
    app.dependency_overrides[get_gateway_token_verifier] = FakeVerifier
    yield
    app.dependency_overrides.pop(get_gateway_token_verifier, None)


def post_tick(world_: World, token: str | None):
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return world_.api.post("/internal/register/sync-tick", headers=headers)


def test_only_the_scheduler_identity_ticks(scheduler_env, capsys):
    w = World()
    w.enable()
    accepted = post_tick(w, "scheduler")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["decision"] == "start" and len(w.trigger.calls) == 1
    assert capsys.readouterr().out.count("SYNC_TICK|") == 1
    for token in ("gateway", "wrong-audience", "issuer", "unverified", "aud-claim", "garbage", None):
        refused = post_tick(w, token)
        assert refused.status_code == 401, token
        assert refused.json()["error"]["code"] == "SCHEDULER_AUTH_INVALID"
    # Browser identity headers are never a scheduler.
    assert w.api.post("/internal/register/sync-tick", headers=as_user()).status_code == 401
    assert len(w.trigger.calls) == 1


@pytest.mark.parametrize("unset", ["MILO_GATEWAY_AUDIENCE", "MILO_REGISTER_SCHEDULER_IDENTITY"])
def test_missing_or_partial_configuration_is_503_and_runs_nothing(scheduler_env, monkeypatch, unset):
    w = World()
    w.enable()
    monkeypatch.delenv(unset)
    answer = post_tick(w, "scheduler")
    assert answer.status_code == 503 and answer.json()["error"]["code"] == "SCHEDULER_AUTH_NOT_CONFIGURED"
    assert w.trigger.calls == []


@pytest.mark.parametrize("variable", ["MILO_APPROVED_GATEWAY_IDENTITIES", "MILO_APPROVED_WORKER_IDENTITIES"])
def test_a_scheduler_identity_shared_with_the_gateway_or_worker_is_503(scheduler_env, monkeypatch, variable):
    w = World()
    w.enable()
    monkeypatch.setenv(variable, f"other@test-project.iam.gserviceaccount.com,{SCHEDULER.upper()}")
    assert post_tick(w, "scheduler").status_code == 503 and w.trigger.calls == []


# =============================================================================
# 3. tick -> start: once, under the recorded identity; single flight
# =============================================================================

def test_a_due_tick_starts_one_sync_under_the_recorded_user_project_and_conversation(capsys):
    w = World()
    assert w.switch("on").status_code == 200
    record = w.tick(T0)
    assert (record["decision"], record["reason"]) == ("start", "SYNC_FIRST")
    (run_id,) = w.started()
    run = w.repo.runs[run_id]
    assert (run["conversation_id"], run["requested_by"]) == (w.w["conversation"], str(USER))
    assert w.repo.register_directory_groups(1)[0]["requested_by"] == str(USER)
    line = capsys.readouterr().out.strip()
    assert line == "SYNC_TICK|decision=start|reason=SYNC_FIRST|backlog=unknown|db_mb=107.0"
    # The sync is live: the next tick skips (busy), records it, and starts nothing.
    assert w.tick(T0 + H)["reason"] == "SYNC_BUSY" and len(w.started()) == 1
    assert w.repo.register_sync_schedule(w.project)["last_tick"]["reason"] == "SYNC_BUSY"


def test_two_concurrent_ticks_start_at_most_one_sync():
    w = World()
    w.switch("on")
    barrier = threading.Barrier(2)
    real_busy = register_service.register_busy

    def both_see_idle(repo: Any) -> bool:
        answer = real_busy(repo)
        barrier.wait(timeout=10)  # both ticks decide before either claims
        return answer

    results: list[dict[str, Any]] = []
    original = register_service.register_busy
    register_service.register_busy = both_see_idle
    try:
        threads = [threading.Thread(target=lambda: results.append(w.tick(T0))) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    finally:
        register_service.register_busy = original
    assert len(w.trigger.calls) == 1
    assert sorted(r["decision"] for r in results) == ["skip", "start"]
    assert {r["reason"] for r in results if r["decision"] == "skip"} == {"SYNC_BUSY"}


def test_a_switch_turned_off_starts_nothing_and_off_beats_a_due_tick():
    w = World()
    w.switch("on")
    w.switch("off")
    assert w.tick(T0)["reason"] == "SYNC_AUTO_OFF" and w.trigger.calls == []


def test_the_kill_switch_turns_every_tick_into_a_skip(monkeypatch, capsys):
    w = World()
    w.switch("on")
    for value in ("false", ""):
        monkeypatch.setenv(register_service.REGISTER_FLAG, value)
        record = w.tick(T0)
        assert (record["decision"], record["reason"]) == ("skip", "SYNC_REGISTER_DISABLED")
    assert w.trigger.calls == [] and w.repo.register_directory_groups(1) == []
    assert capsys.readouterr().out.count("SYNC_TICK|decision=skip|reason=SYNC_REGISTER_DISABLED|") == 2


# =============================================================================
# 4. the switch
# =============================================================================

def test_the_switch_is_authorised_like_a_sync():
    w = World()
    # Another user's conversation (their own project) is refused, and records nothing.
    theirs = str(uuid4())
    w.repo.seed_project(theirs, f"p-{theirs[:8]}", "P", [str(OUTSIDER)], workflow_key="swarm_v2")
    other_conversation = w.repo.create_conversation(UUID(theirs), "theirs", OUTSIDER)["id"]
    for action in ("on", "off", "resume"):
        refused = w.switch(action, conversation=other_conversation)
        assert refused.status_code == 404, action
        assert refused.json()["error"]["code"] == "CATALOG_REGISTER_CONVERSATION_UNAVAILABLE"
        # Someone who is not a member of the project, with their own conversation.
        assert w.switch(action, user=OUTSIDER, conversation=other_conversation).status_code == 404
    assert w.repo.register_sync_schedule(w.project) is None
    answer = w.switch("on")
    assert answer.status_code == 200 and answer.json()["enabled"] is True
    row = w.repo.register_sync_schedule(w.project)
    assert (row["enabled_by"], row["conversation_id"]) == (str(USER), w.w["conversation"])
    assert w.switch("off").json()["enabled"] is False
    assert w.api.post(f"/projects/{w.project}/register/auto-sync", headers=as_user(),
                      json={"action": "pause", "conversation_id": w.w["conversation"]}).status_code == 422


def test_the_switch_is_closed_while_register_capture_is_off(monkeypatch):
    w = World()
    monkeypatch.setenv(register_service.REGISTER_FLAG, "false")
    assert w.switch("on").status_code == 403
    assert w.repo.register_sync_schedule(w.project) is None


def test_resume_over_capacity_is_refused_and_says_what_to_run(sentry):
    w = World()
    w.switch("on")
    w.repo.register_database_bytes = 401 * MB
    assert w.tick(T0)["reason"] == "SYNC_PAUSED_CAPACITY"
    refused = w.switch("resume")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "SYNC_RESUME_OVER_CAPACITY"
    assert "vacuum-full" in refused.json()["error"]["message"]
    page = w.api.get(f"/projects/{w.project}/register", headers=as_user()).json()["auto_sync"]
    assert page["paused_reason"] == "SYNC_PAUSED_CAPACITY" and "vacuum-full" in page["next_step"]
    w.repo.register_database_bytes = 330 * MB  # after VACUUM FULL
    resumed = w.switch("resume").json()
    assert resumed["paused_reason"] is None and resumed["next"] == {"decision": "start", "reason": "SYNC_FIRST"}
    assert w.tick(T0 + H)["decision"] == "start"


# =============================================================================
# 5. pauses and their one Sentry event
# =============================================================================

def test_a_capacity_stop_pauses_and_the_next_ticks_skip(sentry):
    w = World()
    w.switch("on")
    w.tick(T0)
    (run_id,) = w.started()
    w.finish(run_id, at=T0 + timedelta(minutes=5), stop="capacity")
    for hour in range(1, 5):
        record = w.tick(T0 + hour * H)
        assert (record["decision"], record["reason"]) == ("skip", "SYNC_PAUSED_CAPACITY")
    assert len(w.started()) == 1 and sentry == ["SYNC_PAUSED_CAPACITY"]
    row = w.repo.register_sync_schedule(w.project)
    assert row["paused_reason"] == "SYNC_PAUSED_CAPACITY" and row["paused_at"] == (T0 + H).isoformat()
    # Resumed (the database is under the threshold): that old capacity stop is settled.
    assert w.switch("resume").status_code == 200
    assert w.tick(T0 + 6 * H)["decision"] == "start" and sentry == ["SYNC_PAUSED_CAPACITY"]


def test_two_consecutive_failures_pause_once(sentry):
    w = World()
    w.switch("on")
    now = T0
    for _ in range(2):
        assert w.tick(now)["decision"] == "start"
        # A sync that crashed: failed, no summary (only its idempotency key says it was a sync).
        w.finish(w.started()[-1], at=now + timedelta(minutes=3), status="failed", summary=False)
        now += 2 * H
    reasons = [w.tick(now + n * H)["reason"] for n in range(3)]
    assert reasons == ["SYNC_PAUSED_FAILING"] * 3 and len(w.started()) == 2
    assert sentry == ["SYNC_PAUSED_FAILING"]
    page = w.switch("resume").json()
    assert page["paused_reason"] is None and page["consecutive_failures"] == 0
    assert w.tick(now + 4 * H)["decision"] == "start"


def test_a_failure_then_a_success_never_pauses(sentry):
    w = World()
    w.switch("on")
    w.tick(T0)
    w.finish(w.started()[-1], at=T0, status="failed")
    w.tick(T0 + H)
    w.finish(w.started()[-1], at=T0 + H)
    w.tick(T0 + 2 * H)
    w.finish(w.started()[-1], at=T0 + 2 * H, status="failed")
    assert w.tick(T0 + 3 * H)["decision"] == "start" and sentry == []


def test_without_a_dsn_nothing_is_sent():
    assert observability.report_sync_paused("SYNC_PAUSED_CAPACITY") is False


def test_the_page_shows_the_status_the_last_tick_and_a_stale_scheduler():
    w = World()
    page = w.api.get(f"/projects/{w.project}/register", headers=as_user()).json()
    assert page["auto_sync"]["enabled"] is False and page["auto_sync"]["last_tick"] is None
    assert page["auto_sync"]["next"] == {"decision": "skip", "reason": "SYNC_AUTO_OFF"}
    w.switch("on")
    long_ago = datetime.now(UTC) - 3 * H
    w.tick(long_ago)
    view = w.api.get(f"/projects/{w.project}/register", headers=as_user()).json()["auto_sync"]
    assert view["enabled"] is True and view["scheduler_stale"] is False  # enabled just now
    w.repo.update_register_sync_schedule(w.project, {"enabled_at": long_ago.isoformat()})
    view = w.api.get(f"/projects/{w.project}/register", headers=as_user()).json()["auto_sync"]
    assert view["scheduler_stale"] is True
    assert view["last_tick"]["decision"] == "start" and view["last_tick"]["reason"] == "SYNC_FIRST"
    w.repo.register_database_bytes = 365 * MB
    view = w.api.get(f"/projects/{w.project}/register", headers=as_user()).json()["auto_sync"]
    assert view["db_warning"] is True


def test_the_db_warning_rides_the_tick_line(capsys):
    w = World()
    w.switch("on")
    w.repo.register_database_bytes = 360 * MB
    w.tick(T0)
    assert capsys.readouterr().out.strip().endswith("|db_mb=360.0|warning=SYNC_DB_NEAR_CAPACITY")


# =============================================================================
# 6. the production replay (5.10)
# =============================================================================

def replay(*, sync_minutes: int, ticks: int = 24 * 5) -> tuple[list[tuple[datetime, str]], datetime]:
    """Sync #1 finished 5.10 16:29 (backlog 83/41137, stop=budget). The switch
    is on; Cloud Scheduler ticks hourly at minute 7. Each fake sync captures
    18 tozars (sync #1's rate), takes `sync_minutes`, and runs 2 and 3 are
    throttled before capturing anything."""
    w = World()
    w.switch("on")
    w.tick(T0 - 3 * H)  # seeds sync #1 through the same path...
    w.finish(w.started()[-1], at=T0, left=83)  # ...as production recorded it
    left, rows_per = 83, 41137 / 83
    starts: list[tuple[datetime, str]] = []
    converged = None
    now = datetime(2026, 10, 5, 17, 7, tzinfo=UTC)
    for _ in range(ticks):
        record = w.tick(now)
        if record["decision"] == "start":
            run = len(starts) + 1
            if run in (2, 3):
                w.finish(w.started()[-1], at=now + timedelta(minutes=sync_minutes), stop="throttled",
                         left=left, rows=round(left * rows_per), status="failed")
                starts.append((now, "throttled"))
            else:
                left = max(0, left - 18)
                w.finish(w.started()[-1], at=now + timedelta(minutes=sync_minutes),
                         stop="budget" if left else "complete", left=left, rows=round(left * rows_per))
                starts.append((now, f"backlog={left}"))
                if left == 0 and converged is None:
                    converged = now + timedelta(minutes=sync_minutes)
        now += H
    return starts, converged


def test_the_production_replay_converges_then_checks_daily():
    starts, converged = replay(sync_minutes=0)
    times = [at for at, _ in starts]
    labels = [label for _, label in starts]
    assert labels[:7] == ["backlog=65", "throttled", "throttled", "backlog=47", "backlog=29", "backlog=11",
                          "backlog=0"]
    gaps = [(b - a) / H for a, b in zip(times, times[1:])]
    # hourly; 6 h after the first throttle; 12 h after the second; hourly to 0; then daily.
    assert gaps[:6] == [1, 6, 12, 1, 1, 1] and set(gaps[6:]) == {24} and len(gaps[6:]) >= 3
    hours = (converged - datetime(2026, 10, 5, 17, 7, tzinfo=UTC)) / H
    assert hours == 23
    realistic, at = replay(sync_minutes=5)
    # A real sync takes ~5 min (5.10: 16:24-16:29): an hourly tick then finds
    # 55 min since the finish, so a backlog start lands every second tick.
    gaps5 = [(b[0] - a[0]) / H for a, b in zip(realistic, realistic[1:])]
    assert gaps5[:6] == [2, 7, 13, 2, 2, 2]
    hours5 = (at - datetime(2026, 10, 5, 17, 7, tzinfo=UTC)) / H
    assert round(hours5, 2) == 29.08
    print(f"\nREPLAY|instant_syncs=backlog 0/0 after {hours:g} h|five_minute_syncs=after {hours5:.2f} h")


# =============================================================================
# review findings: the pause is global; a start that never ran is a failure
# =============================================================================

def test_a_second_projects_switch_neither_hides_nor_leaves_a_pause(sentry):
    w = World()
    w.switch("on")
    for n in range(2):
        w.tick(T0 + 2 * n * H)
        w.finish(w.started()[-1], at=T0 + 2 * n * H, status="failed")
    assert w.tick(T0 + 4 * H)["reason"] == "SYNC_PAUSED_FAILING"
    theirs = str(uuid4())
    w.repo.seed_project(theirs, f"p-{theirs[:8]}", "P", [str(USER)], workflow_key="swarm_v2")
    conversation = w.repo.create_conversation(UUID(theirs), "b", USER)["id"]
    autosync.set_switch(w.repo, USER, UUID(theirs), action="on", conversation_id=UUID(conversation))
    page_b = autosync.view(w.repo, UUID(theirs))
    assert page_b["paused_reason"] == "SYNC_PAUSED_FAILING"
    assert page_b["next"]["reason"] == "SYNC_PAUSED_FAILING"
    w.switch("off")  # B drives now: still paused
    assert w.tick(T0 + 5 * H)["reason"] == "SYNC_PAUSED_FAILING" and len(w.started()) == 2
    autosync.set_switch(w.repo, USER, UUID(theirs), action="resume", conversation_id=UUID(conversation))
    assert all(row["paused_reason"] is None for row in w.repo.register_sync_schedules())
    assert w.tick(T0 + 6 * H)["decision"] == "start" and sentry == ["SYNC_PAUSED_FAILING"]


def test_a_start_whose_job_never_triggers_counts_as_a_failure(sentry):
    from backend.catalog.scope import prepare_trigger as trig

    w = World()
    w.trigger.state = trig.TRIGGER_FAILED
    w.switch("on")
    reasons = [w.tick(T0 + n * H)["reason"] for n in range(3)]
    assert reasons == ["CATALOG_REGISTER_TRIGGER_FAILED", "CATALOG_REGISTER_TRIGGER_FAILED",
                       "SYNC_PAUSED_FAILING"] and sentry == ["SYNC_PAUSED_FAILING"]

