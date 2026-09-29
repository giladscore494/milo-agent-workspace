"""PR-OBS OBS-5: Sentry error reporting is OFF by default and carries no
request, user, local-variable or model material when it is on."""

from __future__ import annotations

import json

import pytest

from backend import observability
from backend.finalization import TerminalClaim

FAKE_DSN = "https://publickey@sentry.example.invalid/42"
SENTINEL = "PROMPT-SENTINEL-do-not-ship"
RELEASE_SHA = "a" * 40


class Envelopes:
    """An in-memory Sentry transport: every envelope the SDK would send."""

    def __init__(self):
        from sentry_sdk.transport import Transport

        sink = self

        class _Transport(Transport):
            def capture_envelope(self, envelope):
                sink.items.extend(item.payload.json for item in envelope.items
                                  if item.payload.json is not None)

        self.items: list[dict] = []
        self.transport = _Transport()

    def events(self) -> list[dict]:
        return [item for item in self.items if item.get("type", "event") in ("event", None)
                and ("exception" in item or "message" in item or "logentry" in item)]

    def text(self) -> str:
        return json.dumps(self.items, default=str)


@pytest.fixture
def sentry_off():
    """Leave the process exactly as it was: reporting disabled, no client."""
    yield
    import sentry_sdk

    sentry_sdk.init()  # an inactive client (no DSN)
    for scope in (sentry_sdk.get_global_scope(), sentry_sdk.get_isolation_scope(),
                  sentry_sdk.get_current_scope()):
        scope.clear()
    observability._enabled = False


def _env(**extra):
    return {"SENTRY_DSN": FAKE_DSN, "MILO_RELEASE_SHA": RELEASE_SHA,
            "ENVIRONMENT": "test", **extra}


# -- disabled by default -------------------------------------------------------

def test_reporting_is_disabled_without_a_dsn(sentry_off):
    assert observability.configured_dsn({}) == ""
    assert observability.init_sentry("milo-agent-api", env={}) is False
    assert observability.is_enabled() is False
    assert observability.report_run_failed("x", "CODE") is False
    assert observability.report_exception(RuntimeError("x")) is False


@pytest.mark.parametrize("value", ["", "  ", "disabled", "OFF", "none", "0"])
def test_explicitly_disabled_values_keep_reporting_off(value, sentry_off):
    assert observability.init_sentry("svc", env={"SENTRY_DSN": value}) is False


def test_a_malformed_dsn_disables_reporting_without_printing_it(capsys, sentry_off):
    assert observability.init_sentry("svc", env={"SENTRY_DSN": "not-a-dsn-" + SENTINEL}) is False
    out = capsys.readouterr()
    assert SENTINEL not in out.out + out.err


def test_the_test_and_ci_environment_carries_no_dsn():
    import os

    assert observability.configured_dsn(dict(os.environ)) == "", (
        "tests must run with error reporting disabled")


@pytest.mark.parametrize("raw,expected", [("", 0.0), ("0", 0.0), ("0.01", 0.01), ("0.05", 0.05),
                                          ("0.5", 0.05), ("1", 0.05), ("-1", 0.0), ("nan", 0.0),
                                          ("junk", 0.0)])
def test_traces_are_off_by_default_and_capped(raw, expected):
    assert observability.traces_sample_rate({"MILO_SENTRY_TRACES_SAMPLE_RATE": raw}) == expected


def test_release_is_only_a_full_commit_sha():
    assert observability.release({"MILO_RELEASE_SHA": RELEASE_SHA}) == RELEASE_SHA
    assert observability.release({"MILO_RELEASE_SHA": "abc123"}) is None


def test_service_name_prefers_cloud_run_names():
    assert observability.service_name("milo-agent-worker", {}) == "milo-agent-worker"
    assert observability.service_name("x", {"CLOUD_RUN_JOB": "milo-catalog-capture"}) == "milo-catalog-capture"
    assert observability.service_name("x", {"K_SERVICE": "milo-agent-api"}) == "milo-agent-api"


# -- the scrubber --------------------------------------------------------------

def _dirty_event() -> dict:
    return {
        "message": "run failed",
        "logentry": {"message": "run failed", "params": [SENTINEL], "formatted": SENTINEL},
        "request": {"method": "POST", "url": f"https://api.example/runs?token={SENTINEL}",
                    "query_string": f"q={SENTINEL}", "data": {"prompt": SENTINEL},
                    "headers": {"Authorization": f"Bearer {SENTINEL}"},
                    "cookies": {"session": SENTINEL}, "env": {"REMOTE_ADDR": SENTINEL}},
        "user": {"email": f"{SENTINEL}@example.com", "ip_address": "10.0.0.1"},
        "extra": {"model_output": SENTINEL, "tool_result": SENTINEL},
        "breadcrumbs": {"values": [{"message": SENTINEL}]},
        "contexts": {"runtime": {"name": "CPython"}, "response": {"data": SENTINEL},
                     "register_payload": {"row": SENTINEL}, "trace": {"trace_id": "t", "data": {"x": SENTINEL}}},
        "tags": {"service": "milo-agent-api", "url": SENTINEL},
        "exception": {"values": [{"type": "ValueError", "value": f"bad model output {SENTINEL}",
                                  "stacktrace": {"frames": [{"function": "f", "vars": {"prompt": SENTINEL}}]}}]},
        "threads": {"values": [{"stacktrace": {"frames": [{"vars": {"payload": SENTINEL}}]}}]},
        "spans": [{"description": f"GET https://gov.example/api?filters={SENTINEL}",
                   "data": {"http.query": SENTINEL}, "tags": {"q": SENTINEL}}],
        "server_name": SENTINEL,
    }


