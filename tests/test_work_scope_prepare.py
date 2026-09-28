"""E': prepare ONE Mapping Plan revision from the website -- offline.

What this proves
----------------

1.  The API executes the EXISTING capture job with the ONE shared invocation
    (`backend/capture_invocation.py`), byte for byte what
    `government-production-capture.sh --prepare-work-scope` executes (golden
    test through the script's mock `gcloud`).
2.  Idempotency by (plan, revision, digest): a second request -- and two
    CONCURRENT requests -- start exactly one capture; every other caller is
    answered with the preparation in flight.
3.  Authorization: a non-member is refused, and nothing is claimed, created or
    executed for them.
4.  Stale detection: a request for a revision that is no longer the head is
    refused; the status of a revision whose head moved reads `stale`.
5.  The status is derived from DURABLE state only -- the request row, the
    capture run's durable status and the preparation row -- never from what the
    trigger (an execution's exit) answered.
6.  The capture job must run the deployed release image, or nothing runs.
7.  Production scale: a Toyota 2018+ revision over a 6,374-row snapshot is
    read in bounded rows only.

Offline and deterministic: an autouse fixture refuses every outbound
connection, and the trigger is a fake that records what it was asked to run.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend import capture_invocation as ci
from backend.catalog import operator_capture
from backend.catalog.government import source as src
from backend.catalog.scope import contract as wsc
from backend.catalog.scope import prepare_trigger as trig
from backend.catalog.scope import service as ws
from backend.catalog.scope import web_preparation as wp
from backend.dependencies import get_capture_trigger, get_repository
from backend.finalization import RunFinalizer, TerminalClaim
from backend.main import app
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import prepare_plan_head

USER = UUID("11111111-2222-4333-8444-555555555555")
MEMBER = UUID("22222222-2222-4333-8444-555555555555")
OUTSIDER = UUID("99999999-2222-4333-8444-555555555555")
RELEASE_SHA = "84cd8696119c24662a954d0f0e23195268dab23f"
PROJECT_REF = "abcdefghijklmnopqrst"
EXECUTION = "milo-catalog-capture-x7k2p"


@pytest.fixture(autouse=True)
def no_outbound_connections(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a preparation test attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def api_env(monkeypatch):
    """The API's own environment when the Prepare route is enabled."""
    monkeypatch.setenv(wp.PREPARATION_REQUESTS_FLAG, "true")
    monkeypatch.setenv("MILO_RELEASE_SHA", RELEASE_SHA)
    monkeypatch.setenv("MILO_EXPECTED_SUPABASE_PROJECT_REF", PROJECT_REF)
    monkeypatch.setenv("MILO_ENABLE_WORK_SCOPE_MUTATIONS", "true")
    # The route is rate limited like run creation; the tests make many requests.
    monkeypatch.setenv("MILO_RATE_LIMIT_RUN_CREATION_USER", "1000")
    yield
    app.dependency_overrides.clear()


class FakeTrigger:
    """Records every execution it is asked for; answers as a test says."""

    def __init__(self, *, refusal: str | None = None,
                 outcomes: list[trig.TriggerOutcome] | None = None,
                 barrier: threading.Barrier | None = None) -> None:
        self.refusal = refusal
        self.outcomes = list(outcomes or [])
        self.barrier = barrier
        self.checks = 0
        self.calls: list[ci.Invocation] = []
        self._lock = threading.Lock()

    def release_refusal(self) -> str | None:
        with self._lock:
            self.checks += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        return self.refusal

    def run(self, invocation: ci.Invocation) -> trig.TriggerOutcome:
        with self._lock:
            self.calls.append(invocation)
            return self.outcomes.pop(0) if self.outcomes else trig.TriggerOutcome(
                trig.TRIGGERED, EXECUTION)


def world() -> tuple[MemoryRepository, dict[str, Any]]:
    """A Swarm V2 project with two members and ONE plan at revision 1."""
    repo = MemoryRepository()
    for user in (USER, MEMBER, OUTSIDER):
        repo.seed_user(str(user))
    project = str(uuid4())
    repo.seed_project(project, f"p-{project[:8]}", "P", [str(USER), str(MEMBER)],
                      workflow_key="swarm_v2")
    conversation = repo.create_conversation(UUID(project), "plan", USER)["id"]
    scope = wsc.scope_from_fields({"units": ["toyota"], "model_year_from": 2018,
                                   "model_year_to": None, "max_items": 25, "batch_size": 10})
    plan = repo.create_work_scope(UUID(conversation), USER, {
        "scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
        "notes": []})["work_scope"]
    return repo, plan


def client(repo: MemoryRepository, trigger: Any) -> TestClient:
    app.dependency_overrides[get_repository] = lambda: repo
    app.dependency_overrides[get_capture_trigger] = lambda: trigger
    return TestClient(app)


