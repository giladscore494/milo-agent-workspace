"""CODE-1: what the operator capture entrypoint refuses, and what it connects.

Offline and deterministic. An autouse fixture makes creating a socket an error
for the whole module, the transport is the committed R5 Government capture
re-hashed through the R5 manifest, and the repository is the in-memory one --
so "no network and no database in CI" is enforced here rather than asserted.

The suite is in four parts, matching the four properties this stage claims:

0.  **Offline by construction** -- importing the entrypoint pulls in neither an
    HTTP library nor a database client.
1.  **Default posture** -- every way of invoking it that is not a complete,
    explicit authorization refuses with a static code, before anything is
    constructed.
2.  **Trusted construction** -- the authorized path builds exactly the approved
    transport, the approved client at exactly the reviewed bounds, and the
    existing refresh/ingestion operation, over exactly the pinned resource.
3.  **Lease, failure and reporting safety** -- every durable write carries the
    claimed lease, cancellation and lease loss stop the path, an interrupted
    capture never activates, and nothing but reviewed operational facts reaches
    stdout, stderr or a report file.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from backend.catalog import operator_capture as entrypoint
from backend.catalog.government import source as src
from backend.catalog.government.ingest import IngestionReport
from backend.catalog.government.refresh import RefreshOutcome, SnapshotDiff
from backend import job_launcher as job_launcher_module
from backend.errors import AppError
from backend.production_config import TRUE_VALUES
from backend.testing import government_capture as capture_fixtures
from backend.testing.government_capture import FixtureTransport
from backend.testing.memory_repository import MemoryRepository

ENTRYPOINT_SOURCE = Path("backend/catalog/operator_capture.py")

#: The project this suite pretends the process is configured for. Never a real
#: reference, and compared only against the URL the test itself sets.
PROJECT_REF = "milo-operator-capture-test"

#: Strings that must never appear in stdout, stderr or a report file. Each one
#: stands for a class the sanitized contract forbids: a credential, a bearer
#: token, SQL text, an unrestricted URL and model prose.
SECRET_SENTINEL = "sbp-secret-sentinel-value"
TOKEN_SENTINEL = "bearer-token-sentinel-value"
SQL_SENTINEL = "select * from catalog_source_snapshots where 1=1"
URL_SENTINEL = "https://exfiltration.example.invalid/collect"
MODEL_SENTINEL = "as an ai language model i have decided"


# =============================================================================
# 0. offline by construction
# =============================================================================

@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Creating a socket anywhere in this module is a test failure."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline capture test attempted a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_importing_the_entrypoint_pulls_in_no_http_or_database_client():
    """Import is inert: no `requests`, no `supabase`, no Supabase client.

    Run in a FRESH interpreter, because a module another test already imported
    would make this pass for the wrong reason. `_open_transport` and
    `_open_repository` import their dependencies inside the call, which is what
    makes importing this entrypoint -- from a test, a tool or a REPL -- unable
    to bring a socket or a database client into the process.
    """
    probe = ("import sys; import backend.catalog.operator_capture as m; "
             "print(('requests' in sys.modules, 'supabase' in sys.modules))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            cwd=str(Path.cwd()), timeout=180)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "(False, False)"


def test_the_entrypoint_module_names_no_http_library_and_no_database_client():
    """The repository rule, applied to the new module.

    `backend/catalog/government/transport.py` remains the only module that may
    name an HTTP library; this controller constructs that transport and never
    becomes a second one. The database client is named only inside
    `_open_repository`, which is checked separately below.
    """
    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    for token in ("import requests", "import httpx", "import socket", "urllib.request",
                  "create_client", "import psycopg"):
        assert token not in source, f"the capture entrypoint names {token}"


def test_the_transport_rule_still_holds_for_the_government_package():
    """CODE-1 adds no second socket-capable module to the capture package."""
    for path in sorted(Path("backend/catalog/government").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in ("import requests", "import httpx", "import socket", "urllib.request"):
            if path.name == "transport.py" and token == "import requests":
                continue
            assert token not in source, f"{path} names {token}"


def test_the_entrypoint_reads_no_model_client_and_no_provider_credential():
    """A capture is free: no gateway, no provider, no key, no budget."""
    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    for token in ("ModelGateway", "model_gateway", "chat.completions", "moonshot", "openai",
                  "api_key", "API_KEY", "SERVICE_ROLE", "service_role", "BudgetTracker",
                  "ProviderScheduler"):
        assert token not in source, f"the capture entrypoint names {token}"


def test_no_frontend_or_http_route_exposes_the_capture():
    """No browser surface, no API route and no model-callable tool."""
    for name in ("operator_capture", entrypoint.CAPTURE_ENTRYPOINT,
                 entrypoint.EGRESS_ACKNOWLEDGEMENT):
        for path in sorted(Path("frontend").rglob("*.ts*")):
            if "node_modules" in path.parts:
                continue
            assert name not in path.read_text(encoding="utf-8"), f"{path} names {name}"
    api = Path("backend/main.py").read_text(encoding="utf-8")
    assert "operator_capture" not in api
    guard = Path("backend/execution_guard.py").read_text(encoding="utf-8")
    assert "operator_capture" not in guard and entrypoint.CAPTURE_ENTRYPOINT not in guard
    registry = Path("backend/tools/registry.py").read_text(encoding="utf-8")
    assert "operator_capture" not in registry


def entrypoint_code() -> str:
    """The entrypoint's CODE, with every docstring removed.

    The module docstring describes what this entrypoint refuses to be, so a
    scan for forbidden tokens has to read the statements rather than the prose
    about them -- otherwise "creates no cron" fails on the sentence saying so.
    """
    import ast

    tree = ast.parse(ENTRYPOINT_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_entrypoint_creates_no_schedule():
    """No cron, no timer, no automatic refresh, and exactly one capture."""
    code = entrypoint_code()
    for token in ("cron", "schedule", "Scheduler", "apscheduler", "Timer", "asyncio",
                  "while True", "signal."):
        assert token not in code, f"the capture entrypoint names {token}"
    # One invocation per process: the refresh is CALLED from exactly one place
    # (the other mention is the plan document naming the operation).
    assert code.count(".sync_if_changed(") == 1
    # The only loop is the heartbeat, and it exits on a stop event.
    assert code.count("while ") == 1
    assert "while not self._stopping.wait" in code


# =============================================================================
# shared fixtures
# =============================================================================

SUPABASE_URL = f"https://{PROJECT_REF}.supabase.co"


def capture_env(**overrides: str) -> dict[str, str]:
    """A process environment with every prerequisite satisfied."""
    env = {"SUPABASE_URL": SUPABASE_URL, "MILO_ENABLE_CATALOG_EXECUTION": "true"}
    env.update(overrides)
    return env


@pytest.fixture(autouse=True)
def process_environment(monkeypatch):
    """Keep `os.environ` agreeing with the mapping the tests pass.

    `main(env=...)` is a seam for tests and validators, but the repository
    reads `os.environ`, so the entrypoint verifies the project identity against
    BOTH. Every test here therefore sets the process variable to the same
    value; `test_a_divergent_process_environment_refuses` is the one that
    deliberately breaks the agreement.
    """
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


def authorized_argv(run_id: Any, **overrides: Any) -> list[str]:
    """A complete, correct command line, before a test breaks one part of it."""
    values: dict[str, Any] = {
        "--execute": True,
        "--acknowledge-live-government-egress": entrypoint.EGRESS_ACKNOWLEDGEMENT,
        "--acknowledge-schema-report-reviewed": entrypoint.SCHEMA_REPORT_ACKNOWLEDGEMENT,
        "--project-ref": PROJECT_REF,
        "--run-id": str(run_id),
        "--package-id": src.CKAN_PACKAGE_ID,
        "--resource-id": src.WLTP_RESOURCE_ID,
        "--page-limit": str(entrypoint.CAPTURE_PAGE_LIMIT),
    }
    values.update(overrides)
    argv: list[str] = []
    for name, value in values.items():
        if value is None:
            continue
        if value is True:
            argv.append(name)
            continue
        argv.extend([name, str(value)])
    return argv


def whole_resource_page(records: list[Mapping[str, Any]], *, offset: int = 0,
                        total: int | None = None,
                        limit: int = entrypoint.CAPTURE_PAGE_LIMIT) -> bytes:
    """One committed page, re-shaped as a WHOLE-RESOURCE page at this size.

    The structure, the field schema and every row are the committed R5 capture;
    what changes is the paging envelope and the removal of the `q` echo, which
    is exactly the difference between the pinned `q=RAV4` query and the
    whole-resource capture this entrypoint performs.
    """
    document = capture_fixtures.page_document(0)
    result = document["result"]
    result.pop("q", None)
    result["limit"] = limit
    result["offset"] = offset
    result["total"] = len(records) if total is None else total
    result["total_was_estimated"] = False
    result["records"] = list(records)
    return capture_fixtures.encode(document)


def committed_records(count: int) -> list[dict[str, Any]]:
    return list(capture_fixtures.page_document(0)["result"]["records"][:count])


@pytest.fixture
def records() -> list[dict[str, Any]]:
    return committed_records(12)


@pytest.fixture
def transport(records) -> FixtureTransport:
    return FixtureTransport(bodies={0: whole_resource_page(records)})


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


def seed_conversation(repository: MemoryRepository) -> tuple[UUID, UUID]:
    """One project, one member, one conversation. Returns `(conversation, user)`."""
    project_id, user_id = str(uuid4()), str(uuid4())
    repository.seed_user(user_id)
    repository.seed_project(project_id, "catalog-ops", "Catalog ops", [user_id],
                            workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project_id), "operator capture",
                                                  UUID(user_id))
    return UUID(conversation["id"]), UUID(user_id)


def prepare_argv(conversation_id: Any, requested_by: Any, **overrides: Any) -> list[str]:
    values: dict[str, Any] = {
        "--prepare": True,
        "--acknowledge-schema-report-reviewed": entrypoint.SCHEMA_REPORT_ACKNOWLEDGEMENT,
        "--project-ref": PROJECT_REF,
        "--conversation-id": str(conversation_id),
        "--requested-by": str(requested_by),
    }
    values.update(overrides)
    argv: list[str] = []
    for name, value in values.items():
        if value is None:
            continue
        if value is True:
            argv.append(name)
            continue
        argv.extend([name, str(value)])
    return argv


def prepare_run(repository: MemoryRepository, capsys, **overrides: Any) -> UUID:
    """A capture run produced by THE SUPPORTED OPERATOR WORKFLOW.

    This drives `--prepare` itself rather than manufacturing the run state, so
    every capture test below rests on the same path an operator actually has.
    An earlier round of this suite built the state with a direct
    `create_queued_run`, which proved the capture worked over a state nothing
    in the repository could produce.
    """
    conversation_id, user_id = seed_conversation(repository)
    status = entrypoint.main(prepare_argv(conversation_id, user_id, **overrides),
                             env=capture_env())
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    return UUID(document["preparation"]["run_id"])


def ordinary_run(repository: MemoryRepository, *,
                 metadata: Mapping[str, Any] | None = None) -> UUID:
    """A run created the way the PRODUCT creates one, and never prepared.

    `create_queued_run` is the repository method both production creation paths
    reach; it inserts `launch_state='pending'`, which is the launchable state.
    `metadata` is the browser-supplied metadata, so a test can put the operator
    marker on an ordinary run and prove the marker alone changes nothing.
    """
    conversation_id, user_id = seed_conversation(repository)
    message = repository.create_user_message(conversation_id, "ordinary run", {})
    run = repository.create_queued_run(
        conversation_id, message["id"], "ordinary run", dict(metadata or {}),
        requested_by=user_id, idempotency_key=str(uuid4()))
    return UUID(run["id"])


@pytest.fixture
def tripwire(monkeypatch):
    """Constructing a transport or a repository is a test failure.

    Installed for every refusal test, so "refuses BEFORE side effects" is
    checked at the two construction seams rather than inferred from an exit
    code.
    """
    def refuse_transport() -> Any:
        raise AssertionError("a refused capture constructed a network transport")

    def refuse_repository() -> Any:
        raise AssertionError("a refused capture constructed a repository")

    monkeypatch.setattr(entrypoint, "_open_transport", refuse_transport)
    monkeypatch.setattr(entrypoint, "_open_repository", refuse_repository)


@pytest.fixture
def wired(monkeypatch, repository, transport):
    """The authorized path with ONLY the two seams replaced.

    The transport is the committed capture and the repository is the in-memory
    one. Everything between them -- `DataGovClient`, `GovernmentCatalogRefresh`,
    `GovernmentCatalogIngestor`, `WorkerLease`, the claim, the heartbeat, the
    guarded writes and activation -- is the production code, unmodified.
    """
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    return repository, transport


def run_main(argv: list[str], env: Mapping[str, str], capsys) -> tuple[int, dict[str, Any], str]:
    status = entrypoint.main(argv, env=dict(env))
    captured = capsys.readouterr()
    return status, json.loads(captured.out), captured.err


# =============================================================================
# 1. default posture -- every incomplete authorization refuses
# =============================================================================

def test_help_has_no_side_effects(tripwire, capsys):
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main(["--help"], env=capture_env())
    assert exit_info.value.code == 0
    assert "Refuses by default" in capsys.readouterr().out


def test_default_invocation_refuses(tripwire, capsys):
    status, document, stderr = run_main([], capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["status"] == "refused"
    assert document["reason_code"] == "CAPTURE_NOT_AUTHORIZED"
    assert "CAPTURE_NOT_AUTHORIZED" in stderr


def test_plan_performs_no_network_repository_claim_or_write(tripwire, capsys):
    status, document, _ = run_main(["--plan"], capture_env(), capsys)
    assert status == entrypoint.EXIT_OK
    assert document["status"] == "planned"
    assert document["reason_code"] == ""
    assert document["plan"]["side_effects_performed"] == []
    assert document["plan"]["page_limit"] == 1000
    assert document["plan"]["resource_id"] == src.WLTP_RESOURCE_ID
    assert document["plan"]["query"] == "whole_resource"
    assert document["plan"]["operation"] == "GovernmentCatalogRefresh.sync_if_changed"


def test_plan_and_execute_together_refuse(tripwire, capsys):
    status, document, _ = run_main(["--plan", "--execute"], capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_MODE_CONTRADICTORY"


@pytest.mark.parametrize("override, expected", [
    ({"--acknowledge-live-government-egress": None}, "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-live-government-egress": "yes"}, "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-live-government-egress": entrypoint.EGRESS_ACKNOWLEDGEMENT.lower()},
     "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-live-government-egress": entrypoint.EGRESS_ACKNOWLEDGEMENT + " "},
     "CAPTURE_EGRESS_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-schema-report-reviewed": None},
     "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-schema-report-reviewed": entrypoint.EGRESS_ACKNOWLEDGEMENT},
     "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"),
])
def test_either_acknowledgement_missing_or_inexact_refuses(tripwire, capsys, override, expected):
    status, document, _ = run_main(authorized_argv(uuid4(), **override), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == expected


def test_wrong_project_identity_refuses(tripwire, capsys):
    argv = authorized_argv(uuid4(), **{"--project-ref": "some-other-project"})
    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PROJECT_MISMATCH"


def test_missing_project_identity_refuses(tripwire, capsys):
    argv = authorized_argv(uuid4(), **{"--project-ref": None})
    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PROJECT_MISMATCH"


def test_a_divergent_process_environment_refuses(tripwire, capsys, monkeypatch):
    """The gate holds against the mapping the REPOSITORY will read.

    `env` is a seam, so an argument that satisfies the gate under one mapping
    must not open a connection to the project named by another.
    """
    monkeypatch.setenv("SUPABASE_URL", "https://a-different-project.supabase.co")
    status, document, _ = run_main(authorized_argv(uuid4()), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PROJECT_MISMATCH"


def test_an_unconfigured_process_refuses(tripwire, capsys):
    env = capture_env()
    env.pop("SUPABASE_URL")
    status, document, _ = run_main(authorized_argv(uuid4()), env, capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PROJECT_NOT_CONFIGURED"


def test_the_project_reference_comes_from_the_configured_url():
    assert entrypoint.configured_project_ref(
        {"SUPABASE_URL": "https://abcdefghij.supabase.co"}) == "abcdefghij"
    assert entrypoint.configured_project_ref({"SUPABASE_URL": ""}) == ""
    assert entrypoint.configured_project_ref({}) == ""
    assert entrypoint.configured_project_ref({"SUPABASE_URL": "not a url"}) == ""


@pytest.mark.parametrize("run_id", [None, "", "not-a-uuid", "0d44d491"])
def test_a_missing_or_malformed_run_identity_refuses(tripwire, capsys, run_id):
    argv = authorized_argv(uuid4(), **{"--run-id": run_id})
    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_RUN_IDENTITY_INVALID"


@pytest.mark.parametrize("value", [None, "", "false", "0", "no", "off", "enabled", "truthy",
                                   "1;true", "TRUE FALSE"])
def test_catalog_flag_off_unset_or_malformed_refuses(tripwire, capsys, value):
    """CODE-2's switch gates this entrypoint with the same parser it gates the
    worker with: unset is off, `false` is off, and a value nobody recognises is
    a misconfiguration whose safe reading is off."""
    env = capture_env()
    if value is None:
        env.pop("MILO_ENABLE_CATALOG_EXECUTION")
    else:
        env["MILO_ENABLE_CATALOG_EXECUTION"] = value
    status, document, _ = run_main(authorized_argv(uuid4()), env, capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_CATALOG_EXECUTION_DISABLED"


@pytest.mark.parametrize("value", sorted(TRUE_VALUES))
def test_paid_execution_enabled_refuses(tripwire, capsys, value):
    env = capture_env(MILO_ENABLE_PAID_EXECUTION=value)
    status, document, _ = run_main(authorized_argv(uuid4()), env, capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PAID_EXECUTION_ENABLED"


def test_the_paid_execution_reading_matches_the_budget_module():
    """One spelling of "an operator turned this on", proven rather than stated."""
    from backend.budget import paid_execution_enabled

    for value in sorted(TRUE_VALUES) + ["", "false", "0", "enabled", "truthy", " on ", "ON"]:
        env = {entrypoint.PAID_EXECUTION_FLAG: value}
        mine = (env.get(entrypoint.PAID_EXECUTION_FLAG) or "").strip().lower() in TRUE_VALUES
        import os
        previous = os.environ.get(entrypoint.PAID_EXECUTION_FLAG)
        os.environ[entrypoint.PAID_EXECUTION_FLAG] = value
        try:
            assert mine == paid_execution_enabled(), value
        finally:
            if previous is None:
                os.environ.pop(entrypoint.PAID_EXECUTION_FLAG, None)
            else:
                os.environ[entrypoint.PAID_EXECUTION_FLAG] = previous


@pytest.mark.parametrize("override, expected", [
    ({"--package-id": None}, "CAPTURE_PACKAGE_NOT_SUPPORTED"),
    ({"--package-id": "degem-rechev-wltp-copy"}, "CAPTURE_PACKAGE_NOT_SUPPORTED"),
    ({"--resource-id": None}, "CAPTURE_RESOURCE_NOT_SUPPORTED"),
    ({"--resource-id": src.QUANTITY_RESOURCE_ID}, "CAPTURE_RESOURCE_NOT_SUPPORTED"),
    ({"--resource-id": "142afde2-6228-49f9-8a29-9b6c3a0cbe41"},
     "CAPTURE_RESOURCE_NOT_SUPPORTED"),
])
def test_an_unsupported_package_or_resource_refuses(tripwire, capsys, override, expected):
    status, document, _ = run_main(authorized_argv(uuid4(), **override), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == expected


@pytest.mark.parametrize("override", [
    {"--page-limit": None},
    {"--page-limit": "100"},
    {"--page-limit": "1001"},
    {"--page-limit": "0"},
    {"--page-limit": "abc"},
    {"--page-limit": "1000.0"},
    {"--page-limit": "+1000"},
    {"--max-pages": "500"},
    {"--max-pages": "abc"},
    {"--max-records": "200000"},
    {"--max-records": ""},
])
def test_an_unsupported_page_size_or_bound_refuses(tripwire, capsys, override):
    status, document, _ = run_main(authorized_argv(uuid4(), **override), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_BOUNDS_NOT_SUPPORTED"


@pytest.mark.parametrize("extra", [
    ["--query", "q=RAV4"],
    ["--filters", '{"tozar":"טויוטה"}'],
    ["--url", "https://data.gov.il/api/3/action/datastore_search"],
    ["--host", "data.gov.il.example.test"],
    ["--action", "datastore_search"],
    ["--offset", "0"],
    ["--limit", "1000"],
])
def test_an_unsupported_argument_refuses(tripwire, capsys, extra):
    """There is no URL, host, action, query, filter or paging argument here."""
    status, document, _ = run_main(authorized_argv(uuid4()) + extra, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_ARGUMENT_NOT_SUPPORTED"


@pytest.mark.parametrize("override", [
    {"MILO_WORKER_LEASE_SECONDS": "not-a-number"},
    {"MILO_WORKER_LEASE_SECONDS": "0"},
    {"MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS": "-1"},
    {"MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS": "zero"},
])
def test_an_unreadable_lease_configuration_refuses(tripwire, capsys, override):
    status, document, _ = run_main(authorized_argv(uuid4()), capture_env(**override), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_LEASE_CONFIG_INVALID"


def test_a_refusal_writes_no_file(tripwire, capsys, tmp_path):
    report = tmp_path / "nested" / "report.json"
    status, _, _ = run_main([], capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert not report.exists()
    assert not report.parent.exists()


# =============================================================================
# 1b. the run gate -- checked server-side, BEFORE the claim
# =============================================================================

def test_an_unknown_run_refuses_without_claiming(wired, capsys):
    repository, transport = wired
    status, document, _ = run_main(authorized_argv(uuid4()), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_RUN_UNAVAILABLE"
    assert transport.calls == []


@pytest.mark.parametrize("mutate, field", [
    (lambda run: run.update({"launch_state": "launching"}), "launch_state"),
    (lambda run: run.update({"launch_state": "launched"}), "launch_state"),
    (lambda run: run.update({"launch_state": "pending"}), "launch_state"),
    (lambda run: run.update({"status": "running"}), "status"),
])
def test_a_run_the_operator_does_not_own_refuses_without_claiming(wired, capsys, mutate, field):
    """The blocker this gate exists for: a run in any launch state the ordinary
    path can reach is an ordinary model run, and claiming it would race its
    worker. Each state here is forced onto an already-prepared run, which is
    the only way to reach them from an operator-owned run at all."""
    repository, transport = wired
    run_id = prepare_run(repository, capsys)
    mutate(repository.runs[str(run_id)])
    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_RUN_NOT_ELIGIBLE"
    assert transport.calls == []
    assert repository.runs[str(run_id)].get("lease_token") is None


@pytest.mark.parametrize("metadata", [
    {},
    {"milo_operation": "chat"},
    {"milo_operation": ""},
    {"other": "catalog.government.capture"},
    # THE ONE THAT MATTERS: the exact operator marker, browser-supplied. A
    # user controls the `metadata` of a run-creation request and that metadata
    # reaches `input.metadata`, so the marker on its own must buy nothing.
    {"milo_operation": entrypoint.OPERATOR_CAPTURE_OPERATION},
])
def test_an_unprepared_run_is_never_capturable_whatever_its_metadata(wired, capsys, metadata):
    repository, transport = wired
    run_id = ordinary_run(repository, metadata=metadata)
    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_RUN_NOT_ELIGIBLE"
    assert transport.calls == []
    assert repository.runs[str(run_id)].get("lease_token") is None
    # It is still an ordinary, launchable run -- this refusal took nothing.
    assert repository.runs[str(run_id)]["launch_state"] == "pending"
    assert repository.try_acquire_launch(run_id) is not None


# =============================================================================
# 2. trusted construction
# =============================================================================

def test_the_execution_branch_constructs_the_approved_transport_and_client(wired, capsys):
    repository, transport = wired
    run_id = prepare_run(repository, capsys)
    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    assert document["status"] == "succeeded"

    # Two `package_show` reads, and that is the existing contract rather than a
    # duplicate: the refresh reads the published version to decide whether to
    # capture at all, and `capture_resource` reads the metadata itself because
    # `ResourceMetadata` is a RESULT of the capture and never an input to it.
    actions = [action for action, _params in transport.calls]
    assert actions == [src.PACKAGE_SHOW, src.PACKAGE_SHOW, src.DATASTORE_SEARCH]
    _action, params = transport.calls[-1]
    # Exactly 1000, and exactly the pinned resource, with NO query parameter.
    assert params["limit"] == "1000"
    assert params["offset"] == "0"
    assert params["resource_id"] == src.WLTP_RESOURCE_ID
    assert "q" not in params and "filters" not in params
    # The response, connect and read bounds reach the transport unchanged.
    for connect_timeout, read_timeout, max_bytes in transport.bounds:
        assert connect_timeout == src.CONNECT_TIMEOUT_SECONDS
        assert read_timeout == src.READ_TIMEOUT_SECONDS
        assert max_bytes == src.MAX_RESPONSE_BYTES


def test_the_existing_capture_bounds_are_unchanged():
    """CODE-1 sets a page size; it weakens no bound.

    The page size it sets is `MAX_PAGE_LIMIT` itself, so the reason a ~101 000
    row resource now fits is arithmetic over the EXISTING ceiling, not a raised
    one: `ceil(101000/1000) = 101 <= 200`, where `ceil(101000/100) = 1010`
    would have been `GOV_PAGE_BUDGET_EXCEEDED`.
    """
    assert src.MAX_PAGE_LIMIT == 1000
    assert src.MAX_PAGES_PER_CAPTURE == 200
    assert src.MAX_RECORDS_PER_CAPTURE == 120_000
    assert src.MAX_RESPONSE_BYTES == 8 * 1024 * 1024
    assert src.DEFAULT_PAGE_LIMIT == 100
    assert entrypoint.CAPTURE_PAGE_LIMIT == src.MAX_PAGE_LIMIT == 1000
    assert entrypoint.CAPTURE_MAX_PAGES == src.MAX_PAGES_PER_CAPTURE
    assert entrypoint.CAPTURE_MAX_RECORDS == src.MAX_RECORDS_PER_CAPTURE
    expected_pages = -(-101_000 // entrypoint.CAPTURE_PAGE_LIMIT)
    assert expected_pages == 101 <= src.MAX_PAGES_PER_CAPTURE
    assert -(-101_000 // src.DEFAULT_PAGE_LIMIT) == 1010 > src.MAX_PAGES_PER_CAPTURE


def test_the_default_transport_seam_names_exactly_the_approved_transport():
    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    body = source.split("def _open_transport()")[1].split("def _open_repository()")[0]
    assert "HttpsDataGovTransport" in body
    assert body.count("return ") == 1
    repository_body = source.split("def _open_repository()")[1].split("\ndef ")[0]
    assert "SupabaseRepository" in repository_body and "get_settings" in repository_body


def test_only_the_pinned_wltp_resource_and_the_whole_resource_query_are_reachable():
    """`source.py` allowlists two resources; this entrypoint reaches one."""
    source = ENTRYPOINT_SOURCE.read_text(encoding="utf-8")
    assert "QUANTITY_RESOURCE_ID" not in source
    assert "query=None" in source
    assert src.WLTP_RESOURCE_ID in {src.WLTP_RESOURCE_ID}
    plan = entrypoint.plan_document()
    assert plan["resource_id"] == src.WLTP_RESOURCE_ID
    assert plan["package_id"] == src.CKAN_PACKAGE_ID
    assert plan["query"] == "whole_resource"


def test_the_capture_lands_a_complete_activated_snapshot(wired, capsys, records):
    repository, _transport = wired
    run_id = prepare_run(repository, capsys)
    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document

    snapshot = document["capture"]["snapshot"]
    assert document["capture"]["outcome"] == "changed"
    assert snapshot["declared_record_count"] == len(records)
    assert snapshot["stored_record_count"] == len(records)
    assert snapshot["page_count"] == 1
    assert snapshot["activated"] is True
    assert snapshot["reused_existing"] is False
    assert len(repository.catalog_snapshots) == 1
    stored = next(iter(repository.catalog_snapshots.values()))
    assert stored["activated_at"] is not None
    assert stored["created_by_run_id"] == str(run_id)
    assert len(repository.catalog_raw_records) == len(records)
    assert repository.runs[str(run_id)]["status"] == "completed"


def test_an_unchanged_source_is_a_no_op(wired, capsys):
    """The refresh path's own property, reached through the entrypoint."""
    repository, transport = wired
    first = prepare_run(repository, capsys)
    status, document, _ = run_main(authorized_argv(first), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    assert document["capture"]["outcome"] == "changed"
    snapshots_after_first = dict(repository.catalog_snapshots)
    records_after_first = dict(repository.catalog_raw_records)

    second = prepare_run(repository, capsys)
    status, document, _ = run_main(authorized_argv(second), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    assert document["capture"]["outcome"] == "unchanged"
    assert document["capture"]["no_op"] is True
    assert "snapshot" not in document["capture"]
    # Nothing was captured and nothing was written: one `package_show` only.
    assert [action for action, _ in transport.calls][-1] == src.PACKAGE_SHOW
    assert repository.catalog_snapshots == snapshots_after_first
    assert repository.catalog_raw_records == records_after_first


# =============================================================================
# 3. lease and failure safety
# =============================================================================

class LeaseWatchingRepository(MemoryRepository):
    """Records the lease every durable catalog write was given."""

    def __init__(self) -> None:
        super().__init__()
        self.write_leases: list[tuple[str, str, int, str]] = []

    def _note(self, name: str, worker_id: str, attempt: int, lease_token: str) -> None:
        self.write_leases.append((name, worker_id, attempt, lease_token))

    def record_catalog_snapshot(self, run_id, snapshot, *, worker_id, attempt, lease_token):
        self._note("snapshot", worker_id, attempt, lease_token)
        return super().record_catalog_snapshot(run_id, snapshot, worker_id=worker_id,
                                               attempt=attempt, lease_token=lease_token)

    def record_catalog_raw_record(self, run_id, record, *, worker_id, attempt, lease_token):
        self._note("raw_record", worker_id, attempt, lease_token)
        return super().record_catalog_raw_record(run_id, record, worker_id=worker_id,
                                                 attempt=attempt, lease_token=lease_token)

    def record_catalog_candidate(self, run_id, candidate, *, worker_id, attempt, lease_token):
        self._note("candidate", worker_id, attempt, lease_token)
        return super().record_catalog_candidate(run_id, candidate, worker_id=worker_id,
                                                attempt=attempt, lease_token=lease_token)

    def activate_catalog_snapshot(self, run_id, activation, *, worker_id, attempt, lease_token):
        self._note("activate", worker_id, attempt, lease_token)
        return super().activate_catalog_snapshot(run_id, activation, worker_id=worker_id,
                                                 attempt=attempt, lease_token=lease_token)


def test_every_durable_write_carries_the_claimed_lease(monkeypatch, transport, capsys):
    repository = LeaseWatchingRepository()
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document

    run = repository.runs[str(run_id)]
    assert repository.write_leases, "the capture made no durable write"
    for name, worker_id, attempt, lease_token in repository.write_leases:
        assert worker_id == run["worker_id"], name
        assert attempt == int(run["attempt"]), name
        assert lease_token == run["lease_token"], name
        assert worker_id.startswith("operator-capture-"), name
    assert {name for name, *_ in repository.write_leases} >= {
        "snapshot", "raw_record", "candidate", "activate"}


class CancellingRepository(MemoryRepository):
    """Requests cancellation the moment the capture claims the run."""

    def claim_run(self, run_id, worker_id, lease_seconds=300):
        claimed = super().claim_run(run_id, worker_id, lease_seconds=lease_seconds)
        self.runs[str(run_id)]["status"] = "cancellation_requested"
        return claimed


def test_cancellation_stops_the_path_before_any_write(monkeypatch, transport, capsys):
    """The immediate heartbeat sees it, so no page is ever read."""
    repository = CancellingRepository()
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, stderr = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_CANCELLED"
    assert "CAPTURE_CANCELLED" in stderr
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert repository.runs[str(run_id)]["status"] == "cancelled"


class LeaseStealingRepository(MemoryRepository):
    """Another worker reclaims the run after the Nth raw record."""

    def __init__(self, steal_after: int = 3) -> None:
        super().__init__()
        self._steal_after = steal_after
        self.written = 0

    def record_catalog_raw_record(self, run_id, record, *, worker_id, attempt, lease_token):
        row = super().record_catalog_raw_record(run_id, record, worker_id=worker_id,
                                                attempt=attempt, lease_token=lease_token)
        self.written += 1
        if self.written == self._steal_after:
            self.runs[str(run_id)]["lease_token"] = "a-replacement-worker-token"
        return row


def test_lease_loss_prevents_subsequent_writes_and_activation(monkeypatch, transport, capsys):
    repository = LeaseStealingRepository()
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_REPOSITORY_UNAVAILABLE"
    assert "capture" not in document
    # The snapshot exists and is NOT active: a partial capture is invisible.
    assert len(repository.catalog_snapshots) == 1
    stored = next(iter(repository.catalog_snapshots.values()))
    assert stored["activated_at"] is None
    assert repository.find_active_catalog_snapshot(
        src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, stored["snapshot_key"]) is None
    assert repository.written < len(committed_records(12))


def test_an_invalid_capture_response_creates_no_snapshot(monkeypatch, repository, capsys,
                                                         records):
    """A page whose length contradicts its own total is refused before any
    durable write, so an inconsistent capture leaves no trace at all."""
    broken = whole_resource_page(records, total=len(records) + 5)
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(bodies={0: broken}))
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, stderr = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "GOV_PAGE_COUNT_UNEXPECTED"
    assert document["reason"] == src.GOVERNMENT_SOURCE_REASONS["GOV_PAGE_COUNT_UNEXPECTED"]
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert repository.runs[str(run_id)]["status"] == "failed"
    assert "GOV_PAGE_COUNT_UNEXPECTED" in stderr


class FailingAfterSnapshotRepository(MemoryRepository):
    """A durable write fails once the snapshot is open."""

    def record_catalog_candidate(self, run_id, candidate, *, worker_id, attempt, lease_token):
        raise AppError("REPOSITORY_ERROR", f"{SQL_SENTINEL} -- {SECRET_SENTINEL}", 502)


def test_an_interruption_after_opening_a_snapshot_never_activates_it(monkeypatch, transport,
                                                                     capsys):
    repository = FailingAfterSnapshotRepository()
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, stderr = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_REPOSITORY_UNAVAILABLE"
    stored = next(iter(repository.catalog_snapshots.values()))
    assert stored["activated_at"] is None
    rendered = json.dumps(document) + stderr
    assert SQL_SENTINEL not in rendered and SECRET_SENTINEL not in rendered


def test_a_previous_snapshot_remains_usable_after_a_failed_refresh(monkeypatch, repository,
                                                                   transport, capsys, records):
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    first = prepare_run(repository, capsys)
    status, document, _ = run_main(authorized_argv(first), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    good_key = document["capture"]["snapshot"]["snapshot_key"]

    # A later refresh whose metadata read fails outright.
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(transport_failures=9))
    second = prepare_run(repository, capsys)
    status, failure, _ = run_main(authorized_argv(second), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert failure["reason_code"] == "GOV_TRANSPORT_FAILED"

    still_active = repository.find_active_catalog_snapshot(
        src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, good_key)
    assert still_active is not None
    assert still_active["activated_at"] is not None
    assert int(still_active["stored_record_count"]) == len(records)


def test_an_unexpected_exception_is_reduced_to_static_safe_output(monkeypatch, transport,
                                                                  repository, capsys):
    def explode(*_args, **_kwargs):
        raise RuntimeError(f"{SECRET_SENTINEL} {URL_SENTINEL} {MODEL_SENTINEL}")

    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)
    monkeypatch.setattr(repository, "record_catalog_snapshot", explode)

    status, document, stderr = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_UNEXPECTED_FAILURE"
    assert document["reason"] == entrypoint.CAPTURE_REASONS["CAPTURE_UNEXPECTED_FAILURE"]
    rendered = json.dumps(document, ensure_ascii=False) + stderr
    for sentinel in (SECRET_SENTINEL, URL_SENTINEL, MODEL_SENTINEL):
        assert sentinel not in rendered


# =============================================================================
# 4. reporting
# =============================================================================

def test_the_success_report_is_bounded_and_deterministic(wired, capsys, tmp_path):
    repository, _transport = wired
    run_id = prepare_run(repository, capsys)
    report_path = tmp_path / "capture.json"
    argv = authorized_argv(run_id, **{"--report-path": str(report_path)})

    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    assert json.loads(report_path.read_text(encoding="utf-8")) == document

    capture = document["capture"]
    assert set(document) == {"entrypoint", "status", "reason_code", "reason", "capture"}
    assert set(capture) == {"outcome", "resource_id", "upstream_version",
                            "upstream_version_kind", "active_snapshot_key", "no_op",
                            "diff_unavailable", "research_required", "snapshot", "diff"}
    snapshot = capture["snapshot"]
    assert set(snapshot) == {
        "snapshot_id", "snapshot_key", "content_sha256", "schema_fingerprint", "resource_id",
        "upstream_version", "upstream_version_kind", "declared_record_count",
        "stored_record_count", "page_count", "candidate_count", "candidate_status_counts",
        "normalization_contract", "normalized_record_count", "normalization_issue_count",
        "normalization_issues", "normalization_issue_records", "rejected_record_count",
        "activated", "reused_existing"}
    assert len(snapshot["content_sha256"]) == 64
    assert snapshot["schema_fingerprint"].startswith("gov.schema.")
    assert len(snapshot["schema_fingerprint"]) <= entrypoint.MAX_REPORT_TEXT_CHARS
    assert snapshot["normalization_contract"]
    assert set(capture["diff"]) == {"previous_snapshot_key", "added_count", "changed_count",
                                    "removed_count", "bounded"}
    # A first capture has no previous side, so everything is `added`. The count
    # is over distinct candidate IDENTITIES, which is at most the candidate row
    # count and is smaller whenever two register rows read to one identity.
    assert capture["diff"]["previous_snapshot_key"] == ""
    assert 0 < capture["diff"]["added_count"] <= snapshot["candidate_count"]
    assert capture["diff"]["changed_count"] == 0
    assert capture["diff"]["removed_count"] == 0
    for value in (snapshot["content_sha256"], snapshot["upstream_version"],
                  capture["active_snapshot_key"]):
        assert len(value) <= entrypoint.MAX_REPORT_TEXT_CHARS


def test_the_report_carries_normalization_issues_and_never_a_raw_row(monkeypatch, repository,
                                                                     capsys, records):
    """A row the register contradicts itself about is COUNTED, never quoted."""
    hostile = [dict(record) for record in records]
    hostile[0] = {**hostile[0], "tozeret_nm": SECRET_SENTINEL, "degem_nm": SQL_SENTINEL,
                  "tozar": URL_SENTINEL, "kinuy_mishari": MODEL_SENTINEL}
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(bodies={0: whole_resource_page(hostile)}))
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    run_id = prepare_run(repository, capsys)

    status, document, stderr = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    snapshot = document["capture"]["snapshot"]
    assert isinstance(snapshot["normalization_issue_count"], int)
    assert isinstance(snapshot["normalization_issues"], dict)
    assert len(snapshot["normalization_issue_records"]) <= entrypoint.MAX_REPORT_ISSUE_RECORDS

    rendered = json.dumps(document, ensure_ascii=False) + stderr
    for sentinel in (SECRET_SENTINEL, SQL_SENTINEL, URL_SENTINEL, MODEL_SENTINEL):
        assert sentinel not in rendered
    # The row itself IS durable -- it is the REPORT that does not carry it.
    assert len(repository.catalog_raw_records) == len(hostile)