def test_the_scrubber_removes_every_sensitive_field_class():
    event = observability.scrub_event(_dirty_event())
    assert SENTINEL not in json.dumps(event)
    # What stays is what triage needs: the type, the stack, method and path.
    assert event["request"] == {"method": "POST", "url": "https://api.example/runs"}
    assert event["exception"]["values"][0]["type"] == "ValueError"
    assert event["exception"]["values"][0]["value"] == observability.REDACTED
    assert event["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] == "f"
    assert set(event["contexts"]) == {"runtime", "trace"}
    assert event["tags"] == {"service": "milo-agent-api"}
    for removed in ("user", "extra", "breadcrumbs", "server_name"):
        assert removed not in event


def test_the_scrubber_survives_odd_shapes():
    scrubbed = observability.scrub_event({"request": "x", "tags": ["a"], "exception": None})
    assert "request" not in scrubbed and "tags" not in scrubbed
    assert observability.scrub_event("not a dict") == "not a dict"


def test_error_codes_are_allowlisted():
    assert observability.safe_code("SWARM_V2_EXECUTION_FAILED") == "SWARM_V2_EXECUTION_FAILED"
    assert observability.safe_code(f"bad {SENTINEL}") == "UNCLASSIFIED"
    assert observability.safe_code(None) == "UNCLASSIFIED"


# -- end to end through the real SDK ---------------------------------------------

def test_an_exception_event_through_the_sdk_carries_no_locals_or_message(sentry_off):
    sink = Envelopes()
    assert observability.init_sentry("milo-agent-worker", env=_env(), transport=sink.transport)
    observability.set_run_id("11111111-2222-3333-4444-555555555555")

    def fails(prompt):
        model_output = SENTINEL  # noqa: F841 -- a local the SDK must not attach
        raise RuntimeError(f"model said {prompt}")

    try:
        fails(SENTINEL)
    except RuntimeError as exc:
        assert observability.report_exception(exc)
    observability.flush()
    events = sink.events()
    assert len(events) == 1
    assert SENTINEL not in sink.text()
    event = events[0]
    assert event["release"] == RELEASE_SHA
    assert event["tags"]["service"] == "milo-agent-worker"
    assert event["tags"]["run_id"] == "11111111-2222-3333-4444-555555555555"
    assert event["exception"]["values"][0]["type"] == "RuntimeError"


def test_the_api_integration_reports_a_5xx_without_request_material(sentry_off):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    sink = Envelopes()
    assert observability.init_sentry("milo-agent-api", env=_env(), web=True, transport=sink.transport)
    app = FastAPI()

    @app.post("/runs/{run_id}/boom")
    async def boom(run_id: str, payload: dict):
        raise RuntimeError(f"tool result {payload}")

    run_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/runs/{run_id}/boom?secret={SENTINEL}", json={"prompt": SENTINEL},
                           headers={"Authorization": f"Bearer {SENTINEL}", "Cookie": f"s={SENTINEL}"})
    assert response.status_code == 500
    observability.flush()
    assert sink.events(), "the unhandled 5xx was not reported"
    assert SENTINEL not in sink.text()
    event = sink.events()[0]
    assert set(event.get("request", {})) <= {"method", "url"}
    assert event["release"] == RELEASE_SHA
    assert event["tags"]["service"] == "milo-agent-api"
    # A run named in the path is tagged on the event (review follow-up).
    assert event["tags"]["run_id"] == run_id


# -- the worker reports a failed run exactly once --------------------------------

def test_the_worker_reports_a_failed_run_once(monkeypatch):
    from tests.test_worker import WorkerRepo
    from backend.worker import main as worker_main

    calls = []
    monkeypatch.setattr(observability, "report_run_failed",
                        lambda run_id, code, workflow_key=None: calls.append((run_id, code, workflow_key)))

    class FailingSwarm:
        workflow_key = "swarm_v2"

        def run(self, run):
            raise RuntimeError(SENTINEL)

    class SwarmRepo(WorkerRepo):
        def get_project(self, project_id):
            return {"id": project_id, "workflow_key": "swarm_v2"}

    repo = SwarmRepo()
    assert worker_main.execute_run(repo.run_id, repo, FailingSwarm()) == 0
    assert repo.failed is not None
    assert calls == [(repo.run_id, "SWARM_V2_EXECUTION_FAILED", "swarm_v2")]
    assert SENTINEL not in repr(calls)


