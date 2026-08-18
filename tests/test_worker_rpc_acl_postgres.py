"""Worker-path RPC ACL contract against ephemeral PostgreSQL.

Why this module exists (Stage C Attempts 4 and 5): the production Supabase
project does NOT extend default function EXECUTE privileges to
service_role, so any service-path function without an EXPLICIT
`grant execute ... to service_role` in a migration is unusable by the
backend runtimes there. The main migration suite deliberately replicates
Supabase's default-privilege behavior and therefore cannot see this class
of gap: with that default in place every new function looks
service_role-executable even when no migration grants it. Attempt 4 failed
on `create_message_and_run` / `create_project_from_proposal_with_owner`
(fixed by 20260818000100); Attempt 5 failed on `claim_run_lease`
(SQLSTATE 42501, fixed by 20260818000200).

This module closes the class of bug, not just the instances:

1. The audited RPC inventory is DERIVED from backend/repository/supabase.py
   (every `rpc("...")` / `_guarded_rpc("...")` call site), so a future
   repository RPC cannot silently skip ACL coverage.
2. The audit follows SECURITY INVOKER dependency chains through the live
   function bodies (pg_get_functiondef), so a wrapper's grant can never
   mask a missing grant on the base function it calls.
3. Roles here get NO default-privilege help: every EXECUTE must come from
   an explicit migration grant, exactly like production.
4. The complete worker lifecycle is actually executed under
   `set role service_role` — claim, heartbeat, events, checkpoint,
   blackboard, agent message, supervisor decision, budget
   reservation/settlement, usage, transitions to running and completed.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.test_migrations_postgres import (
    BASELINE,
    MIGRATIONS,
    EphemeralPostgres,
    _require_pg_bin,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_SOURCE = REPO_ROOT / "backend" / "repository" / "supabase.py"
CORRECTIVE_MIGRATION = (
    REPO_ROOT / "supabase" / "migrations" / "20260818000200_claim_run_lease_service_role_acl.sql"
)
ACL_PG_PORT = "54995"

# Repository RPCs that are deliberately NOT service_role-callable, with the
# documented reason. Anything else the repository can call MUST satisfy the
# service-path ACL matrix.
EXCLUDED_RPCS = {
    # Migration 014 deprecates the legacy daily reservation RPCs and revokes
    # service_role EXECUTE; test_excluded_rpcs_* below proves no backend
    # code path outside the repository definition can reach them.
    "reserve_daily_user_budget",
    "reserve_daily_project_budget",
}

PRODUCTION_FIDELITY_SHIM = """
create schema if not exists auth;
create table if not exists auth.users (id uuid primary key);
create or replace function auth.uid() returns uuid language sql stable as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end $$;
grant usage on schema public to anon, authenticated, service_role;
-- Production fidelity (Attempts 4 and 5): NO default-privilege EXECUTE
-- grants for anon/authenticated/service_role. Every EXECUTE a role holds
-- in this cluster comes from an explicit migration statement.
"""


def repository_rpc_inventory() -> set[str]:
    """Every RPC name the repository can invoke, derived from source."""
    source = REPOSITORY_SOURCE.read_text()
    return set(re.findall(r'(?:_guarded_)?rpc\(\s*"([a-z0-9_]+)"', source))


@pytest.fixture(scope="module")
def acl_db():
    server = EphemeralPostgres(_require_pg_bin(), port=ACL_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(sql=PRODUCTION_FIDELITY_SHIM)
        server.psql(file=BASELINE)
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


def _function_catalog(db) -> dict[str, list[dict]]:
    rows = db.psql(
        "select p.oid, p.proname, p.prosecdef::int, "
        "regexp_replace(pg_get_functiondef(p.oid), E'\\n', ' ', 'g') "
        "from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public'"
    )
    catalog: dict[str, list[dict]] = {}
    for line in rows.splitlines():
        oid, name, secdef, body = line.split("|", 3)
        catalog.setdefault(name, []).append(
            {"oid": oid, "secdef": secdef == "1", "body": body}
        )
    return catalog


def _extension_function_oids(db) -> set[str]:
    return set(
        db.psql(
            "select objid from pg_depend "
            "where refclassid = 'pg_extension'::regclass "
            "and classid = 'pg_proc'::regclass"
        ).splitlines()
    )


def _worker_rpc_closure(db) -> dict[str, list[dict]]:
    """Audited inventory plus every function reachable through SECURITY
    INVOKER bodies (a DEFINER function executes its callees as its owner,
    so its internal calls do not need caller grants)."""
    catalog = _function_catalog(db)
    audited = sorted(repository_rpc_inventory() - EXCLUDED_RPCS)
    missing = [name for name in audited if name not in catalog]
    assert not missing, f"repository invokes RPCs absent from the schema: {missing}"

    closure: dict[str, list[dict]] = {}
    queue = list(audited)
    while queue:
        name = queue.pop()
        if name in closure:
            continue
        closure[name] = catalog[name]
        for overload in catalog[name]:
            if overload["secdef"]:
                continue
            for callee in catalog:
                if callee != name and re.search(rf"\b{callee}\s*\(", overload["body"]):
                    queue.append(callee)
    return closure


def _acl_row(db, oid: str) -> tuple[str, str, str, str]:
    public = db.psql(
        "select coalesce((select bool_or(a.grantee = 0 and a.privilege_type = 'EXECUTE') "
        f"from pg_proc p, aclexplode(p.proacl) a where p.oid = {oid}), "
        f"(select proacl is null from pg_proc where oid = {oid}))"
    )
    anon = db.psql(f"select has_function_privilege('anon', {oid}, 'EXECUTE')")
    authenticated = db.psql(f"select has_function_privilege('authenticated', {oid}, 'EXECUTE')")
    service_role = db.psql(f"select has_function_privilege('service_role', {oid}, 'EXECUTE')")
    return public, anon, authenticated, service_role


def test_inventory_is_derived_and_covers_known_worker_rpcs():
    inventory = repository_rpc_inventory()
    # Regex-rot guard: the derivation must keep seeing the known call sites.
    for known in (
        "claim_run_lease",
        "append_run_event_guarded",
        "heartbeat_run_guarded",
        "transition_run_worker_guarded",
        "reserve_model_call_budget_guarded",
        "settle_model_call_budget_guarded",
        "save_checkpoint_guarded",
        "upsert_run_blackboard_guarded",
        "create_agent_message_guarded",
        "create_supervisor_decision_guarded",
        "update_run_usage_guarded",
        "create_message_and_run_v2",
    ):
        assert known in inventory, f"inventory derivation lost {known}"
    assert EXCLUDED_RPCS <= inventory, "exclusions must reference real call sites"


def test_excluded_rpcs_have_no_backend_callers_and_stay_revoked(acl_db):
    # The deprecated daily RPCs may appear only inside the repository module
    # that defines their (unused) wrappers; any other backend reference
    # would put a service_role-revoked function back on a live code path.
    for name in EXCLUDED_RPCS:
        for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
            if path == REPOSITORY_SOURCE:
                continue
            assert name not in path.read_text(), (
                f"{path} references deprecated RPC {name}, which migration 014 "
                "revoked from service_role"
            )
    # And the migration-014 contract still holds live:
    for name in EXCLUDED_RPCS:
        for overload in _function_catalog(acl_db).get(name, []):
            service_role = acl_db.psql(
                f"select has_function_privilege('service_role', {overload['oid']}, 'EXECUTE')"
            )
            assert service_role == "f", f"{name} must remain revoked from service_role"


def test_worker_rpc_closure_acl_matrix(acl_db):
    closure = _worker_rpc_closure(acl_db)
    assert len(closure) >= 20, f"suspiciously small closure: {sorted(closure)}"
    extension_oids = _extension_function_oids(acl_db)
    violations = []
    for name in sorted(closure):
        for overload in closure[name]:
            signature = acl_db.psql(f"select {overload['oid']}::regprocedure")
            public, anon, authenticated, service_role = _acl_row(acl_db, overload["oid"])
            if overload["oid"] in extension_oids:
                # Extension helpers (pgcrypto) keep their standard PUBLIC
                # EXECUTE; the worker path only needs service_role to be
                # able to call them.
                if service_role != "t":
                    violations.append(f"{signature}: service_role cannot execute extension helper")
            elif (public, anon, authenticated, service_role) != ("f", "f", "f", "t"):
                violations.append(
                    f"{signature}: public={public} anon={anon} "
                    f"authenticated={authenticated} service_role={service_role} "
                    "(expected f/f/f/t)"
                )
    assert not violations, "worker-path ACL contract violated:\n" + "\n".join(violations)


def test_corrective_migration_is_rerun_safe(acl_db):
    before = acl_db.psql("select proacl from pg_proc where oid = 'public.claim_run_lease(uuid, text, integer)'::regprocedure")
    acl_db.psql(file=CORRECTIVE_MIGRATION)
    acl_db.psql(file=CORRECTIVE_MIGRATION)
    after = acl_db.psql("select proacl from pg_proc where oid = 'public.claim_run_lease(uuid, text, integer)'::regprocedure")
    assert before == after, "re-applying 20260818000200 must not change the ACL"


def _as_role(db, role: str, sql: str) -> str:
    return db.psql(f"set role {role}; {sql}")


def test_browser_roles_cannot_claim_lease(acl_db):
    for role in ("anon", "authenticated"):
        result = subprocess.run(
            ["psql", "-h", acl_db.dir, "-p", acl_db.port, "-U", "postgres", "-d", "milo",
             "-v", "ON_ERROR_STOP=1", "-X", "-q", "-t", "-A", "-c",
             f"set role {role}; select public.claim_run_lease("
             "'00000000-0000-0000-0000-000000000001'::uuid, 'w', 60)"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0 and "permission denied" in result.stderr, (
            f"{role} must not be able to execute claim_run_lease: {result.stderr}"
        )


def test_worker_lifecycle_executes_under_service_role(acl_db):
    """Execute the real worker durable-write sequence as service_role.

    This is the executable regression for Attempts 4 and 5: run creation
    through the service path, lease claim, and every guarded worker write
    the engine performs, all under `set role service_role` with no
    default-privilege help.
    """
    acl_db.psql(
        "insert into auth.users (id) values ('aaaaaaaa-0000-0000-0000-000000000001') "
        "on conflict do nothing"
    )
    acl_db.psql(
        "insert into public.projects (id, slug, name, workflow_key) values "
        "('cccccccc-0000-0000-0000-000000000001', 'acl-lifecycle', "
        "'ACL lifecycle project', 'vehicle_catalog_v1') on conflict do nothing"
    )
    acl_db.psql(
        "insert into public.conversations (id, project_id, title) values "
        "('bbbbbbbb-0000-0000-0000-000000000001', 'cccccccc-0000-0000-0000-000000000001', "
        "'acl lifecycle conversation') on conflict do nothing"
    )

    run_id = _as_role(
        acl_db, "service_role",
        "select v->'run'->>'id' from public.create_message_and_run_v2("
        "p_conversation_id := 'bbbbbbbb-0000-0000-0000-000000000001'::uuid, "
        "p_content := 'acl lifecycle prompt', p_metadata := '{}'::jsonb, "
        "p_requested_by := 'aaaaaaaa-0000-0000-0000-000000000001'::uuid, "
        "p_idempotency_key := 'acl-lifecycle-0001', "
        "p_request_fingerprint := 'acl-fingerprint-0001') v",
    )
    assert run_id, "service_role must be able to create the run via the service path"

    claim = _as_role(
        acl_db, "service_role",
        f"select worker_id, attempt, lease_token from public.claim_run_lease("
        f"'{run_id}'::uuid, 'acl-worker-1', 300)",
    )
    assert claim, "service_role must be able to claim the run lease (Attempt 5 regression)"
    worker_id, attempt, lease_token = claim.split("|")
    lease = f"'{run_id}'::uuid, '{worker_id}', {attempt}, '{lease_token}'"

    assert _as_role(
        acl_db, "service_role",
        f"select status from public.heartbeat_run_guarded({lease}, 300)",
    )
    assert _as_role(
        acl_db, "service_role",
        f"select event_type from public.append_run_event_guarded({lease}, "
        "'run_started', 'Run started', null, null, null, '{}'::jsonb)",
    ) == "run_started"
    assert _as_role(
        acl_db, "service_role",
        f"select status from public.transition_run_worker_guarded('{run_id}'::uuid, "
        f"'running', 'starting', '{worker_id}', {attempt}, '{lease_token}', "
        "null, null, false, null, now(), null)",
    ) == "running"
    assert _as_role(
        acl_db, "service_role",
        f"select run_id from public.upsert_run_blackboard_guarded({lease}, "
        "'{\"goal\": \"acl lifecycle\"}'::jsonb)",
    )
    assert _as_role(
        acl_db, "service_role",
        f"select run_id from public.create_agent_message_guarded({lease}, "
        "'{\"run_id\": \"" + run_id + "\", \"message_type\": \"progress\", "
        "\"sender\": \"builder\", \"recipient\": \"supervisor\", \"payload\": {}}'::jsonb)",
    )
    assert _as_role(
        acl_db, "service_role",
        f"select run_id from public.create_supervisor_decision_guarded({lease}, "
        "'{\"assessment\": \"acl lifecycle\", \"mode\": \"shadow\"}'::jsonb)",
    )
    assert _as_role(
        acl_db, "service_role",
        f"select run_id from public.save_checkpoint_guarded("
        f"p_run_id := '{run_id}'::uuid, p_worker_id := '{worker_id}', "
        f"p_attempt := {attempt}, p_lease_token := '{lease_token}', "
        "p_engine_version := 'v1', p_workflow_key := 'vehicle_catalog_v1', "
        "p_phase := 'fetch', p_completed_tasks := '[]'::jsonb, "
        "p_artifacts := '{}'::jsonb, p_failures := '[]'::jsonb, "
        "p_token_usage := '{}'::jsonb, p_last_event := null)",
    )
    reservation = _as_role(
        acl_db, "service_role",
        f"select id, status from public.reserve_model_call_budget_guarded("
        f"p_run_id := '{run_id}'::uuid, p_call_seq := 1, "
        "p_user_id := 'aaaaaaaa-0000-0000-0000-000000000001'::uuid, "
        "p_project_id := null, p_estimated_cost := 0.02, "
        "p_daily_user_limit := 5.00, p_daily_project_limit := null, "
        f"p_worker_id := '{worker_id}', p_attempt := {attempt}, "
        f"p_lease_token := '{lease_token}')",
    )
    reservation_id, reservation_status = reservation.split("|")
    assert reservation_status == "reserved"
    assert _as_role(
        acl_db, "service_role",
        f"select status from public.settle_model_call_budget_guarded("
        f"p_reservation_id := '{reservation_id}'::uuid, p_actual_cost := 0.01, "
        f"p_run_id := '{run_id}'::uuid, p_worker_id := '{worker_id}', "
        f"p_attempt := {attempt}, p_lease_token := '{lease_token}', "
        "p_status := 'settled', p_rejection_reason := null)",
    ) == "settled"
    assert _as_role(
        acl_db, "service_role",
        f"select status from public.update_run_usage_guarded({lease}, "
        "'{\"total_tokens\": 10}'::jsonb)",
    )
    assert _as_role(
        acl_db, "service_role",
        f"select status from public.transition_run_worker_guarded('{run_id}'::uuid, "
        f"'completed', 'running', '{worker_id}', {attempt}, '{lease_token}', "
        "'{\"status\": \"success\"}'::jsonb, null, true, null, null, now())",
    ) == "completed"