def test_no_lease_material_or_url_reaches_stdout_stderr_or_the_report(wired, capsys, tmp_path):
    repository, _transport = wired
    run_id = prepare_run(repository, capsys)
    report_path = tmp_path / "capture.json"
    argv = authorized_argv(run_id, **{"--report-path": str(report_path)})

    status, document, stderr = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    rendered = json.dumps(document, ensure_ascii=False) + stderr + report_path.read_text(
        encoding="utf-8")

    run = repository.runs[str(run_id)]
    assert run["lease_token"] not in rendered
    assert run["worker_id"] not in rendered
    assert "lease_token" not in rendered and "worker_id" not in rendered
    assert src.DATA_GOV_HOST not in rendered
    assert "https://" not in rendered
    assert PROJECT_REF not in rendered
    # No raw row: neither a captured field NAME nor a captured field VALUE.
    row = committed_records(1)[0]
    for field in ("sug_degem", "tozeret_nm", "degem_nm", "shnat_yitzur", "nefah_manoa"):
        assert field not in rendered
    for value in (row["tozeret_nm"], row["degem_nm"], row["tozar"]):
        assert str(value) not in rendered
    # And no response body: the JSON holds no `records` key at any depth.
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    assert "records" not in set(keys(document))


def test_a_report_path_is_the_only_file_written(wired, capsys, tmp_path):
    repository, _transport = wired
    run_id = prepare_run(repository, capsys)
    status, _document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_report_path_is_a_non_zero_outcome(wired, capsys, tmp_path):
    repository, _transport = wired
    run_id = prepare_run(repository, capsys)
    argv = authorized_argv(run_id, **{"--report-path": str(tmp_path / "absent" / "r.json")})
    status = entrypoint.main(argv, env=capture_env())
    captured = capsys.readouterr()
    assert status == entrypoint.EXIT_FAILED
    assert "CAPTURE_REPORT_NOT_WRITTEN" not in captured.out
    assert entrypoint.safe_message("CAPTURE_REPORT_NOT_WRITTEN") in captured.err