def test_a_repeated_or_superseded_finalize_never_reports_again(monkeypatch):
    from tests.test_worker import WorkerRepo
    from backend.worker.main import _ReportingFinalizer

    calls = []
    monkeypatch.setattr(observability, "report_run_failed",
                        lambda *args, **kwargs: calls.append(args))
    repo = WorkerRepo()
    repo.claim_run(repo.run_id, "worker-1")
    lease = {"worker_id": "worker-1", "attempt": repo.attempt, "lease_token": repo.lease_token}
    finalizer = _ReportingFinalizer(repo=repo, run_id=repo.run_id, engine="vehicle_catalog_v1",
                                    lease_ctx=lease)
    first = finalizer.finalize(TerminalClaim.failure("vehicle_catalog_v1", "ENGINE_FAILED", "x"))
    again = finalizer.finalize(TerminalClaim.failure("vehicle_catalog_v1", "ENGINE_FAILED", "x"))
    assert first.wrote and first.status == "failed"
    assert not again.wrote
    assert len(calls) == 1


def test_a_successful_run_reports_nothing(monkeypatch):
    from tests.test_worker import WorkerRepo
    from backend.worker import main as worker_main

    calls = []
    monkeypatch.setattr(observability, "report_run_failed", lambda *a, **k: calls.append(a))
    class Succeeds:
        workflow_key = "vehicle_catalog_v1"
        checkpoint_sink = None

        def run(self, run):
            return {"status": "success", "result": {"models": []}}

    repo = WorkerRepo()
    assert worker_main.execute_run(repo.run_id, repo, Succeeds()) == 0
    assert repo.completed is not None
    assert calls == []


def test_report_run_failed_through_the_sdk_is_one_tagged_event(sentry_off):
    sink = Envelopes()
    assert observability.init_sentry("milo-agent-worker", env=_env(), transport=sink.transport)
    assert observability.report_run_failed("11111111-2222-3333-4444-555555555555",
                                           "SWARM_V2_EXECUTION_FAILED", "swarm_v2")
    observability.flush()
    events = sink.events()
    assert len(events) == 1
    assert events[0]["tags"] == {"service": "milo-agent-worker", "run_id": "11111111-2222-3333-4444-555555555555",
                                 "error_code": "SWARM_V2_EXECUTION_FAILED", "workflow_key": "swarm_v2"}
    assert events[0]["release"] == RELEASE_SHA



def test_the_scrubber_tags_a_run_named_in_the_request_path():
    event = observability.scrub_event({"request": {
        "method": "GET", "url": "https://api/runs/0F8FAD5B-D9CB-469F-A165-70867728950E/events?after=1"}})
    assert event["tags"] == {"run_id": "0f8fad5b-d9cb-469f-a165-70867728950e"}
    assert event["request"]["url"] == "https://api/runs/0F8FAD5B-D9CB-469F-A165-70867728950E/events"
    assert "tags" not in observability.scrub_event({"request": {"method": "GET", "url": "https://api/health"}})


def test_the_capture_job_reports_a_failed_capture_once_and_keeps_its_status(monkeypatch):
    from backend.catalog import operator_capture

    calls = []
    monkeypatch.setattr(observability, "init_sentry", lambda service, **kw: calls.append(("init", service)))
    monkeypatch.setattr(observability, "report_run_failed",
                        lambda run_id, code, workflow_key=None: calls.append(("failed", code, workflow_key)))
    monkeypatch.setattr(observability, "flush", lambda timeout=2.0: None)
    monkeypatch.setattr(operator_capture, "main", lambda argv: operator_capture.EXIT_FAILED)
    assert operator_capture._process_main([]) == operator_capture.EXIT_FAILED
    assert calls == [("init", "milo-catalog-capture"),
                     ("failed", "CATALOG_CAPTURE_FAILED", "operator_capture")]
    calls.clear()
    monkeypatch.setattr(operator_capture, "main", lambda argv: operator_capture.EXIT_OK)
    assert operator_capture._process_main([]) == operator_capture.EXIT_OK
    assert calls == [("init", "milo-catalog-capture")]


def test_the_capture_job_reports_a_crash_and_reraises_it(monkeypatch):
    from backend.catalog import operator_capture

    reported = []
    monkeypatch.setattr(observability, "init_sentry", lambda service, **kw: None)
    monkeypatch.setattr(observability, "report_exception", lambda exc: reported.append(type(exc).__name__))
    monkeypatch.setattr(observability, "flush", lambda timeout=2.0: None)

    def boom(argv):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(operator_capture, "main", boom)
    with pytest.raises(RuntimeError):
        operator_capture._process_main([])
    assert reported == ["RuntimeError"]