def as_user(user: UUID = USER) -> dict[str, str]:
    return {"x-milo-auth-user-id": str(user)}


def prepare(api: TestClient, plan: dict[str, Any], *, user: UUID = USER,
            revision: int | None = None, digest: str | None = None):
    return api.post(f"/work-scopes/{plan['id']}/preparations", headers=as_user(user), json={
        "expected_revision": plan["head_revision"] if revision is None else revision,
        "expected_digest": plan["head_digest"] if digest is None else digest})


def status(api: TestClient, plan: dict[str, Any], *, user: UUID = USER,
           revision: int | None = None, digest: str | None = None):
    return api.get(f"/work-scopes/{plan['id']}/preparation", headers=as_user(user), params={
        "revision": plan["head_revision"] if revision is None else revision,
        "digest": plan["head_digest"] if digest is None else digest})


def requests(repo: MemoryRepository) -> list[dict[str, Any]]:
    return list(repo.work_scope_preparation_requests.values())


def capture_runs(repo: MemoryRepository) -> list[dict[str, Any]]:
    return [run for run in repo.runs.values()
            if (run.get("run_identity") or {}).get("workflow_key") == "operator_capture"]


# =============================================================================
# 1. the shared invocation, byte for byte
# =============================================================================

def test_the_pinned_invocation_values_are_the_entrypoints_own():
    from tests.test_production_operator_bundle import _contract_value

    assert ci.ENTRYPOINT_MODULE == _contract_value("MILO_CAPTURE_ENTRYPOINT_MODULE")
    assert (ci.PACKAGE_ID, ci.RESOURCE_ID) == (src.CKAN_PACKAGE_ID, src.WLTP_RESOURCE_ID)
    assert (ci.PAGE_LIMIT, ci.MAX_PAGES, ci.MAX_RECORDS) == (
        operator_capture.CAPTURE_PAGE_LIMIT, operator_capture.CAPTURE_MAX_PAGES,
        operator_capture.CAPTURE_MAX_RECORDS)
    assert ci.EGRESS_ACKNOWLEDGEMENT == operator_capture.EGRESS_ACKNOWLEDGEMENT
    assert ci.SCHEMA_REPORT_ACKNOWLEDGEMENT == operator_capture.SCHEMA_REPORT_ACKNOWLEDGEMENT
    assert ci.PREPARATION_SWITCH == operator_capture.WORK_SCOPE_PREPARATION_FLAG \
        == _contract_value("MILO_WORK_SCOPE_PREPARATION_FLAG_NAME")


def test_the_shared_invocation_is_accepted_by_the_entrypoint_itself():
    """What the API sends parses, in the entrypoint's own parser, to a scoped
    capture with no refusal but the environment's (checked separately)."""
    invocation = ci.work_scope_preparation(
        project_ref=PROJECT_REF, run_id=str(uuid4()), work_scope_id=str(uuid4()),
        revision=3, digest="d" * 64)
    assert invocation.container_args[:2] == ("-m", ci.ENTRYPOINT_MODULE)
    args, extra = operator_capture.build_parser().parse_known_args(
        list(invocation.entrypoint_args))
    env = {"SUPABASE_URL": f"https://{PROJECT_REF}.supabase.co",
           "MILO_ENABLE_CATALOG_EXECUTION": "true", "MILO_RELEASE_SHA": RELEASE_SHA,
           **dict(invocation.env_overrides)}
    assert operator_capture._refusal(args, extra, env) == ""
    # Without the ONE override, the entrypoint refuses the scoped mode.
    env.pop(ci.PREPARATION_SWITCH)
    assert operator_capture._refusal(args, extra, env) == "CAPTURE_WORK_SCOPE_PREPARATION_DISABLED"