# =============================================================================
# 4b. the report's own vocabulary and outcome mapping
# =============================================================================

def _report(**overrides: Any) -> IngestionReport:
    values: dict[str, Any] = {
        "snapshot_id": str(uuid4()), "snapshot_key": "government:wltp:abc",
        "content_sha256": "a" * 64, "resource_id": src.WLTP_RESOURCE_ID,
        "upstream_version": "2026-09-01T00:00:00", "upstream_version_kind": "last_modified",
        "schema_fingerprint": "b" * 64, "declared_record_count": 3, "stored_record_count": 3,
        "page_count": 1, "candidate_count": 3, "candidate_status_counts": {"ready": 3},
    }
    values.update(overrides)
    return IngestionReport(**values)


def _outcome(**overrides: Any) -> RefreshOutcome:
    values: dict[str, Any] = {
        "changed": True, "resource_id": src.WLTP_RESOURCE_ID,
        "upstream_version": "2026-09-01T00:00:00", "upstream_version_kind": "last_modified",
        "active_snapshot_key": "government:wltp:abc", "report": _report(), "no_op": False,
    }
    values.update(overrides)
    return RefreshOutcome(**values)


@pytest.mark.parametrize("outcome, replayed, expected", [
    (_outcome(changed=False, report=None, no_op=True), False, "unchanged"),
    (_outcome(report=_report(reused_existing=True)), False, "reused"),
    (_outcome(), True, "replayed"),
    (_outcome(), False, "changed"),
])
def test_every_outcome_state_is_named_from_what_the_refresh_states(outcome, replayed, expected):
    assert entrypoint.capture_document(outcome, replayed=replayed)["outcome"] == expected


