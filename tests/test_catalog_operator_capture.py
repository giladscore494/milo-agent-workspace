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


def prepare_run(repository: MemoryRepository, *, metadata: Mapping[str, Any] | None = None
                ) -> UUID:
    """One run prepared exactly as an operator prepares a capture run.

    `create_queued_run` is the repository method both production creation paths
    reach, and it inserts `launch_state='pending'`. Nothing launches it here,
    which is the state the entrypoint requires and which the API route leaves
    behind only for a run it never handed to a launcher.
    """
    project_id, user_id = str(uuid4()), str(uuid4())
    repository.seed_user(user_id)
    repository.seed_project(project_id, "catalog-ops", "Catalog ops", [user_id],
                            workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project_id), "operator capture",
                                                  UUID(user_id))
    message = repository.create_user_message(UUID(conversation["id"]), "operator capture", {})
    payload = {"milo_operation": entrypoint.OPERATOR_CAPTURE_OPERATION}
    run = repository.create_queued_run(
        UUID(conversation["id"]), message["id"], "operator capture",
        dict(payload if metadata is None else metadata))
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
    (lambda run: run.update({"status": "running"}), "status"),
])
def test_a_run_a_launcher_touched_refuses_without_claiming(wired, capsys, mutate, field):
    """The blocker this gate exists for: a run that was handed to a launcher is
    an ordinary model run, and claiming it would race its worker."""
    repository, transport = wired
    run_id = prepare_run(repository)
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
])
def test_a_run_without_the_operator_marker_refuses(wired, capsys, metadata):
    repository, transport = wired
    run_id = prepare_run(repository, metadata=metadata)
    status, document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_REFUSED
    assert document["reason_code"] == "CAPTURE_RUN_NOT_ELIGIBLE"
    assert transport.calls == []
    assert repository.runs[str(run_id)].get("lease_token") is None


# =============================================================================
# 2. trusted construction
# =============================================================================

def test_the_execution_branch_constructs_the_approved_transport_and_client(wired, capsys):
    repository, transport = wired
    run_id = prepare_run(repository)
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
    run_id = prepare_run(repository)
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
    first = prepare_run(repository)
    status, document, _ = run_main(authorized_argv(first), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    assert document["capture"]["outcome"] == "changed"
    snapshots_after_first = dict(repository.catalog_snapshots)
    records_after_first = dict(repository.catalog_raw_records)

    second = prepare_run(repository)
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
    run_id = prepare_run(repository)

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
    run_id = prepare_run(repository)

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
    run_id = prepare_run(repository)

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
    run_id = prepare_run(repository)

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
    run_id = prepare_run(repository)

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
    first = prepare_run(repository)
    status, document, _ = run_main(authorized_argv(first), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK, document
    good_key = document["capture"]["snapshot"]["snapshot_key"]

    # A later refresh whose metadata read fails outright.
    monkeypatch.setattr(entrypoint, "_open_transport",
                        lambda: FixtureTransport(transport_failures=9))
    second = prepare_run(repository)
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
    run_id = prepare_run(repository)
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
    run_id = prepare_run(repository)
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
    run_id = prepare_run(repository)

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
    run_id = prepare_run(repository)
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
    run_id = prepare_run(repository)
    status, _document, _ = run_main(authorized_argv(run_id), capture_env(), capsys)
    assert status == entrypoint.EXIT_OK
    assert list(tmp_path.iterdir()) == []


def test_an_unwritable_report_path_is_a_non_zero_outcome(wired, capsys, tmp_path):
    repository, _transport = wired
    run_id = prepare_run(repository)
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