def test_the_api_body_is_exactly_what_the_operator_script_executes(tmp_path):
    """GOLDEN: the script's `gcloud run jobs execute` against the API's jobs.run body."""
    from tests.test_work_scope_preparation import SCOPED_VALUES, _capture_script, _flag_value

    result, log = _capture_script(tmp_path, "--enable-catalog-execution",
                                  "--enable-work-scope-preparation", *SCOPED_VALUES, gcloud=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    execute = next(call for call in calls if call[:3] == ["run", "jobs", "execute"])
    values = dict(zip(SCOPED_VALUES[::2], SCOPED_VALUES[1::2]))
    body = trig.run_request_body(ci.work_scope_preparation(
        project_ref="testprojectref",     # the fake operator config's project ref
        run_id=values["--run-id"], work_scope_id=values["--work-scope-id"],
        revision=values["--work-scope-revision"], digest=values["--work-scope-digest"]))
    (override,) = body["overrides"]["containerOverrides"]
    assert _flag_value(execute, "--args").split(",") == override["args"]
    assert _flag_value(execute, "--update-env-vars") == ",".join(
        f"{item['name']}={item['value']}" for item in override["env"])
    assert override["env"] == [{"name": "MILO_ENABLE_WORK_SCOPE_PREPARATION", "value": "true"}]


def test_the_script_builds_its_arguments_from_the_shared_definition_only():
    script = open("scripts/catalog/government-production-capture.sh", encoding="utf-8").read()
    assert 'CAPTURE_INVOCATION="${REPO_ROOT}/backend/capture_invocation.py"' in script
    # The acknowledgements are no longer assembled into --args by the script.
    for function in ("capture_args() {", "work_scope_invocation() {"):
        body = script.split(function)[1].split("\n}\n")[0]
        assert '"$CAPTURE_INVOCATION"' in body
        assert "MILO_CAPTURE_EGRESS_ACK" not in body and "--work-scope-id," not in body


@pytest.mark.parametrize("field,value", [
    ("run_id", "not-a-uuid"), ("work_scope_id", "ABC"), ("revision", "0"),
    ("digest", "D" * 64), ("project_ref", "has,comma")])
def test_the_shared_definition_refuses_every_malformed_value(field, value):
    values = {"project_ref": PROJECT_REF, "run_id": str(uuid4()), "work_scope_id": str(uuid4()),
              "revision": 2, "digest": "a" * 64, field: value}
    with pytest.raises(ci.InvocationError):
        ci.work_scope_preparation(**values)


# =============================================================================
# 2. idempotency
# =============================================================================

def test_the_first_request_executes_the_shared_invocation_once():
    repo, plan = world()
    trigger = FakeTrigger()
    response = prepare(client(repo, trigger), plan)
    assert response.status_code == 202, response.text
    view = response.json()
    assert (view["state"], view["attempt"], view["can_prepare"], view["blocked_by"]) == \
        ("preparing", 1, False, "in_flight")
    (request,) = requests(repo)
    (run,) = capture_runs(repo)
    assert (request["trigger_state"], request["run_id"], request["execution_name"]) == \
        ("triggered", run["id"], EXECUTION)
    # The run is an operator capture run the ordinary launcher can never take.
    assert (run["status"], run["launch_state"], run["conversation_id"]) == \
        ("queued", "none", plan["conversation_id"])
    assert run["input"]["metadata"]["milo_operation"] == "catalog.government.capture"
    assert trigger.calls == [ci.work_scope_preparation(
        project_ref=PROJECT_REF, run_id=run["id"], work_scope_id=plan["id"],
        revision=plan["head_revision"], digest=plan["head_digest"])]


def test_a_second_request_returns_the_preparation_in_flight_and_starts_nothing():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    assert prepare(api, plan).status_code == 202
    again = prepare(api, plan, user=MEMBER)
    assert again.status_code == 200
    assert (again.json()["state"], again.json()["attempt"]) == ("preparing", 1)
    assert len(trigger.calls) == 1 and len(requests(repo)) == 1 and len(capture_runs(repo)) == 1


def test_two_concurrent_requests_start_exactly_one_capture():
    repo, plan = world()
    trigger = FakeTrigger(barrier=threading.Barrier(2))
    api = client(repo, trigger)
    answers: list[Any] = []

    def post(user: UUID) -> None:
        answers.append(prepare(api, plan, user=user))

    threads = [threading.Thread(target=post, args=(user,)) for user in (USER, MEMBER)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert trigger.checks == 2                     # both got past the pre-checks...
    assert sorted(answer.status_code for answer in answers) == [200, 202]
    assert len(trigger.calls) == 1                 # ...and exactly one executed
    assert len(requests(repo)) == 1 and len(capture_runs(repo)) == 1
    assert {answer.json()["attempt"] for answer in answers} == {1}


def test_a_prepared_revision_is_never_prepared_again():
    repo, plan = world()
    prepare_plan_head(repo, plan["id"], user_id=str(USER))
    trigger = FakeTrigger()
    response = prepare(client(repo, trigger), plan)
    assert response.status_code == 200
    assert (response.json()["state"], response.json()["blocked_by"]) == ("prepared", "prepared")
    assert trigger.calls == [] and trigger.checks == 0 and requests(repo) == []


# =============================================================================
# 3. authorization and the flag
# =============================================================================

def test_a_non_member_is_refused_and_nothing_is_written_or_executed():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    for response in (prepare(api, plan, user=OUTSIDER), status(api, plan, user=OUTSIDER)):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "WORK_SCOPE_NOT_FOUND"
    # An unknown plan is the same answer.
    assert prepare(api, {**plan, "id": str(uuid4())}).status_code == 404
    assert trigger.checks == 0 and trigger.calls == []
    assert requests(repo) == [] and capture_runs(repo) == []


def test_the_route_is_refused_by_the_surface_guard_when_the_flag_is_off(monkeypatch):
    repo, plan = world()
    monkeypatch.setenv(wp.PREPARATION_REQUESTS_FLAG, "false")
    trigger = FakeTrigger()
    api = client(repo, trigger)
    response = prepare(api, plan)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    view = status(api, plan).json()
    assert (view["state"], view["can_prepare"], view["blocked_by"]) == \
        ("not_requested", False, "preparation_disabled")
    capabilities = api.get(f"/projects/{repo.work_scopes[plan['id']]['project_id']}"
                           "/work-scope/capabilities", headers=as_user()).json()
    assert capabilities["can_prepare"] is False
    assert trigger.calls == [] and requests(repo) == []


def test_capabilities_say_can_prepare_only_with_the_flag_and_a_capture_job():
    repo, plan = world()
    project = repo.work_scopes[plan["id"]]["project_id"]
    assert client(repo, FakeTrigger()).get(f"/projects/{project}/work-scope/capabilities",
                                           headers=as_user()).json()["can_prepare"] is True
    assert client(repo, None).get(f"/projects/{project}/work-scope/capabilities",
                                  headers=as_user()).json()["can_prepare"] is False
    response = prepare(client(repo, None), plan)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WORK_SCOPE_PREPARATION_DISABLED"


# =============================================================================
# 4. stale detection
# =============================================================================

def test_a_request_for_a_revision_that_is_not_the_head_is_refused():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    response = prepare(api, plan, digest="0" * 64)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORK_SCOPE_STALE"
    assert trigger.calls == [] and requests(repo) == []


def test_a_revision_whose_head_moved_reads_stale():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    assert prepare(api, plan).status_code == 202
    ws.revise_work_scope(repo, USER, UUID(plan["id"]), plan["head_revision"], plan["head_digest"],
                         None, {"units": ["toyota"], "model_year_from": 2019,
                                "model_year_to": None, "max_items": 25, "batch_size": 10})
    old = status(api, plan).json()
    assert (old["state"], old["can_prepare"], old["blocked_by"]) == ("stale", False, "stale")
    assert prepare(api, plan).status_code == 409
    head = repo.work_scopes[plan["id"]]
    fresh = status(api, plan, revision=head["head_revision"], digest=head["head_digest"]).json()
    assert (fresh["state"], fresh["can_prepare"]) == ("not_requested", True)


# =============================================================================
# 5. the status is derived from durable state, never from an exit code
# =============================================================================

def _capture_claims_and_finishes(repo: MemoryRepository, run_id: str, claim: TerminalClaim) -> None:
    worker = f"capture-{run_id[:8]}"
    claimed = repo.claim_run(UUID(run_id), worker)
    lease = {"worker_id": worker, "attempt": claimed["attempt"],
             "lease_token": claimed["lease_token"]}
    RunFinalizer(repo, UUID(run_id), "operator_capture", lease).finalize(claim)


def test_a_failed_trigger_reads_failed_and_the_retry_reuses_the_unclaimed_run():
    repo, plan = world()
    trigger = FakeTrigger(outcomes=[trig.TriggerOutcome(trig.TRIGGER_FAILED)])
    api = client(repo, trigger)
    first = prepare(api, plan)
    assert first.status_code == 502
    assert first.json()["error"]["code"] == "WORK_SCOPE_PREPARATION_TRIGGER_FAILED"
    view = status(api, plan).json()
    assert (view["state"], view["reason_code"], view["can_prepare"]) == \
        ("failed", "PREPARATION_TRIGGER_FAILED", True)
    retry = prepare(api, plan)
    assert retry.status_code == 202 and retry.json()["attempt"] == 2
    # The run no capture ever claimed is re-triggered, not duplicated.
    assert len(capture_runs(repo)) == 1
    assert trigger.calls[0] == trigger.calls[1]


def test_the_status_follows_the_capture_runs_durable_outcome_not_the_trigger():
    repo, plan = world()
    # The trigger answers "triggered" -- an exit code of 0, in effect.
    trigger = FakeTrigger()
    api = client(repo, trigger)
    assert prepare(api, plan).status_code == 202
    (run,) = capture_runs(repo)
    # ...but the capture itself failed, and the database says so.
    _capture_claims_and_finishes(repo, run["id"], TerminalClaim.failure(
        "operator_capture", "GOV_TRANSPORT_FAILED", "the transport failed"))
    view = status(api, plan).json()
    assert (view["state"], view["reason_code"], view["can_prepare"]) == \
        ("failed", "GOV_TRANSPORT_FAILED", True)
    # A retry is a NEW attempt with a NEW run: the failed one is terminal.
    assert prepare(api, plan).json()["attempt"] == 2
    assert len(capture_runs(repo)) == 2


def test_an_uncertain_trigger_is_prepared_once_the_database_says_so():
    repo, plan = world()
    # The trigger could not tell whether Cloud Run accepted it.
    trigger = FakeTrigger(outcomes=[trig.TriggerOutcome(trig.TRIGGER_UNKNOWN)])
    api = client(repo, trigger)
    assert prepare(api, plan).status_code == 202
    assert status(api, plan).json()["state"] == "preparing"
    prepare_plan_head(repo, plan["id"], user_id=str(USER))
    view = status(api, plan).json()
    assert (view["state"], view["reason_code"], view["blocked_by"]) == \
        ("prepared", None, "prepared")
    assert view["preparation"]["queued_item_count"] >= 1
    (unit,) = view["units"]
    assert unit["unit_key"] == "toyota"
    assert set(unit["coverage"]) == {"enriched", "ambiguous", "pending", "queued"}


def test_a_messages_and_logs_never_reach_the_status():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    prepare(api, plan)
    (run,) = capture_runs(repo)
    _capture_claims_and_finishes(repo, run["id"], TerminalClaim.failure(
        "operator_capture", "not a code; select * from secrets", "raw SQL text"))
    text = status(api, plan).text
    assert "select" not in text and "raw SQL" not in text
    assert status(api, plan).json()["reason_code"] == "PREPARATION_FAILED"


def _facts(**overrides: Any) -> dict[str, Any]:
    facts = {"work_scope_id": str(uuid4()), "head_revision": 2, "head_digest": "a" * 64,
             "closed": False, "revision": 2, "digest": "a" * 64, "preparation": None,
             "request": {"id": str(uuid4()), "attempt": 1, "trigger_state": "triggered",
                         "run_id": str(uuid4()), "claimed_seconds": 5, "triggered_seconds": 5},
             "run": {"status": "queued", "launch_state": "none", "claimed": False,
                     "lease_expired_seconds": None, "error_code": None},
             "known_unresolved": None}
    facts.update(overrides)
    return facts


@pytest.mark.parametrize("overrides,expected", [
    ({"head_revision": 3}, ("stale", None, False)),
    ({"head_digest": "b" * 64}, ("stale", None, False)),
    ({"closed": True}, ("stale", None, False)),
    ({"preparation": {"id": "p"}}, ("prepared", None, False)),
    ({"request": None}, ("not_requested", None, False)),
    ({}, ("preparing", None, False)),
    ({"request": {"trigger_state": "triggered", "claimed_seconds": 2000,
                  "triggered_seconds": 2000, "attempt": 1}},
     ("failed", "PREPARATION_NOT_STARTED", True)),
    ({"request": {"trigger_state": "claimed", "claimed_seconds": 10, "attempt": 1}},
     ("preparing", None, False)),
    ({"request": {"trigger_state": "claimed", "claimed_seconds": 901, "attempt": 1}},
     ("failed", "PREPARATION_NOT_STARTED", True)),
    ({"run": {"status": "running", "claimed": True, "lease_expired_seconds": 2000}},
     ("failed", "PREPARATION_INTERRUPTED", False)),
    ({"run": {"status": "running", "claimed": True, "lease_expired_seconds": 30}},
     ("preparing", None, False)),
    ({"run": {"status": "completed", "claimed": True}}, ("failed", "PREPARATION_INCOMPLETE", True)),
    ({"run": {"status": "cancelled", "claimed": True}}, ("failed", "PREPARATION_CANCELLED", True)),
    ({"run": {"status": "failed", "claimed": True, "error_code": "CAPTURE_LEASE_LOST"}},
     ("failed", "CAPTURE_LEASE_LOST", True)),
    ({"run": {"status": "failed", "claimed": True, "error_code": "SOMETHING_UNKNOWN"}},
     ("failed", "PREPARATION_FAILED", True)),
])
def test_the_status_derivation(overrides, expected):
    derived = wp.derive_status(_facts(**overrides))
    assert (derived["state"], derived["reason_code"], derived["retryable"]) == expected


def test_an_interrupted_capture_is_never_retried_beside_itself():
    repo, plan = world()
    trigger = FakeTrigger()
    api = client(repo, trigger)
    prepare(api, plan)
    (run,) = capture_runs(repo)
    claimed = repo.claim_run(UUID(run["id"]), "capture-worker")
    repo.runs[run["id"]]["lease_expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=wp.START_GRACE_SECONDS + 60)).isoformat()
    assert claimed["status"] == "starting"
    view = status(api, plan).json()
    assert (view["state"], view["reason_code"], view["blocked_by"]) == \
        ("failed", "PREPARATION_INTERRUPTED", "needs_operator")
    response = prepare(api, plan)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORK_SCOPE_PREPARATION_NEEDS_OPERATOR"
    assert len(trigger.calls) == 1 and len(capture_runs(repo)) == 1


# =============================================================================
# 6. the capture job must run the deployed release image
# =============================================================================

def test_a_capture_job_off_the_release_image_is_refused_before_anything_is_written():
    repo, plan = world()
    trigger = FakeTrigger(refusal=trig.JOB_NOT_RELEASE)
    response = prepare(client(repo, trigger), plan)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == trig.JOB_NOT_RELEASE
    assert trigger.calls == [] and requests(repo) == [] and capture_runs(repo) == []


def _job(image: str, sha: str | None = RELEASE_SHA) -> dict[str, Any]:
    env = [{"name": "MILO_RELEASE_SHA", "value": sha}] if sha else []
    return {"template": {"template": {"containers": [{"image": image, "env": env}]}}}


RELEASE_IMAGE = f"us-central1-docker.pkg.dev/p/milo-agent/worker:{RELEASE_SHA}"


@pytest.mark.parametrize("capture,worker,sha,expected", [
    (_job(RELEASE_IMAGE), _job(RELEASE_IMAGE), RELEASE_SHA, None),
    (_job(RELEASE_IMAGE.replace(RELEASE_SHA, "0" * 40)), _job(RELEASE_IMAGE), RELEASE_SHA,
     trig.JOB_NOT_RELEASE),
    (_job(RELEASE_IMAGE), _job(RELEASE_IMAGE), "1" * 40, trig.JOB_NOT_RELEASE),
    (_job(RELEASE_IMAGE, sha="0" * 40), _job(RELEASE_IMAGE), RELEASE_SHA, trig.JOB_NOT_RELEASE),
    (_job(RELEASE_IMAGE, sha=None), _job(RELEASE_IMAGE), RELEASE_SHA, trig.JOB_NOT_RELEASE),
    ({}, _job(RELEASE_IMAGE), RELEASE_SHA, trig.JOB_UNREADABLE),
    (_job(RELEASE_IMAGE), _job(RELEASE_IMAGE), "", trig.JOB_NOT_RELEASE),
])
def test_the_release_image_rule(capture, worker, sha, expected):
    assert trig.release_refusal_for(capture, worker, sha) == expected


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, *, gets: dict[str, FakeResponse], post: Any) -> None:
        self.gets, self.post_answer = gets, post
        self.posts: list[tuple[str, dict]] = []

    def get(self, url: str, timeout: int) -> FakeResponse:
        return self.gets[url.rsplit("/", 1)[-1]]

    def post(self, url: str, data: str, headers: dict, timeout: int) -> FakeResponse:
        self.posts.append((url, json.loads(data)))
        if isinstance(self.post_answer, Exception):
            raise self.post_answer
        return self.post_answer


def _trigger(session: FakeSession) -> trig.CloudRunCaptureJobTrigger:
    return trig.CloudRunCaptureJobTrigger(project="p", region="us-central1",
                                          capture_job="milo-catalog-capture",
                                          worker_job="milo-agent-worker",
                                          release_sha=RELEASE_SHA,
                                          session_factory=lambda: session)


def test_the_cloud_run_trigger_reads_both_jobs_and_runs_with_the_shared_overrides():
    operation = {"name": "projects/p/locations/us-central1/operations/abc-123",
                 "metadata": {"name": f"projects/p/locations/us-central1/jobs/milo-catalog-capture/executions/{EXECUTION}"}}
    session = FakeSession(gets={"milo-catalog-capture": FakeResponse(200, _job(RELEASE_IMAGE)),
                                "milo-agent-worker": FakeResponse(200, _job(RELEASE_IMAGE))},
                          post=FakeResponse(200, operation))
    trigger = _trigger(session)
    assert trigger.release_refusal() is None
    invocation = ci.work_scope_preparation(project_ref=PROJECT_REF, run_id=str(uuid4()),
                                           work_scope_id=str(uuid4()), revision=1,
                                           digest="c" * 64)
    assert trigger.run(invocation) == trig.TriggerOutcome(trig.TRIGGERED, EXECUTION)
    ((url, body),) = session.posts
    assert url.endswith("/v2/projects/p/locations/us-central1/jobs/milo-catalog-capture:run")
    assert body == trig.run_request_body(invocation)


@pytest.mark.parametrize("answer,state", [
    (FakeResponse(403, {"error": "denied"}), trig.TRIGGER_FAILED),
    (FakeResponse(500), trig.TRIGGER_FAILED),
    (TimeoutError("read timed out"), trig.TRIGGER_UNKNOWN),
])
def test_the_cloud_run_trigger_separates_definite_failure_from_uncertainty(answer, state):
    session = FakeSession(gets={}, post=answer)
    invocation = ci.work_scope_preparation(project_ref=PROJECT_REF, run_id=str(uuid4()),
                                           work_scope_id=str(uuid4()), revision=1,
                                           digest="c" * 64)
    assert _trigger(session).run(invocation).state == state


def test_an_unreadable_job_refuses():
    session = FakeSession(gets={"milo-catalog-capture": FakeResponse(403),
                                "milo-agent-worker": FakeResponse(200, _job(RELEASE_IMAGE))},
                          post=None)
    assert _trigger(session).release_refusal() == trig.JOB_UNREADABLE


# =============================================================================
# 6b. PR-E'2: every refusal is ONE static log line, and the body names the code
# =============================================================================

PREPARATION_LOGGER = "milo.work_scope.preparation"


def refusal_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == PREPARATION_LOGGER]


def expected_line(plan: dict[str, Any], code: str, revision: int = 1) -> str:
    return (f"event=work_scope_preparation_refused work_scope_id={plan['id']} "
            f"revision={revision} code={code}")


@pytest.mark.parametrize("unreadable", ["milo-catalog-capture", "milo-agent-worker"])
def test_a_403_on_a_job_get_refuses_job_unreadable_logs_one_line_and_writes_nothing(
        caplog, unreadable):
    """The production incident: the API identity held the executor role on
    both jobs but could not READ them, so release_refusal's GET answered 403.
    The real trigger turns that into JOB_UNREADABLE; the route answers 409 with
    that static code and its reason, writes nothing, and logs ONE line."""
    caplog.set_level("INFO", logger=PREPARATION_LOGGER)
    repo, plan = world()
    readable = FakeResponse(200, _job(RELEASE_IMAGE))
    denied = FakeResponse(403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message":
        "Permission 'run.jobs.get' denied on resource 'namespaces/p/jobs/x' secret-token-abc"}})
    session = FakeSession(gets={"milo-catalog-capture": readable, "milo-agent-worker": readable,
                                unreadable: denied}, post=AssertionError("must never run"))
    trigger = _trigger(session)
    assert trigger.release_refusal() == trig.JOB_UNREADABLE
    response = prepare(client(repo, trigger), plan)
    assert response.status_code == 409
    assert response.json() == {"error": {"code": trig.JOB_UNREADABLE,
                                         "message": wp.REQUEST_REASONS[trig.JOB_UNREADABLE]}}
    assert session.posts == [] and requests(repo) == [] and capture_runs(repo) == []
    assert refusal_lines(caplog) == [expected_line(plan, trig.JOB_UNREADABLE)]
    logged = caplog.text
    for leak in ("https://", "run.googleapis.com", "secret-token-abc", "PERMISSION_DENIED",
                 "run.jobs.get", "milo-catalog-capture", "milo-agent-worker", plan["head_digest"]):
        assert leak not in logged, leak