def test_the_report_carries_diff_counts_but_never_diff_items():
    diff = SnapshotDiff(previous_snapshot_key="government:wltp:old",
                        snapshot_key="government:wltp:abc", added_count=7, changed_count=2,
                        removed_count=1, bounded=True)
    document = entrypoint.capture_document(_outcome(diff=diff), replayed=False)
    assert document["diff"] == {"previous_snapshot_key": "government:wltp:old",
                                "added_count": 7, "changed_count": 2, "removed_count": 1,
                                "bounded": True}


def test_every_reported_reason_code_comes_from_a_closed_vocabulary():
    from backend.catalog.government.ingest import GOVERNMENT_INGESTION_REASONS

    for code, message in entrypoint.CAPTURE_REASONS.items():
        assert code.startswith("CAPTURE_")
        assert entrypoint.safe_message(code) == message
    for code in src.GOVERNMENT_SOURCE_REASONS:
        assert entrypoint.safe_message(code) == src.GOVERNMENT_SOURCE_REASONS[code]
    for code in GOVERNMENT_INGESTION_REASONS:
        assert entrypoint.safe_message(code) == GOVERNMENT_INGESTION_REASONS[code]
    # An unknown code is reported as unknown, never echoed back.
    assert entrypoint.safe_message("SOMETHING_NOBODY_DECLARED") == \
        entrypoint.CAPTURE_REASONS["CAPTURE_UNEXPECTED_FAILURE"]