def _needs_operator(api: TestClient, repo: MemoryRepository, plan: dict[str, Any]) -> None:
    prepare(api, plan)
    (run,) = capture_runs(repo)
    repo.claim_run(UUID(run["id"]), "capture-worker")
    repo.runs[run["id"]]["lease_expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=wp.START_GRACE_SECONDS + 60)).isoformat()


@pytest.mark.parametrize("case,status_code,code", [
    ("disabled", 503, "WORK_SCOPE_PREPARATION_DISABLED"),
    ("not_release", 409, trig.JOB_NOT_RELEASE),
    ("unreadable", 409, trig.JOB_UNREADABLE),
    ("stale", 409, "WORK_SCOPE_STALE"),
    ("trigger_failed", 502, "WORK_SCOPE_PREPARATION_TRIGGER_FAILED"),
    ("needs_operator", 409, "WORK_SCOPE_PREPARATION_NEEDS_OPERATOR"),
    ("outsider", 404, "WORK_SCOPE_NOT_FOUND"),
])
def test_every_refusal_is_logged_once_with_its_static_code(caplog, case, status_code, code):
    repo, plan = world()
    trigger: Any = {
        "disabled": None,
        "not_release": FakeTrigger(refusal=trig.JOB_NOT_RELEASE),
        "unreadable": FakeTrigger(refusal=trig.JOB_UNREADABLE),
        "trigger_failed": FakeTrigger(outcomes=[trig.TriggerOutcome(trig.TRIGGER_FAILED)]),
    }.get(case, FakeTrigger())
    api = client(repo, trigger)
    if case == "needs_operator":
        _needs_operator(api, repo, plan)
    caplog.clear()
    caplog.set_level("INFO", logger=PREPARATION_LOGGER)
    response = prepare(api, plan, user=OUTSIDER if case == "outsider" else USER,
                       digest="0" * 64 if case == "stale" else None)
    assert response.status_code == status_code, response.text
    assert response.json()["error"]["code"] == code
    assert refusal_lines(caplog) == [expected_line(plan, code)]


def test_a_started_or_answered_preparation_logs_no_refusal(caplog):
    caplog.set_level("INFO", logger=PREPARATION_LOGGER)
    repo, plan = world()
    api = client(repo, FakeTrigger())
    assert prepare(api, plan).status_code == 202
    assert prepare(api, plan).status_code == 200
    assert refusal_lines(caplog) == []


def test_the_log_line_keeps_only_static_fields(caplog):
    caplog.set_level("INFO", logger=PREPARATION_LOGGER)
    wp._log_refusal("not-a-uuid", "7; DROP TABLE", "https://run.googleapis.com/?token=abc")
    wp._log_refusal(uuid4(), True, "lower_case_code")
    (first, second) = refusal_lines(caplog)
    assert first == ("event=work_scope_preparation_refused work_scope_id=invalid revision=-1 "
                     "code=UNCLASSIFIED")
    assert second.endswith("revision=-1 code=UNCLASSIFIED")


def test_the_website_has_reason_text_for_every_refusal_code():
    """Every static code the Prepare route refuses with is on the website's
    allowlist with its own copy, so a refusal never shows the generic sentence."""
    import re as _re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "frontend" / "lib" / "errorText.ts").read_text(
        encoding="utf-8")
    listed = _re.search(r"PREPARATION_REFUSAL_CODES: readonly string\[\] = \[(.*?)\];", source, _re.S)
    assert listed is not None
    codes = set(_re.findall(r"'([A-Z_]+)'", listed.group(1)))
    assert codes == set(wp.REQUEST_REASONS) | {"WORK_SCOPE_STALE"}
    for code in codes:
        assert f"['{code}', '" in source, code