def test_bounded_helpers_drop_what_they_cannot_read():
    assert entrypoint._counts({"a": 1, "b": "2", "c": True, "d": 3}, limit=10) == {"a": 1, "d": 3}
    assert len(entrypoint._counts({str(index): index for index in range(50)}, limit=5)) == 5
    assert entrypoint._counts("not a mapping", limit=5) == {}
    assert entrypoint._identifiers(list(range(500)), limit=4) == ["0", "1", "2", "3"]
    assert entrypoint._identifiers("not a list", limit=4) == []
    assert entrypoint._text("x" * 500) == "x" * entrypoint.MAX_REPORT_TEXT_CHARS


# =============================================================================
# 5. the operator preparation seam, and the launch-vs-capture race
#
# This section is about LIFECYCLE SEMANTICS, not about the final state: every
# test here reaches the state through the supported path and then interferes
# with it, rather than constructing the state it wants to see. An earlier round
# of this suite proved the capture worked over a run state nothing in the
# repository could actually produce, which is exactly the gap these close.
# =============================================================================

class RecordingLauncher:
    """A `JobLauncher` that records every call and launches nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch(self, run_id: UUID) -> dict[str, str]:
        self.calls.append(str(run_id))
        return {"mode": "recording", "run_id": str(run_id)}


def prepared(repository: MemoryRepository, capsys, **overrides: Any) -> tuple[UUID, dict]:
    """Prepare a run and return `(run_id, the preparation document)`."""
    conversation_id, user_id = seed_conversation(repository)
    status = entrypoint.main(prepare_argv(conversation_id, user_id, **overrides),
                             env=capture_env())
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    return UUID(document["preparation"]["run_id"]), document


# --- 1. a supported preparation path exists, and it launches nothing ---------

def test_preparation_produces_a_capture_run_without_any_launcher(monkeypatch, repository,
                                                                 capsys):
    """The blocker this seam closes: before it, the required state was
    reachable only by a manual row edit nothing in the repository supported."""
    launcher = RecordingLauncher()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: (_ for _ in ()).throw(
        AssertionError("preparation constructed a network transport")))
    monkeypatch.setattr(job_launcher_module, "build_job_launcher",
                        lambda _settings: launcher)

    run_id, document = prepared(repository, capsys)

    assert document["status"] == "prepared"
    assert document["reason_code"] == ""
    assert document["preparation"]["already_prepared"] is False
    assert document["preparation"]["launch_owner"] == "operator"
    assert document["preparation"]["captured"] is False
    # No launcher was built, let alone called.
    assert launcher.calls == []

    run = repository.runs[str(run_id)]
    assert run["status"] == entrypoint.ELIGIBLE_RUN_STATUS
    assert run["launch_state"] == entrypoint.OPERATOR_OWNED_LAUNCH_STATE
    assert run["input"]["metadata"]["milo_operation"] == entrypoint.OPERATOR_CAPTURE_OPERATION
    # Nothing was captured: no snapshot, no raw record, no candidate.
    assert repository.catalog_snapshots == {} and repository.catalog_raw_records == {}
    assert run.get("lease_token") is None


def test_preparation_reports_only_the_run_identity(monkeypatch, repository, capsys):
    """A preparation report is not a place to widen what is printed."""
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    conversation_id, user_id = seed_conversation(repository)
    status = entrypoint.main(prepare_argv(conversation_id, user_id), env=capture_env())
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert status == entrypoint.EXIT_OK

    assert set(document) == {"entrypoint", "status", "reason_code", "reason", "preparation"}
    assert set(document["preparation"]) == {"run_id", "already_prepared", "launch_owner",
                                            "captured"}
    rendered = json.dumps(document) + captured.err
    assert str(conversation_id) not in rendered
    assert str(user_id) not in rendered
    assert PROJECT_REF not in rendered
    assert "lease_token" not in rendered and "worker_id" not in rendered


def test_preparation_sends_no_government_request_and_reads_no_provider(monkeypatch,
                                                                       repository, capsys):
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: (_ for _ in ()).throw(
        AssertionError("preparation constructed a network transport")))
    for name in ("MOONSHOT_API_KEY", "OPENAI_API_KEY", "MILO_PROVIDER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    run_id, _ = prepared(repository, capsys)
    assert run_id
    # The construction seam for egress was never reached, and the module names
    # no provider at all (asserted in section 0).
    code = entrypoint_code()
    prepare_body = code.split("def _prepare(")[1].split("\ndef ")[0]
    assert "_open_transport" not in prepare_body
    assert "JobLauncher" not in code and "build_job_launcher" not in code


# --- 5. the launch CAS wins after the read, before ownership is acquired -----

class LauncherStealsLaunchOwnership(MemoryRepository):
    """The ordinary launch path wins the CAS the instant the run exists.

    This is the exact interleaving the review named: the run is created, and
    before the operator can take launch ownership, `try_acquire_launch` has
    already moved it to `launching` for a real launcher.
    """

    def create_queued_run(self, conversation_id, user_message_id, content, metadata,
                          requested_by=None, idempotency_key=None, request_fingerprint=None):
        run = super().create_queued_run(conversation_id, user_message_id, content, metadata,
                                        requested_by=requested_by,
                                        idempotency_key=idempotency_key,
                                        request_fingerprint=request_fingerprint)
        # The ordinary path gets there first, through the same CAS.
        assert super().try_acquire_launch(UUID(str(run["id"]))) is not None
        return run


def test_preparation_refuses_when_the_ordinary_launch_path_wins_the_cas(monkeypatch, capsys):
    repository = LauncherStealsLaunchOwnership()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: (_ for _ in ()).throw(
        AssertionError("a refused preparation constructed a network transport")))
    conversation_id, user_id = seed_conversation(repository)

    status = entrypoint.main(prepare_argv(conversation_id, user_id), env=capture_env())
    document = json.loads(capsys.readouterr().out)

    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_LAUNCH_OWNERSHIP_LOST"
    assert "preparation" not in document
    # The run is left exactly as the launcher holds it: not stolen back.
    run = next(iter(repository.runs.values()))
    assert run["launch_state"] == "launching"
    assert run.get("lease_token") is None
    assert repository.catalog_snapshots == {}


class LauncherStealsAfterTheRead(MemoryRepository):
    """Launch ownership changes between the capture's read and its claim.

    The capture's pre-claim read sees an operator-owned run; by the time the
    lease is claimed, the run's launch state has moved. Only a check on the
    row the CLAIM returned can see that.
    """

    def get_run(self, run_id, user_id=None):
        run = super().get_run(run_id, user_id)
        self.runs[str(run_id)]["launch_state"] = "launching"
        return run


def test_capture_refuses_when_ownership_changes_between_the_read_and_the_claim(
        monkeypatch, transport, capsys):
    repository = LauncherStealsAfterTheRead()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    run_id = prepare_run(repository, capsys)

    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)

    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_LAUNCH_OWNERSHIP_LOST"
    # Nothing was captured and nothing was written.
    assert transport.calls == []
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert "capture" not in document
    assert repository.runs[str(run_id)]["status"] == "failed"


# --- 6. operator ownership blocks the ordinary launch path -------------------

def test_an_operator_owned_run_can_never_be_launched(monkeypatch, repository, capsys):
    """Through the production call path, not through an assertion about it."""
    from backend.auth import AuthenticatedUser
    from backend.main import _create_and_launch_run

    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    conversation_id, user_id = seed_conversation(repository)
    key = "operator-capture-idempotency"
    status = entrypoint.main(prepare_argv(conversation_id, user_id,
                                          **{"--idempotency-key": key}), env=capture_env())
    document = json.loads(capsys.readouterr().out)
    assert status == entrypoint.EXIT_OK, document
    run_id = UUID(document["preparation"]["run_id"])

    # The CAS the ordinary launch path depends on now refuses this run.
    assert repository.try_acquire_launch(run_id) is None

    # And the ordinary route itself, reaching the very same run by its
    # idempotency key, returns without ever invoking the launcher.
    launcher = RecordingLauncher()
    created = _create_and_launch_run(
        repository, launcher, AuthenticatedUser(user_id=user_id), conversation_id,
        entrypoint.PREPARED_RUN_CONTENT, {"milo_operation": "chat"}, key)

    assert launcher.calls == []
    assert str(created.run_id) == str(run_id)
    run = repository.runs[str(run_id)]
    assert run["launch_state"] == entrypoint.OPERATOR_OWNED_LAUNCH_STATE
    assert run["status"] == entrypoint.ELIGIBLE_RUN_STATUS


def test_the_legacy_launch_fallback_is_not_laxer_than_the_cas():
    """`backend/main.py`'s non-CAS branch mirrors the acquirable set exactly.

    A repository fake without `try_acquire_launch` must not be able to launch
    a run the production CAS would refuse -- including an operator-owned one.
    """
    api = Path("backend/main.py").read_text(encoding="utf-8")
    assert 'if run.get("launch_state") not in {None, "pending", "launch_failed"}:' in api
    assert '{"launching", "launched"}' not in api
    # The two sets agree: the fallback's allowed states are the CAS's.
    assert entrypoint.LAUNCH_ACQUIRABLE_STATES == {"pending", "launch_failed"}
    assert entrypoint.OPERATOR_OWNED_LAUNCH_STATE not in entrypoint.LAUNCH_ACQUIRABLE_STATES


def test_the_operator_owned_state_is_one_the_product_never_writes():
    """The state's whole value is that nothing else can produce it."""
    api = Path("backend/main.py").read_text(encoding="utf-8")
    written = set(re.findall(r'set_launch_state\(run_id, "([a-z_]+)"', api))
    assert written == {"launching", "launched", "launch_failed", "launch_unknown"}
    assert entrypoint.OPERATOR_OWNED_LAUNCH_STATE not in written
    repository_source = Path("backend/repository/supabase.py").read_text(encoding="utf-8")
    # Both creation paths insert the LAUNCHABLE state, never the operator one.
    assert '"launch_state": "pending"' in repository_source
    assert f'"launch_state": "{entrypoint.OPERATOR_OWNED_LAUNCH_STATE}"' not in repository_source


# --- 7. preparation then capture reaches the authentic lease -----------------

def test_preparation_then_capture_uses_the_authentic_claim_and_lease(monkeypatch, transport,
                                                                     capsys):
    repository = LeaseWatchingRepository()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    run_id = prepare_run(repository, capsys)

    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document

    run = repository.runs[str(run_id)]
    assert run["lease_token"] and run["worker_id"].startswith("operator-capture-")
    assert repository.write_leases
    for name, worker_id, attempt, lease_token in repository.write_leases:
        assert (worker_id, attempt, lease_token) == (
            run["worker_id"], int(run["attempt"]), run["lease_token"]), name
    assert document["capture"]["snapshot"]["activated"] is True


# --- 8. an interrupted preparation is fail-closed in both directions ---------

class PreparationInterrupted(MemoryRepository):
    """The process dies between winning the CAS and coming to rest."""

    def set_launch_state(self, run_id, state, error=None):
        raise AppError("REPOSITORY_ERROR", f"{SQL_SENTINEL} {SECRET_SENTINEL}", 502)


def test_an_interrupted_preparation_is_neither_launchable_nor_capturable(monkeypatch,
                                                                         transport, capsys):
    repository = PreparationInterrupted()
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: transport)
    conversation_id, user_id = seed_conversation(repository)

    status = entrypoint.main(prepare_argv(conversation_id, user_id), env=capture_env())
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert status == entrypoint.EXIT_FAILED
    assert document["reason_code"] == "CAPTURE_PREPARATION_FAILED"
    rendered = json.dumps(document) + captured.err
    assert SQL_SENTINEL not in rendered and SECRET_SENTINEL not in rendered

    run_id = UUID(next(iter(repository.runs)))
    run = repository.runs[str(run_id)]
    # Stuck where the CAS left it: a launcher cannot acquire it...
    assert run["launch_state"] == "launching"
    assert repository.try_acquire_launch(run_id) is None
    # ...and the capture refuses it, because the operator never took ownership.
    status, refusal, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert refusal["reason_code"] == "CAPTURE_RUN_NOT_ELIGIBLE"
    assert transport.calls == []
    assert repository.catalog_snapshots == {}