def test_no_capture_job_configured_means_no_trigger():
    from backend.config import Settings

    settings = Settings(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_ROLE_KEY="k")
    assert trig.build_capture_trigger(settings, {"MILO_RELEASE_SHA": RELEASE_SHA}) is None
    settings = Settings(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_ROLE_KEY="k",
                        CLOUD_RUN_CAPTURE_JOB="milo-catalog-capture")
    built = trig.build_capture_trigger(settings, {"MILO_RELEASE_SHA": RELEASE_SHA})
    assert isinstance(built, trig.CloudRunCaptureJobTrigger)
    assert (built.capture_job, built.release_sha) == ("milo-catalog-capture", RELEASE_SHA)


# =============================================================================
# 7. E'-5 and production scale
# =============================================================================

def test_the_known_unresolved_count_comes_from_the_plans_latest_preparation():
    """aa63369b's four ambiguous rows are left out of a Toyota 2018+ plan; the
    status says the toggle would re-queue exactly those four."""
    from tests.test_catalog_variant_coverage import AA, aa_world

    coverage_world, _plan, _run = aa_world()
    revision = coverage_world.plan(AA["snapshot_rows"], model_year_from=2018)
    repo = coverage_world.repository
    plan = repo.work_scopes[revision["work_scope_id"]]
    view = wp.status(repo, UUID(coverage_world.user), UUID(plan["id"]), plan["head_revision"],
                     plan["head_digest"], trigger=FakeTrigger())
    assert view["known_unresolved"] == {"revision": 1, "count": 4}
    # The next revision -- with the toggle on -- still shows what it would
    # re-queue, and its digest is new while the old one never changed.
    old_digest = plan["head_digest"]
    fields = {**wsc.scope_from_text(repo.get_work_scope_revision(
        UUID(plan["id"]), 1)["scope_text"]).fields(), "include_unresolved": True}
    ws.revise_work_scope(repo, UUID(coverage_world.user), UUID(plan["id"]), 1, old_digest,
                         None, fields)
    head = repo.work_scopes[plan["id"]]
    assert head["head_digest"] != old_digest
    assert repo.get_work_scope_revision(UUID(plan["id"]), 1)["digest"] == old_digest
    view = wp.status(repo, UUID(coverage_world.user), UUID(plan["id"]), head["head_revision"],
                     head["head_digest"], trigger=FakeTrigger())
    assert (view["state"], view["known_unresolved"]) == ("not_requested",
                                                         {"revision": 1, "count": 4})


def test_a_toyota_2018_plus_revision_over_6374_rows_is_read_in_bounded_rows():
    from tests.test_catalog_variant_coverage import (AA, REAL_TOYOTA_SNAPSHOT_SIZE, BoundedSpy,
                                                     World, grow_snapshot)

    coverage_world = World()
    revision = coverage_world.plan(AA["snapshot_rows"], model_year_from=2018)
    grow_snapshot(coverage_world, revision["snapshot_key"], REAL_TOYOTA_SNAPSHOT_SIZE)
    spy = BoundedSpy(coverage_world.repository)
    plan = coverage_world.repository.work_scopes[revision["work_scope_id"]]
    view = wp.status(spy, UUID(coverage_world.user), UUID(plan["id"]), plan["head_revision"],
                     plan["head_digest"], trigger=FakeTrigger())
    assert view["state"] == "prepared"
    (unit,) = view["units"]
    assert unit["unit_key"] == "toyota" and unit["coverage"]["queued"] >= 1
    # Only the per-unit counts were read (one row per unit), never a snapshot.
    assert all(rows <= 64 for _name, rows in spy.calls)
    assert sum(rows for _name, rows in spy.calls) < 100