def test_a_refused_preparation_creates_no_run_at_all(monkeypatch, repository, capsys):
    """A membership failure happens before anything is created."""
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    conversation_id, _user_id = seed_conversation(repository)
    outsider = uuid4()

    status = entrypoint.main(prepare_argv(conversation_id, outsider), env=capture_env())
    document = json.loads(capsys.readouterr().out)

    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_CONVERSATION_UNAVAILABLE"
    assert repository.runs == {}
    assert repository.messages == []


# --- 9. replay of a preparation is defined and idempotent --------------------

def test_replaying_a_preparation_returns_the_same_run_and_creates_no_second_one(
        monkeypatch, repository, capsys):
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    conversation_id, user_id = seed_conversation(repository)
    argv = prepare_argv(conversation_id, user_id,
                        **{"--idempotency-key": "prepare-once"})

    first_status = entrypoint.main(argv, env=capture_env())
    first = json.loads(capsys.readouterr().out)
    second_status = entrypoint.main(argv, env=capture_env())
    second = json.loads(capsys.readouterr().out)

    assert (first_status, second_status) == (entrypoint.EXIT_OK, entrypoint.EXIT_OK)
    assert first["preparation"]["run_id"] == second["preparation"]["run_id"]
    assert first["preparation"]["already_prepared"] is False
    assert second["preparation"]["already_prepared"] is True
    assert len(repository.runs) == 1
    run = next(iter(repository.runs.values()))
    assert run["launch_state"] == entrypoint.OPERATOR_OWNED_LAUNCH_STATE
    assert run["status"] == entrypoint.ELIGIBLE_RUN_STATUS


def test_preparation_without_an_idempotency_key_makes_a_distinct_run(monkeypatch, repository,
                                                                     capsys):
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    first = prepare_run(repository, capsys)
    second = prepare_run(repository, capsys)
    assert first != second
    assert len(repository.runs) == 2


# --- the preparation gate refuses before anything is constructed -------------

@pytest.mark.parametrize("override, expected", [
    ({"--acknowledge-schema-report-reviewed": None},
     "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"),
    ({"--acknowledge-schema-report-reviewed": "yes"},
     "CAPTURE_SCHEMA_REPORT_NOT_ACKNOWLEDGED"),
    ({"--project-ref": "another-project"}, "CAPTURE_PROJECT_MISMATCH"),
    ({"--conversation-id": None}, "CAPTURE_CONVERSATION_IDENTITY_INVALID"),
    ({"--conversation-id": "not-a-uuid"}, "CAPTURE_CONVERSATION_IDENTITY_INVALID"),
    ({"--requested-by": None}, "CAPTURE_CONVERSATION_IDENTITY_INVALID"),
    ({"--requested-by": "not-a-uuid"}, "CAPTURE_CONVERSATION_IDENTITY_INVALID"),
])
def test_preparation_refuses_before_constructing_anything(tripwire, capsys, override,
                                                          expected):
    argv = prepare_argv(uuid4(), uuid4(), **override)
    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == expected


@pytest.mark.parametrize("value", [None, "", "false", "enabled", "1;true"])
def test_preparation_honours_the_catalog_flag(tripwire, capsys, value):
    env = capture_env()
    if value is None:
        env.pop("MILO_ENABLE_CATALOG_EXECUTION")
    else:
        env["MILO_ENABLE_CATALOG_EXECUTION"] = value
    status, document, _ = run_main(prepare_argv(uuid4(), uuid4()), env, capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_CATALOG_EXECUTION_DISABLED"


@pytest.mark.parametrize("value", sorted(TRUE_VALUES))
def test_preparation_refuses_while_paid_execution_is_enabled(tripwire, capsys, value):
    env = capture_env(MILO_ENABLE_PAID_EXECUTION=value)
    status, document, _ = run_main(prepare_argv(uuid4(), uuid4()), env, capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_PAID_EXECUTION_ENABLED"


@pytest.mark.parametrize("extra", [
    ["--execute"],
    ["--plan"],
])
def test_preparation_and_another_mode_together_refuse(tripwire, capsys, extra):
    argv = prepare_argv(uuid4(), uuid4()) + extra
    status, document, _ = run_main(argv, capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_MODE_CONTRADICTORY"


def test_preparation_never_starts_a_capture(monkeypatch, repository, capsys):
    """Preparing is not capturing: the two are separate invocations."""
    monkeypatch.setattr(entrypoint, "_open_repository", lambda: repository)
    monkeypatch.setattr(entrypoint, "_open_transport", lambda: (_ for _ in ()).throw(
        AssertionError("preparation started a capture")))
    run_id, document = prepared(repository, capsys)
    assert document["preparation"]["captured"] is False
    assert repository.catalog_snapshots == {}
    assert repository.catalog_raw_records == {}
    assert repository.catalog_candidates == {}
    assert repository.runs[str(run_id)].get("lease_token") is None
