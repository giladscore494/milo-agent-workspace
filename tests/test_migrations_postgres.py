"""Executable migration compatibility test against ephemeral PostgreSQL.

This module has two distinct kinds of tests, and they must not be confused:

1. Confirmed production-baseline tests (`pre_migration_db`, `db` fixtures):
   apply the exact confirmed legacy production baseline
   (tests/fixtures/legacy_baseline.sql, including its confirmed constraint
   names, foreign-key delete behavior, and indexes) with only seed data that
   the confirmed production `runs_status_check` actually permits. These
   prove real production data survives migrations 001-006 unmodified, and
   that the fixture matches the confirmed schema property-for-property.

2. Synthetic defensive edge-case tests (`synthetic_invalid_status_db`
   fixture): start from the same confirmed baseline but then deliberately
   drop the confirmed `runs_status_check` and insert a status value that
   could never exist under that confirmed constraint, purely to exercise
   migration 002's defensive NOT VALID handling for hypothetical historical
   anomalies. This is explicitly labeled synthetic in every fixture,
   docstring, and test name below and must never be read as describing
   real production state.

The whole module is skipped (not silently passed) when no PostgreSQL server
binaries are available, so a skip can never be mistaken for executable
validation.
"""

import hashlib
import json
import os
import uuid
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.catalog import keys as catalog_keys
from backend.catalog.digest import canonical_payload_text, catalog_payload_digest
from backend.testing import evidence_fixtures
from backend.engines.swarm_v2.conflict_policy import CONFLICT_POLICY_VERSION
from backend.engines.swarm_v2.evidence_contracts import (document_span_locator,
                                                         record_field_locator)
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.normalization import (
    SCOPE_NORMALIZATION_VERSION, canonical_scope_hash, canonical_scope_key,
    normalize_field_key,
)
from backend.catalog.contracts import (candidate_identity_scope, claim_entity_key,
                                       record_locator_id)
from backend.catalog.pipeline import PROMOTABLE_TOOL_OPERATION

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((REPO_ROOT / "supabase" / "migrations").glob("*.sql"))
BASELINE = REPO_ROOT / "tests" / "fixtures" / "legacy_baseline.sql"
PG_BIN_CANDIDATES = ["/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/15/bin", ""]
PG_PORT = "54991"
PRE_MIGRATION_PG_PORT = "54992"
SYNTHETIC_PG_PORT = "54993"


def _find_pg_bin() -> str | None:
    for candidate in PG_BIN_CANDIDATES:
        initdb = os.path.join(candidate, "initdb") if candidate else "initdb"
        if shutil.which(initdb):
            return candidate
    return None


class EphemeralPostgres:
    """A throwaway PostgreSQL cluster on a unix socket.

    When running as root (initdb refuses root), the cluster is owned by the
    `postgres` system user via `su`; otherwise it runs as the current user.
    The directory lives directly under /tmp because the postgres system user
    must be able to traverse every parent directory.
    """

    def __init__(self, pg_bin: str, port: str = PG_PORT):
        self.pg_bin = pg_bin
        self.port = port
        self.as_postgres_user = os.geteuid() == 0
        self.dir = tempfile.mkdtemp(prefix="milo-pgmig-", dir="/tmp")
        os.chmod(self.dir, 0o755)
        if self.as_postgres_user:
            shutil.chown(self.dir, "postgres", "postgres")

    def _server_cmd(self, command: str) -> list[str]:
        if self.as_postgres_user:
            return ["su", "postgres", "-s", "/bin/bash", "-c", command]
        return ["/bin/bash", "-c", command]

    def start(self) -> None:
        initdb = os.path.join(self.pg_bin, "initdb")
        pg_ctl = os.path.join(self.pg_bin, "pg_ctl")
        subprocess.run(
            self._server_cmd(f"{initdb} -D {self.dir}/data -U postgres --auth=trust"),
            check=True, capture_output=True,
        )
        subprocess.run(
            self._server_cmd(
                f"{pg_ctl} -D {self.dir}/data -l {self.dir}/log -w "
                f"-o '-k {self.dir} -p {self.port} -c listen_addresses=' start"
            ),
            check=True, capture_output=True,
        )

    def stop(self) -> None:
        pg_ctl = os.path.join(self.pg_bin, "pg_ctl")
        subprocess.run(self._server_cmd(f"{pg_ctl} -D {self.dir}/data -m immediate stop"), capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def create_database(self, name: str = "milo") -> None:
        subprocess.run(
            ["psql", "-h", self.dir, "-p", self.port, "-U", "postgres", "-d", "postgres",
             "-X", "-q", "-c", f"create database {name}"],
            check=True, capture_output=True,
        )

    def psql(self, sql: str | None = None, file: Path | None = None) -> str:
        cmd = ["psql", "-h", self.dir, "-p", self.port, "-U", "postgres", "-d", "milo",
               "-v", "ON_ERROR_STOP=1", "-X", "-q", "-t", "-A"]
        if file is not None:
            cmd += ["-f", str(file)]
        else:
            cmd += ["-c", sql]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"psql failed:\n{result.stderr}\n(sql: {file or sql})")
        return result.stdout.strip()


def _require_pg_bin() -> str:
    pg_bin = _find_pg_bin()
    if pg_bin is None or shutil.which("psql") is None:
        if os.getenv("MILO_REQUIRE_PG_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}:
            # The dedicated CI job MUST run these tests; a silent skip would
            # let unverified migrations look green.
            pytest.fail("MILO_REQUIRE_PG_TESTS is set but PostgreSQL server binaries are unavailable; the executable migration suite is mandatory here")
        pytest.skip("PostgreSQL server binaries not available; executable migration validation skipped")
    return pg_bin


@pytest.fixture(scope="module")
def pre_migration_db():
    """Confirmed production baseline, seeded, with NO migrations applied.

    Used only to assert what production looks like *before* 001-006 ever
    run: the confirmed runs_status_check/runs_progress_check constraints,
    the confirmed ON DELETE CASCADE foreign key, and that the confirmed
    constraint genuinely rejects statuses outside the confirmed enum.
    """
    server = EphemeralPostgres(_require_pg_bin(), port=PRE_MIGRATION_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="module")
def db():
    """Confirmed production baseline, seeded, with migrations 001-006
    applied. Used for all post-migration assertions, including that the
    confirmed baseline's own properties (FK cascade, progress check) survive
    migration, and that the confirmed legacy seed data needs no defensive
    NOT VALID exemption because it already satisfies the expanded status
    constraint migration 002 installs.
    """
    server = EphemeralPostgres(_require_pg_bin(), port=PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="module")
def synthetic_invalid_status_db():
    """SYNTHETIC DEFENSIVE FIXTURE -- NOT PART OF THE CONFIRMED PRODUCTION
    BASELINE.

    Confirmed production always enforces runs_status_check, so a row with
    an unconfirmed status value can never actually exist there. This fixture
    starts from the confirmed baseline but then deliberately drops that
    confirmed constraint and inserts a status value outside every confirmed
    or migrated enum, purely to exercise migration 002's defensive NOT VALID
    handling for a hypothetical historical anomaly. Nothing asserted against
    this fixture describes real production state.
    """
    server = EphemeralPostgres(_require_pg_bin(), port=SYNTHETIC_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=BASELINE)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        server.psql(
            "insert into public.conversations (id, title) values "
            "('99999999-9999-9999-9999-999999999999', 'synthetic conversation')"
        )
        # Synthetic-only: the confirmed constraint is dropped so a row with
        # an unconfirmed status can be inserted. Real production never
        # allows this state.
        server.psql("alter table public.runs drop constraint runs_status_check")
        server.psql(
            "insert into public.runs (id, conversation_id, user_prompt, status, progress) values "
            "('88888888-8888-8888-8888-888888888888', '99999999-9999-9999-9999-999999999999', "
            "'synthetic prompt', 'synthetic_unconfirmed_status', 0)"
        )
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


# Every status value here ('completed', 'failed') is permitted by the
# confirmed production runs_status_check -- this seed represents data that
# could genuinely exist in production today, not a synthetic edge case.
SUPABASE_AUTH_SHIM = """
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
-- Replicate Supabase's default function privileges: every function created
-- by postgres in schema public grants EXECUTE to anon, authenticated and
-- service_role. Without this, plain PostgreSQL would hide the exact ACL gap
-- migration 20260810000100 closes (anon EXECUTE on service-only RPCs), and
-- the revocation tests below would pass vacuously.
alter default privileges in schema public grant execute on functions to anon, authenticated, service_role;
"""

SEED_LEGACY_ROWS = """
insert into public.conversations (id, title) values
  ('11111111-1111-1111-1111-111111111111', 'legacy conversation');
insert into public.runs (id, conversation_id, user_prompt, status, current_phase, progress, result, error_message) values
  ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111',
   'legacy prompt', 'completed', 'summary', 100, '{"models": []}'::jsonb, null),
  ('33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111',
   'legacy failed prompt', 'failed', 'fetch', 40, null, 'legacy failure text');
insert into public.messages (conversation_id, run_id, sender_role, content) values
  ('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222', 'user', 'legacy user message'),
  ('11111111-1111-1111-1111-111111111111', null, 'assistant', 'legacy assistant message');
insert into public.run_events (run_id, event_type, agent_name, progress, message, payload) values
  ('22222222-2222-2222-2222-222222222222', 'legacy_event_type', 'legacy-agent', 55, 'legacy event', '{"k": "v"}'::jsonb);
"""


def test_messages_sender_role_renamed_to_role_with_data(db):
    assert db.psql("select count(*) from public.messages") == "2"
    assert db.psql(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='messages' and column_name in ('role','sender_role') "
        "order by column_name"
    ) == "role"
    assert db.psql(
        "select is_nullable from information_schema.columns "
        "where table_schema='public' and table_name='messages' and column_name='role'"
    ) == "NO"
    assert db.psql("select role from public.messages order by id") == "user\nassistant"


def test_messages_role_check_constraint_survives_rename(db):
    with pytest.raises(AssertionError, match="check constraint"):
        db.psql(
            "insert into public.messages (conversation_id, role, content) values "
            "('11111111-1111-1111-1111-111111111111', 'bogus', 'x')"
        )


def test_messages_backend_shape_insert_succeeds(db):
    new_id = db.psql(
        "insert into public.messages (conversation_id, role, content, metadata) values "
        "('11111111-1111-1111-1111-111111111111', 'user', 'new-shape message', '{}'::jsonb) returning id"
    )
    assert int(new_id) > 0
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='messages' and column_name='id'"
    ) == "bigint"


def test_runs_input_output_error_updated_at_backfilled(db):
    row = db.psql(
        "select input->>'content', output->>'models' is not null, updated_at is not null "
        "from public.runs where id = '22222222-2222-2222-2222-222222222222'"
    )
    assert row == "legacy prompt|t|t"
    err = db.psql(
        "select input->>'content', error->>'message' "
        "from public.runs where id = '33333333-3333-3333-3333-333333333333'"
    )
    assert err == "legacy failed prompt|legacy failure text"
    assert db.psql("select count(*) from public.runs where input is null or updated_at is null") == "0"


def test_runs_legacy_columns_preserved(db):
    columns = db.psql(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='runs' and column_name in "
        "('user_prompt','result','error_message','progress','current_phase','cancel_requested') "
        "order by column_name"
    ).splitlines()
    assert columns == ["cancel_requested", "current_phase", "error_message", "progress", "result", "user_prompt"]
    assert db.psql(
        "select user_prompt from public.runs where id = '22222222-2222-2222-2222-222222222222'"
    ) == "legacy prompt"


def test_runs_backend_shape_insert_without_user_prompt(db):
    run_id = db.psql(
        "insert into public.runs (conversation_id, status, input, idempotency_key) values "
        "('11111111-1111-1111-1111-111111111111', 'queued', "
        "'{\"message_id\": \"1\", \"content\": \"go\"}'::jsonb, 'idem-1') returning id"
    )
    assert len(run_id) == 36  # run ids remain UUID
    assert db.psql(f"select updated_at is not null from public.runs where id = '{run_id}'") == "t"


def test_runs_updated_at_trigger_fires_on_update(db):
    before = db.psql("select updated_at from public.runs where id = '22222222-2222-2222-2222-222222222222'")
    db.psql("update public.runs set status = 'completed' where id = '22222222-2222-2222-2222-222222222222'")
    after = db.psql("select updated_at from public.runs where id = '22222222-2222-2222-2222-222222222222'")
    assert after >= before


def test_runs_status_check_rejects_new_invalid_status_and_stays_fully_validated(db):
    """With the confirmed baseline's own legacy data (all statuses already
    inside the expanded set migration 002 installs), the replaced
    runs_status_check needs no NOT VALID exemption at all: it validates
    cleanly against real production-shaped data. Contrast with
    test_synthetic_migration_leaves_status_check_not_valid_for_unconfirmed_status
    below, where an unconfirmed status forces the NOT VALID fallback."""
    assert db.psql(
        "select status from public.runs where id = '33333333-3333-3333-3333-333333333333'"
    ) == "failed"
    assert db.psql(
        "select convalidated from pg_constraint where conname = 'runs_status_check'"
    ) == "t"
    with pytest.raises(AssertionError, match="runs_status_check"):
        db.psql(
            "insert into public.runs (conversation_id, status, input) values "
            "('11111111-1111-1111-1111-111111111111', 'made_up_status', '{}'::jsonb)"
        )


def test_pre_migration_runs_conversation_fk_is_cascade(pre_migration_db):
    row = pre_migration_db.psql(
        "select confdeltype from pg_constraint "
        "where conrelid = 'public.runs'::regclass and contype = 'f' "
        "and confrelid = 'public.conversations'::regclass"
    )
    assert row == "c"  # 'c' = ON DELETE CASCADE


def test_pre_migration_runs_progress_and_status_checks_exist(pre_migration_db):
    names = pre_migration_db.psql(
        "select conname from pg_constraint where conrelid = 'public.runs'::regclass and contype = 'c' order by conname"
    ).splitlines()
    assert "runs_progress_check" in names
    assert "runs_status_check" in names


def test_pre_migration_confirmed_status_check_rejects_unconfirmed_status(pre_migration_db):
    """Proves the confirmed production constraint is real and enforced,
    which is exactly why a row with an unconfirmed status cannot exist in
    production without first being dropped (see the synthetic fixture)."""
    with pytest.raises(AssertionError, match="runs_status_check"):
        pre_migration_db.psql(
            "insert into public.runs (conversation_id, status, user_prompt) values "
            "('11111111-1111-1111-1111-111111111111', 'legacy_error_state', 'x')"
        )


def test_pre_migration_confirmed_seed_rows_present(pre_migration_db):
    assert pre_migration_db.psql("select count(*) from public.runs") == "2"
    assert pre_migration_db.psql("select count(*) from public.messages") == "2"


def test_runs_conversation_fk_cascade_survives_migration(db):
    row = db.psql(
        "select confdeltype from pg_constraint "
        "where conrelid = 'public.runs'::regclass and contype = 'f' "
        "and confrelid = 'public.conversations'::regclass"
    )
    assert row == "c"


def test_runs_progress_check_survives_migration(db):
    assert db.psql(
        "select conname from pg_constraint where conrelid = 'public.runs'::regclass and conname = 'runs_progress_check'"
    ) == "runs_progress_check"
    with pytest.raises(AssertionError, match="runs_progress_check"):
        db.psql(
            "insert into public.runs (conversation_id, status, progress, input) values "
            "('11111111-1111-1111-1111-111111111111', 'queued', 250, '{}'::jsonb)"
        )


def test_confirmed_non_primary_indexes_all_present(db):
    expected = {
        "messages_conversation_id_created_at_idx",
        "run_events_run_id_created_at_idx",
        "runs_conversation_id_idx",
        "runs_status_idx",
    }
    found = set(db.psql(
        "select indexname from pg_indexes where schemaname = 'public' "
        "and indexname in ("
        "'messages_conversation_id_created_at_idx',"
        "'run_events_run_id_created_at_idx',"
        "'runs_conversation_id_idx',"
        "'runs_status_idx')"
    ).splitlines())
    assert found == expected


def test_synthetic_migration_leaves_status_check_not_valid_for_unconfirmed_status(synthetic_invalid_status_db):
    """SYNTHETIC DEFENSIVE TEST -- not part of the confirmed production
    baseline (see synthetic_invalid_status_db fixture docstring). Confirms
    migration 002 does not fail outright when a hypothetical historical row
    holds a status outside every confirmed or migrated enum, and that the
    row's data is preserved rather than discarded."""
    assert synthetic_invalid_status_db.psql(
        "select status from public.runs where id = '88888888-8888-8888-8888-888888888888'"
    ) == "synthetic_unconfirmed_status"
    assert synthetic_invalid_status_db.psql(
        "select convalidated from pg_constraint where conname = 'runs_status_check'"
    ) == "f"  # NOT VALID: the synthetic row does not satisfy the expanded constraint
    with pytest.raises(AssertionError, match="runs_status_check"):
        synthetic_invalid_status_db.psql(
            "insert into public.runs (conversation_id, status, input) values "
            "('99999999-9999-9999-9999-999999999999', 'still_not_a_real_status', '{}'::jsonb)"
        )


def test_run_events_id_remains_bigint(db):
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='run_events' and column_name='id'"
    ) == "bigint"


def test_run_events_legacy_event_type_preserved(db):
    assert db.psql(
        "select event_type from public.run_events where message = 'legacy event'"
    ) == "legacy_event_type"


def test_run_events_integer_progress_preserved_as_progress_percent(db):
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='run_events' and column_name='progress_percent'"
    ) == "integer"
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='run_events' and column_name='progress'"
    ) == "jsonb"
    assert db.psql(
        "select progress_percent from public.run_events where message = 'legacy event'"
    ) == "55"


def test_run_events_progress_percent_check_retained(db):
    with pytest.raises(AssertionError, match="check constraint"):
        db.psql(
            "insert into public.run_events (run_id, event_type, progress_percent) values "
            "('22222222-2222-2222-2222-222222222222', 'agent_progress', 250)"
        )


def test_run_events_backend_shape_insert_succeeds(db):
    new_id = db.psql(
        "insert into public.run_events (run_id, event_type, message, agent, phase, progress, payload) values "
        "('22222222-2222-2222-2222-222222222222', 'agent_progress', 'm', 'builder', 'fetch', "
        "'{\"done\": 3, \"total\": 9}'::jsonb, '{}'::jsonb) returning id"
    )
    assert new_id.isdigit() and int(new_id) > 0  # run_events.id is bigint, not UUID
    assert db.psql(
        "select progress->>'done' from public.run_events where event_type = 'agent_progress'"
    ) == "3"
    assert db.psql(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='run_events' and column_name='agent_name'"
    ) == "agent_name"


def test_run_event_api_response_model_validates_bigint_id(db):
    """The API response model (backend.schemas.RunEvent) must accept the
    real bigint id PostgreSQL returns, proving the backend contract matches
    the executable schema above rather than only the SQL text."""
    from backend.schemas import RunEvent

    new_id = db.psql(
        "insert into public.run_events (run_id, event_type, message, payload) values "
        "('22222222-2222-2222-2222-222222222222', 'agent_completed', 'm2', '{}'::jsonb) returning id"
    )
    row = db.psql(
        f"select id, run_id, event_type from public.run_events where id = {new_id}"
    )
    raw_id, run_id, event_type = row.split("|")
    event = RunEvent(id=int(raw_id), run_id=run_id, event_type=event_type)
    assert isinstance(event.id, int)
    assert str(event.run_id) == run_id


def test_stuck_runs_view_exists_and_selects(db):
    db.psql("select * from public.stuck_runs")


def test_fixture_still_declares_run_events_id_bigint_and_event_type():
    """Regression guard, independent of PostgreSQL availability: fails if the
    legacy-baseline fixture is ever edited to declare run_events.id as uuid
    again, or to drop the pre-existing event_type NOT NULL column — either
    change would silently make the fixture stop matching production."""
    text = BASELINE.read_text().lower()
    run_events_block = text.split("create table public.run_events")[1].split(";")[0]
    assert "id bigint not null generated by default as identity primary key" in run_events_block, (
        "run_events.id must remain bigint identity to match production"
    )
    assert "uuid" not in run_events_block.split("run_id")[0], (
        "run_events.id must not be declared as uuid"
    )
    assert "event_type text not null" in run_events_block, (
        "run_events.event_type text not null must be present to match production"
    )


def test_migrations_are_rerun_safe(db):
    for migration in MIGRATIONS:
        db.psql(file=migration)
    assert db.psql("select count(*) from public.messages where content like 'legacy%'") == "2"
    assert db.psql(
        "select user_prompt from public.runs where id = '22222222-2222-2222-2222-222222222222'"
    ) == "legacy prompt"
    assert db.psql("select progress_percent from public.run_events where message = 'legacy event'") == "55"
    assert db.psql("select event_type from public.run_events where message = 'legacy event'") == "legacy_event_type"
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='run_events' and column_name='id'"
    ) == "bigint"
    assert db.psql(
        "select confdeltype from pg_constraint "
        "where conrelid = 'public.runs'::regclass and contype = 'f' "
        "and confrelid = 'public.conversations'::regclass"
    ) == "c"
    assert db.psql(
        "select count(*) from pg_indexes where schemaname = 'public' and indexname in ("
        "'messages_conversation_id_created_at_idx','run_events_run_id_created_at_idx',"
        "'runs_conversation_id_idx','runs_status_idx')"
    ) == "4"


# --- migration 007 (project_members + RLS) executable validation ---

MEMBER_USER = "aaaaaaaa-0000-4000-8000-000000000001"
OUTSIDER_USER = "aaaaaaaa-0000-4000-8000-000000000002"
MEMBER_PROJECT = "bbbbbbbb-0000-4000-8000-000000000001"
ORPHAN_PROJECT = "bbbbbbbb-0000-4000-8000-000000000002"


def _as_authenticated(db, user_id: str | None, sql: str) -> str:
    claim = user_id or ""
    return db.psql(
        f"select set_config('request.jwt.claim.sub', '{claim}', false); "
        "set role authenticated; "
        f"{sql}"
    ).splitlines()[-1]


def _seed_membership_fixture(db) -> None:
    db.psql(
        f"insert into auth.users (id) values ('{MEMBER_USER}'), ('{OUTSIDER_USER}') on conflict do nothing; "
        f"insert into public.projects (id, slug, name, workflow_key) values "
        f"('{MEMBER_PROJECT}', 'membership-scope', 'Membership Scope', 'vehicle_catalog_v1'), "
        f"('{ORPHAN_PROJECT}', 'membership-orphan', 'Membership Orphan', 'vehicle_catalog_v1') "
        "on conflict (id) do nothing; "
        f"insert into public.project_members (project_id, user_id, role) values "
        f"('{MEMBER_PROJECT}', '{MEMBER_USER}', 'owner') on conflict do nothing"
    )


def test_project_members_table_rls_and_policies_exist(db):
    assert db.psql(
        "select count(*) from information_schema.tables "
        "where table_schema='public' and table_name='project_members'"
    ) == "1"
    rls_enabled = db.psql(
        "select relname from pg_class where relnamespace='public'::regnamespace "
        "and relname in ('projects','conversations','messages','runs','run_events','project_members') "
        "and relrowsecurity order by relname"
    ).splitlines()
    assert rls_enabled == ["conversations", "messages", "project_members", "projects", "run_events", "runs"]
    assert int(db.psql(
        "select count(*) from pg_policies where schemaname='public' and tablename in "
        "('projects','conversations','messages','runs','run_events','project_members')"
    )) >= 7


def test_project_members_rejects_unknown_role(db):
    _seed_membership_fixture(db)
    with pytest.raises(AssertionError, match="project_members_role_check"):
        db.psql(
            f"insert into public.project_members (project_id, user_id, role) "
            f"values ('{MEMBER_PROJECT}', '{OUTSIDER_USER}', 'superadmin')"
        )


def test_membership_scopes_authenticated_project_reads(db):
    _seed_membership_fixture(db)
    member_rows = _as_authenticated(
        db, MEMBER_USER, "select count(*) from public.projects"
    )
    assert member_rows == "1"
    assert _as_authenticated(
        db, MEMBER_USER,
        f"select count(*) from public.projects where id = '{ORPHAN_PROJECT}'"
    ) == "0"
    assert _as_authenticated(db, OUTSIDER_USER, "select count(*) from public.projects") == "0"
    assert _as_authenticated(db, None, "select count(*) from public.projects") == "0"


def test_projects_without_members_stay_invisible_but_intact(db):
    _seed_membership_fixture(db)
    # The seeded legacy/baseline projects have no members: invisible to the
    # authenticated role, still present for the trusted service path.
    assert int(db.psql("select count(*) from public.projects")) >= 2
    assert _as_authenticated(
        db, MEMBER_USER,
        f"select count(*) from public.projects where id = '{MEMBER_PROJECT}'"
    ) == "1"


def test_authenticated_role_has_no_mutation_grants_on_projects(db):
    grants = db.psql(
        "select privilege_type from information_schema.role_table_grants "
        "where grantee='authenticated' and table_schema='public' and table_name='projects' "
        "order by privilege_type"
    ).splitlines()
    assert grants == ["SELECT"]


# --- migration 008 (workflow proposal ownership) executable validation ---

PROPOSAL_MEMBER_USER = "aaaaaaaa-0000-4000-8000-000000000011"
PROPOSAL_OUTSIDER_USER = "aaaaaaaa-0000-4000-8000-000000000012"
PROPOSAL_PROJECT = "bbbbbbbb-0000-4000-8000-000000000011"
LEGACY_PROPOSAL = "cccccccc-0000-4000-8000-000000000001"
OWNED_PROPOSAL = "cccccccc-0000-4000-8000-000000000002"
OWNERSHIP_PG_PORT = "54994"


@pytest.fixture(scope="module")
def ownership_db():
    """Confirmed baseline + migrations, with a legacy proposal inserted
    BEFORE migration 008 runs, then 008 applied twice (idempotency), then
    an ownership fixture seeded through the trusted service path."""
    server = EphemeralPostgres(_require_pg_bin(), port=OWNERSHIP_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        migration_008 = next(m for m in MIGRATIONS if m.name.startswith("008"))
        for migration in MIGRATIONS:
            if migration.name.startswith("008"):
                # Seed a proposal exactly as production holds it today,
                # before ownership columns exist.
                server.psql(
                    f"insert into public.workflow_proposals (id, status, user_request) "
                    f"values ('{LEGACY_PROPOSAL}', 'approved', 'legacy proposal request')"
                )
            server.psql(file=migration)
        # Repeated application must be a no-op, not an error.
        server.psql(file=migration_008)
        server.psql(
            f"insert into auth.users (id) values ('{PROPOSAL_MEMBER_USER}'), ('{PROPOSAL_OUTSIDER_USER}') on conflict do nothing; "
            f"insert into public.projects (id, slug, name, workflow_key) values "
            f"('{PROPOSAL_PROJECT}', 'proposal-scope', 'Proposal Scope', 'vehicle_catalog_v1') on conflict (id) do nothing; "
            f"insert into public.project_members (project_id, user_id, role) values "
            f"('{PROPOSAL_PROJECT}', '{PROPOSAL_MEMBER_USER}', 'owner') on conflict do nothing; "
            f"insert into public.workflow_proposals (id, status, user_request, created_by, project_id) "
            f"values ('{OWNED_PROPOSAL}', 'approved', 'owned proposal request', "
            f"'{PROPOSAL_MEMBER_USER}', '{PROPOSAL_PROJECT}')"
        )
        yield server
    finally:
        server.stop()


def test_008_adds_ownership_columns_with_expected_types(ownership_db):
    rows = ownership_db.psql(
        "select column_name, data_type, is_nullable from information_schema.columns "
        "where table_schema='public' and table_name='workflow_proposals' "
        "and column_name in ('created_by','project_id') order by column_name"
    ).splitlines()
    assert rows == ["created_by|uuid|YES", "project_id|uuid|YES"]


def test_008_adds_foreign_keys_and_indexes(ownership_db):
    fks = ownership_db.psql(
        "select confrelid::regclass::text from pg_constraint "
        "where conrelid='public.workflow_proposals'::regclass and contype='f' "
        "order by 1"
    ).splitlines()
    assert fks == ["auth.users", "projects"]
    indexes = ownership_db.psql(
        "select indexname from pg_indexes where schemaname='public' and tablename='workflow_proposals' "
        "and indexname in ('workflow_proposals_created_by_idx','workflow_proposals_project_id_idx') order by 1"
    ).splitlines()
    assert indexes == ["workflow_proposals_created_by_idx", "workflow_proposals_project_id_idx"]


def test_008_preserves_legacy_proposal_rows_without_assigning_ownership(ownership_db):
    row = ownership_db.psql(
        f"select status, user_request, created_by is null, project_id is null "
        f"from public.workflow_proposals where id='{LEGACY_PROPOSAL}'"
    )
    assert row == "approved|legacy proposal request|t|t"


def test_008_is_rerun_safe_and_keeps_row_count(ownership_db):
    assert ownership_db.psql("select count(*) from public.workflow_proposals") == "2"
    migration_008 = next(m for m in MIGRATIONS if m.name.startswith("008"))
    ownership_db.psql(file=migration_008)
    assert ownership_db.psql("select count(*) from public.workflow_proposals") == "2"


def test_008_rls_member_and_creator_can_read_owned_proposal(ownership_db):
    assert _as_authenticated(
        ownership_db, PROPOSAL_MEMBER_USER,
        f"select count(*) from public.workflow_proposals where id='{OWNED_PROPOSAL}'"
    ) == "1"


def test_008_rls_non_member_cannot_read_or_update_owned_proposal(ownership_db):
    assert _as_authenticated(
        ownership_db, PROPOSAL_OUTSIDER_USER,
        "select count(*) from public.workflow_proposals"
    ) == "0"
    _as_authenticated(
        ownership_db, PROPOSAL_OUTSIDER_USER,
        f"update public.workflow_proposals set user_request='hijacked' where id='{OWNED_PROPOSAL}'"
    )
    assert ownership_db.psql(
        f"select user_request from public.workflow_proposals where id='{OWNED_PROPOSAL}'"
    ) == "owned proposal request"


def test_008_rls_legacy_unowned_proposal_is_invisible_to_authenticated(ownership_db):
    for user in (PROPOSAL_MEMBER_USER, PROPOSAL_OUTSIDER_USER):
        assert _as_authenticated(
            ownership_db, user,
            f"select count(*) from public.workflow_proposals where id='{LEGACY_PROPOSAL}'"
        ) == "0"
    assert _as_authenticated(ownership_db, None, "select count(*) from public.workflow_proposals") == "0"


def test_008_rls_insert_requires_creator_identity_and_membership(ownership_db):
    inserted = _as_authenticated(
        ownership_db, PROPOSAL_MEMBER_USER,
        f"insert into public.workflow_proposals (status, user_request, created_by, project_id) "
        f"values ('approved', 'member insert', '{PROPOSAL_MEMBER_USER}', '{PROPOSAL_PROJECT}') returning id"
    )
    assert inserted
    with pytest.raises(AssertionError, match="row-level security"):
        _as_authenticated(
            ownership_db, PROPOSAL_OUTSIDER_USER,
            f"insert into public.workflow_proposals (status, user_request, created_by, project_id) "
            f"values ('approved', 'outsider insert', '{PROPOSAL_OUTSIDER_USER}', '{PROPOSAL_PROJECT}')"
        )
    with pytest.raises(AssertionError, match="row-level security"):
        _as_authenticated(
            ownership_db, PROPOSAL_MEMBER_USER,
            f"insert into public.workflow_proposals (status, user_request, created_by, project_id) "
            f"values ('approved', 'spoofed creator', '{PROPOSAL_OUTSIDER_USER}', '{PROPOSAL_PROJECT}')"
        )


def test_008_service_path_retains_full_visibility(ownership_db):
    # The trusted service path (table owner / service_role) bypasses RLS and
    # keeps maintenance access to legacy rows.
    assert int(ownership_db.psql("select count(*) from public.workflow_proposals")) >= 2


def test_008_member_can_update_owned_proposal(ownership_db):
    _as_authenticated(
        ownership_db, PROPOSAL_MEMBER_USER,
        f"update public.workflow_proposals set repair_count = repair_count + 1 where id='{OWNED_PROPOSAL}'"
    )
    assert ownership_db.psql(
        f"select repair_count from public.workflow_proposals where id='{OWNED_PROPOSAL}'"
    ) == "1"


def test_008_authenticated_grants_are_least_privilege(ownership_db):
    grants = ownership_db.psql(
        "select privilege_type from information_schema.role_table_grants "
        "where grantee='authenticated' and table_schema='public' and table_name='workflow_proposals' "
        "order by privilege_type"
    ).splitlines()
    # UPDATE is column-scoped only (no table-level update grant).
    assert grants == ["INSERT", "SELECT"]
    update_columns = ownership_db.psql(
        "select column_name from information_schema.column_privileges "
        "where grantee='authenticated' and table_schema='public' and table_name='workflow_proposals' "
        "and privilege_type='UPDATE' order by column_name"
    ).splitlines()
    assert "created_by" not in update_columns
    assert "project_id" not in update_columns
    assert "user_request" in update_columns


# --- migration 009 (run idempotency + lifecycle) executable validation ---

def test_009_adds_idempotency_and_launch_columns(db):
    rows = db.psql(
        "select column_name from information_schema.columns "
        "where table_schema='public' and table_name='runs' and column_name in "
        "('requested_by','request_fingerprint','launch_state','launched_at','launch_error') order by 1"
    ).splitlines()
    assert rows == ["launch_error", "launch_state", "launched_at", "request_fingerprint", "requested_by"]


def test_009_legacy_runs_keep_default_launch_state_and_null_ownership(db):
    assert db.psql(
        "select count(*) from public.runs where id in "
        "('22222222-2222-2222-2222-222222222222','33333333-3333-3333-3333-333333333333') "
        "and launch_state = 'none' and requested_by is null and idempotency_key is null"
    ) == "2"


def test_009_expanded_status_values_are_accepted(db):
    for status in ("launching", "timed_out", "budget_exhausted"):
        run_id = db.psql(
            f"insert into public.runs (conversation_id, status, input) values "
            f"('11111111-1111-1111-1111-111111111111', '{status}', '{{}}'::jsonb) returning id"
        )
        assert run_id
    with pytest.raises(AssertionError, match="runs_status_check"):
        db.psql(
            "insert into public.runs (conversation_id, status, input) values "
            "('11111111-1111-1111-1111-111111111111', 'not_a_state', '{}'::jsonb)"
        )


def test_009_launch_state_check_rejects_unknown_values(db):
    with pytest.raises(AssertionError, match="runs_launch_state_check"):
        db.psql(
            "insert into public.runs (conversation_id, status, input, launch_state) values "
            "('11111111-1111-1111-1111-111111111111', 'queued', '{}'::jsonb, 'bogus')"
        )


def test_009_idempotency_unique_index_blocks_duplicates_per_user(db):
    _seed_membership_fixture(db)
    db.psql(
        f"insert into public.runs (conversation_id, status, input, requested_by, idempotency_key) values "
        f"('11111111-1111-1111-1111-111111111111', 'queued', '{{}}'::jsonb, '{MEMBER_USER}', 'idem-dup-1')"
    )
    with pytest.raises(AssertionError, match="runs_user_conversation_idempotency_uidx"):
        db.psql(
            f"insert into public.runs (conversation_id, status, input, requested_by, idempotency_key) values "
            f"('11111111-1111-1111-1111-111111111111', 'queued', '{{}}'::jsonb, '{MEMBER_USER}', 'idem-dup-1')"
        )
    # A different user may reuse the same key in the same conversation.
    db.psql(
        f"insert into public.runs (conversation_id, status, input, requested_by, idempotency_key) values "
        f"('11111111-1111-1111-1111-111111111111', 'queued', '{{}}'::jsonb, '{OUTSIDER_USER}', 'idem-dup-1')"
    )


def test_009_is_rerun_safe(db):
    migration_009 = next(m for m in MIGRATIONS if m.name.startswith("009"))
    before = db.psql("select count(*) from public.runs")
    db.psql(file=migration_009)
    assert db.psql("select count(*) from public.runs") == before


# --- migration 010 (run usage accounting) executable validation ---

def test_010_adds_usage_column_with_empty_default(db):
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='runs' and column_name='usage'"
    ) == "jsonb"
    assert db.psql(
        "select count(*) from public.runs where id='22222222-2222-2222-2222-222222222222' and usage = '{}'::jsonb"
    ) == "1"


def test_010_is_rerun_safe(db):
    migration_010 = next(m for m in MIGRATIONS if m.name.startswith("010"))
    db.psql(file=migration_010)
    assert db.psql("select count(*) from public.runs where usage is null") == "0"


# --- migration 011 (protected ownership + atomic project creation) ---

def test_011_authenticated_cannot_update_ownership_columns(ownership_db):
    with pytest.raises(AssertionError, match="permission denied"):
        _as_authenticated(
            ownership_db, PROPOSAL_MEMBER_USER,
            f"update public.workflow_proposals set created_by='{PROPOSAL_OUTSIDER_USER}' where id='{OWNED_PROPOSAL}'"
        )
    with pytest.raises(AssertionError, match="permission denied"):
        _as_authenticated(
            ownership_db, PROPOSAL_MEMBER_USER,
            f"update public.workflow_proposals set project_id=null where id='{OWNED_PROPOSAL}'"
        )
    # Non-ownership columns stay updatable for members.
    _as_authenticated(
        ownership_db, PROPOSAL_MEMBER_USER,
        f"update public.workflow_proposals set user_request='member edit ok' where id='{OWNED_PROPOSAL}'"
    )
    assert ownership_db.psql(
        f"select created_by::text, user_request from public.workflow_proposals where id='{OWNED_PROPOSAL}'"
    ) == f"{PROPOSAL_MEMBER_USER}|member edit ok"


def test_011_project_creation_with_owner_is_atomic(ownership_db):
    before = ownership_db.psql("select count(*) from public.projects")
    row = ownership_db.psql(
        "select id from public.create_project_from_proposal_with_owner("
        f"'{OWNED_PROPOSAL}', 'atomic-proj', 'Atomic Proj', null, '{{}}'::jsonb, '{PROPOSAL_MEMBER_USER}')"
    )
    assert row
    assert int(ownership_db.psql("select count(*) from public.projects")) == int(before) + 1
    assert ownership_db.psql(
        f"select role from public.project_members pm join public.projects p on p.id = pm.project_id "
        f"where p.slug='atomic-proj' and pm.user_id='{PROPOSAL_MEMBER_USER}'"
    ) == "owner"


def test_011_no_orphan_project_when_membership_insert_fails(ownership_db):
    before = ownership_db.psql("select count(*) from public.projects")
    with pytest.raises(AssertionError, match="foreign key|violates"):
        ownership_db.psql(
            "select public.create_project_from_proposal_with_owner("
            f"'{OWNED_PROPOSAL}', 'orphan-proj', 'Orphan Proj', null, '{{}}'::jsonb, "
            "'99999999-9999-4999-8999-999999999999')"  # not a real auth.users id
        )
    assert ownership_db.psql("select count(*) from public.projects") == before
    assert ownership_db.psql("select count(*) from public.projects where slug='orphan-proj'") == "0"


def test_011_authenticated_cannot_execute_project_creation_function(ownership_db):
    with pytest.raises(AssertionError, match="permission denied"):
        _as_authenticated(
            ownership_db, PROPOSAL_MEMBER_USER,
            "select public.create_project_from_proposal_with_owner("
            f"'{OWNED_PROPOSAL}', 'sneaky-proj', 'Sneaky', null, '{{}}'::jsonb, '{PROPOSAL_MEMBER_USER}')"
        )


def test_011_is_rerun_safe(ownership_db):
    migration_011 = next(m for m in MIGRATIONS if m.name.startswith("011"))
    ownership_db.psql(file=migration_011)
    assert ownership_db.psql(
        "select count(*) from pg_proc where proname='create_project_from_proposal_with_owner'"
    ) == "1"


# --- migration 012 (atomic run operations) executable validation ---

ATOMIC_PROJECT = "bbbbbbbb-0000-4000-8000-000000000021"
ATOMIC_CONVERSATION = "dddddddd-0000-4000-8000-000000000001"


def _seed_atomic_fixture(db) -> None:
    db.psql(
        f"insert into auth.users (id) values ('{PROPOSAL_MEMBER_USER}') on conflict do nothing; "
        f"insert into public.projects (id, slug, name, workflow_key) values "
        f"('{ATOMIC_PROJECT}', 'atomic-scope', 'Atomic Scope', 'vehicle_catalog_v1') on conflict (id) do nothing; "
        f"insert into public.conversations (id, project_id, title) values "
        f"('{ATOMIC_CONVERSATION}', '{ATOMIC_PROJECT}', 'atomic conversation') on conflict (id) do nothing"
    )


def _create_run_sql(key: str, content: str = "concurrent content", max_user: str = "null", max_project: str = "null") -> str:
    return (
        "select public.create_message_and_run("
        f"'{ATOMIC_CONVERSATION}', '{content}', '{{}}'::jsonb, '{PROPOSAL_MEMBER_USER}', "
        f"'{key}', 'fp-{key}', {max_user}, {max_project})"
    )


def test_012_concurrent_same_key_creates_exactly_one_message_and_run(ownership_db):
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    key = "concurrent-key-1"

    def attempt(_):
        try:
            return ("ok", ownership_db.psql(_create_run_sql(key)))
        except AssertionError as exc:
            return ("err", str(exc))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert all(kind == "ok" for kind, _ in results), results
    created_flags = ["'created': True" in out or '"created": true' in out for _, out in results]
    assert sum(created_flags) == 1, results
    assert ownership_db.psql(
        f"select count(*) from public.runs where idempotency_key='{key}'"
    ) == "1"
    assert ownership_db.psql(
        f"select count(*) from public.messages where conversation_id='{ATOMIC_CONVERSATION}' "
        f"and content='concurrent content'"
    ) == "1"


def test_012_concurrent_admission_never_exceeds_user_cap(ownership_db):
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    ownership_db.psql(
        f"update public.runs set status='completed' where requested_by='{PROPOSAL_MEMBER_USER}' "
        "and status in ('queued','launching','starting','running','waiting','cancellation_requested')"
    )

    def attempt(i):
        try:
            ownership_db.psql(_create_run_sql(f"admission-key-{i}", content=f"admission {i}", max_user="2"))
            return "ok"
        except AssertionError as exc:
            assert "USER_CONCURRENCY_LIMIT" in str(exc)
            return "limited"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count("ok") == 2, results
    assert results.count("limited") == 4, results
    active = ownership_db.psql(
        f"select count(*) from public.runs where requested_by='{PROPOSAL_MEMBER_USER}' and status='queued'"
    )
    assert active == "2"


def test_012_message_rolls_back_when_run_insert_fails(ownership_db):
    _seed_atomic_fixture(ownership_db)
    messages_before = ownership_db.psql("select count(*) from public.messages")
    runs_before = ownership_db.psql("select count(*) from public.runs")
    # Deterministically fail the second insert (the run) with a temporary
    # check constraint, proving the message insert rolls back with it.
    ownership_db.psql(
        "alter table public.runs add constraint test_block_rollback_fp "
        "check (request_fingerprint is distinct from 'fp-rollback-key')"
    )
    try:
        with pytest.raises(AssertionError, match="test_block_rollback_fp"):
            ownership_db.psql(_create_run_sql("rollback-key", content="rollback content"))
    finally:
        ownership_db.psql("alter table public.runs drop constraint test_block_rollback_fp")
    assert ownership_db.psql("select count(*) from public.messages") == messages_before
    assert ownership_db.psql("select count(*) from public.runs") == runs_before
    assert ownership_db.psql(
        "select count(*) from public.messages where content='rollback content'"
    ) == "0"


def test_012_launch_state_check_includes_launch_unknown(ownership_db):
    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input, launch_state) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb, 'launch_unknown') returning id"
    )
    assert run_id


def test_012_launch_cas_only_one_winner(ownership_db):
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input, launch_state) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb, 'pending') returning id"
    )

    def attempt(_):
        return ownership_db.psql(
            f"update public.runs set launch_state='launching' "
            f"where id='{run_id}' and status='queued' and launch_state in ('pending','launch_failed') "
            "returning id"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    winners = [r for r in results if r.strip()]
    assert len(winners) == 1, results


def test_012_create_message_and_run_reports_created_and_writes_no_replay_message(ownership_db):
    """The creation contract CODE-1's `--prepare` depends on, in the database.

    Three properties, all load-bearing for operator preparation:

    1.  a first call reports `created = true`;
    2.  a replay on the same (conversation, requested_by, idempotency key)
        reports `created = false` and returns the SAME run;
    3.  the replay writes **no second message and no second run** -- the
        idempotency lookup precedes every insert, which is what stops a
        preparation replay leaving an orphan message behind.

    Without (1) and (2), `--prepare` cannot tell "I created this" from "an
    ordinary product run already held this key", and would win the launch CAS
    for a run it did not create.
    """
    _seed_atomic_fixture(ownership_db)
    messages_before = ownership_db.psql("select count(*) from public.messages")
    runs_before = ownership_db.psql("select count(*) from public.runs")

    first = ownership_db.psql(_create_run_sql("operator-parity-key", content="prepared"))
    assert '"created": true' in first.replace("'", '"'), first
    run_id = ownership_db.psql(
        "select id from public.runs where idempotency_key = 'operator-parity-key'")
    assert run_id

    messages_after = ownership_db.psql("select count(*) from public.messages")
    runs_after = ownership_db.psql("select count(*) from public.runs")
    assert int(messages_after) == int(messages_before) + 1
    assert int(runs_after) == int(runs_before) + 1

    replay = ownership_db.psql(_create_run_sql("operator-parity-key", content="prepared"))
    assert '"created": false' in replay.replace("'", '"'), replay
    assert run_id in replay
    # The decisive half: a replay inserts nothing at all.
    assert ownership_db.psql("select count(*) from public.messages") == messages_after
    assert ownership_db.psql("select count(*) from public.runs") == runs_after


def test_012_an_idempotency_collision_never_yields_two_runs(ownership_db):
    """Concurrent creation on one idempotency key: exactly one run exists.

    The race operator preparation must survive. Whoever wins, the loser is
    told `created = false` and is handed the winner's run -- which is precisely
    the signal `--prepare` uses to refuse rather than adopt a run it did not
    create.
    """
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    key = "concurrent-collision-key"

    def create(index):
        try:
            return ownership_db.psql(_create_run_sql(key, content=f"body-{index}"))
        except AssertionError as failure:  # a unique-violation loser
            return f"ERROR {failure}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(create, range(6)))

    rows = ownership_db.psql(
        f"select count(*) from public.runs where idempotency_key = '{key}'")
    assert rows == "1", results
    created_true = [row for row in results if '"created": true' in row.replace("'", '"')]
    assert len(created_true) == 1, results
    # Every other caller was told it did not create the run.
    others = [row for row in results if row not in created_true]
    assert all('"created": false' in row.replace("'", '"') or row.startswith("ERROR")
               for row in others), results


def test_012_operator_owned_launch_state_is_unacquirable_by_the_launch_cas(ownership_db):
    """CODE-1's operator ownership, proven against real PostgreSQL.

    An operator capture run comes to rest in `launch_state = 'none'`. That is
    the value migration 009 already defaults to and already constrains, so
    nothing is invented here -- what matters is that the launch CAS in
    `backend/main.py` (`launch_state in ('pending','launch_failed')`) can never
    acquire it. If that were false, an ordinary `JobLauncher` could start a
    model worker on a run the operator is capturing with.
    """
    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input, launch_state) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', "
        f"""'{{"metadata": {{"milo_operation": "catalog.government.capture"}}}}'::jsonb, """
        f"'none') returning id"
    )
    acquired = ownership_db.psql(
        f"update public.runs set launch_state='launching' "
        f"where id='{run_id}' and status='queued' "
        "and launch_state in ('pending','launch_failed') returning id"
    )
    assert acquired.strip() == "", "the launch CAS acquired an operator-owned run"
    assert ownership_db.psql(
        f"select launch_state from public.runs where id='{run_id}'") == "none"


def test_012_operator_preparation_and_launch_contend_at_one_cas(ownership_db):
    """Exactly one winner, and the loser can never take it afterwards.

    This is the atomic boundary CODE-1's `--prepare` competes at: several
    ordinary launchers and one operator preparation all issue the SAME
    single-statement CAS against one freshly created run. One wins. The
    operator then rests the run in 'none', after which no further launcher --
    winner or loser -- can acquire it.
    """
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input, launch_state) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb, 'pending') returning id"
    )

    def acquire(_):
        return ownership_db.psql(
            f"update public.runs set launch_state='launching' "
            f"where id='{run_id}' and status='queued' "
            "and launch_state in ('pending','launch_failed') returning id"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(acquire, range(6)))
    winners = [row for row in results if row.strip()]
    assert len(winners) == 1, results

    # The winner is the operator: it rests the run in the unacquirable state.
    ownership_db.psql(f"update public.runs set launch_state='none' where id='{run_id}'")
    assert acquire(None).strip() == "", "a launcher acquired an operator-owned run"

    # And a lease is still claimable by the operator afterwards: ownership of
    # the LAUNCH and ownership of the LEASE are different boundaries, which is
    # exactly why the launch one has to be settled first.
    claimed = ownership_db.psql(
        f"select launch_state from public.claim_run_lease('{run_id}', 'operator-capture', 300)")
    assert claimed == "none"


def test_012_lease_claim_single_holder_under_concurrency(ownership_db):
    import concurrent.futures

    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb) returning id"
    )

    def claim(i):
        return ownership_db.psql(
            f"select worker_id from public.claim_run_lease('{run_id}', 'worker-{i}', 300)"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(claim, range(6)))
    winners = [r for r in results if r.strip()]
    assert len(winners) == 1, results
    assert ownership_db.psql(f"select status from public.runs where id='{run_id}'") == "starting"
    assert ownership_db.psql(f"select attempt from public.runs where id='{run_id}'") == "1"


def test_012_expired_lease_is_reclaimable_with_incremented_attempt(ownership_db):
    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb) returning id"
    )
    assert ownership_db.psql(f"select worker_id from public.claim_run_lease('{run_id}', 'worker-old', 300)") == "worker-old"
    # A second worker cannot claim while the lease is fresh.
    assert ownership_db.psql(f"select worker_id from public.claim_run_lease('{run_id}', 'worker-new', 300)") == ""
    ownership_db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    row = ownership_db.psql(f"select worker_id, attempt from public.claim_run_lease('{run_id}', 'worker-new', 300)")
    assert row == "worker-new|2"


def test_012_stale_worker_cannot_overwrite_newer_result(ownership_db):
    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input) values "
        f"('{ATOMIC_CONVERSATION}', 'queued', '{{}}'::jsonb) returning id"
    )
    ownership_db.psql(f"select public.claim_run_lease('{run_id}', 'worker-old', 300)")
    ownership_db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    ownership_db.psql(f"select public.claim_run_lease('{run_id}', 'worker-new', 300)")
    # The stale worker's conditional terminal write matches zero rows.
    stale_write = ownership_db.psql(
        f"update public.runs set status='failed' where id='{run_id}' and worker_id='worker-old' returning id"
    )
    assert stale_write.strip() == ""
    # The new holder completes; the terminal result is protected afterwards.
    ownership_db.psql(
        f"update public.runs set status='running' where id='{run_id}' and worker_id='worker-new'"
    )
    ownership_db.psql(
        f"update public.runs set status='completed' where id='{run_id}' and worker_id='worker-new' and status='running'"
    )
    late_stale = ownership_db.psql(
        f"update public.runs set status='failed' where id='{run_id}' and worker_id='worker-old' and status='running' returning id"
    )
    assert late_stale.strip() == ""
    assert ownership_db.psql(f"select status from public.runs where id='{run_id}'") == "completed"


def test_012_cancellation_stays_visible_through_lease_claim(ownership_db):
    _seed_atomic_fixture(ownership_db)
    run_id = ownership_db.psql(
        f"insert into public.runs (conversation_id, status, input) values "
        f"('{ATOMIC_CONVERSATION}', 'cancellation_requested', '{{}}'::jsonb) returning id"
    )
    claimed = ownership_db.psql(f"select status from public.claim_run_lease('{run_id}', 'worker-c', 300)")
    assert claimed == "cancellation_requested"


def test_012_is_rerun_safe(ownership_db):
    migration_012 = next(m for m in MIGRATIONS if m.name.startswith("012"))
    ownership_db.psql(file=migration_012)
    assert ownership_db.psql("select count(*) from pg_proc where proname='create_message_and_run'") == "1"
    assert ownership_db.psql("select count(*) from pg_proc where proname='claim_run_lease'") == "1"


def test_012_authenticated_cannot_execute_run_functions(ownership_db):
    _seed_atomic_fixture(ownership_db)
    with pytest.raises(AssertionError, match="permission denied"):
        _as_authenticated(ownership_db, PROPOSAL_MEMBER_USER, _create_run_sql("sneaky-key"))


# --- migration 013 (append-only usage ledger) executable validation ---

def test_013_ledger_table_shape_and_decimal_costs(db):
    assert db.psql(
        "select data_type, numeric_precision, numeric_scale from information_schema.columns "
        "where table_schema='public' and table_name='run_usage_ledger' and column_name='estimated_cost'"
    ) == "numeric|12|6"
    assert db.psql(
        "select data_type from information_schema.columns "
        "where table_schema='public' and table_name='run_usage_ledger' and column_name='actual_cost'"
    ) == "numeric"


def test_013_ledger_appends_and_is_append_only(db):
    run_id = db.psql(
        "insert into public.runs (conversation_id, status, input) values "
        "('11111111-1111-1111-1111-111111111111', 'queued', '{}'::jsonb) returning id"
    )
    entry_id = db.psql(
        f"insert into public.run_usage_ledger (run_id, provider, model, call_seq, decision, reserved_input_tokens, reserved_output_tokens, estimated_cost) "
        f"values ('{run_id}', 'moonshot', 'kimi', 1, 'reserved', 120, 500, 0.050000) returning id"
    )
    assert entry_id
    with pytest.raises(AssertionError, match="append-only"):
        db.psql(f"update public.run_usage_ledger set actual_cost = 0 where id = {entry_id}")
    with pytest.raises(AssertionError, match="append-only"):
        db.psql(f"delete from public.run_usage_ledger where id = {entry_id}")


def test_013_ledger_rejects_unknown_decision(db):
    run_id = db.psql(
        "insert into public.runs (conversation_id, status, input) values "
        "('11111111-1111-1111-1111-111111111111', 'queued', '{}'::jsonb) returning id"
    )
    with pytest.raises(AssertionError, match="decision"):
        db.psql(
            f"insert into public.run_usage_ledger (run_id, decision) values ('{run_id}', 'bogus')"
        )


def test_013_ledger_denies_authenticated_access(db):
    assert db.psql(
        "select count(*) from information_schema.role_table_grants "
        "where grantee='authenticated' and table_name='run_usage_ledger'"
    ) == "0"


def test_013_daily_cost_query_uses_settled_actuals_over_reserved_estimates(db):
    user = "aaaaaaaa-0000-4000-8000-000000000031"
    db.psql(f"insert into auth.users (id) values ('{user}') on conflict do nothing")
    run_id = db.psql(
        "insert into public.runs (conversation_id, status, input) values "
        "('11111111-1111-1111-1111-111111111111', 'queued', '{}'::jsonb) returning id"
    )
    db.psql(
        f"insert into public.run_usage_ledger (run_id, user_id, call_seq, decision, estimated_cost) values "
        f"('{run_id}', '{user}', 1, 'reserved', 0.05), ('{run_id}', '{user}', 2, 'reserved', 0.05)"
    )
    db.psql(
        f"insert into public.run_usage_ledger (run_id, user_id, call_seq, decision, actual_cost) values "
        f"('{run_id}', '{user}', 1, 'settled', 0.02)"
    )
    # call 1 settled at 0.02; call 2 still reserved at 0.05 => 0.07
    total = db.psql(
        "select round(sum(cost), 6) from ("
        "  select distinct on (run_id, call_seq) coalesce(actual_cost, estimated_cost) as cost "
        f"  from public.run_usage_ledger where user_id='{user}' and created_at > now() - interval '24 hours' "
        "  order by run_id, call_seq, (decision='settled') desc, id desc"
        ") settled_first"
    )
    assert total == "0.070000"


def test_013_is_rerun_safe(db):
    migration_013 = next(m for m in MIGRATIONS if m.name.startswith("013"))
    db.psql(file=migration_013)
    assert db.psql("select count(*) from pg_trigger where tgname='run_usage_ledger_append_only'") == "1"


# --- migrations 014/015 + 20260810000100 (service RPC ACLs) ---

# Every service-only RPC with its exact signature. Browser roles (anon,
# authenticated) must hold EXECUTE on none of these; the trusted backend
# (service_role) keeps EXECUTE except where a migration deliberately revoked
# it (the deprecated migration-014 daily RPCs).
SERVICE_ONLY_RPCS = [
    "public.create_message_and_run_v2(uuid, text, jsonb, uuid, text, text, integer, integer)",
    "public.create_project_from_proposal_with_owner_v2(uuid, text, text, text, jsonb, uuid)",
    "public.reserve_model_call_budget_v2(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text)",
    "public.settle_model_call_budget_v2(uuid, numeric, text, text)",
    "public.create_project_from_proposal_with_owner(uuid, text, text, text, jsonb, uuid)",
    "public.create_message_and_run(uuid, text, jsonb, uuid, text, text, integer, integer)",
    "public.claim_run_lease(uuid, text, integer)",
    "public.reserve_daily_user_budget(uuid, uuid, numeric, numeric, text, text)",
    "public.reserve_daily_project_budget(uuid, uuid, numeric, numeric, text, text)",
    "public.model_call_budget_committed(uuid, uuid, date)",
    "public.reserve_model_call_budget(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, text)",
    "public.settle_model_call_budget(uuid, numeric, text, text)",
    # 20260921000200 -- the run-identity binder and the three worker writes
    # that had no lease fence before it.
    "public.bind_run_identity(uuid, jsonb)",
    "public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)",
    "public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)",
    "public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)",
]
DEPRECATED_RPCS_WITHOUT_SERVICE_ROLE = {
    "public.reserve_daily_user_budget(uuid, uuid, numeric, numeric, text, text)",
    "public.reserve_daily_project_budget(uuid, uuid, numeric, numeric, text, text)",
}


def _has_execute(db, role: str, signature: str) -> bool:
    return db.psql(
        f"select has_function_privilege('{role}', '{signature}', 'execute')"
    ) == "t"


def test_anon_has_no_execute_on_any_service_rpc(db):
    granted = [sig for sig in SERVICE_ONLY_RPCS if _has_execute(db, "anon", sig)]
    assert granted == [], f"anon must not execute service RPCs: {granted}"


def test_authenticated_has_no_execute_on_any_service_rpc(db):
    granted = [sig for sig in SERVICE_ONLY_RPCS if _has_execute(db, "authenticated", sig)]
    assert granted == [], f"authenticated must not execute service RPCs: {granted}"


def test_service_role_grant_matrix(db):
    for sig in SERVICE_ONLY_RPCS:
        expected = sig not in DEPRECATED_RPCS_WITHOUT_SERVICE_ROLE
        assert _has_execute(db, "service_role", sig) is expected, (
            f"service_role EXECUTE on {sig} expected={expected}"
        )


def test_no_public_non_trigger_function_is_executable_by_anon(db):
    """Future-proof guard: a migration that adds an anon-callable RPC (or
    forgets to revoke Supabase's default anon EXECUTE grant) must fail this
    test. Trigger functions are excluded because PostgREST cannot invoke
    them and trigger firing does not check the caller's EXECUTE privilege."""
    leaked = db.psql(
        "select p.oid::regprocedure::text from pg_proc p "
        "join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.prorettype <> 'trigger'::regtype "
        # Extension-owned functions (pgcrypto) sit in public only in this
        # test cluster; Supabase installs them in the unexposed `extensions`
        # schema, so they are not part of the PostgREST RPC surface.
        "and not exists (select 1 from pg_depend d where d.objid = p.oid and d.deptype = 'e') "
        "and has_function_privilege('anon', p.oid, 'execute') order by 1"
    ).splitlines()
    assert leaked == [], f"anon-executable public functions: {leaked}"


def test_revoke_migration_is_rerun_safe(db):
    migration = next(m for m in MIGRATIONS if "revoke_anon_execute" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert not _has_execute(db, "anon", SERVICE_ONLY_RPCS[0])


def test_future_functions_following_repo_convention_are_fully_locked(db):
    """Historically, `revoke ... from public` (the convention every service
    RPC migration follows) was NOT enough on Supabase: anon held a direct
    EXECUTE grant from Supabase's default privileges that survived the
    public revoke. Migration 20260810000100 removes those default grants for
    anon/authenticated, so a future function that follows the existing
    convention is now genuinely browser-inaccessible. (The built-in PUBLIC
    execute default cannot be removed per-schema, which is why the explicit
    `from public` revoke stays part of the convention and is enforced by
    test_no_public_non_trigger_function_is_executable_by_anon.)"""
    db.psql(
        "create or replace function public.zz_test_future_probe() returns int "
        "language sql as $$ select 1 $$"
    )
    try:
        db.psql("revoke execute on function public.zz_test_future_probe() from public")
        assert not _has_execute(db, "anon", "public.zz_test_future_probe()")
        assert not _has_execute(db, "authenticated", "public.zz_test_future_probe()")
        assert _has_execute(db, "service_role", "public.zz_test_future_probe()")
    finally:
        db.psql("drop function public.zz_test_future_probe()")


# --- migration 20260810000200 (explicit RLS on service-only tables) ---

def test_every_public_table_has_rls_enabled_without_external_trigger(db):
    """This plain-PostgreSQL cluster has no `ensure_rls` event trigger (a
    platform guardrail some managed environments install), so this proves
    the migrations themselves enable RLS on every public table. It is also
    the future-proof guard: a migration creating a table without enabling
    RLS fails here."""
    assert db.psql("select count(*) from pg_event_trigger where evtname='ensure_rls'") == "0"
    missing = db.psql(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity order by 1"
    ).splitlines()
    assert missing == [], f"public tables without RLS: {missing}"


def test_service_only_tables_have_no_policies(db):
    """RLS with zero policies is the deny-all posture for browser roles on
    service-path tables; only the eight browser tables carry policies."""
    policy_tables = set(db.psql(
        "select distinct tablename from pg_policies where schemaname='public'"
    ).splitlines())
    browser_tables = {
        "projects", "project_members", "conversations", "messages",
        "runs", "run_events", "workflow_proposals",
    }
    assert policy_tables <= browser_tables, (
        f"unexpected policies outside the browser surface: {policy_tables - browser_tables}"
    )
    service_only = {
        "run_checkpoints", "worker_heartbeats", "agent_instances", "agent_tasks",
        "task_dependencies", "agent_messages", "run_blackboards", "supervisor_decisions",
        "tool_access_requests", "tool_grants", "tool_usage", "sources", "claims",
        "source_claim_links", "conflicts", "run_usage_ledger",
        "model_call_budget_reservations", "run_invocations",
        "source_evidence_fragments",
        # The durable catalog namespace: service-path only, exactly like the
        # evidence relations above it.
        "catalog_source_snapshots", "catalog_raw_records",
        "catalog_candidate_variants", "catalog_candidate_evidence_links",
        "catalog_models", "catalog_model_variants",
    }
    assert policy_tables & service_only == set()


def test_rls_migration_is_rerun_safe(db):
    migration = next(m for m in MIGRATIONS if "enable_rls_on_service_only_tables" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql(
        "select relrowsecurity from pg_class where relname='model_call_budget_reservations'"
    ) == "t"


# --- migration 20260810000300 (lease-guarded worker writes) ---

STALE_CONVERSATION = "dddddddd-0000-4000-8000-000000000099"


def _seed_stale_worker_run(db) -> str:
    db.psql(
        f"insert into public.projects (id, slug, name, workflow_key) values "
        f"('bbbbbbbb-0000-4000-8000-000000000099', 'stale-scope', 'Stale Scope', 'vehicle_catalog_v1') on conflict (id) do nothing; "
        f"insert into public.conversations (id, project_id, title) values "
        f"('{STALE_CONVERSATION}', 'bbbbbbbb-0000-4000-8000-000000000099', 'stale worker conversation') on conflict (id) do nothing"
    )
    return db.psql(
        f"insert into public.runs (conversation_id, status, input) values "
        f"('{STALE_CONVERSATION}', 'queued', '{{}}'::jsonb) returning id"
    )


def _guarded_calls(run_id: str, worker: str, attempt: str, token: str) -> dict[str, str]:
    """Every worker-originated durable write, expressed through the guarded
    surface a stale worker would hit."""
    lease = f"'{run_id}', '{worker}', {attempt}, '{token}'"
    return {
        "event": f"select id from public.append_run_event_guarded({lease}, 'run_started', 'msg', null, null, null, '{{}}'::jsonb)",
        "checkpoint": f"select id from public.save_checkpoint_guarded({lease}, 'v1', 'vehicle_catalog_v1', 'fetch')",
        "blackboard": f"select id from public.upsert_run_blackboard_guarded({lease}, '{{\"goal\": \"g\"}}'::jsonb)",
        "agent_message": f"select id from public.create_agent_message_guarded({lease}, '{{\"message_type\": \"progress\", \"sender\": \"a\", \"recipient\": \"supervisor\"}}'::jsonb)",
        "supervisor_decision": f"select id from public.create_supervisor_decision_guarded({lease}, '{{\"assessment\": \"ok\", \"rationale_summary\": \"r\"}}'::jsonb)",
        "reserve": f"select status from public.reserve_model_call_budget_guarded('{run_id}', {attempt}00, null, null, 0.01, null, null, '{worker}', {attempt}, '{token}')",
        "usage_ledger": f"select version from public.record_run_usage_guarded({lease}, '{{\"model_calls\": 1, \"provider_attempts\": 1}}'::jsonb)",
        "usage_snapshot": f"select id from public.update_run_usage_guarded({lease}, '{{\"model_calls\": 1}}'::jsonb)",
    }


def test_stale_worker_full_scenario_every_mutation_rejected(db):
    """The Stage B acceptance scenario, executed against real PostgreSQL:
    worker A claims and receives lease A; A's lease is reclaimed by worker B
    with a new attempt+token; every durable write A attempts is rejected
    atomically at the database boundary while B continues to completion."""
    run_id = _seed_stale_worker_run(db)

    # 1-2) Worker A acquires the run and its lease token.
    row_a = db.psql(f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-A', 300)")
    worker_a, attempt_a, token_a = row_a.split("|")
    assert (worker_a, attempt_a) == ("worker-A", "1")

    # While current, worker A can perform every guarded write.
    for name, sql in _guarded_calls(run_id, "worker-A", attempt_a, token_a).items():
        assert db.psql(sql).strip(), f"live worker A blocked on {name}"
    # A settles its own reservation while still holding the lease.
    reservation_a = db.psql(f"select id from public.model_call_budget_reservations where run_id='{run_id}' and call_seq={attempt_a}00")
    assert db.psql(
        f"select status from public.settle_model_call_budget_guarded('{reservation_a}', 0.005, '{run_id}', 'worker-A', {attempt_a}, '{token_a}')"
    ) == "settled"

    # 3) A's lease becomes stale/reclaimable.
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")

    # 4) Worker B acquires the run with a new attempt and token.
    row_b = db.psql(f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-B', 300)")
    worker_b, attempt_b, token_b = row_b.split("|")
    assert (worker_b, attempt_b) == ("worker-B", "2")
    assert token_b != token_a

    # 5-6) Every relevant mutation worker A attempts is rejected.
    for name, sql in _guarded_calls(run_id, "worker-A", attempt_a, token_a).items():
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            db.psql(sql)
    # A cannot settle a reservation for a run it no longer owns.
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        db.psql(f"select public.settle_model_call_budget_guarded('{reservation_a}', 0.001, '{run_id}', 'worker-A', {attempt_a}, '{token_a}')")
    # A's usage snapshot matches zero rows (repository-style conditional UPDATE).
    assert db.psql(
        f"update public.runs set usage='{{\"stale\": true}}'::jsonb where id='{run_id}' "
        f"and worker_id='worker-A' and attempt={attempt_a} and lease_token='{token_a}' and lease_expires_at > now() returning id"
    ).strip() == ""
    # A's terminal transition matches zero rows.
    assert db.psql(
        f"update public.runs set status='failed' where id='{run_id}' "
        f"and worker_id='worker-A' and attempt={attempt_a} and lease_token='{token_a}' and lease_expires_at > now() returning id"
    ).strip() == ""
    # A's heartbeat matches zero rows.
    assert db.psql(
        f"update public.runs set lease_expires_at = now() + interval '5 minutes' where id='{run_id}' "
        f"and worker_id='worker-A' and lease_token='{token_a}' and lease_expires_at > now() returning id"
    ).strip() == ""

    # 7) Worker B continues successfully through every write and completes.
    for name, sql in _guarded_calls(run_id, "worker-B", attempt_b, token_b).items():
        assert db.psql(sql).strip(), f"new holder worker B blocked on {name}"
    assert db.psql(
        f"update public.runs set status='running' where id='{run_id}' "
        f"and worker_id='worker-B' and attempt={attempt_b} and lease_token='{token_b}' and lease_expires_at > now() returning id"
    ).strip()
    assert db.psql(
        f"update public.runs set status='completed', finished_at=now() where id='{run_id}' and status='running' "
        f"and worker_id='worker-B' and attempt={attempt_b} and lease_token='{token_b}' and lease_expires_at > now() returning id"
    ).strip()
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "completed"
    # Even after completion, A's stale writes stay rejected.
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        db.psql(_guarded_calls(run_id, "worker-A", attempt_a, token_a)["event"])
    # The event stream contains only lease-valid writes: one per worker per kind.
    assert db.psql(
        f"select count(*) from public.run_events where run_id='{run_id}' and event_type='run_started'"
    ) == "2"


def test_guarded_write_race_with_concurrent_reclaim_is_atomic(db):
    """FOR SHARE on the runs row makes guard+insert atomic: a reclaim that
    runs concurrently with a guarded write cannot interleave between the
    lease check and the insert."""
    import concurrent.futures

    run_id = _seed_stale_worker_run(db)
    row_a = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-A', 300)")
    attempt_a, token_a = row_a.split("|")
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")

    def stale_write(_):
        try:
            db.psql(_guarded_calls(run_id, "worker-A", attempt_a, token_a)["event"])
            return "wrote"
        except AssertionError:
            return "rejected"

    def reclaim(_):
        return db.psql(f"select worker_id from public.claim_run_lease('{run_id}', 'worker-B', 300)")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        stale_result = pool.submit(stale_write, None)
        reclaim_result = pool.submit(reclaim, None)
        assert reclaim_result.result() == "worker-B"
        # The stale write must be rejected: its lease was already expired
        # before the race, and the reclaim serializes against FOR SHARE.
        assert stale_result.result() == "rejected"


def test_guarded_functions_are_service_path_only(db):
    for signature in [
        "public.assert_worker_lease(uuid, text, integer, text)",
        "public.append_run_event_guarded(uuid, text, integer, text, text, text, text, text, jsonb, jsonb)",
        "public.save_checkpoint_guarded(uuid, text, integer, text, text, text, text, jsonb, jsonb, jsonb, jsonb, jsonb)",
        "public.upsert_run_blackboard_guarded(uuid, text, integer, text, jsonb)",
        "public.create_agent_message_guarded(uuid, text, integer, text, jsonb)",
        "public.create_supervisor_decision_guarded(uuid, text, integer, text, jsonb)",
        "public.reserve_model_call_budget_guarded(uuid, integer, uuid, uuid, numeric, numeric, numeric, text, integer, text, text, text)",
        "public.settle_model_call_budget_guarded(uuid, numeric, uuid, text, integer, text, text, text)",
    ]:
        assert not _has_execute(db, "anon", signature), signature
        assert not _has_execute(db, "authenticated", signature), signature
        assert _has_execute(db, "service_role", signature), signature


def test_lease_guard_migration_is_rerun_safe(db):
    migration = next(m for m in MIGRATIONS if "lease_guarded_worker_writes" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql("select count(*) from pg_proc where proname='assert_worker_lease'") == "1"


def test_every_http_facing_rpc_returns_a_set(db):
    """The pinned supabase-py/postgrest-py client parses RPC responses as a
    LIST; a function returning a single composite row (JSON object) fails
    client-side AFTER the write commits (observed live in staging). Every
    RPC the repository calls over PostgREST must therefore return SETOF."""
    rpcs = [
        "create_message_and_run_v2",
        "create_project_from_proposal_with_owner_v2",
        "claim_run_lease",
        "reserve_model_call_budget_v2",
        "settle_model_call_budget_v2",
        "reserve_model_call_budget_guarded",
        "settle_model_call_budget_guarded",
        "append_run_event_guarded",
        "save_checkpoint_guarded",
        "upsert_run_blackboard_guarded",
        "create_agent_message_guarded",
        "create_supervisor_decision_guarded",
        "model_call_budget_committed",
    ]
    for name in rpcs:
        assert db.psql(
            f"select bool_and(proretset) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            f"where n.nspname='public' and p.proname='{name}'"
        ) == "t", f"{name} must return SETOF for PostgREST client compatibility"


def test_ledger_accepts_every_code_written_decision(db):
    """BudgetTracker writes decisions reserved/settled/rejected/overage/
    released; the ledger constraint must accept all five (a live staging
    overage crashed on the pre-20260810000500 constraint) and still reject
    unknown values."""
    run_id = db.psql(
        "insert into public.runs (conversation_id, status, input) values "
        "('11111111-1111-1111-1111-111111111111', 'queued', '{}'::jsonb) returning id"
    )
    for seq, decision in enumerate(["reserved", "settled", "rejected", "overage", "released"], start=900):
        db.psql(
            f"insert into public.run_usage_ledger (run_id, call_seq, decision) values ('{run_id}', {seq}, '{decision}')"
        )
    with pytest.raises(AssertionError, match="run_usage_ledger_decision_check"):
        db.psql(
            f"insert into public.run_usage_ledger (run_id, call_seq, decision) values ('{run_id}', 999, 'bogus')"
        )


# --- migration 20260810000600 (corrective lease/attempt hardening) ---

def test_settle_guard_rejects_cross_run_reservation(db):
    """A valid worker lease for run A must NEVER settle a reservation
    belonging to run B; B's reservation stays byte-for-byte unchanged."""
    run_a = _seed_stale_worker_run(db)
    run_b = _seed_stale_worker_run(db)
    row_a = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_a}', 'worker-XA', 300)")
    attempt_a, token_a = row_a.split("|")
    row_b = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_b}', 'worker-XB', 300)")
    attempt_b, token_b = row_b.split("|")
    reservation_b = db.psql(
        f"select id from public.reserve_model_call_budget_guarded('{run_b}', 7, null, null, 0.01, null, null, 'worker-XB', {attempt_b}, '{token_b}')"
    )
    before = db.psql(f"select status || '|' || coalesce(actual_cost::text,'') || '|' || coalesce(settled_at::text,'') from public.model_call_budget_reservations where id='{reservation_b}'")
    # Worker A holds a perfectly valid lease for run A — and must be rejected.
    with pytest.raises(AssertionError, match="RESERVATION_RUN_MISMATCH_OR_SETTLED"):
        db.psql(f"select public.settle_model_call_budget_guarded('{reservation_b}', 0.005, '{run_a}', 'worker-XA', {attempt_a}, '{token_a}')")
    after = db.psql(f"select status || '|' || coalesce(actual_cost::text,'') || '|' || coalesce(settled_at::text,'') from public.model_call_budget_reservations where id='{reservation_b}'")
    assert before == after == "reserved||"
    # The rightful owner can still settle it.
    assert db.psql(
        f"select status from public.settle_model_call_budget_guarded('{reservation_b}', 0.005, '{run_b}', 'worker-XB', {attempt_b}, '{token_b}')"
    ) == "settled"


def test_db_clock_decides_lease_expiry_for_every_guarded_run_write(db):
    """Clock skew must not matter: once the DATABASE considers the lease
    expired, usage/heartbeat/terminal-transition writes with the correct
    (worker_id, attempt, lease_token) tuple are rejected — there is no
    application timestamp anywhere in these predicates."""
    run_id = _seed_stale_worker_run(db)
    row = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-CLK', 300)")
    attempt, token = row.split("|")
    lease = f"'{run_id}', 'worker-CLK', {attempt}, '{token}'"
    # While DB-current, all three writes succeed.
    assert db.psql(f"select id from public.update_run_usage_guarded({lease}, '{{\"calls\": 1}}'::jsonb)").strip()
    assert db.psql(f"select id from public.heartbeat_run_guarded({lease}, 300)").strip()
    assert db.psql(
        f"select status from public.transition_run_worker_guarded('{run_id}', 'running', 'starting', 'worker-CLK', {attempt}, '{token}')"
    ) == "running"
    # DB-expire the lease; the tuple is still 'correct' from the worker's view.
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 second' where id='{run_id}'")
    for sql in [
        f"select public.update_run_usage_guarded({lease}, '{{\"stale\": true}}'::jsonb)",
        f"select public.heartbeat_run_guarded({lease}, 300)",
        f"select public.transition_run_worker_guarded('{run_id}', 'completed', 'running', 'worker-CLK', {attempt}, '{token}', null, null, true, null, null, now())",
    ]:
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            db.psql(sql)
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "running"


def test_heartbeat_guarded_extends_lease_and_records_heartbeat_row(db):
    run_id = _seed_stale_worker_run(db)
    row = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-HB', 60)")
    attempt, token = row.split("|")
    before = db.psql(f"select lease_expires_at from public.runs where id='{run_id}'")
    extended = db.psql(f"select lease_expires_at from public.heartbeat_run_guarded('{run_id}', 'worker-HB', {attempt}, '{token}', 600)")
    assert extended > before
    assert db.psql(
        f"select count(*) from public.worker_heartbeats where run_id='{run_id}' and worker_id='worker-HB'"
    ) == "1"


def test_attempt_aware_reservations_do_not_collide_and_stay_counted(db):
    """A reclaimed attempt reserving the same call_seq creates a NEW
    reservation row; the earlier attempt's possibly-spent reservation is
    never mutated and keeps counting toward committed budget."""
    user = "aaaaaaaa-0000-4000-8000-000000000077"
    db.psql(f"insert into auth.users (id) values ('{user}') on conflict do nothing")
    run_id = _seed_stale_worker_run(db)
    row1 = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-A1', 300)")
    attempt1, token1 = row1.split("|")
    r1 = db.psql(
        f"select id from public.reserve_model_call_budget_guarded('{run_id}', 1, '{user}', null, 0.25, 10.0, null, 'worker-A1', {attempt1}, '{token1}')"
    )
    committed_1 = db.psql(f"select user_committed from public.model_call_budget_committed('{user}', null, (now() at time zone 'utc')::date)")
    # Crash: lease expires; a new attempt reclaims and reuses call_seq 1.
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    row2 = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-A2', 300)")
    attempt2, token2 = row2.split("|")
    assert int(attempt2) == int(attempt1) + 1
    r2 = db.psql(
        f"select id from public.reserve_model_call_budget_guarded('{run_id}', 1, '{user}', null, 0.25, 10.0, null, 'worker-A2', {attempt2}, '{token2}')"
    )
    assert r2 and r2 != r1, "reclaimed attempt must get its own reservation row"
    # Attempt 1's row is untouched and still counted (conservative: a
    # possibly-spent provider call never disappears from accounting).
    assert db.psql(f"select status || '|' || attempt::text from public.model_call_budget_reservations where id='{r1}'") == f"reserved|{attempt1}"
    committed_2 = db.psql(f"select user_committed from public.model_call_budget_committed('{user}', null, (now() at time zone 'utc')::date)")
    assert float(committed_2) == float(committed_1) + 0.25
    # The new attempt settles ITS row; attempt 1's row still cannot be
    # touched by attempt 2 (attempt binding in the settle guard).
    assert db.psql(
        f"select status from public.settle_model_call_budget_guarded('{r2}', 0.20, '{run_id}', 'worker-A2', {attempt2}, '{token2}')"
    ) == "settled"
    with pytest.raises(AssertionError, match="RESERVATION_RUN_MISMATCH_OR_SETTLED"):
        db.psql(f"select public.settle_model_call_budget_guarded('{r1}', 0.20, '{run_id}', 'worker-A2', {attempt2}, '{token2}')")
    assert db.psql(f"select status from public.model_call_budget_reservations where id='{r1}'") == "reserved"


def test_corrective_migration_functions_are_service_path_only(db):
    for signature in [
        "public.reserve_model_call_budget_for_attempt(uuid, integer, integer, uuid, uuid, numeric, numeric, numeric, text, text)",
        "public.update_run_usage_guarded(uuid, text, integer, text, jsonb)",
        "public.heartbeat_run_guarded(uuid, text, integer, text, integer)",
        "public.transition_run_worker_guarded(uuid, text, text, text, integer, text, jsonb, jsonb, boolean, jsonb, timestamptz, timestamptz)",
    ]:
        assert not _has_execute(db, "anon", signature), signature
        assert not _has_execute(db, "authenticated", signature), signature
        assert _has_execute(db, "service_role", signature), signature


def test_corrective_migration_is_rerun_safe(db):
    migration = next(m for m in MIGRATIONS if "corrective_lease_and_attempt_hardening" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql("select count(*) from pg_proc where proname='transition_run_worker_guarded'") == "1"


# --- migration 20260823000100 (lease-guarded evidence writes) ---

def _evidence_fixture(db, suffix: str):
    run_a = _seed_stale_worker_run(db)
    run_b = _seed_stale_worker_run(db)
    attempt_a, token_a = db.psql(
        f"select attempt, lease_token from public.claim_run_lease('{run_a}', 'evidence-{suffix}-a', 300)"
    ).split("|")
    attempt_b, token_b = db.psql(
        f"select attempt, lease_token from public.claim_run_lease('{run_b}', 'evidence-{suffix}-b', 300)"
    ).split("|")
    grant_a = db.psql(
        f"insert into public.tool_grants(run_id,agent,tool,max_searches,max_rounds,expires_at,approver_policy) "
        f"values ('{run_a}','agent','search',10,2,now()+interval '1 hour','test') returning id"
    )
    grant_b = db.psql(
        f"insert into public.tool_grants(run_id,agent,tool,max_searches,max_rounds,expires_at,approver_policy) "
        f"values ('{run_b}','agent','search',10,2,now()+interval '1 hour','test') returning id"
    )
    return (run_a, "evidence-" + suffix + "-a", attempt_a, token_a, grant_a), (run_b, "evidence-" + suffix + "-b", attempt_b, token_b, grant_b)


def _rpc_as_service(db, sql: str) -> str:
    return db.psql(f"set role service_role; {sql}; reset role")


def _source_json(key: str, *, task: str = "task") -> str:
    return json.dumps({"agent": "agent", "url": f"https://example.test/{key}", "title": "title",
                       "domain": "example.test", "source_type": "primary", "source_strength": "strong",
                       "query": "query", "tool_operation": "search", "evidence_key": key, "task_key": task})


def _claim_json(key: str, source_id: str, value: int, *, entity: str = "entity",
                field: str = "price", market: str | None = "IL", geography: str | None = None,
                time_scope: dict | None = None) -> str:
    time_scope = {"as_of": "2026-08"} if time_scope is None else time_scope
    # The canonical identity is computed by the trusted backend normalization
    # module — exactly the production claim persistence path.
    scope = canonical_scope_key(entity=entity, field=field, geography=geography,
                                market=market, time_scope=time_scope)
    return json.dumps({"entity_key": entity, "field_key": field, "value": value,
                       "time_scope": time_scope, "market": market, "geography": geography,
                       "source_id": source_id,
                       "source_strength": "strong", "confidence": .9, "agent": "agent",
                       "canonical_scope_hash": canonical_scope_hash(scope),
                       "scope_normalization_version": SCOPE_NORMALIZATION_VERSION,
                       "evidence_key": key, "task_key": "task"})


def test_evidence_rpcs_trusted_role_lifecycle_idempotency_and_blackboard_preservation(db):
    lease, _ = _evidence_fixture(db, "life")
    run_id, worker, attempt, token, grant = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    usage = json.dumps({"grant_id": grant, "agent": "agent", "tool": "search", "operation": "query",
                        "status": "succeeded", "idempotency_key": "usage-life", "task_key": "task"})
    assert _rpc_as_service(db, f"select id from public.create_tool_usage_guarded({args},'{usage}'::jsonb)")
    source_1 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('source-life-1')}'::jsonb)")
    source_2 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('source-life-2')}'::jsonb)")
    claim_1 = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('claim-life-1', source_1, 100)}'::jsonb)")
    claim_2 = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('claim-life-2', source_2, 120)}'::jsonb)")
    conflict = json.dumps({"entity_key": "entity", "field_key": "price", "claim_ids": [claim_1, claim_2],
                           "rationale": "values differ", "evidence_key": "conflict-life", "task_key": "review"})
    conflict_id = _rpc_as_service(db, f"select id from public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    # Retry every operation through a fresh SQL call: all identities are stable.
    _rpc_as_service(db, f"select id from public.create_tool_usage_guarded({args},'{usage}'::jsonb)")
    _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('source-life-1')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('claim-life-1', source_1, 100)}'::jsonb)")
    _rpc_as_service(db, f"select id from public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    assert db.psql(f"select (select count(*) from public.tool_usage where run_id='{run_id}') || '|' || "
                   f"(select count(*) from public.sources where run_id='{run_id}') || '|' || "
                   f"(select count(*) from public.claims where run_id='{run_id}') || '|' || "
                   f"(select count(*) from public.source_claim_links l join public.claims c on c.id=l.claim_id where c.run_id='{run_id}') || '|' || "
                   f"(select count(*) from public.conflicts where run_id='{run_id}')") == "1|2|2|2|1"
    db.psql(f"insert into public.run_blackboards(run_id,goal,approved_plan,completed_tasks,active_agents,open_questions,missing_fields,artifacts,remaining_budget,completion_score) "
            f"values ('{run_id}','goal','{{\"plan\":1}}','[\"done\"]','[\"agent\"]','[\"q\"]','[\"field\"]','{{\"a\":1}}','{{\"units\":7}}',.75)")
    summary_a = json.dumps({"known_entities": [{"claim_id": claim_1, "task_key": "task-a"}],
                            "claims_conflict_summaries": [{"conflict_id": conflict_id, "task_key": "task-a"}]})
    summary_b = json.dumps({"known_entities": [{"claim_id": claim_2, "task_key": "task-b"}],
                            "claims_conflict_summaries": [{"conflict_id": "00000000-0000-4000-8000-000000000061", "task_key": "task-b"}]})
    _rpc_as_service(db, f"select id from public.patch_run_blackboard_evidence_guarded({args},'{summary_a}'::jsonb)")
    _rpc_as_service(db, f"select id from public.patch_run_blackboard_evidence_guarded({args},'{summary_b}'::jsonb)")
    _rpc_as_service(db, f"select id from public.patch_run_blackboard_evidence_guarded({args},'{summary_b}'::jsonb)")
    assert db.psql(f"select jsonb_array_length(known_entities) || '|' || jsonb_array_length(claims_conflict_summaries) || '|' || "
                   f"(select count(distinct item->>'claim_id') from jsonb_array_elements(known_entities) item) || '|' || "
                   f"(select count(distinct item->>'conflict_id') from jsonb_array_elements(claims_conflict_summaries) item) "
                   f"from public.run_blackboards where run_id='{run_id}'") == "2|2|2|2"
    assert db.psql(f"select approved_plan::text || '|' || completed_tasks::text || '|' || active_agents::text || '|' || open_questions::text || '|' || missing_fields::text || '|' || artifacts::text || '|' || remaining_budget::text || '|' || completion_score from public.run_blackboards where run_id='{run_id}'") == '{"plan": 1}|["done"]|["agent"]|["q"]|["field"]|{"a": 1}|{"units": 7}|0.75'
    before = db.psql(f"select known_entities::text || '|' || claims_conflict_summaries::text || '|' || updated_at::text from public.run_blackboards where run_id='{run_id}'")
    invalid_summaries = [
        {"known_entities": [], "claims_conflict_summaries": [], "artifacts": {}},
        {"known_entities": {}, "claims_conflict_summaries": []},
        {"known_entities": [], "claims_conflict_summaries": {}},
        {"known_entities": [{"claim_id": "x", "provider_detail": "secret sentinel"}], "claims_conflict_summaries": []},
    ]
    for invalid in invalid_summaries:
        payload = json.dumps(invalid)
        with pytest.raises(AssertionError, match="invalid evidence summary|unsafe evidence payload rejected"):
            _rpc_as_service(db, f"select public.patch_run_blackboard_evidence_guarded({args},'{payload}'::jsonb)")
    after = db.psql(f"select known_entities::text || '|' || claims_conflict_summaries::text || '|' || updated_at::text from public.run_blackboards where run_id='{run_id}'")
    assert after == before


def test_evidence_rpc_acl_and_cross_run_provenance_guards(db):
    lease_a, lease_b = _evidence_fixture(db, "cross")
    run_a, worker_a, attempt_a, token_a, grant_a = lease_a
    run_b, _, _, _, grant_b = lease_b
    args = f"'{run_a}','{worker_a}',{attempt_a},'{token_a}'"
    for role in ("anon", "authenticated"):
        for rpc in ("create_tool_usage_guarded", "upsert_source_guarded",
                    "create_claim_with_source_guarded", "create_conflict_guarded",
                    "patch_run_blackboard_evidence_guarded"):
            with pytest.raises(AssertionError, match="permission denied"):
                db.psql(f"set role {role}; select public.{rpc}({args},'{{}}'::jsonb)")
    for grant, agent, tool in ((grant_b, "agent", "search"), (grant_a, "wrong", "search"), (grant_a, "agent", "wrong")):
        usage = json.dumps({"grant_id": grant, "agent": agent, "tool": tool, "operation": "query",
                            "status": "succeeded", "idempotency_key": f"bad-{grant}-{agent}-{tool}", "task_key": "task"})
        with pytest.raises(AssertionError, match="invalid tool grant"):
            _rpc_as_service(db, f"select public.create_tool_usage_guarded({args},'{usage}'::jsonb)")
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded('{run_b}','{lease_b[1]}',{lease_b[2]},'{lease_b[3]}','{_source_json('cross-source')}'::jsonb)")
    with pytest.raises(AssertionError, match="invalid claim source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_claim_json('cross-claim', source_b, 1)}'::jsonb)")
    assert db.psql(f"select count(*) from public.claims where run_id='{run_a}' and evidence_key='cross-claim'") == "0"
    source_a = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('cross-source-a')}'::jsonb)")
    claim_a = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('cross-claim-a', source_a, 1)}'::jsonb)")
    claim_b = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded('{run_b}','{lease_b[1]}',{lease_b[2]},'{lease_b[3]}','{_claim_json('cross-claim-b', source_b, 2)}'::jsonb)")
    mixed = json.dumps({"entity_key": "entity", "field_key": "price", "claim_ids": [claim_a, claim_b],
                        "evidence_key": "cross-conflict", "task_key": "review"})
    with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{mixed}'::jsonb)")


def test_evidence_stale_wrong_expired_leases_and_claim_link_rollback_write_nothing(db):
    lease, _ = _evidence_fixture(db, "stale")
    run_id, worker, attempt, token, grant = lease
    source_payload = _source_json("never-written")
    usage = json.dumps({"grant_id": grant, "agent": "agent", "tool": "search", "operation": "query",
                        "status": "succeeded", "idempotency_key": "never-written", "task_key": "task"})
    bad_leases = [("wrong", attempt, token, False), (worker, str(int(attempt) + 1), token, False),
                  (worker, attempt, "wrong", False), (worker, attempt, token, True)]
    for bad_worker, bad_attempt, bad_token, expire in bad_leases:
        if expire:
            db.psql(f"update public.runs set lease_expires_at=now()-interval '1 second' where id='{run_id}'")
        bad = f"'{run_id}','{bad_worker}',{bad_attempt},'{bad_token}'"
        for call in (f"select public.create_tool_usage_guarded({bad},'{usage}'::jsonb)",
                     f"select public.upsert_source_guarded({bad},'{source_payload}'::jsonb)",
                     f"select public.create_claim_with_source_guarded({bad},'{{}}'::jsonb)",
                     f"select public.create_conflict_guarded({bad},'{{}}'::jsonb)",
                     f"select public.patch_run_blackboard_evidence_guarded({bad},'{{}}'::jsonb)"):
            with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
                _rpc_as_service(db, call)
    assert db.psql(f"select (select count(*) from public.tool_usage where run_id='{run_id}') + (select count(*) from public.sources where run_id='{run_id}') + (select count(*) from public.claims where run_id='{run_id}') + (select count(*) from public.source_claim_links l join public.claims c on c.id=l.claim_id where c.run_id='{run_id}') + (select count(*) from public.conflicts where run_id='{run_id}') + (select count(*) from public.run_blackboards where run_id='{run_id}')") == "0"

    # A forced link failure proves the claim inserted earlier in the same RPC rolls back.
    db.psql(f"update public.runs set lease_expires_at=now()+interval '5 minutes' where id='{run_id}'")
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source_id = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('rollback-source')}'::jsonb)")
    db.psql("create or replace function public.reject_test_link() returns trigger language plpgsql as $$ begin raise exception 'forced link failure'; end $$; create trigger reject_test_link before insert on public.source_claim_links for each row execute function public.reject_test_link()")
    with pytest.raises(AssertionError, match="forced link failure"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_claim_json('rollback-claim', source_id, 1)}'::jsonb)")
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}' and evidence_key='rollback-claim'") == "0"
    db.psql("drop trigger reject_test_link on public.source_claim_links; drop function public.reject_test_link()")


def test_evidence_migration_is_executably_rerun_safe(db):
    migration = next(m for m in MIGRATIONS if "lease_guarded_evidence_writes" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    for name in ("create_tool_usage_guarded", "upsert_source_guarded",
                 "create_claim_with_source_guarded", "create_conflict_guarded",
                 "patch_run_blackboard_evidence_guarded"):
        assert db.psql(f"select count(*) from pg_proc where proname='{name}'") == "1"


def test_swarm_checkpoint_shape_persists_and_null_engine_version_rejected(db):
    """The Swarm V2 engine's durable checkpoint must satisfy the real
    run_checkpoints NOT NULL columns through the guarded RPC. The NULL
    engine_version case reproduces the pre-fix engine payload (it sent
    'version' instead of 'engine_version') and must be rejected by the
    database, never silently accepted."""
    run_id = _seed_stale_worker_run(db)
    row = db.psql(f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-SWM', 300)")
    worker, attempt, token = row.split("|")
    lease = f"'{run_id}', '{worker}', {attempt}, '{token}'"
    checkpoint_id = db.psql(
        f"select id from public.save_checkpoint_guarded({lease}, 'swarm_v2.1', 'swarm_v2', 'swarm_v2', "
        f"'[]'::jsonb, '{{\"swarm_state\": {{\"run_id\": \"{run_id}\", \"objective\": \"o\"}}}}'::jsonb, "
        f"'[]'::jsonb, '{{\"model_calls\": 1, \"total_tokens\": 160}}'::jsonb, null)"
    )
    assert checkpoint_id
    stored = db.psql(
        f"select engine_version, workflow_key, phase from public.run_checkpoints where id='{checkpoint_id}'"
    )
    assert stored == "swarm_v2.1|swarm_v2|swarm_v2"
    with pytest.raises(AssertionError, match="null value|not-null"):
        db.psql(f"select id from public.save_checkpoint_guarded({lease}, null, 'swarm_v2', 'swarm_v2')")


def test_claim_run_lease_returns_no_row_for_every_terminal_status(db):
    """A Cloud Run retry claiming a durably finalized run matches zero rows
    (surfaced as RUN_ALREADY_CLAIMED by the repository); the worker treats
    that as a no-op success, so the retry chain ends without touching the
    run."""
    for status in ("completed", "failed", "cancelled", "timed_out", "budget_exhausted", "partial_success"):
        run_id = _seed_stale_worker_run(db)
        db.psql(f"update public.runs set status='{status}' where id='{run_id}'")
        assert db.psql(f"select worker_id from public.claim_run_lease('{run_id}', 'worker-RETRY', 300)") == ""
        assert db.psql(f"select status, attempt from public.runs where id='{run_id}'") == f"{status}|1"


# --- migration 20260828000100 (canonical scope conflict identity) ---

@pytest.fixture
def canonical_db(db):
    """The shared module DB with the canonical-scope migration guaranteed
    current, even after earlier rerun-safety tests re-applied the older
    evidence migration (which restores the pre-canonical RPC definitions)."""
    migration = next(m for m in MIGRATIONS if "canonical_scope_conflict_identity" in m.name)
    db.psql(file=migration)
    return db


def _canonical_lease(db, suffix: str):
    lease, _ = _evidence_fixture(db, suffix)
    run_id, worker, attempt, token, _grant = lease
    return run_id, f"'{run_id}','{worker}',{attempt},'{token}'"


def _seed_claim(db, args: str, key: str, value: int, **scope) -> str:
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json(f'src-{key}')}'::jsonb)")
    return _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json(key, source, value, **scope)}'::jsonb)")


def _conflict_json(key: str, entity: str, field: str, claim_ids: list[str]) -> str:
    return json.dumps({"entity_key": entity, "field_key": field, "claim_ids": sorted(claim_ids),
                       "rationale": "values differ", "evidence_key": key, "task_key": "review"})


VARIANT_A = dict(entity="Toyota Corolla 2020", field="engine-power", market="Israel",
                 geography="IL", time_scope={"year": 2020})
VARIANT_B = dict(entity="toyota_corolla_2020", field="Engine Power", market=" israel ",
                 geography="il", time_scope={"year": 2020})


def test_formatting_variant_claims_persist_as_one_durable_conflict(canonical_db):
    """B1 regression: before the canonical-scope migration this exact flow was
    rejected with 'must exist, share one scope, and contradict' because the
    conflict RPC compared raw scope text."""
    db = canonical_db
    run_id, args = _canonical_lease(db, "canon")
    claim_a = _seed_claim(db, args, "canon-a", 100, **VARIANT_A)
    claim_b = _seed_claim(db, args, "canon-b", 120, **VARIANT_B)
    conflict = _conflict_json("canon-conflict", "Toyota Corolla 2020", "engine-power", [claim_a, claim_b])
    conflict_id = _rpc_as_service(db, f"select id from public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    assert conflict_id
    # Idempotent retry reuses the same durable conflict.
    retried = _rpc_as_service(db, f"select id from public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    assert retried == conflict_id
    assert db.psql(f"select count(*) from public.conflicts where run_id='{run_id}'") == "1"
    # Original provenance is stored untouched; only the canonical identity is shared.
    assert db.psql(f"select entity_key || '|' || field_key || '|' || market || '|' || geography || '|' || time_scope::text from public.claims where id='{claim_a}'") == 'Toyota Corolla 2020|engine-power|Israel|IL|{"year": 2020}'
    assert db.psql(f"select entity_key || '|' || field_key || '|' || market || '|' || geography from public.claims where id='{claim_b}'") == "toyota_corolla_2020|Engine Power| israel |il"
    stored = db.psql(f"select distinct canonical_scope_hash || '|' || scope_normalization_version from public.claims where id in ('{claim_a}','{claim_b}')")
    expected = canonical_scope_hash(canonical_scope_key(
        entity=VARIANT_A["entity"], field=VARIANT_A["field"], geography=VARIANT_A["geography"],
        market=VARIANT_A["market"], time_scope=VARIANT_A["time_scope"]))
    assert stored == f"{expected}|{SCOPE_NORMALIZATION_VERSION}"


def test_semantic_year_and_market_scope_differences_stay_rejected(canonical_db):
    db = canonical_db
    _, args = _canonical_lease(db, "canonsem")
    base = _seed_claim(db, args, "sem-base", 100, **VARIANT_A)
    different_scopes = [
        ("sem-year", dict(VARIANT_A, time_scope={"year": 2021})),
        ("sem-market", dict(VARIANT_A, market="Global")),
        ("sem-geo", dict(VARIANT_A, geography="US")),
        ("sem-alias", dict(VARIANT_A, entity="Toyota Corolla 2020 New")),
    ]
    for key, scope in different_scopes:
        other = _seed_claim(db, args, key, 999, **scope)
        assert db.psql(f"select count(distinct canonical_scope_hash) from public.claims where id in ('{base}','{other}')") == "2"
        conflict = _conflict_json(f"{key}-conflict", scope["entity"], scope["field"], [base, other])
        with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
            _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    accent = _seed_claim(db, args, "sem-accent", 1, **dict(VARIANT_A, entity="Accent"))
    i25 = _seed_claim(db, args, "sem-i25", 2, **dict(VARIANT_A, entity="i25"))
    with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{_conflict_json('sem-accent-conflict', 'Accent', VARIANT_A['field'], [accent, i25])}'::jsonb)")


def test_same_canonical_scope_same_value_missing_and_cross_run_stay_rejected(canonical_db):
    db = canonical_db
    _, args = _canonical_lease(db, "canonneg")
    claim_a = _seed_claim(db, args, "neg-a", 100, **VARIANT_A)
    claim_b = _seed_claim(db, args, "neg-b", 100, **VARIANT_B)
    agreeing = _conflict_json("neg-agree", VARIANT_A["entity"], VARIANT_A["field"], [claim_a, claim_b])
    with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{agreeing}'::jsonb)")
    missing = _conflict_json("neg-missing", VARIANT_A["entity"], VARIANT_A["field"],
                             [claim_a, "00000000-0000-4000-8000-000000000099"])
    with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{missing}'::jsonb)")
    _, args_other = _canonical_lease(db, "canonother")
    foreign = _seed_claim(db, args_other, "neg-foreign", 120, **VARIANT_A)
    crossed = _conflict_json("neg-cross", VARIANT_A["entity"], VARIANT_A["field"], [claim_a, foreign])
    with pytest.raises(AssertionError, match="must exist, share one scope, and contradict"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{crossed}'::jsonb)")
    single = json.dumps({"entity_key": VARIANT_A["entity"], "field_key": VARIANT_A["field"],
                         "claim_ids": [claim_a], "evidence_key": "neg-single", "task_key": "review"})
    with pytest.raises(AssertionError, match="at least two claims"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{single}'::jsonb)")


def test_legacy_and_untrusted_canonical_identities_fail_closed(canonical_db):
    db = canonical_db
    run_id, args = _canonical_lease(db, "canonlegacy")
    modern = _seed_claim(db, args, "legacy-modern", 100, **VARIANT_A)
    # A pre-canonical legacy row: created outside the new RPC, canonical
    # identity absent, provenance preserved verbatim, never backfilled.
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('legacy-src')}'::jsonb)")
    legacy = db.psql(
        f"insert into public.claims (run_id, entity_key, field_key, value, time_scope, geography, market, source_id, source_strength, confidence, agent, evidence_key, task_key) "
        f"values ('{run_id}', 'Toyota Corolla 2020', 'engine-power', '120'::jsonb, '{{\"year\": 2020}}'::jsonb, 'IL', 'Israel', '{source}', 'strong', 0.9, 'agent', 'legacy-claim', 'task') returning id"
    )
    conflict = _conflict_json("legacy-conflict", VARIANT_A["entity"], VARIANT_A["field"], [modern, legacy])
    with pytest.raises(AssertionError, match="require a trusted canonical scope identity"):
        _rpc_as_service(db, f"select public.create_conflict_guarded({args},'{conflict}'::jsonb)")
    assert db.psql(f"select canonical_scope_hash is null and scope_normalization_version is null from public.claims where id='{legacy}'") == "t"
    # New claims cannot skip or forge the canonical identity.
    bad_payloads = [
        {"canonical_scope_hash": None, "scope_normalization_version": None},
        {"canonical_scope_hash": "not-a-hash", "scope_normalization_version": 1},
        {"canonical_scope_hash": "a" * 64, "scope_normalization_version": 0},
    ]
    for index, overrides in enumerate(bad_payloads):
        payload = json.loads(_claim_json(f"legacy-bad-{index}", source, 5, **VARIANT_A))
        payload.update(overrides)
        with pytest.raises(AssertionError, match="trusted canonical scope identity is required"):
            _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(payload)}'::jsonb)")


def test_canonical_migration_is_rerun_safe_and_service_only(canonical_db):
    db = canonical_db
    migration = next(m for m in MIGRATIONS if "canonical_scope_conflict_identity" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    for name in ("create_claim_with_source_guarded", "create_conflict_guarded"):
        assert db.psql(f"select count(*) from pg_proc where proname='{name}'") == "1"
        signature = f"public.{name}(uuid,text,integer,text,jsonb)"
        assert not _has_execute(db, "anon", signature), signature
        assert not _has_execute(db, "authenticated", signature), signature
        assert _has_execute(db, "service_role", signature), signature


def _apply_evidence_migration(db, marker: str) -> None:
    db.psql(file=next(m for m in MIGRATIONS if marker in m.name))


def test_pre_canonical_claim_replay_upgrades_in_place_and_reaches_conflict(canonical_db):
    """Upgrade-boundary regression: a claim persisted by the pre-canonical
    release and replayed after the canonical deployment must keep its row id
    and evidence_key, gain ONLY the canonical identity, and then work with
    canonical conflict persistence."""
    db = canonical_db
    run_id, args = _canonical_lease(db, "upg")
    # STEP 1 — pre-canonical deployment: the older evidence RPC is active and
    # ignores the canonical payload fields entirely.
    _apply_evidence_migration(db, "lease_guarded_evidence_writes")
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('src-upg')}'::jsonb)")
    payload = _claim_json("upg-claim", source, 100, **VARIANT_A)
    legacy_id = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select canonical_scope_hash is null and scope_normalization_version is null from public.claims where id='{legacy_id}'") == "t"
    provenance_sql = (f"select entity_key || '|' || field_key || '|' || value::text || '|' || time_scope::text || '|' || "
                      f"geography || '|' || market || '|' || source_id::text || '|' || evidence_key || '|' || task_key || '|' || id::text "
                      f"from public.claims where run_id='{run_id}'")
    before = db.psql(provenance_sql)
    # STEP 2 — canonical deployment boundary: replay the exact same logical claim.
    _apply_evidence_migration(db, "canonical_scope_conflict_identity")
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{payload}'::jsonb)") == legacy_id
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "1"
    expected = canonical_scope_hash(canonical_scope_key(
        entity=VARIANT_A["entity"], field=VARIANT_A["field"], geography=VARIANT_A["geography"],
        market=VARIANT_A["market"], time_scope=VARIANT_A["time_scope"]))
    assert db.psql(f"select canonical_scope_hash || '|' || scope_normalization_version from public.claims where id='{legacy_id}'") == f"{expected}|{SCOPE_NORMALIZATION_VERSION}"
    assert db.psql(provenance_sql) == before
    # Exact replay after the upgrade: same id, still no duplicate or mutation.
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{payload}'::jsonb)") == legacy_id
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "1"
    assert db.psql(provenance_sql) == before
    # STEP 3 — the replay-upgraded claim persists one canonical conflict with a
    # formatting-variant contradiction.
    other = _seed_claim(db, args, "upg-variant", 120, **VARIANT_B)
    conflict = _conflict_json("upg-conflict", VARIANT_A["entity"], VARIANT_A["field"], [legacy_id, other])
    assert _rpc_as_service(db, f"select id from public.create_conflict_guarded({args},'{conflict}'::jsonb)")


def test_mismatched_and_partial_canonical_replays_fail_closed(canonical_db):
    db = canonical_db
    run_id, args = _canonical_lease(db, "upgneg")
    _apply_evidence_migration(db, "lease_guarded_evidence_writes")
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('src-upgneg')}'::jsonb)")
    payload = _claim_json("upgneg-claim", source, 100, **VARIANT_A)
    legacy_id = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{payload}'::jsonb)")
    _apply_evidence_migration(db, "canonical_scope_conflict_identity")
    # Same evidence_key, different original payload: never silently upgraded.
    tampered = json.loads(payload)
    tampered["value"] = 999
    with pytest.raises(AssertionError, match="does not match the stored claim"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(tampered)}'::jsonb)")
    assert db.psql(f"select canonical_scope_hash is null from public.claims where id='{legacy_id}'") == "t"
    # Same evidence_key, different source: existing idempotency guard holds.
    source_2 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('src-upgneg-2')}'::jsonb)")
    moved = json.loads(payload)
    moved["source_id"] = source_2
    with pytest.raises(AssertionError, match="idempotency key belongs to a different source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(moved)}'::jsonb)")
    # The exact replay upgrades; afterwards a differing canonical identity or
    # normalization version is rejected instead of overwritten.
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{payload}'::jsonb)") == legacy_id
    forged = json.loads(payload)
    forged["canonical_scope_hash"] = "0" * 64
    with pytest.raises(AssertionError, match="canonical scope identity mismatch"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(forged)}'::jsonb)")
    bumped = json.loads(payload)
    bumped["scope_normalization_version"] = 2
    with pytest.raises(AssertionError, match="canonical scope identity mismatch"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(bumped)}'::jsonb)")
    assert db.psql(f"select scope_normalization_version from public.claims where id='{legacy_id}'") == str(SCOPE_NORMALIZATION_VERSION)
    # Half-populated canonical state is invalid, never guessed or repaired.
    partial_payload = _claim_json("upgneg-partial", source, 100, **VARIANT_A)
    partial_hash = json.loads(partial_payload)["canonical_scope_hash"]
    db.psql(f"insert into public.claims (run_id, entity_key, field_key, value, time_scope, geography, market, source_id, source_strength, confidence, agent, evidence_key, task_key, canonical_scope_hash) "
            f"values ('{run_id}', '{VARIANT_A['entity']}', '{VARIANT_A['field']}', '100'::jsonb, '{{\"year\": 2020}}'::jsonb, '{VARIANT_A['geography']}', '{VARIANT_A['market']}', '{source}', 'strong', 0.9, 'agent', 'upgneg-partial', 'task', '{partial_hash}')")
    with pytest.raises(AssertionError, match="canonical scope state is invalid"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{partial_payload}'::jsonb)")


# --- migration 20260828000200 (source evidence fragments) ---

@pytest.fixture
def fragment_db(db):
    """The shared module DB with the evidence-fragment migration guaranteed
    current, independent of earlier rerun-safety tests re-applying older
    evidence migrations."""
    migration = next(m for m in MIGRATIONS if "source_evidence_fragments" in m.name)
    db.psql(file=migration)
    return db


def _fragment_json(source_id: str, text: str, *, key: str, task: str = "task",
                   index: int = 0, content_hash: str | None = None) -> str:
    # The content hash is produced by the trusted backend helper the worker
    # itself uses, so PostgreSQL validates the real production value.
    return json.dumps({"source_id": source_id, "fragment_text": text,
                       "content_hash": content_hash or fragment_content_hash(text),
                       "fragment_index": index, "task_key": task, "evidence_key": key})


FRAGMENT_TEXT = "The 2020 model was rated at 1798 cc by the official importer."


def test_evidence_fragment_persists_replays_and_stays_bound_to_one_source_and_run(fragment_db):
    db = fragment_db
    lease_a, lease_b = _evidence_fixture(db, "frag")
    run_a, worker_a, attempt_a, token_a, _ = lease_a
    run_b, worker_b, attempt_b, token_b, _ = lease_b
    args = f"'{run_a}','{worker_a}',{attempt_a},'{token_a}'"
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    source_1 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-src-1')}'::jsonb)")
    source_2 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-src-2')}'::jsonb)")
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args_b},'{_source_json('frag-src-b')}'::jsonb)")

    # A valid source-bound fragment persists with complete provenance.
    payload = _fragment_json(source_1, FRAGMENT_TEXT, key="frag-1")
    stored = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)")
    assert db.psql(
        f"select run_id, source_id, task_key, fragment_index, fragment_text "
        f"from public.source_evidence_fragments where id='{stored}'"
    ) == f"{run_a}|{source_1}|task|0|{FRAGMENT_TEXT}"

    # An exact replay returns the same durable row instead of duplicating it.
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)") == stored
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_a}'") == "1"

    # One source can never inherit another source's fragment relationship.
    hijack = _fragment_json(source_2, FRAGMENT_TEXT, key="frag-1")
    with pytest.raises(AssertionError, match="evidence fragment idempotency conflict"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{hijack}'::jsonb)")
    own = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_2, FRAGMENT_TEXT, key='frag-2')}'::jsonb)")
    assert db.psql(f"select source_id from public.source_evidence_fragments where id='{own}'") == source_2

    # A source belonging to another run is rejected and nothing is written.
    cross = _fragment_json(source_b, FRAGMENT_TEXT, key="frag-cross")
    with pytest.raises(AssertionError, match="invalid evidence fragment source"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{cross}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where evidence_key='frag-cross'") == "0"

    # Multiple fragments of one source read back in a deterministic order, and
    # the source metadata itself is untouched by any of this.
    for index, sentence in enumerate(("Second recorded sentence.", "Third recorded sentence."), start=1):
        _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_1, sentence, key=f'frag-1-{index}', index=index)}'::jsonb)")
    assert db.psql(
        f"select string_agg(fragment_index::text, ',' order by fragment_index, content_hash) "
        f"from public.source_evidence_fragments where source_id='{source_1}'"
    ) == "0,1,2"
    assert db.psql(
        f"select url, title, domain, evidence_key, task_key from public.sources where id='{source_1}'"
    ) == f"https://example.test/frag-src-1|title|example.test|frag-src-1|task"


def test_evidence_fragment_rejects_stale_leases_unsafe_text_and_every_hard_bound(fragment_db):
    db = fragment_db
    lease, _ = _evidence_fixture(db, "fragguard")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source_id = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-guard-src')}'::jsonb)")

    # A stale, wrong or expired lease writes nothing at all.
    never = _fragment_json(source_id, "Never written.", key="frag-never")
    bad_leases = [("wrong", attempt, token, False), (worker, str(int(attempt) + 1), token, False),
                  (worker, attempt, "wrong", False), (worker, attempt, token, True)]
    for bad_worker, bad_attempt, bad_token, expire in bad_leases:
        if expire:
            db.psql(f"update public.runs set lease_expires_at=now()-interval '1 second' where id='{run_id}'")
        bad = f"'{run_id}','{bad_worker}',{bad_attempt},'{bad_token}'"
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({bad},'{never}'::jsonb)")
    db.psql(f"update public.runs set lease_expires_at=now()+interval '5 minutes' where id='{run_id}'")

    rejected = [
        # Over the hard character bound; the durable boundary rejects, never truncates.
        (_fragment_json(source_id, "w" * 401, key="frag-long"), "exceeds the durable bound"),
        # Empty and whitespace-only text.
        (_fragment_json(source_id, "", key="frag-empty"), "must not be empty"),
        (_fragment_json(source_id, "   ", key="frag-blank"), "must not be empty"),
        # Credential and hidden-reasoning markers.
        (_fragment_json(source_id, "api_key=ABCDEFGHIJKLMNOP", key="frag-key"), "unsafe evidence"),
        (_fragment_json(source_id, "authorization: Token abcdef", key="frag-auth"), "unsafe evidence"),
        (_fragment_json(source_id, "the lease_token was printed", key="frag-lease"), "unsafe evidence"),
        (_fragment_json(source_id, "-----begin certificate-----", key="frag-pem"), "unsafe evidence"),
        (_fragment_json(source_id, "captured chain of thought", key="frag-cot"), "unsafe evidence"),
        # A hash that does not describe the durable text.
        (_fragment_json(source_id, FRAGMENT_TEXT, key="frag-hash", content_hash="0" * 64),
         "content hash does not match"),
        (_fragment_json(source_id, FRAGMENT_TEXT, key="frag-shape", content_hash="not-a-hash"),
         "content hash does not match"),
        # Provenance is mandatory.
        (_fragment_json(source_id, FRAGMENT_TEXT, key="", task=""), "evidence_key and task_key are required"),
        # The index bound mirrors the per-source count limit.
        (_fragment_json(source_id, FRAGMENT_TEXT, key="frag-index", index=4), "fragment_index is outside"),
    ]
    for payload, message in rejected:
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"

    # The per-source count limit holds after four real fragments.
    for index in range(4):
        _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_id, f'Bounded sentence number {index}.', key=f'frag-cap-{index}', index=index)}'::jsonb)")
    with pytest.raises(AssertionError, match="count limit reached"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{_fragment_json(source_id, 'One too many.', key='frag-cap-x')}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{source_id}'") == "4"
    # A retry of an already durable fragment still succeeds with the quota full:
    # replay is resolved before the budget is consulted and consumes nothing.
    full_replay = _fragment_json(source_id, "Bounded sentence number 0.", key="frag-cap-0")
    replayed = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{full_replay}'::jsonb)")
    assert replayed == db.psql(f"select id from public.source_evidence_fragments where run_id='{run_id}' and evidence_key='frag-cap-0'")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{source_id}'") == "4"

    # The per-source character budget holds independently of the count.
    budget_source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-budget-src')}'::jsonb)")
    for index in range(3):
        _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(budget_source, chr(97 + index) * 399, key=f'frag-budget-{index}', index=index)}'::jsonb)")
    with pytest.raises(AssertionError, match="character budget exhausted"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{_fragment_json(budget_source, 'z' * 399, key='frag-budget-3', index=3)}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{budget_source}'") == "3"


def test_evidence_fragment_surface_is_service_only_append_only_and_rerun_safe(fragment_db):
    db = fragment_db
    lease, _ = _evidence_fixture(db, "fragacl")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source_id = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-acl-src')}'::jsonb)")
    stored = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_id, FRAGMENT_TEXT, key='frag-acl')}'::jsonb)")

    # Browser roles can neither execute the write RPC nor touch the relation.
    for role in ("anon", "authenticated"):
        with pytest.raises(AssertionError, match="permission denied"):
            db.psql(f"set role {role}; select public.record_evidence_fragment_guarded({args},'{{}}'::jsonb)")
        assert db.psql(
            "select count(*) from information_schema.role_table_grants "
            f"where grantee='{role}' and table_name='source_evidence_fragments'"
        ) == "0"
    assert db.psql(
        "select relrowsecurity from pg_class where relname='source_evidence_fragments'"
    ) == "t"
    assert db.psql(
        "select count(*) from pg_policies where tablename='source_evidence_fragments'"
    ) == "0"

    # Captured evidence is an audit record: no role may rewrite or remove it.
    for mutation in (f"update public.source_evidence_fragments set fragment_text='x' where id='{stored}'",
                     f"delete from public.source_evidence_fragments where id='{stored}'"):
        with pytest.raises(AssertionError, match="append-only"):
            db.psql(mutation)
        with pytest.raises(AssertionError, match="append-only|permission denied"):
            _rpc_as_service(db, mutation)
    assert db.psql(f"select fragment_text from public.source_evidence_fragments where id='{stored}'") == FRAGMENT_TEXT

    # Rerun-safe, and the fragment written before the rerun survives untouched.
    migration = next(m for m in MIGRATIONS if "source_evidence_fragments" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql("select count(*) from pg_proc where proname='record_evidence_fragment_guarded'") == "1"
    assert db.psql(f"select fragment_text from public.source_evidence_fragments where id='{stored}'") == FRAGMENT_TEXT


def test_legacy_sources_and_claims_stay_valid_without_any_fragment(fragment_db):
    """No historical row is backfilled, and a source with no grounding context
    is distinguishable from one with fragments rather than fabricated."""
    db = fragment_db
    lease, _ = _evidence_fixture(db, "fraglegacy")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    legacy = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-legacy-src')}'::jsonb)")
    grounded = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-grounded-src')}'::jsonb)")
    claim_id = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('frag-legacy-claim', legacy, 100)}'::jsonb)")
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(grounded, FRAGMENT_TEXT, key='frag-grounded')}'::jsonb)")
    assert claim_id
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{legacy}'") == "0"
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{grounded}'") == "1"
    # The internal read path a future verifier uses: only the requested run and
    # sources, in a deterministic order.
    assert db.psql(
        f"select string_agg(source_id::text, ',' order by source_id, fragment_index, content_hash) "
        f"from public.source_evidence_fragments "
        f"where run_id='{run_id}' and source_id in ('{legacy}','{grounded}')"
    ) == grounded


def _fragment_call(db, args: str, payload: str, *, hold_seconds: float = 0.0) -> str:
    """One service-role transaction that calls the RPC and optionally keeps the
    per-source lock afterwards.  Each db.psql is its own psql process, hence its
    own session and transaction, so two of these genuinely contend."""
    hold = f"select pg_sleep({hold_seconds}); " if hold_seconds else ""
    db.psql(f"set role service_role; begin; "
            f"select public.record_evidence_fragment_guarded({args},'{payload}'::jsonb); "
            f"{hold}commit; reset role")
    return "admitted"


def _fragment_outcome(db, args: str, payload: str, *, hold_seconds: float = 0.0) -> str:
    try:
        return _fragment_call(db, args, payload, hold_seconds=hold_seconds)
    except AssertionError as exc:
        return str(exc)


def test_evidence_fragment_replay_must_be_identical_and_carry_the_source_task(fragment_db):
    """The durable replay invariant and the task -> source -> fragment lineage."""
    db = fragment_db
    lease, _ = _evidence_fixture(db, "fragident")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source_a = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-ident-a', task='task-a')}'::jsonb)")
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('frag-ident-b', task='task-b')}'::jsonb)")

    # A fragment may only be attributed to the task that captured its source;
    # belonging to the same run is not enough, and nothing is written.
    wrong_task = _fragment_json(source_a, FRAGMENT_TEXT, key="frag-wrong-task", task="task-b")
    with pytest.raises(AssertionError, match="evidence fragment task provenance mismatch"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{wrong_task}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"

    stored = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_a, FRAGMENT_TEXT, key='frag-ident', task='task-a')}'::jsonb)")
    # An identical replay is the same durable row.
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_a, FRAGMENT_TEXT, key='frag-ident', task='task-a')}'::jsonb)") == stored

    # Reusing that evidence_key while changing any identity field fails closed
    # instead of being silently accepted as a replay.
    conflicts = [
        # different text (and therefore a different, still self-consistent hash)
        _fragment_json(source_a, "A completely different durable sentence.", key="frag-ident", task="task-a"),
        # different source of the same run, which also carries a different task
        _fragment_json(source_b, FRAGMENT_TEXT, key="frag-ident", task="task-b"),
    ]
    for payload in conflicts:
        with pytest.raises(AssertionError, match="evidence fragment idempotency conflict|task provenance mismatch"):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "1"
    assert db.psql(
        f"select fragment_text, task_key, fragment_index from public.source_evidence_fragments where id='{stored}'"
    ) == f"{FRAGMENT_TEXT}|task-a|0"

    # A replay at a different tool position returns the row and never rewrites
    # the stored index: position is outside the fragment's logical identity.
    moved = _fragment_json(source_a, FRAGMENT_TEXT, key="frag-ident", task="task-a", index=3)
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{moved}'::jsonb)") == stored
    assert db.psql(f"select fragment_index from public.source_evidence_fragments where id='{stored}'") == "0"


def test_concurrent_writers_for_one_source_cannot_exceed_the_durable_fragment_limits(fragment_db):
    """The per-source quota is a hard durable limit, not a best-effort check.

    Each attempt runs in its own psql session/transaction.  The holder takes the
    source row lock inside the RPC and keeps it after inserting, so the
    challenger provably blocks at the same admission point instead of reading a
    stale pre-insert count and being admitted alongside it.
    """
    import concurrent.futures
    import time

    db = fragment_db
    lease, _ = _evidence_fixture(db, "fragrace")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    def race(source_key: str, seeded: list[str], holder: str, challenger: str) -> dict:
        source_id = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json(source_key)}'::jsonb)")
        for index, seed in enumerate(seeded):
            _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source_id, seed, key=f'{source_key}-seed-{index}', index=index)}'::jsonb)")
        outcome: dict = {"source_id": source_id}

        def hold():
            outcome["holder"] = _fragment_outcome(
                db, args, _fragment_json(source_id, holder, key=f"{source_key}-hold", index=len(seeded)),
                hold_seconds=1.2)

        def challenge():
            time.sleep(0.4)  # the holder owns the source lock by now
            started = time.monotonic()
            outcome["challenger"] = _fragment_outcome(
                db, args, _fragment_json(source_id, challenger, key=f"{source_key}-chal", index=len(seeded)))
            outcome["waited"] = time.monotonic() - started

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda call: call(), (hold, challenge)))
        return outcome

    # Count boundary: three fragments already durable, two concurrent fourths.
    counted = race("frag-race-count", [f"Seeded race sentence {index}." for index in range(3)],
                   "Fourth race sentence.", "Fifth race sentence.")
    assert counted["holder"] == "admitted"
    assert "count limit reached" in counted["challenger"]
    assert counted["waited"] >= 0.5, counted["waited"]  # it blocked on the source lock
    assert db.psql(f"select count(*) from public.source_evidence_fragments where source_id='{counted['source_id']}'") == "4"

    # Character-budget boundary: 798 durable characters, two concurrent 399s of
    # which only the first can fit inside the 1200-character source budget.
    budget = race("frag-race-budget", ["a" * 399, "b" * 399], "c" * 399, "d" * 399)
    assert budget["holder"] == "admitted"
    assert "character budget exhausted" in budget["challenger"]
    assert budget["waited"] >= 0.5, budget["waited"]
    assert db.psql(
        f"select count(*), coalesce(sum(char_length(fragment_text)), 0) "
        f"from public.source_evidence_fragments where source_id='{budget['source_id']}'"
    ) == "3|1197"

    # Neither race left the relation over its documented hard limits anywhere.
    assert db.psql(
        "select count(*) from (select source_id, count(*) as rows, "
        "sum(char_length(fragment_text)) as chars from public.source_evidence_fragments "
        "group by source_id) as per_source where rows > 4 or chars > 1200"
    ) == "0"


# --- migration 20260902000100 (R3 versioned, located evidence) ---

@pytest.fixture
def r3_db(db):
    """The shared module DB with the R3 evidence migration guaranteed current,
    independent of earlier rerun-safety tests re-applying older evidence
    migrations."""
    for name in ("source_evidence_fragments", "r3_versioned_focused_evidence"):
        db.psql(file=next(m for m in MIGRATIONS if name in m.name))
    return db


R3_VERSION = ("dataset_version", "2026.08.1")
R3_LOCATOR = record_field_locator("rec-1", ("engine_displacement_cc",)).locator_key
R3_SPAN = document_span_locator("doc-1", 612, 680, section="Engine specifications").locator_key
R3_TEXT = "model_name=Fixture Hatch; model_year=2020; engine_displacement_cc=1798"


def _r3_source_json(key: str, *, task: str = "task", kind: str | None = R3_VERSION[0],
                    identifier: str | None = R3_VERSION[1],
                    tool_operation: str | None = None) -> str:
    payload = json.loads(_source_json(key, task=task))
    if tool_operation is not None:
        payload["tool_operation"] = tool_operation
    payload.update(source_version_kind=kind, source_version_id=identifier)
    return json.dumps(payload)


def _r3_claim_json(key: str, source_id: str, value, *, locator: str | None = R3_LOCATOR,
                   unit: str | None = "cc", field: str = "engine_displacement_cc",
                   entity: str = "entity", market: str | None = "IL",
                   geography: str | None = None, time_scope: dict | None = None,
                   identity: dict | None = None) -> str:
    payload = json.loads(_claim_json(key, source_id, 0, field=field, entity=entity,
                                     market=market, geography=geography,
                                     time_scope=time_scope))
    payload.update(value=value, unit=unit, evidence_locator=locator)
    # R4 `identity_scope`, stored exactly as the trusted Evidence Board stores
    # it: already NORMALIZED. The promotion trigger compares it to the
    # candidate's own text under the same normalization, so a test that stored
    # raw text here would be testing a shape production never writes.
    if identity is not None:
        payload["identity_scope"] = {name: normalize_field_key(str(text))
                                     for name, text in identity.items()}
    return json.dumps(payload)


def _r3_fragment_json(source_id: str, text: str = R3_TEXT, *, key: str, task: str = "task",
                      index: int = 0, kind: str | None = "structured_projection",
                      locator: str | None = R3_LOCATOR) -> str:
    payload = json.loads(_fragment_json(source_id, text, key=key, task=task, index=index))
    payload.update(fragment_type=kind, locator_key=locator)
    return json.dumps(payload)


def test_r3_evidence_persists_with_its_version_locator_and_fragment_type(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-src-1')}'::jsonb)")
    assert db.psql(f"select source_version_kind, source_version_id from public.sources where id='{source}'") \
        == "dataset_version|2026.08.1"

    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key='r3-frag-1')}'::jsonb)")
    assert db.psql(f"select fragment_type, locator_key from public.source_evidence_fragments where id='{fragment}'") \
        == f"structured_projection|{R3_LOCATOR}"

    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-claim-1', source, 1798)}'::jsonb)")
    assert db.psql(f"select value, unit, evidence_locator from public.claims where id='{claim}'") \
        == f"1798|cc|{R3_LOCATOR}"

    # An exact replay of the whole bundle returns the same durable rows.
    assert _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-src-1')}'::jsonb)") == source
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key='r3-frag-1')}'::jsonb)") == fragment
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-claim-1', source, 1798)}'::jsonb)") == claim
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "1"
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "1"

    # Identical TEXT read from a different locator is different evidence: the
    # backend folds the locator into evidence_key, so both rows survive.
    other = record_field_locator("rec-2", ("engine_displacement_cc",)).locator_key
    twin = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key='r3-frag-2', index=1, locator=other)}'::jsonb)")
    assert twin != fragment
    assert db.psql(
        f"select count(*), count(distinct content_hash), count(distinct locator_key) "
        f"from public.source_evidence_fragments where source_id='{source}'") == "2|1|2"

    # A new source VERSION is new provenance, never a merge into the old row.
    republished = json.loads(_r3_source_json("r3-src-1", identifier="2026.09.1"))
    republished["evidence_key"] = "r3-src-1-v2"   # the backend key includes the version
    second = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(republished)}'::jsonb)")
    assert second != source
    assert db.psql(f"select count(distinct source_version_id) from public.sources where run_id='{run_id}'") == "2"
    # Reusing ONE evidence_key across two versions fails closed instead.
    with pytest.raises(AssertionError, match="source version identity conflict"):
        _rpc_as_service(db, f"select public.upsert_source_guarded({args},'{_r3_source_json('r3-src-1', identifier='2026.10.1')}'::jsonb)")


def test_r3_rpcs_reject_every_incomplete_or_malformed_contract(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3guard")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    # Source version: all-or-nothing, closed kind set, bounded identifier.
    for payload, message in (
            (_r3_source_json("r3-half-a", identifier=None), "requires both a kind and an identifier"),
            (_r3_source_json("r3-half-b", kind=None), "requires both a kind and an identifier"),
            (_r3_source_json("r3-kind", kind="retrieved_at"), "unknown or malformed source version"),
            (_r3_source_json("r3-long", identifier="v" * 129), "unknown or malformed source version")):
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.upsert_source_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.sources where run_id='{run_id}'") == "0"

    versioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-guard-src')}'::jsonb)")
    legacy = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('r3-legacy-src')}'::jsonb)")

    # Fragment focus provenance: all-or-nothing, closed type set, bounded locator.
    for payload, message in (
            (_r3_fragment_json(versioned, key="r3-f-a", locator=None), "requires both a type and a locator"),
            (_r3_fragment_json(versioned, key="r3-f-b", kind=None), "requires both a type and a locator"),
            (_r3_fragment_json(versioned, key="r3-f-c", kind="verbatim_quote"), "unknown fragment type or oversized locator"),
            (_r3_fragment_json(versioned, key="r3-f-d", locator="l" * 801), "unknown fragment type or oversized locator"),
            # R3 evidence may only rest on a source whose version was captured.
            (_r3_fragment_json(legacy, key="r3-f-e"), "requires a versioned source")):
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"

    # A located fact states its unit, rests on a versioned source, and its
    # locator is bounded.
    for payload, message in (
            (_r3_claim_json("r3-c-a", versioned, 1798, unit=None), "requires an explicit unit"),
            (_r3_claim_json("r3-c-b", versioned, 1798.5, unit=""), "requires an explicit unit"),
            (_r3_claim_json("r3-c-c", versioned, 1798, locator="l" * 801), "evidence locator exceeds the durable bound"),
            (_r3_claim_json("r3-c-d", legacy, 1798), "requires a versioned source")):
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "0"

    # A replay may never move a stored fact or fragment to a different place.
    # (The fragment comes first: a located claim must be backed by focused
    # evidence at the same locator of the same source.)
    stored = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, key='r3-f-ok')}'::jsonb)")
    relocated = _r3_fragment_json(versioned, key="r3-f-ok", locator=R3_SPAN, kind="verbatim_excerpt")
    with pytest.raises(AssertionError, match="evidence fragment idempotency conflict"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{relocated}'::jsonb)")
    assert db.psql(f"select locator_key from public.source_evidence_fragments where id='{stored}'") == R3_LOCATOR
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-c-ok', versioned, 1798)}'::jsonb)")
    moved = _r3_claim_json("r3-c-ok", versioned, 1798,
                           locator=record_field_locator("rec-9", ("engine_displacement_cc",)).locator_key)
    with pytest.raises(AssertionError, match="must be backed by focused evidence"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{moved}'::jsonb)")
    # Backed by a fragment at that other locator, the replay is STILL refused:
    # the stored fact's own locator is part of its identity.
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, key='r3-f-moved', index=1, locator=record_field_locator('rec-9', ('engine_displacement_cc',)).locator_key)}'::jsonb)")
    with pytest.raises(AssertionError, match="claim evidence locator mismatch"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{moved}'::jsonb)")
    assert db.psql(f"select evidence_locator from public.claims where id='{claim}'") == R3_LOCATOR


def test_r3_preserves_every_pre_existing_evidence_guarantee(r3_db):
    """The replaced RPCs keep the lease, safety, lineage and quota rules."""
    db = r3_db
    lease, other = _evidence_fixture(db, "r3keep")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-keep-src')}'::jsonb)")
    foreign = _rpc_as_service(db, f"select id from public.upsert_source_guarded('{other[0]}','{other[1]}',{other[2]},'{other[3]}','{_r3_source_json('r3-keep-foreign')}'::jsonb)")

    # A stale, wrong or expired lease still writes nothing at all.
    never = _r3_fragment_json(source, key="r3-keep-never")
    for bad_worker, bad_attempt, bad_token, expire in (
            ("wrong", attempt, token, False), (worker, str(int(attempt) + 1), token, False),
            (worker, attempt, "wrong", False), (worker, attempt, token, True)):
        if expire:
            db.psql(f"update public.runs set lease_expires_at=now()-interval '1 second' where id='{run_id}'")
        bad = f"'{run_id}','{bad_worker}',{bad_attempt},'{bad_token}'"
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({bad},'{never}'::jsonb)")
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            _rpc_as_service(db, f"select public.upsert_source_guarded({bad},'{_r3_source_json('r3-keep-never')}'::jsonb)")
    db.psql(f"update public.runs set lease_expires_at=now()+interval '5 minutes' where id='{run_id}'")

    # Cross-run sources, task lineage, unsafe text, empty/oversized text, the
    # index bound and the hash recomputation are all still enforced.
    for payload, message in (
            (_r3_fragment_json(foreign, key="r3-keep-cross"), "invalid evidence fragment source"),
            (_r3_fragment_json(source, key="r3-keep-task", task="other-task"), "task provenance mismatch"),
            (_r3_fragment_json(source, "api_key=ABCDEFGHIJKLMNOP", key="r3-keep-secret"), "unsafe evidence"),
            (_r3_fragment_json(source, "   ", key="r3-keep-blank"), "must not be empty"),
            (_r3_fragment_json(source, "w" * 401, key="r3-keep-long"), "exceeds the durable bound"),
            (_r3_fragment_json(source, key="r3-keep-index", index=4), "fragment_index is outside the durable bound"),
            (json.dumps({**json.loads(_r3_fragment_json(source, key="r3-keep-hash")),
                         "content_hash": "0" * 64}), "content hash does not match")):
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{payload}'::jsonb)")
    with pytest.raises(AssertionError, match="unsafe evidence payload rejected"):
        _rpc_as_service(db, f"select public.upsert_source_guarded({args},'{json.dumps({**json.loads(_r3_source_json('r3-keep-unsafe')), 'api_key': 'x'})}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"

    # The per-source quota and the atomic claim link both survive.
    for index in range(4):
        _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, f'projection {index}', key=f'r3-keep-{index}', index=index)}'::jsonb)")
    with pytest.raises(AssertionError, match="count limit reached"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, 'one too many', key='r3-keep-fifth')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-keep-claim', source, 1798)}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_claim_links where claim_id='{claim}' and source_id='{source}'") == "1"
    # The trusted canonical scope identity is still mandatory.
    without_scope = json.loads(_r3_claim_json("r3-keep-noscope", source, 1798))
    without_scope.pop("canonical_scope_hash")
    with pytest.raises(AssertionError, match="trusted canonical scope identity is required"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(without_scope)}'::jsonb)")


def test_r3_legacy_rows_stay_valid_readable_and_never_backfilled(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3legacy")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    # A pre-R3 source, fragment and claim: no version, no locator, no type.
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('r3-legacy')}'::jsonb)")
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source, FRAGMENT_TEXT, key='r3-legacy-frag')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('r3-legacy-claim', source, 100)}'::jsonb)")
    assert db.psql(f"select coalesce(source_version_kind,'-'), coalesce(source_version_id,'-') from public.sources where id='{source}'") == "-|-"
    assert db.psql(f"select coalesce(fragment_type,'-'), coalesce(locator_key,'-') from public.source_evidence_fragments where id='{fragment}'") == "-|-"
    assert db.psql(f"select coalesce(evidence_locator,'-') from public.claims where id='{claim}'") == "-"
    # A legacy numeric claim with no unit is never retro-invalidated.
    assert db.psql(f"select value, coalesce(unit,'-') from public.claims where id='{claim}'") == "100|-"
    # Replaying them keeps working and still backfills nothing.
    assert _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('r3-legacy')}'::jsonb)") == source
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(source, FRAGMENT_TEXT, key='r3-legacy-frag')}'::jsonb)") == fragment
    assert db.psql(f"select count(*) from public.sources where run_id='{run_id}' and source_version_kind is not null") == "0"


def test_r3_surface_stays_service_only_append_only_and_rerun_safe(r3_db):
    db = r3_db
    migration = next(m for m in MIGRATIONS if "r3_versioned_focused_evidence" in m.name)
    lease, _ = _evidence_fixture(db, "r3acl")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-acl-src')}'::jsonb)")
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key='r3-acl-frag')}'::jsonb)")

    # The new columns never become browser payload and never become mutable.
    for role in ("anon", "authenticated"):
        assert db.psql(
            f"select coalesce(bool_or(has_column_privilege('{role}', 'public.source_evidence_fragments', "
            f"column_name, 'SELECT')), false) from information_schema.columns "
            f"where table_schema='public' and table_name='source_evidence_fragments'") == "f"
        for function in ("upsert_source_guarded", "create_claim_with_source_guarded",
                         "record_evidence_fragment_guarded"):
            assert db.psql(
                f"select has_function_privilege('{role}', "
                f"'public.{function}(uuid,text,integer,text,jsonb)', 'EXECUTE')") == "f"
        assert db.psql(f"select has_function_privilege('{role}', "
                       f"'public.upsert_source_guarded(uuid,text,integer,text,jsonb)', 'EXECUTE')") == "f"
    assert db.psql("select relrowsecurity from pg_class where oid='public.source_evidence_fragments'::regclass") == "t"
    assert db.psql("select count(*) from pg_policies where schemaname='public' and tablename='source_evidence_fragments'") == "0"
    with pytest.raises(AssertionError, match="append-only"):
        db.psql(f"update public.source_evidence_fragments set locator_key='rewritten' where id='{fragment}'")
    with pytest.raises(AssertionError, match="append-only"):
        db.psql(f"delete from public.source_evidence_fragments where id='{fragment}'")

    # Re-applying the migration preserves every row and every constraint.
    before = db.psql("select count(*) from public.sources") + "|" + db.psql("select count(*) from public.source_evidence_fragments")
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql("select count(*) from public.sources") + "|" + db.psql("select count(*) from public.source_evidence_fragments") == before
    assert db.psql(f"select source_version_id from public.sources where id='{source}'") == "2026.08.1"
    assert db.psql(f"select locator_key from public.source_evidence_fragments where id='{fragment}'") == R3_LOCATOR
    for name in ("sources_version_pairing", "claims_evidence_locator_canonical",
                 "source_evidence_fragments_focus_pairing"):
        assert db.psql(f"select count(*) from pg_constraint where conname='{name}'") == "1"
    assert db.psql("select count(*) from pg_constraint where conname='claims_evidence_locator_bounded'") == "0"
    # The table constraints themselves reject a half-specified row written
    # around the RPC, so the durable shape holds even for a direct insert.
    with pytest.raises(AssertionError, match="sources_version_pairing"):
        db.psql(f"insert into public.sources(run_id, agent, url, title, domain, source_type, "
                f"source_strength, query, tool_operation, evidence_key, task_key, source_version_kind) "
                f"values ('{run_id}','a','u','t','d','primary','strong','q','op','r3-direct','task','dataset_version')")


def test_r3_rpcs_enforce_kind_specific_version_identifiers(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3kinds")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    rejected = (("content_sha256", "abc"), ("content_sha256", "A" * 64), ("content_sha256", "a" * 63),
                ("git_commit", "zzzzzzz"), ("git_commit", "abcdef"), ("dataset_version", "-x"),
                ("dataset_version", "v" * 129), ("document_revision", "rev 7"))
    for index, (kind, identifier) in enumerate(rejected):
        with pytest.raises(AssertionError, match="unknown or malformed source version"):
            _rpc_as_service(db, f"select public.upsert_source_guarded({args},'{_r3_source_json(f'r3-kind-{index}', kind=kind, identifier=identifier)}'::jsonb)")
    assert db.psql(f"select count(*) from public.sources where run_id='{run_id}'") == "0"
    accepted = (("content_sha256", "a" * 64), ("git_commit", "abcdef0"), ("git_commit", "f" * 64),
                ("dataset_version", "2026.08.1"), ("dataset_version", "v1:x+y"), ("document_revision", "rev-7"))
    for index, (kind, identifier) in enumerate(accepted):
        assert _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json(f'r3-kind-ok-{index}', kind=kind, identifier=identifier)}'::jsonb)")
    assert db.psql(f"select count(*) from public.sources where run_id='{run_id}'") == str(len(accepted))


R3_MALFORMED_LOCATORS = (
    '["record_field", "rec-1", ["engine_displacement_cc"], null, null, null]',  # not canonical
    "not json", "[]", '{"kind": "record_field"}',
    '["record_field","rec-1",["$..price"],null,null,null]',
    '["record_field","rec-1",["a[0]"],null,null,null]',
    '["record_field","rec-1",["*"],null,null,null]',
    '["record_field","rec-1",[],null,null,null]',
    '["record_field","rec-1",["a","b","c","d","e","f","g"],null,null,null]',
    '["record_field","rec-1",["a"],"section",null,null]',
    '["record_field","rec-1",["a"],null,0,5]',
    '["record_field","rec-1",["a"],null,null,null,1]',
    '["document_span","doc-1",["x"],null,0,5]',
    '["document_span","doc-1",[],null,0,5.0]',
    '["document_span","doc-1",[],null,0,1e3]',
    '["document_span","doc-1",[],null,5,5]',
    '["document_span","doc-1",[],null,0,401]',
    '["document_span","doc-1",[],null,-1,5]',
    '["document_span","doc-1",[],"a  b",0,5]',
    '["made_up","rec-1",["a"],null,null,null]',
    '["record_field","rec 1",["a"],null,null,null]',
)


def test_r3_rpcs_reject_non_json_non_canonical_and_malformed_locators(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3loc")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    versioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-loc-src')}'::jsonb)")
    for index, locator in enumerate(R3_MALFORMED_LOCATORS):
        kind = "verbatim_excerpt" if locator.startswith('["document_span"') else "structured_projection"
        with pytest.raises(AssertionError, match="locator is not a canonical bounded location"):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, key=f'r3-loc-f-{index}', kind=kind, locator=locator)}'::jsonb)")
        with pytest.raises(AssertionError, match="evidence locator is not a canonical bounded location"):
            _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_r3_claim_json(f'r3-loc-c-{index}', versioned, 1798, locator=locator)}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "0"
    # The canonical forms of both shapes, including a non-ASCII section, pass.
    hebrew = document_span_locator("doc-1", 612, 680, section='Engine "specs" / מנוע').locator_key
    for index, (kind, locator) in enumerate((("structured_projection", R3_LOCATOR),
                                             ("verbatim_excerpt", R3_SPAN), ("verbatim_excerpt", hebrew))):
        assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, f'text {index}', key=f'r3-loc-ok-{index}', index=index, kind=kind, locator=locator)}'::jsonb)")
    assert db.psql(f"select locator_key from public.source_evidence_fragments where run_id='{run_id}' and evidence_key='r3-loc-ok-2'") == hebrew


def test_r3_rpcs_reject_fragment_type_locator_kind_mismatch(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3focus")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    versioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-focus-src')}'::jsonb)")
    for kind, locator in (("verbatim_excerpt", R3_LOCATOR), ("structured_projection", R3_SPAN)):
        with pytest.raises(AssertionError, match="fragment type does not match the locator kind"):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, key='r3-focus-bad', kind=kind, locator=locator)}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(versioned, key='r3-focus-ok', kind='verbatim_excerpt', locator=R3_SPAN)}'::jsonb)")


def test_r3_located_claim_must_be_backed_by_focused_evidence_of_its_own_source(r3_db):
    db = r3_db
    lease_a, lease_b = _evidence_fixture(db, "r3back")
    run_a, worker_a, attempt_a, token_a, _ = lease_a
    run_b, worker_b, attempt_b, token_b, _ = lease_b
    args = f"'{run_a}','{worker_a}',{attempt_a},'{token_a}'"
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    source_1 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-back-1')}'::jsonb)")
    source_2 = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-back-2')}'::jsonb)")
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args_b},'{_r3_source_json('r3-back-b')}'::jsonb)")
    # Focused evidence: R3_LOCATOR on source_1 (run a) and R3_SPAN on source_b (run b).
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source_1, key='r3-back-f1')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args_b},'{_r3_fragment_json(source_b, key='r3-back-fb', kind='verbatim_excerpt', locator=R3_SPAN)}'::jsonb)")

    # Backed by its own source's fragment: accepted.
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-back-ok', source_1, 1798)}'::jsonb)")
    # A locator that exists only on ANOTHER source of the same run.
    with pytest.raises(AssertionError, match="must be backed by focused evidence of its own source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-back-other-source', source_2, 1798)}'::jsonb)")
    # A locator that exists only in ANOTHER run.
    with pytest.raises(AssertionError, match="must be backed by focused evidence of its own source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-back-other-run', source_1, 1798, locator=R3_SPAN)}'::jsonb)")
    # A locator recorded nowhere at all.
    never = record_field_locator("rec-1", ("fuel_type",)).locator_key
    with pytest.raises(AssertionError, match="must be backed by focused evidence of its own source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{_r3_claim_json('r3-back-never', source_1, 1798, locator=never)}'::jsonb)")
    assert db.psql(f"select count(*) from public.claims where run_id='{run_a}'") == "1"
    # A legacy (unlocated) claim on the same source is unaffected.
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('r3-back-legacy', source_2, 100)}'::jsonb)")


def test_r3_half_populated_provenance_combinations_fail_closed(r3_db):
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3half")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    legacy = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('r3-half-legacy')}'::jsonb)")
    versioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-half-versioned')}'::jsonb)")
    cases = (
        ("upsert_source_guarded", _r3_source_json("r3-half-k", identifier=None), "requires both a kind and an identifier"),
        ("upsert_source_guarded", _r3_source_json("r3-half-i", kind=None), "requires both a kind and an identifier"),
        ("record_evidence_fragment_guarded", _r3_fragment_json(versioned, key="r3-half-t", locator=None), "requires both a type and a locator"),
        ("record_evidence_fragment_guarded", _r3_fragment_json(versioned, key="r3-half-l", kind=None), "requires both a type and a locator"),
        # R3 provenance on a legacy source.
        ("record_evidence_fragment_guarded", _r3_fragment_json(legacy, key="r3-half-legacy-f"), "requires a versioned source"),
        ("create_claim_with_source_guarded", _r3_claim_json("r3-half-legacy-c", legacy, 1798), "requires a versioned source"),
        # A located claim on a versioned source with no focused evidence yet.
        ("create_claim_with_source_guarded", _r3_claim_json("r3-half-unbacked", versioned, 1798), "must be backed by focused evidence"),
    )
    for rpc, payload, message in cases:
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.{rpc}({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "0"
    # The legacy shape (nothing R3 at all) still persists exactly as before.
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_fragment_json(legacy, FRAGMENT_TEXT, key='r3-half-legacy-ok')}'::jsonb)")
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('r3-half-legacy-claim', legacy, 100)}'::jsonb)")


def test_r3_direct_inserts_that_bypass_the_rpc_are_held_to_the_same_shape(r3_db):
    """The table constraints apply the SAME helper functions as the RPCs."""
    db = r3_db
    lease, _ = _evidence_fixture(db, "r3direct")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    versioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('r3-direct-src')}'::jsonb)")
    source_columns = ("run_id, agent, url, title, domain, source_type, source_strength, query, "
                      "tool_operation, evidence_key, task_key, source_version_kind, source_version_id")
    for index, (kind, identifier) in enumerate((("content_sha256", "abc"), ("git_commit", "zzzzzzz"),
                                                ("retrieved_at", "2026"), ("dataset_version", None))):
        value = "null" if identifier is None else f"'{identifier}'"
        with pytest.raises(AssertionError, match="sources_version_pairing"):
            db.psql(f"insert into public.sources({source_columns}) values ('{run_id}','a','u','t','d',"
                    f"'primary','strong','q','op','r3-direct-src-{index}','task','{kind}',{value})")
    fragment_columns = "run_id, source_id, task_key, evidence_key, fragment_text, content_hash, fragment_index, fragment_type, locator_key"
    spaced = '["record_field", "rec-1", ["engine_displacement_cc"], null, null, null]'
    for index, (kind, locator) in enumerate((("verbatim_excerpt", R3_LOCATOR),            # kind mismatch
                                             ("structured_projection", spaced),           # non-canonical
                                             ("structured_projection", "not json"),
                                             ("structured_projection", None),             # half-populated
                                             (None, R3_LOCATOR))):
        kind_sql = "null" if kind is None else f"'{kind}'"
        locator_sql = "null" if locator is None else "'" + locator.replace("'", "''") + "'"
        with pytest.raises(AssertionError, match="source_evidence_fragments_focus_pairing"):
            db.psql(f"insert into public.source_evidence_fragments({fragment_columns}) values ('{run_id}','{versioned}','task',"
                    f"'r3-direct-f-{index}','{R3_TEXT}','{fragment_content_hash(R3_TEXT)}',0,{kind_sql},{locator_sql})")
    claim_columns = ("run_id, entity_key, field_key, value, time_scope, source_id, source_strength, "
                     "confidence, agent, evidence_key, task_key, evidence_locator")
    for index, locator in enumerate(("not json", spaced, '["record_field","rec-1",["$..x"],null,null,null]')):
        with pytest.raises(AssertionError, match="claims_evidence_locator_canonical"):
            db.psql(f"insert into public.claims({claim_columns}) values ('{run_id}','e','f','1798'::jsonb,'{{}}'::jsonb,"
                    f"'{versioned}','strong',0.9,'a','r3-direct-c-{index}','task','{locator}')")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "0"
    assert db.psql(f"select count(*) from public.claims where run_id='{run_id}'") == "0"
    # The constraints are exact, not over-strict: canonical R3 values and the
    # all-NULL legacy shape both insert directly.
    db.psql(f"insert into public.source_evidence_fragments({fragment_columns}) values ('{run_id}','{versioned}','task',"
            f"'r3-direct-f-ok','{R3_TEXT}','{fragment_content_hash(R3_TEXT)}',0,'structured_projection','{R3_LOCATOR}')")
    db.psql(f"insert into public.source_evidence_fragments({fragment_columns}) values ('{run_id}','{versioned}','task',"
            f"'r3-direct-f-legacy','legacy text','{fragment_content_hash('legacy text')}',1,null,null)")
    db.psql(f"insert into public.sources({source_columns}) values ('{run_id}','a','u','t','d','primary','strong','q','op',"
            f"'r3-direct-src-ok','task','content_sha256','{'a' * 64}')")
    assert db.psql(f"select count(*) from public.source_evidence_fragments where run_id='{run_id}'") == "2"


def test_r3_shape_helpers_are_service_only_and_pure(r3_db):
    db = r3_db
    for signature in ("public.r3_source_version_valid(text,text)", "public.r3_canonical_locator(text)",
                      "public.r3_focus_valid(text,text)"):
        for role in ("anon", "authenticated"):
            assert db.psql(f"select has_function_privilege('{role}', '{signature}', 'execute')") == "f"
        assert db.psql(f"select has_function_privilege('service_role', '{signature}', 'execute')") == "t"
        assert db.psql(f"select provolatile from pg_proc where oid = '{signature}'::regprocedure") == "i"
    # The helper is a pure comparison: the SAME canonical text the backend
    # emits is accepted and the SAME malformed inputs are refused.
    assert db.psql(f"select public.r3_canonical_locator('{R3_LOCATOR}') is not null") == "t"
    assert db.psql("select public.r3_canonical_locator('[\"record_field\", \"rec-1\", [\"a\"], null, null, null]') is null") == "t"
    assert db.psql("select public.r3_focus_valid('structured_projection', '" + R3_LOCATOR + "')") == "t"
    assert db.psql("select public.r3_focus_valid('verbatim_excerpt', '" + R3_LOCATOR + "')") == "f"
    assert db.psql("select public.r3_source_version_valid('content_sha256', 'abc')") == "f"
    assert db.psql(f"select public.r3_source_version_valid('content_sha256', '{'a' * 64}')") == "t"
    # Strictly boolean on every input, including NULL and non-canonical text:
    # a NULL result would pass a CHECK constraint.
    for probe in ("public.r3_focus_valid('structured_projection', 'not json')",
                  "public.r3_focus_valid('structured_projection', '[\"record_field\", \"rec-1\", [\"a\"], null, null, null]')",
                  "public.r3_focus_valid(null, '" + R3_LOCATOR + "')",
                  "public.r3_focus_valid('structured_projection', null)",
                  "public.r3_source_version_valid(null, 'abc')",
                  "public.r3_source_version_valid('content_sha256', null)",
                  "public.r3_source_version_valid('made_up', 'abc')"):
        assert db.psql(f"select ({probe}) is false") == "t", probe


# --- migration 20260907000100 (R4 deterministic verification) ---

@pytest.fixture
def r4_db(db):
    """The shared module DB with the R3 and R4 evidence migrations guaranteed
    current, independent of earlier rerun-safety tests re-applying older
    evidence migrations."""
    for name in ("source_evidence_fragments", "r3_versioned_focused_evidence",
                 "r4_deterministic_verification"):
        db.psql(file=next(m for m in MIGRATIONS if name in m.name))
    return db


R4_CONTRACT = VERIFIER_CONTRACT_VERSION
R4_SCOPE_HASH = "d" * 64


def _r4_bundle(db, args, prefix: str, *, value=1798, identity: dict | None = None,
               field: str = "engine_displacement_cc", locator: str = R3_LOCATOR,
               text: str = R3_TEXT):
    """One versioned source + one located fragment + one located claim."""
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json(prefix + '-src')}'::jsonb)")
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, text, key=prefix + '-frag', locator=locator)}'::jsonb)")
    payload = json.loads(_r3_claim_json(prefix + "-claim", source, value, locator=locator,
                                        field=field))
    if identity is not None:
        payload["identity_scope"] = identity
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{json.dumps(payload)}'::jsonb)")
    return source, fragment, claim


def _r4_verdict_json(claim_id: str, *, key: str, verdict: str = "verified",
                     reason: str = "R4_STRUCTURED_MATCH",
                     mode: str = "deterministic_structured",
                     support: list[dict] | None = None) -> str:
    return json.dumps({"claim_id": claim_id, "verdict": verdict, "reason": reason,
                       "verification_mode": mode, "verifier_contract_version": R4_CONTRACT,
                       "support": support or [], "evidence_key": key})


def _r4_support(fragment_id: str, text: str = R3_TEXT, locator: str = R3_LOCATOR) -> dict:
    return {"fragment_id": fragment_id, "content_hash": fragment_content_hash(text),
            "locator_key": locator}


def _r4_resolution_json(claim_ids: list[str], *, key: str, state: str = "resolved",
                        reason: str = "R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                        winner: str | None = None,
                        superseded: list[str] | None = None) -> str:
    return json.dumps({"evidence_key": key, "scope_hash": R4_SCOPE_HASH, "entity": "entity",
                       "field": "engine_displacement_cc", "state": state, "reason": reason,
                       "policy_version": CONFLICT_POLICY_VERSION, "claim_ids": claim_ids,
                       "winning_claim_id": winner,
                       "superseded_claim_ids": superseded or []})


def test_r4_verdict_persists_with_its_support_and_replays_onto_the_same_row(r4_db):
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _, fragment, claim = _r4_bundle(db, args, "r4a")

    payload = _r4_verdict_json(claim, key="r4a-verdict", support=[_r4_support(fragment)])
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{payload}'::jsonb)")
    assert db.psql(
        f"select verdict, reason, verification_mode, verifier_contract_version "
        f"from public.claim_verdicts where id='{verdict}'"
    ) == f"verified|R4_STRUCTURED_MATCH|deterministic_structured|{R4_CONTRACT}"
    assert db.psql(
        f"select fragment_id, content_hash, locator_key from public.claim_verdict_supports "
        f"where verdict_id='{verdict}'") == f"{fragment}|{fragment_content_hash(R3_TEXT)}|{R3_LOCATOR}"

    # An exact replay -- a resumed run, a re-verification after the one bounded
    # correction round, a retried batch -- lands on the SAME row and the same
    # single support link.
    assert _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{payload}'::jsonb)") == verdict
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_id}'") == "1"
    assert db.psql(f"select count(*) from public.claim_verdict_supports where run_id='{run_id}'") == "1"

    # Reusing one evidence_key for a DIFFERENT decision fails closed.
    with pytest.raises(AssertionError, match="idempotency conflict"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4a-verdict', verdict='rejected', reason='R4_VALUE_MISMATCH')}'::jsonb)")


def test_r4_forged_cross_source_and_missing_support_links_fail_closed(r4_db):
    db = r4_db
    lease_a, lease_b = _evidence_fixture(db, "r4support")
    run_a, worker_a, attempt_a, token_a, _ = lease_a
    run_b, worker_b, attempt_b, token_b, _ = lease_b
    args = f"'{run_a}','{worker_a}',{attempt_a},'{token_a}'"
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    _, fragment, claim = _r4_bundle(db, args, "r4b")
    other_locator = record_field_locator("rec-9", ("engine_displacement_cc",)).locator_key
    _, other_fragment, _ = _r4_bundle(db, args, "r4c", locator=other_locator,
                                      text="model_name=Other; engine_displacement_cc=1600")
    _, foreign_fragment, _ = _r4_bundle(db, args_b, "r4d")

    # A support link naming evidence of ANOTHER source of the same run.
    with pytest.raises(AssertionError, match="belongs to another source"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-cross', support=[_r4_support(other_fragment, 'model_name=Other; engine_displacement_cc=1600', other_locator)])}'::jsonb)")
    # A support link naming evidence of another RUN entirely.
    with pytest.raises(AssertionError, match="does not name durable evidence"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-foreign', support=[_r4_support(foreign_fragment)])}'::jsonb)")
    # A completely invented fragment id.
    with pytest.raises(AssertionError, match="does not name durable evidence"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-invented', support=[_r4_support('11111111-2222-4333-8444-555555555555')])}'::jsonb)")
    # A real fragment cited with a hash or locator that is not its own.
    forged_hash = {**_r4_support(fragment), "content_hash": "b" * 64}
    with pytest.raises(AssertionError, match="content hash mismatch"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-hash', support=[forged_hash])}'::jsonb)")
    forged_locator = {**_r4_support(fragment), "locator_key": other_locator}
    with pytest.raises(AssertionError, match="locator mismatch"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-loc', support=[forged_locator])}'::jsonb)")
    # An ACCEPTED verdict with no evidence at all.
    with pytest.raises(AssertionError, match="must cite durable evidence"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-empty')}'::jsonb)")
    # A locally settled verdict that cites evidence it never compared.
    with pytest.raises(AssertionError, match="cites no evidence"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4b-local', verdict='needs_review', reason='unresolved conflict', mode='deterministic_local', support=[_r4_support(fragment)])}'::jsonb)")
    # Nothing above was written.
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_a}'") == "0"
    assert db.psql(f"select count(*) from public.claim_verdict_supports where run_id='{run_a}'") == "0"


def test_r4_verdict_rejects_stale_leases_unknown_vocabulary_and_unsafe_payloads(r4_db):
    db = r4_db
    lease, other = _evidence_fixture(db, "r4guard")
    run_id, worker, attempt, token, _ = lease
    run_b, worker_b, attempt_b, token_b, _ = other
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _, fragment, claim = _r4_bundle(db, args, "r4e")
    support = [_r4_support(fragment)]

    for bad in (f"'{run_id}','wrong-worker',{attempt},'{token}'",
                f"'{run_id}','{worker}',{attempt},'wrong-token'",
                f"'{run_id}','{worker}',{int(attempt) + 1},'{token}'"):
        with pytest.raises(AssertionError):
            _rpc_as_service(db, f"select public.record_claim_verdict_guarded({bad},'{_r4_verdict_json(claim, key='r4e-stale', support=support)}'::jsonb)")

    # A claim of ANOTHER run can never receive a verdict from this one.
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    _, _, foreign_claim = _r4_bundle(db, args_b, "r4f")
    with pytest.raises(AssertionError, match="invalid claim verdict claim"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(foreign_claim, key='r4e-cross')}'::jsonb)")

    for payload, pattern in (
        (_r4_verdict_json(claim, key="r4e-verdict", verdict="probably", support=support),
         "claim_verdicts_verdict_allowlisted"),
        (_r4_verdict_json(claim, key="r4e-mode", mode="vibes", support=support),
         "claim_verdicts_mode_allowlisted"),
        (json.dumps({**json.loads(_r4_verdict_json(claim, key="r4e-nokey", support=support)),
                     "evidence_key": ""}), "evidence_key is required"),
        (json.dumps({**json.loads(_r4_verdict_json(claim, key="r4e-secret", support=support)),
                     "chain_of_thought": "hidden"}), "unsafe evidence payload rejected"),
        (json.dumps({**json.loads(_r4_verdict_json(claim, key="r4e-long", support=support)),
                     "verifier_contract_version": "x" * 200}),
         "claim_verdicts_contract_bounded"),
        (json.dumps({**json.loads(_r4_verdict_json(claim, key="r4e-many", support=support)),
                     "support": [_r4_support(fragment)] * 5}),
         "more evidence than a source can hold"),
    ):
        with pytest.raises(AssertionError, match=pattern):
            _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_id}'") == "0"


def test_r4_conflict_resolution_persists_supersedes_and_never_deletes_a_claim(r4_db):
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4conflict")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _, _, loser = _r4_bundle(db, args, "r4g", value=1798)
    other_locator = record_field_locator("rec-2", ("engine_displacement_cc",)).locator_key
    _, _, winner = _r4_bundle(db, args, "r4h", value=1600, locator=other_locator,
                              text="model_name=Fixture; engine_displacement_cc=1600")

    payload = _r4_resolution_json([loser, winner], key="r4-res-1", winner=winner,
                                  superseded=[loser])
    resolution = _rpc_as_service(db, f"select id from public.record_conflict_resolution_guarded({args},'{payload}'::jsonb)")
    assert db.psql(
        f"select state, reason, policy_version, winning_claim_id "
        f"from public.conflict_resolutions where id='{resolution}'"
    ) == f"resolved|R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE|{CONFLICT_POLICY_VERSION}|{winner}"
    # The losing claim is still there, with its evidence, exactly as recorded.
    assert db.psql(f"select count(*) from public.claims where id='{loser}'") == "1"
    assert db.psql(
        f"select count(*) from public.source_claim_links l join public.claims c "
        f"on c.id = l.claim_id where c.run_id='{run_id}'") == "2"
    # A replay returns the same decision.
    assert _rpc_as_service(db, f"select id from public.record_conflict_resolution_guarded({args},'{payload}'::jsonb)") == resolution
    assert db.psql(f"select count(*) from public.conflict_resolutions where run_id='{run_id}'") == "1"

    for bad, pattern in (
        (_r4_resolution_json([loser, winner], key="r4-res-1", state="unresolved",
                             reason="R4_CONFLICT_UNRESOLVED_AMBIGUOUS"),
         "idempotency conflict"),
        (_r4_resolution_json([loser, winner], key="r4-res-2", winner=winner),
         "supersedes at least one losing claim"),
        (_r4_resolution_json([loser, winner], key="r4-res-3", winner=winner,
                             superseded=[winner]),
         "supersedes at least one losing claim"),
        (_r4_resolution_json([loser], key="r4-res-4", winner=loser, superseded=[loser]),
         "decides at least two claims"),
        (_r4_resolution_json([loser, "11111111-2222-4333-8444-555555555555"],
                             key="r4-res-5", winner=loser, superseded=[loser]),
         "must decide claims of this run"),
        (_r4_resolution_json([loser, winner], key="r4-res-6", state="settled",
                             reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                             winner=winner, superseded=[loser]),
         "conflict_resolutions_state_allowlisted"),
        (_r4_resolution_json([loser, winner], key="r4-res-7", reason="because",
                             winner=winner, superseded=[loser]),
         "conflict_resolutions_reason_allowlisted"),
    ):
        with pytest.raises(AssertionError, match=pattern):
            _rpc_as_service(db, f"select public.record_conflict_resolution_guarded({args},'{bad}'::jsonb)")
    assert db.psql(f"select count(*) from public.conflict_resolutions where run_id='{run_id}'") == "1"


def test_r4_identity_scope_is_closed_bounded_and_optional(r4_db):
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4identity")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _, _, qualified = _r4_bundle(db, args, "r4i", identity={"generation": "e210"})
    assert db.psql(f"select identity_scope->>'generation' from public.claims where id='{qualified}'") == "e210"

    # A pre-R4 claim carries no identity at all and stays perfectly valid.
    other_locator = record_field_locator("rec-3", ("engine_displacement_cc",)).locator_key
    _, _, legacy = _r4_bundle(db, args, "r4j", locator=other_locator,
                              text="model_name=Legacy; engine_displacement_cc=1798")
    assert db.psql(f"select identity_scope is null from public.claims where id='{legacy}'") == "t"

    # The closed vocabulary and the value bound are enforced at the table, so a
    # direct insert that bypasses every RPC is held to exactly the same shape.
    source = db.psql(f"select source_id from public.claims where id='{qualified}'")
    for identity, pattern in (('{"invented": "x"}', "claims_identity_scope_closed"),
                              ('{"generation": 5}', "claims_identity_scope_closed"),
                              ('{"generation": ""}', "claims_identity_scope_closed"),
                              (json.dumps({"generation": "x" * 200}),
                               "claims_identity_scope_closed"),
                              ('["generation"]', "claims_identity_scope_closed")):
        with pytest.raises(AssertionError, match=pattern):
            db.psql(
                f"insert into public.claims(run_id,entity_key,field_key,value,source_id,"
                f"source_strength,confidence,agent,identity_scope) values "
                f"('{run_id}','e','f','1'::jsonb,'{source}','strong',0.9,'agent',"
                f"'{identity}'::jsonb)")
    assert db.psql("select public.r4_identity_scope_valid(null) is false") == "t"
    assert db.psql("""select public.r4_identity_scope_valid('{"generation":"e210"}'::jsonb)""") == "t"


def test_r4_surface_stays_service_only_append_only_and_rerun_safe(r4_db):
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4acl")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _, fragment, claim = _r4_bundle(db, args, "r4k")
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4k-verdict', support=[_r4_support(fragment)])}'::jsonb)")

    tables = ("claim_verdicts", "claim_verdict_supports", "conflict_resolutions")
    for table in tables:
        assert db.psql(f"select relrowsecurity from pg_class where relname='{table}'") == "t"
        assert db.psql(f"select count(*) from pg_policies where tablename='{table}'") == "0"
        for role in ("anon", "authenticated"):
            for privilege in ("select", "insert", "update", "delete"):
                assert db.psql(
                    f"select has_table_privilege('{role}', 'public.{table}', '{privilege}')") == "f"
        for privilege in ("select", "insert"):
            assert db.psql(
                f"select has_table_privilege('service_role', 'public.{table}', '{privilege}')") == "t"
        for privilege in ("update", "delete"):
            assert db.psql(
                f"select has_table_privilege('service_role', 'public.{table}', '{privilege}')") == "f"
    for signature in ("public.record_claim_verdict_guarded(uuid,text,integer,text,jsonb)",
                      "public.record_conflict_resolution_guarded(uuid,text,integer,text,jsonb)",
                      "public.r4_identity_scope_valid(jsonb)"):
        for role in ("anon", "authenticated"):
            assert db.psql(f"select has_function_privilege('{role}', '{signature}', 'execute')") == "f"
        assert db.psql(f"select has_function_privilege('service_role', '{signature}', 'execute')") == "t"

    # Append-only: a recorded verdict, its support and a conflict decision can
    # never be rewritten or removed by ANY role, including the owner.
    for statement in (f"update public.claim_verdicts set verdict='rejected' where id='{verdict}'",
                      f"delete from public.claim_verdicts where id='{verdict}'",
                      f"update public.claim_verdict_supports set content_hash='{'a' * 64}' where verdict_id='{verdict}'",
                      f"delete from public.claim_verdict_supports where verdict_id='{verdict}'"):
        with pytest.raises(AssertionError, match="append-only"):
            db.psql(statement)

    # Rerun-safe, and the re-application changes nothing that already exists.
    migration = next(m for m in MIGRATIONS if "r4_deterministic_verification" in m.name)
    db.psql(file=migration)
    db.psql(file=migration)
    assert db.psql(f"select count(*) from public.claim_verdicts where id='{verdict}'") == "1"
    for name in ("record_claim_verdict_guarded", "record_conflict_resolution_guarded",
                 "r4_identity_scope_valid"):
        assert db.psql(f"select count(*) from pg_proc where proname='{name}'") == "1"


def test_r4_legacy_evidence_stays_valid_readable_and_never_backfilled(r4_db):
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4legacy")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    # A pre-R3 source and a pre-R3 claim: no version, no locator, no identity.
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_source_json('r4-legacy-src')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_claim_json('r4-legacy-claim', source, 1798)}'::jsonb)")
    assert db.psql(
        f"select source_version_kind is null, evidence_locator is null, identity_scope is null "
        f"from public.claims join public.sources on sources.id = claims.source_id "
        f"where claims.id='{claim}'") == "t|t|t"
    # It has no verdict row, which is exactly how a legacy verdict stays
    # readable in its checkpoint without being presented as R4-grounded.
    assert db.psql(f"select count(*) from public.claim_verdicts where claim_id='{claim}'") == "0"
    # And a locally settled verdict about it is still perfectly recordable.
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4-legacy-verdict', verdict='needs_review', reason='SOURCE_CONTEXT_UNAVAILABLE', mode='deterministic_local')}'::jsonb)")
    assert db.psql(f"select verification_mode from public.claim_verdicts where id='{verdict}'") == "deterministic_local"


def test_r4_identical_text_at_two_locators_persists_only_the_cited_row(r4_db):
    """R4 correction: one citation is one durable support row, never an expansion.

    R3 deliberately allows identical text at two different locators, so a bare
    content hash can name two durable rows. The verdict must persist exactly
    the fragment the decision selected, and a replay must not accumulate the
    other one.
    """
    db = r4_db
    lease, _ = _evidence_fixture(db, "r4twin")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    other_locator = record_field_locator("rec-2", ("engine_displacement_cc",)).locator_key
    source, first, claim = _r4_bundle(db, args, "r4t")
    # The SAME text at a second locator: a second durable row, one hash.
    second = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, R3_TEXT, key='r4t-frag-2', index=1, locator=other_locator)}'::jsonb)")
    assert second != first
    assert db.psql(
        f"select count(*), count(distinct content_hash), count(distinct locator_key) "
        f"from public.source_evidence_fragments where source_id='{source}'") == "2|1|2"

    # A verdict citing the SECOND row persists that row and only that row.
    payload = _r4_verdict_json(claim, key="r4t-verdict",
                               support=[_r4_support(second, R3_TEXT, other_locator)])
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{payload}'::jsonb)")
    assert db.psql(
        f"select count(*) from public.claim_verdict_supports where verdict_id='{verdict}'") == "1"
    assert db.psql(
        f"select fragment_id, locator_key from public.claim_verdict_supports "
        f"where verdict_id='{verdict}'") == f"{second}|{other_locator}"

    # A replay lands on the same row and does not accumulate the twin.
    assert _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{payload}'::jsonb)") == verdict
    assert db.psql(
        f"select count(*) from public.claim_verdict_supports where verdict_id='{verdict}'") == "1"
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_id}'") == "1"

    # Citing the FIRST row is a different verdict with its own single link, and
    # neither verdict ever acquires the other's evidence.
    first_payload = _r4_verdict_json(claim, key="r4t-verdict-first",
                                     support=[_r4_support(first)])
    other = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{first_payload}'::jsonb)")
    assert db.psql(
        f"select fragment_id from public.claim_verdict_supports where verdict_id='{other}'") == first
    assert db.psql(
        f"select count(*) from public.claim_verdict_supports where run_id='{run_id}'") == "2"
    # A support link naming a fragment whose hash matches but whose locator
    # does not is still refused: the row, not the hash, is the provenance.
    with pytest.raises(AssertionError, match="locator mismatch"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='r4t-mixed', support=[_r4_support(second, R3_TEXT, R3_LOCATOR)])}'::jsonb)")


# ===========================================================================
# Catalog PR1: an EMPTY, evidence-backed catalog foundation
# ===========================================================================
#
# Everything below runs against the same real PostgreSQL cluster with the
# whole migration set applied, so these are properties of the SCHEMA and not
# of any backend release. The catalog relations are long-lived: unlike
# public.sources / public.claims / public.claim_verdicts they are not
# run-scoped and do not cascade away with a run.

CATALOG_STAGING_TABLES = ("catalog_source_snapshots", "catalog_raw_records",
                          "catalog_candidate_variants", "catalog_candidate_evidence_links")
CATALOG_CANONICAL_TABLES = ("catalog_models", "catalog_model_variants")
#: Catalog PR3: the append-only field provenance, and the READ MODEL derived
#: from it. The views are what "the current canonical value" means; the two
#: canonical tables above are the frozen revision-1 identity.
CATALOG_PROVENANCE_TABLES = ("catalog_canonical_field_provenance",)
CATALOG_VIEWS = ("catalog_canonical_field_current", "catalog_canonical_variant_current")
CATALOG_RPCS = ("record_catalog_snapshot_guarded", "record_catalog_raw_record_guarded",
                "activate_catalog_snapshot_guarded", "record_catalog_candidate_guarded",
                "link_catalog_candidate_evidence_guarded")

CATALOG_MIGRATION_MARKER = "catalog_evidence_foundation"


def _catalog_key(domain: str, label: str) -> str:
    """A shape-valid catalog key for a test object, stable per label.

    PostgreSQL enforces a key's DOMAIN and SHAPE (`cs1.`/`cr1.`/`cc1.`/`cl1.`
    plus 32 hex characters); the FULL derivation from an object's structural
    identity is a repository-side rule, proven in `tests/test_catalog_keys.py`
    and `tests/test_catalog_persistence.py`. Deriving from a label here keeps a
    key stable across the deliberately CONFLICTING payloads an idempotency test
    replays under one identity -- which a true structural derivation, by
    design, could not do.
    """
    return catalog_keys.derive_key(domain, test_label=label)


def _catalog_snapshot_json(key: str, *, family: str = "government",
                           resource: str = "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
                           version: str = "2026.09.1", kind: str = "dataset_version",
                           declared: int = 1, content: str | None = None, **extra) -> str:
    payload = {"snapshot_key": _catalog_key("catalog.snapshot", key),
               "source_family": family, "resource_id": resource,
               "upstream_version": version, "upstream_version_kind": kind,
               "content_sha256": content or hashlib.sha256(key.encode()).hexdigest(),
               "retrieved_at": "2026-09-14T16:11:13.272Z",
               "retrieval_metadata": {"http_status": 200, "redirect_chain": []},
               "declared_record_count": declared}
    payload.update(extra)
    return json.dumps(payload)


def _catalog_record_json(snapshot_id: str, key: str, *, upstream: str = "36327",
                         resource: str = "142afde2-6228-49f9-8a29-9b6c3a0cbe40",
                         payload: dict | None = None, locator: dict | None = None) -> str:
    payload = {"_id": 36327, "kinuy_mishari": "RAV4"} if payload is None else payload
    # The digest is DERIVED inside the trusted persistence path after the
    # corrective round, so a caller that still sends one is refused.
    record = {"snapshot_id": snapshot_id,
              "record_key": _catalog_key("catalog.raw_record", key),
              "upstream_record_id": upstream, "resource_id": resource,
              "payload": payload}
    # PR2: where the row sat in the retrieval that captured it. Optional, and
    # exact when stated -- `None` omits it entirely rather than nulling it.
    if locator is not None:
        record["source_locator"] = locator
    return json.dumps(record)


def _catalog_candidate_json(snapshot_id: str, record_id: str, key: str, *,
                            status: str = "candidate", make: str = "Toyota",
                            model: str = "RAV4", years: tuple[int, int] | None = (2021, 2021),
                            code: str | None = "AXAP54L-ANXGBW", trim: str | None = "PRIME AWD SE",
                            dimensions: dict | None = None) -> str:
    payload = {"snapshot_id": snapshot_id, "raw_record_id": record_id,
               "candidate_key": _catalog_key("catalog.candidate", key),
               "manufacturer": make, "commercial_model": model, "status": status,
               "official_model_code": code, "trim": trim,
               "identity_dimensions": {"drivetrain": "awd", "fuel_type": "plug_in_hybrid"}
                                      if dimensions is None else dimensions}
    if years is not None:
        payload["model_year_start"], payload["model_year_end"] = years
    return json.dumps(payload)


def _catalog_link_json(candidate_id: str, source_id: str, key: str, *,
                       claim_id: str | None = None, verdict_id: str | None = None,
                       locator: str | None = R3_LOCATOR,
                       version: str | None = R3_VERSION[1],
                       kind: str | None = R3_VERSION[0]) -> str:
    """One evidence-link payload. `None` OMITS a field rather than nulling it.

    Omission is the supported path after the corrective round: the locator and
    the source version are derived from the cited claim and source, so a
    caller that states them is asserting something the database will check
    rather than supplying something it will trust.
    """
    payload = {"candidate_id": candidate_id, "source_id": source_id,
               "link_key": _catalog_key("catalog.evidence_link", key)}
    for field, value in (("record_locator", locator), ("source_version", version),
                         ("source_version_kind", kind), ("claim_id", claim_id),
                         ("verdict_id", verdict_id)):
        if value is not None:
            payload[field] = value
    return json.dumps(payload)


def _catalog_claim_for(db, args: str, label: str) -> tuple[str, str]:
    """A real source and a real claim resting on it, for one leased run.

    After the corrective round an evidence link must cite a claim, because the
    locator and the source version it records are DERIVED from that claim and
    that source rather than supplied. Tests therefore build genuine evidence
    instead of standing an arbitrary uuid in for it.
    """
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json(label)}'::jsonb)")
    # R3 order: a located fact must already be backed by focused evidence of
    # its own source.
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key=f'{label}-fragment')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json(f'{label}-claim', source, 1798)}'::jsonb)")
    return source, claim


def _catalog_fixture(db, suffix: str):
    """A leased run plus a complete, ACTIVE government snapshot with one row."""
    lease, other = _evidence_fixture(db, f"catalog-{suffix}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json(f'snap-{suffix}')}'::jsonb)")
    record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-{suffix}')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    candidate = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, f'cand-{suffix}')}'::jsonb)")
    return lease, other, args, snapshot, record, candidate


#: Every catalog migration, in apply order. Rerun safety is a property of the
#: ORDERED SET, not of one file: replaying an earlier migration alone would
#: restore the function bodies a later one corrected, which is exactly the
#: hazard this list exists to avoid reintroducing.
CATALOG_MIGRATIONS = tuple(m for m in MIGRATIONS if "catalog" in m.name)


def _reapply_catalog_migrations(db) -> None:
    for migration in CATALOG_MIGRATIONS:
        db.psql(file=migration)


def test_catalog_migration_applies_and_is_rerun_safe(db):
    """They applied inside the `db` fixture with every other migration, and
    applying the ordered set twice more changes nothing -- the repository's
    forward-only, idempotent migration contract."""
    assert [m.name for m in CATALOG_MIGRATIONS] == [
        "20260914200000_catalog_evidence_foundation.sql",
        "20260915120000_catalog_integrity_corrections.sql",
        "20260915180000_catalog_raw_record_source_locator.sql",
        "20260916090000_catalog_bounded_candidate_queries.sql",
        "20260916120000_catalog_field_level_promotion.sql"]
    before = db.psql(
        "select count(*) from information_schema.tables where table_schema='public' "
        "and table_name like 'catalog\\_%'")
    # `information_schema.tables` lists views too, so the expected count is the
    # base relations plus the two canonical read-model views PR3 adds.
    assert before == str(len(CATALOG_STAGING_TABLES) + len(CATALOG_CANONICAL_TABLES)
                         + len(CATALOG_PROVENANCE_TABLES) + len(CATALOG_VIEWS))
    _reapply_catalog_migrations(db)
    _reapply_catalog_migrations(db)
    assert db.psql(
        "select count(*) from information_schema.tables where table_schema='public' "
        "and table_name like 'catalog\\_%'") == before
    for rpc in CATALOG_RPCS:
        assert db.psql(f"select count(*) from pg_proc where proname='{rpc}'") == "1"
    # A rerun must not resurrect a browser grant or an RLS policy either.
    assert db.psql(
        "select count(*) from pg_policies where schemaname='public' "
        "and tablename like 'catalog\\_%'") == "0"


def test_catalog_canonical_tables_start_empty_and_hold_no_legacy_row(db):
    """The product decision, checked against the live schema.

    The existing aggregated catalog is incomplete and holds incorrect values,
    so nothing is seeded from it. Both canonical relations are created empty
    and no migration puts a row in either -- there is no legacy model, no
    legacy variant, and no legacy alias anywhere in this schema.
    """
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"
    # Nor is any staging relation pre-seeded: the catalog begins with nothing
    # at all, and every row it ever holds arrives through a guarded write.
    for table in CATALOG_STAGING_TABLES:
        assert db.psql(
            f"select count(*) from public.{table} where created_at < (select min(created_at) "
            f"from public.runs)") == "0"
    # And a `legacy_reference` snapshot can never be evidence, at any point in
    # its life, by any writer -- the constraint, not the write path.
    with pytest.raises(AssertionError, match="trust_pinned_to_family"):
        db.psql(
            "insert into public.catalog_source_snapshots (created_by_run_id, source_family, "
            "trust_state, resource_id, upstream_version, upstream_version_kind, content_sha256, "
            "retrieved_at, declared_record_count, snapshot_key) select id, 'legacy_reference', "
            "'evidence', 'yeda', '2026.01.1', 'dataset_version', repeat('a', 64), now(), 0, "
            "'cs1.00000000000000000000000000000001' from public.runs limit 1")


def test_catalog_privileges_are_minimal_and_the_canonical_pair_is_append_only(db):
    """Least privilege, table by table, read out of the live ACLs.

    PR1 and PR2 kept the canonical relations SELECT-only, because a row-level
    `promoted_from_verdict_id` cannot verify a multi-field row. Catalog PR3
    adds the field-level provenance that makes an insert checkable and grants
    EXACTLY that: `service_role` gains INSERT on the canonical pair and on the
    provenance relation, and gains nothing else -- no UPDATE and no DELETE
    anywhere, so a promoted row can never be rewritten and a later, better
    source APPENDS a revision instead.
    """
    for table in CATALOG_STAGING_TABLES:
        assert db.psql(
            f"select has_table_privilege('service_role','public.{table}','select') || '|' || "
            f"has_table_privilege('service_role','public.{table}','insert') || '|' || "
            f"has_table_privilege('service_role','public.{table}','delete')") == "true|true|false"
    # Raw source material takes no UPDATE at all; the two relations that carry
    # a reviewed transition (snapshot completion, candidate status) do.
    for table in ("catalog_raw_records", "catalog_candidate_evidence_links"):
        assert db.psql(
            f"select has_table_privilege('service_role','public.{table}','update')") == "f"
    for table in ("catalog_source_snapshots", "catalog_candidate_variants"):
        assert db.psql(
            f"select has_table_privilege('service_role','public.{table}','update')") == "t"
    for table in CATALOG_CANONICAL_TABLES + CATALOG_PROVENANCE_TABLES:
        assert db.psql(
            f"select has_table_privilege('service_role','public.{table}','select') || '|' || "
            f"has_table_privilege('service_role','public.{table}','insert') || '|' || "
            f"has_table_privilege('service_role','public.{table}','update') || '|' || "
            f"has_table_privilege('service_role','public.{table}','delete')"
        ) == "true|true|false|false", table
    # The read model is exactly that: SELECT for the service path, nothing for
    # a browser role, and no privilege of its own beyond the caller's.
    for view in CATALOG_VIEWS:
        assert db.psql(
            f"select has_table_privilege('service_role','public.{view}','select') || '|' || "
            f"has_table_privilege('service_role','public.{view}','insert')") == "true|false", view
        for role in ("anon", "authenticated"):
            assert db.psql(
                f"select has_table_privilege('{role}','public.{view}','select')") == "f", (role, view)
        # `security_invoker` so a view can never read more than its caller.
        assert db.psql(
            "select count(*) from pg_class where relname='" + view +
            "' and 'security_invoker=true' = any(reloptions)") == "1", view


def test_catalog_browser_roles_have_no_access_at_all(db):
    """RLS with zero policies, and not a single grant, for anon/authenticated.

    Two independent barriers: the privilege check fails first, and RLS with no
    policy would deny even if a grant were ever restored by accident.
    """
    for table in CATALOG_STAGING_TABLES + CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select relrowsecurity from pg_class where relname='{table}'") == "t"
        assert db.psql(
            f"select count(*) from pg_policies where schemaname='public' and tablename='{table}'") == "0"
        for role in ("anon", "authenticated"):
            for privilege in ("select", "insert", "update", "delete"):
                assert db.psql(
                    f"select has_table_privilege('{role}','public.{table}','{privilege}')"
                ) == "f", (role, table, privilege)
    # And no browser role may call a catalog RPC either.
    for role in ("anon", "authenticated"):
        for rpc in CATALOG_RPCS:
            with pytest.raises(AssertionError, match="permission denied"):
                db.psql(f"set role {role}; select public.{rpc}("
                        f"'00000000-0000-4000-8000-000000000001','w',1,'t','{{}}'::jsonb)")


def test_catalog_snapshot_replay_is_idempotent_and_conflict_fails_closed(db):
    lease, _ = _evidence_fixture(db, "catalog-snap")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    payload = _catalog_snapshot_json("snap-replay", declared=2)
    first = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{payload}'::jsonb)")
    # Byte-identical replay collapses onto the same row.
    assert _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{payload}'::jsonb)") == first
    replay_key = _catalog_key("catalog.snapshot", "snap-replay")
    assert db.psql(f"select count(*) from public.catalog_source_snapshots where snapshot_key='{replay_key}'") == "1"

    # The SAME identity with DIFFERENT content is a different retrieval wearing
    # the same name. Every dimension of that is a refusal, never an overwrite.
    for changed in ({"content": "b" * 64}, {"version": "2026.09.2"}, {"declared": 3},
                    {"resource": "5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6"},
                    {"family": "manufacturer"}):
        conflicting = _catalog_snapshot_json("snap-replay", **{"declared": 2, **changed})
        with pytest.raises(AssertionError, match="catalog snapshot idempotency conflict"):
            _rpc_as_service(db, f"select public.record_catalog_snapshot_guarded({args},'{conflicting}'::jsonb)")
    # Nothing was written and nothing was changed by any of those attempts.
    assert db.psql(
        f"select count(*) || '|' || max(declared_record_count::text) from "
        f"public.catalog_source_snapshots where snapshot_key='{replay_key}'") == "1|2"

    # The caller can neither pin its own trust state nor activate a snapshot
    # on the way in: activation is a separate, evidenced decision.
    for bad, message in ((_catalog_snapshot_json("snap-trust", family="legacy_reference",
                                                 trust_state="evidence"),
                          "pinned to the source family"),
                         (_catalog_snapshot_json("snap-active", activated_at="2026-09-14T00:00:00Z"),
                          "not a caller-supplied field"),
                         (_catalog_snapshot_json("snap-count", stored_record_count=5),
                          "not a caller-supplied field")):
        with pytest.raises(AssertionError, match=message):
            _rpc_as_service(db, f"select public.record_catalog_snapshot_guarded({args},'{bad}'::jsonb)")


def test_catalog_raw_record_replay_is_idempotent_and_conflict_fails_closed(db):
    lease, _ = _evidence_fixture(db, "catalog-raw")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-raw', declared=2)}'::jsonb)")
    payload = _catalog_record_json(snapshot, "rec-replay")
    first = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{payload}'::jsonb)")
    assert _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{payload}'::jsonb)") == first
    # The snapshot's stored count advanced exactly once, in the same
    # transaction as the row, so completeness can never drift from the rows.
    assert db.psql(f"select stored_record_count from public.catalog_source_snapshots where id='{snapshot}'") == "1"

    # Same record_key, different payload: fail closed.
    conflicting = _catalog_record_json(snapshot, "rec-replay", payload={"_id": 36327, "kinuy_mishari": "RAV4 PHEV"})
    with pytest.raises(AssertionError, match="catalog raw record idempotency conflict"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{conflicting}'::jsonb)")
    # ...and a caller that still supplies a digest is refused outright: the
    # value is derived inside the trusted path, so nothing reads a sent one.
    forged = json.loads(_catalog_record_json(snapshot, "rec-forged"))
    forged["payload_sha256"] = "c" * 64
    with pytest.raises(AssertionError, match="payload digest is derived"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{json.dumps(forged)}'::jsonb)")
    assert db.psql(f"select stored_record_count from public.catalog_source_snapshots where id='{snapshot}'") == "1"


def test_catalog_raw_records_cannot_be_silently_changed_or_deleted(db):
    """Append-only, enforced by a trigger as well as by the missing grants.

    Captured source material is an audit record: if it could be rewritten,
    every downstream candidate and link would rest on something that no longer
    says what it said.
    """
    _, _, args, snapshot, record, candidate = _catalog_fixture(db, "immutable")
    before = db.psql(f"select payload::text || '|' || payload_sha256 from public.catalog_raw_records where id='{record}'")
    for statement in (f"update public.catalog_raw_records set payload='{{\"_id\": 1}}'::jsonb where id='{record}'",
                      f"update public.catalog_raw_records set upstream_record_id='999' where id='{record}'",
                      f"delete from public.catalog_raw_records where id='{record}'"):
        with pytest.raises(AssertionError, match="append-only"):
            db.psql(statement)
    assert db.psql(f"select payload::text || '|' || payload_sha256 from public.catalog_raw_records where id='{record}'") == before

    # An ACTIVE snapshot is frozen completely, including against appends: a
    # validated capture must keep saying what it was validated as saying.
    with pytest.raises(AssertionError, match="an active catalog snapshot is immutable"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, 'rec-after-active', upstream='99999')}'::jsonb)")
    with pytest.raises(AssertionError, match="an active catalog snapshot is immutable"):
        db.psql(f"update public.catalog_source_snapshots set declared_record_count=9 where id='{snapshot}'")
    # A candidate's IDENTITY is immutable too, even though its reading may be
    # revised; and no catalog row may be deleted by anyone.
    with pytest.raises(AssertionError, match="catalog candidate identity is immutable"):
        db.psql(f"update public.catalog_candidate_variants set manufacturer='Lexus' where id='{candidate}'")
    with pytest.raises(AssertionError, match="append-only|immutable"):
        db.psql(f"delete from public.catalog_candidate_variants where id='{candidate}'")


def test_catalog_snapshot_is_active_only_after_complete_validation(db):
    """The R5 Government pagination lesson, as a gate rather than a note.

    A capture that holds fewer records than the upstream declared looks
    exactly like a complete one from inside any single record. Here it cannot
    be activated at all, so nothing downstream can read it as complete.
    """
    lease, _ = _evidence_fixture(db, "catalog-active")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-active-gate', declared=3)}'::jsonb)")
    assert db.psql(f"select activated_at is null, validation_state from public.catalog_source_snapshots where id='{snapshot}'") == "t|pending"
    for index in range(2):
        _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'gate-{index}', upstream=str(index), payload={'_id': index})}'::jsonb)")
    # Two of three: refused, and the snapshot stays unusable.
    with pytest.raises(AssertionError, match="catalog snapshot is incomplete"):
        _rpc_as_service(db, f"select public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    assert db.psql(f"select activated_at is null from public.catalog_source_snapshots where id='{snapshot}'") == "t"
    # The third record completes it, and only then does activation succeed.
    _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, 'gate-2', upstream='2', payload={'_id': 2})}'::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    assert db.psql(f"select activated_at is not null, validation_state, stored_record_count = declared_record_count from public.catalog_source_snapshots where id='{snapshot}'") == "t|complete|t"
    # Activation replays cleanly and can never be revoked.
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    with pytest.raises(AssertionError, match="an active catalog snapshot is immutable"):
        _rpc_as_service(db, f"select public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot, 'validation_state': 'failed'})}'::jsonb)")
    # The constraint holds even against a direct write by a superuser.
    with pytest.raises(AssertionError, match="active_only_when_complete|immutable"):
        db.psql("insert into public.catalog_source_snapshots (created_by_run_id, source_family, "
                "trust_state, resource_id, upstream_version, upstream_version_kind, content_sha256, "
                "retrieved_at, declared_record_count, stored_record_count, validation_state, "
                "activated_at, snapshot_key) select id, 'government', 'evidence', 'r', '2026.1', "
                "'dataset_version', repeat('d', 64), now(), 5, 2, 'complete', now(), "
                "'cs1.00000000000000000000000000000002' from public.runs limit 1")


def test_catalog_candidates_may_stay_ambiguous_and_never_guess_an_identity(db):
    """`ambiguous` is an ANSWER, not a staging state.

    A source that states two identities for one vehicle has said something
    true. Forcing a resolution here would invent the fact the source declined
    to state -- the same refusal the R5 registry tool makes for model year
    2026.
    """
    _, _, args, snapshot, record, _ = _catalog_fixture(db, "ambiguous")
    ambiguous = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-unresolved', status='ambiguous', trim=None)}'::jsonb)")
    assert db.psql(f"select status, trim is null from public.catalog_candidate_variants where id='{ambiguous}'") == "ambiguous|t"
    # It stays ambiguous across replays; nothing resolves it implicitly.
    _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-unresolved', status='ambiguous', trim=None)}'::jsonb)")
    assert db.psql(f"select status from public.catalog_candidate_variants where id='{ambiguous}'") == "ambiguous"
    # A later reading MAY revise the status -- that is a decision, made
    # explicitly -- but it can never revise who the candidate is.
    _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-unresolved', status='ready_for_review', trim=None)}'::jsonb)")
    assert db.psql(f"select status from public.catalog_candidate_variants where id='{ambiguous}'") == "ready_for_review"
    with pytest.raises(AssertionError, match="catalog candidate idempotency conflict"):
        _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-unresolved', status='ready_for_review', trim='XSE')}'::jsonb)")

    # No guessed identity: a half-stated year range, an empty dimension, a
    # padded one and a dimension outside the closed vocabulary are refusals
    # rather than fields quietly dropped or filled in.
    half_range = json.loads(_catalog_candidate_json(snapshot, record, "cand-half"))
    half_range.pop("model_year_end")
    with pytest.raises(AssertionError, match="model year range must be whole"):
        _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{json.dumps(half_range)}'::jsonb)")
    for dimensions in ({"drivetrain": ""}, {"drivetrain": " awd"}, {"horsepower": "302"},
                       {"drivetrain": 4}):
        bad = _catalog_candidate_json(snapshot, record, f"cand-{abs(hash(str(dimensions)))}",
                                      dimensions=dimensions)
        with pytest.raises(AssertionError, match="dimensions_allowlisted"):
            _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{bad}'::jsonb)")
    with pytest.raises(AssertionError, match="invalid catalog candidate status"):
        _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-bad-status', status='verified')}'::jsonb)")
    # A candidate that states no year at all is legitimate: an absent range is
    # an absent statement, not a defect.
    yearless = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-yearless', years=None)}'::jsonb)")
    assert db.psql(f"select model_year_start is null from public.catalog_candidate_variants where id='{yearless}'") == "t"


def test_catalog_writes_reject_every_stale_lease_attempt_and_token(db):
    """A superseded worker writes nothing, on every catalog path.

    Same guarantee the evidence RPCs already give, validated atomically in the
    database rather than by an application-side read-then-write check.
    """
    lease, _ = _evidence_fixture(db, "catalog-stale")
    run_id, worker, attempt, token, _ = lease
    good = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({good},'{_catalog_snapshot_json('snap-stale')}'::jsonb)")
    before = db.psql("select count(*) from public.catalog_source_snapshots")

    bad_leases = [("other-worker", attempt, token, False),
                  (worker, str(int(attempt) + 1), token, False),
                  (worker, attempt, "not-the-token", False),
                  (worker, attempt, token, True)]
    for bad_worker, bad_attempt, bad_token, expire in bad_leases:
        if expire:
            db.psql(f"update public.runs set lease_expires_at=now()-interval '1 second' where id='{run_id}'")
        bad = f"'{run_id}','{bad_worker}',{bad_attempt},'{bad_token}'"
        for call in (f"select public.record_catalog_snapshot_guarded({bad},'{_catalog_snapshot_json('snap-never')}'::jsonb)",
                     f"select public.record_catalog_raw_record_guarded({bad},'{_catalog_record_json(snapshot, 'rec-never')}'::jsonb)",
                     f"select public.activate_catalog_snapshot_guarded({bad},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)",
                     f"select public.record_catalog_candidate_guarded({bad},'{{}}'::jsonb)",
                     f"select public.link_catalog_candidate_evidence_guarded({bad},'{{}}'::jsonb)"):
            with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
                _rpc_as_service(db, call)
    db.psql(f"update public.runs set lease_expires_at=now()+interval '5 minutes' where id='{run_id}'")
    # Not one byte was written by any of the twenty refused calls.
    assert db.psql("select count(*) from public.catalog_source_snapshots") == before
    assert db.psql(f"select count(*) from public.catalog_raw_records where snapshot_id='{snapshot}'") == "0"
    assert db.psql(f"select activated_at is null from public.catalog_source_snapshots where id='{snapshot}'") == "t"


def test_catalog_rejects_cross_run_and_cross_snapshot_linkage(db):
    """Provenance is exact: another run's evidence, and another snapshot's
    record, are both refusals rather than plausible-looking links."""
    lease, other, args, snapshot, record, candidate = _catalog_fixture(db, "cross")
    run_id, worker, attempt, token, _ = lease
    run_b, worker_b, attempt_b, token_b, _ = other
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"

    # A second snapshot, in the same run, with its own record.
    other_snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-cross-2', declared=1)}'::jsonb)")
    other_record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(other_snapshot, 'rec-cross-2', upstream='37392')}'::jsonb)")

    # Cross-SNAPSHOT: a candidate must be filed under the snapshot its record
    # actually belongs to. Naming a different one is refused.
    with pytest.raises(AssertionError, match="catalog candidate snapshot mismatch"):
        _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, other_record, 'cand-cross')}'::jsonb)")
    # A raw record may not join a snapshot describing a different resource.
    with pytest.raises(AssertionError, match="catalog raw record resource mismatch"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(other_snapshot, 'rec-wrong-resource', resource='5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6')}'::jsonb)")

    # Cross-RUN: run B's source cannot be attached to a candidate by run A.
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args_b},'{_r3_source_json('catalog-cross-source-b')}'::jsonb)")
    with pytest.raises(AssertionError, match="invalid catalog evidence link source"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source_b, 'link-cross')}'::jsonb)")

    # A claim that exists but rests on a DIFFERENT source is refused too:
    # sharing a run is not provenance.
    source_a = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('catalog-cross-source-a')}'::jsonb)")
    other_source_a = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('catalog-cross-source-a2')}'::jsonb)")
    for grounded, key in ((source_a, "catalog-cross-fragment-a"),
                          (other_source_a, "catalog-cross-fragment-a2")):
        _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(grounded, key=key)}'::jsonb)")
    claim_a = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('catalog-cross-claim', other_source_a, 1798)}'::jsonb)")
    with pytest.raises(AssertionError, match="claim source mismatch"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source_a, 'link-mismatch', claim_id=claim_a)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links where candidate_id='{candidate}'") == "0"

    # The honest link -- candidate, its own run's source, and the claim that
    # rests on exactly that source -- is accepted, and replays idempotently.
    good_claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('catalog-good-claim', source_a, 1798)}'::jsonb)")
    payload = _catalog_link_json(candidate, source_a, "link-good", claim_id=good_claim,
                                 locator=None, version=None, kind=None)
    link = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{payload}'::jsonb)")
    assert _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{payload}'::jsonb)") == link
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links where candidate_id='{candidate}'") == "1"
    # Re-pointing a link under the same key is a new fact, not a retry. Proven
    # with a different CLAIM, because the locator and the version are derived
    # from the evidence now and are no longer the caller's to vary.
    other_claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('catalog-good-claim-2', source_a, 1799)}'::jsonb)")
    with pytest.raises(AssertionError, match="catalog evidence link idempotency conflict"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source_a, 'link-good', claim_id=other_claim, locator=None, version=None, kind=None)}'::jsonb)")
    # The exact locator and the exact source version travelled with the link.
    assert db.psql(f"select record_locator = '{R3_LOCATOR}', source_version, source_version_kind from public.catalog_candidate_evidence_links where id='{link}'") == f"t|{R3_VERSION[1]}|{R3_VERSION[0]}"


def test_the_legacy_catalog_can_suggest_a_candidate_but_never_verifies_a_fact(db):
    """The product decision, end to end against the real schema.

    A `legacy_reference` snapshot may carry raw records and candidates -- that
    is discovery, aliasing and comparison, which is what the old catalog is
    good for. It may never carry a verdict, so nothing reading these links can
    ever treat the old catalog as having confirmed anything.
    """
    lease, _ = _evidence_fixture(db, "catalog-legacy")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-legacy', family='legacy_reference', resource='model_technical_catalog_il')}'::jsonb)")
    assert db.psql(f"select source_family, trust_state from public.catalog_source_snapshots where id='{snapshot}'") == "legacy_reference|unverified"
    record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, 'rec-legacy', resource='model_technical_catalog_il')}'::jsonb)")
    candidate = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, 'cand-legacy', status='candidate')}'::jsonb)")

    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('catalog-legacy-source')}'::jsonb)")
    # R3 order: a located fact must already be backed by focused evidence of
    # its own source, so the fragment is recorded before the claim.
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key='catalog-legacy-fragment')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('catalog-legacy-claim', source, 1798)}'::jsonb)")
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key='catalog-legacy-verdict', support=[_r4_support(fragment)])}'::jsonb)")

    # Discovery: an unverified candidate may cite a source, with no verdict.
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-legacy-discovery', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    # Verification: refused, because the snapshot is not evidence.
    with pytest.raises(AssertionError, match="an unverified catalog source cannot carry a verdict"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-legacy-verdict', claim_id=claim, verdict_id=verdict, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links where candidate_id='{candidate}' and verdict_id is not null") == "0"

    # The same verdict on a GOVERNMENT candidate is accepted, so the refusal
    # above is about the source's trust state and nothing else.
    gov_snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-legacy-gov')}'::jsonb)")
    gov_record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(gov_snapshot, 'rec-legacy-gov')}'::jsonb)")
    gov_candidate = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(gov_snapshot, gov_record, 'cand-legacy-gov')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(gov_candidate, source, 'link-gov-verdict', claim_id=claim, verdict_id=verdict)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links where candidate_id='{gov_candidate}' and verdict_id='{verdict}'") == "1"
    # A verdict with no claim beside it is never provenance.
    # A verdict with no claim beside it is never provenance -- and after the
    # corrective round the claim is required before the verdict is even read.
    with pytest.raises(AssertionError, match="requires the claim it is evidence for"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(gov_candidate, source, 'link-orphan-verdict', verdict_id=verdict, locator=None, version=None, kind=None)}'::jsonb)")


def test_no_canonical_row_can_be_created_through_the_staging_write_path(pr3_db):
    """Ingestion is persistence, not promotion -- and the database says so.

    Every guarded STAGING write is exercised, then the canonical relations are
    counted. They are still empty: none of the five staging RPCs names a
    canonical relation at all, so landing a whole Government capture cannot
    produce a canonical row by any path.

    Catalog PR3 grants `service_role` the INSERT those relations needed, so
    "no role holds the privilege" is no longer what stops a bare insert.
    What stops it is the DEFERRED constraint trigger: a canonical row whose
    stated fields are not all covered by verified field provenance cannot
    COMMIT, whichever writer attempted it.
    """
    db = pr3_db
    _, _, args, snapshot, record, candidate = _catalog_fixture(db, "no-canonical")
    run_id = args.split(",")[0].strip("'")
    source, claim = _catalog_claim_for(db, args, "catalog-canon-source")
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-canon', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    # Staging rows exist; canonical rows do not.
    assert db.psql(f"select count(*) from public.catalog_candidate_variants where snapshot_id='{snapshot}'") >= "1"
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"
    # And no STAGING rpc mentions the canonical tables. The promotion RPC does,
    # and it is the only one -- checked by name rather than by absence.
    for rpc in CATALOG_RPCS:
        body = db.psql(f"select prosrc from pg_proc where proname='{rpc}'")
        for table in CATALOG_CANONICAL_TABLES:
            assert table not in body, (rpc, table)
    canonical_writers = db.psql(
        "select string_agg(proname, ',' order by proname) from pg_proc "
        "where prosrc like '%insert into public.catalog_model_variants%'")
    assert canonical_writers == "promote_catalog_variant_guarded"
    # A bare canonical insert is refused at COMMIT, by the provenance gate.
    # The back-pointer names a REAL verified verdict, so the refusal below is
    # the provenance coverage rule and not a missing foreign key.
    evidence = _pr3_field_evidence(
        db, args, "no-canonical-year", "model_year_start", 2021, "year", "shnat_yitzur",
        record_id=record_locator_id(_catalog_key("catalog.snapshot", "no-canonical"), "36327"),
        scope=_pr3_scope("Toyota"))
    model_key, variant_key = "cm1." + "0" * 32, "cv1." + "0" * 32
    with pytest.raises(AssertionError, match="requires verified provenance for every field"):
        db.psql("begin; set role service_role; "
                "insert into public.catalog_models (manufacturer, commercial_model, canonical_key) "
                f"values ('Toyota','RAV4','{model_key}'); "
                "insert into public.catalog_model_variants (model_id, promoted_from_candidate_id, "
                "promoted_from_verdict_id, canonical_key, model_year_start, model_year_end) "
                f"select id, '{candidate}', '{evidence['verdict']}', '{variant_key}', 2021, 2021 "
                f"from public.catalog_models where canonical_key='{model_key}'; commit")
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"
    assert db.psql(f"select count(*) from public.catalog_raw_records where id='{record}'") == "1"
    assert db.psql(f"select count(*) from public.runs where id='{run_id}'") == "1"


def test_catalog_state_outlives_evidence_and_never_cascades_from_a_run(db):
    """The whole reason these relations exist.

    public.sources, public.claims and public.claim_verdicts all carry
    `on delete cascade` to public.runs: delete the run and the evidence is
    gone. A catalog cannot be built on that, so every catalog reference is
    `on delete restrict` -- durable state is never silently removed, and the
    run it depends on cannot be deleted out from under it either.
    """
    cascading = db.psql(
        "select c.conrelid::regclass::text || '.' || a.attname from pg_constraint c "
        "join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1] "
        "where c.contype='f' and c.confdeltype <> 'r' "
        "and c.conrelid::regclass::text like 'catalog\\_%' order by 1").splitlines()
    assert cascading == [], f"catalog foreign keys must all RESTRICT: {cascading}"

    _, _, args, snapshot, record, candidate = _catalog_fixture(db, "outlives")
    run_id = args.split(",")[0].strip("'")
    source, claim = _catalog_claim_for(db, args, "catalog-outlive-source")
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-outlive', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    # Deleting the run would cascade the evidence away. It is refused, and the
    # catalog rows are all still there afterwards.
    with pytest.raises(AssertionError, match="violates foreign key constraint|append-only"):
        db.psql(f"delete from public.runs where id='{run_id}'")
    assert db.psql(f"select count(*) from public.runs where id='{run_id}'") == "1"
    # And the refusal is the CATALOG's, independently of any other guard: the
    # cited claim cannot be removed while a catalog link names it.
    with pytest.raises(AssertionError,
                       match="catalog_candidate_evidence_links_claim_id_fkey"):
        db.psql(f"delete from public.claims where id='{claim}'")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links where candidate_id='{candidate}'") == "1"
    assert db.psql(f"select count(*) from public.catalog_raw_records where id='{record}'") == "1"
    assert db.psql(f"select count(*) from public.catalog_source_snapshots where id='{snapshot}'") == "1"


def test_catalog_rpcs_refuse_reasoning_and_credential_shaped_payloads(db):
    """The same finite marker set every other durable evidence write applies.

    It screens the three BACKEND-AUTHORED payloads -- snapshot provenance,
    candidate identity and evidence link. It deliberately does not screen a
    raw record's payload, which is source content captured verbatim: a keyword
    screen there would silently drop legitimate upstream rows whose own field
    names collide. That payload is bounded structurally instead (object shape,
    size bound, recomputed digest), which the tests above exercise.
    """
    _, _, args, snapshot, record, candidate = _catalog_fixture(db, "unsafe")
    unsafe_snapshot_keys, unsafe_candidate_keys, unsafe_link_keys = [], [], []
    for field in ("chain_of_thought", "provider_detail", "raw_error", "api_key", "lease_token"):
        snapshot_payload = json.loads(_catalog_snapshot_json(f"snap-unsafe-{field}"))
        snapshot_payload[field] = "anything at all"
        unsafe_snapshot_keys.append(snapshot_payload["snapshot_key"])
        candidate_payload = json.loads(_catalog_candidate_json(snapshot, record, f"cand-unsafe-{field}"))
        candidate_payload[field] = "anything at all"
        unsafe_candidate_keys.append(candidate_payload["candidate_key"])
        link_payload = json.loads(_catalog_link_json(candidate, str(uuid.uuid4()), f"link-unsafe-{field}"))
        link_payload[field] = "anything at all"
        unsafe_link_keys.append(link_payload["link_key"])
        for rpc, payload in (("record_catalog_snapshot_guarded", snapshot_payload),
                             ("record_catalog_candidate_guarded", candidate_payload),
                             ("link_catalog_candidate_evidence_guarded", link_payload)):
            with pytest.raises(AssertionError, match="unsafe catalog payload rejected"):
                _rpc_as_service(db, f"select public.{rpc}({args},'{json.dumps(payload)}'::jsonb)")
    # Nothing was written by any of the refused calls. Counted by the keys the
    # helpers derived for them, since a catalog key is structural now.
    for table, column, domain, labels in (
            ("catalog_source_snapshots", "snapshot_key", "catalog.snapshot", unsafe_snapshot_keys),
            ("catalog_candidate_variants", "candidate_key", "catalog.candidate", unsafe_candidate_keys),
            ("catalog_candidate_evidence_links", "link_key", "catalog.evidence_link", unsafe_link_keys)):
        listed = ", ".join(f"'{key}'" for key in labels)
        assert db.psql(f"select count(*) from public.{table} where {column} in ({listed})") == "0"


# ===========================================================================
# Catalog PR1 corrective round: integrity gaps and their repairs
# ===========================================================================
#
# Catalog PR1 established the relations and the lease/idempotency posture, but
# six of its guarantees were weaker than the migration and the documentation
# said. Each test below FAILED against the PR1 schema and passes against the
# corrective migration; each names the defect it pins.
#
# The common thread is the difference between SYNTACTIC and REFERENTIAL
# validation. PR1 checked that a caller's locator and source version parsed
# (`r3_canonical_locator`, `r3_source_version_valid`) and then stored them
# verbatim beside a claim and a source that might say something else entirely.
# A provenance field that is merely well-formed is not provenance.


def _corrective_evidence(db, suffix, *, verdict="verified", source_key=None):
    """One run with a real source, fragment, claim and verdict of its own.

    Deliberately built through the REAL evidence RPCs -- no invented uuid ever
    stands in for a claim or a verdict here, because the whole point of this
    round is that a link must cite evidence that actually exists and actually
    says what the link claims it says.
    """
    lease, other = _evidence_fixture(db, f"corrective-{suffix}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    key = source_key or f"corrective-{suffix}"
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json(key)}'::jsonb)")
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{_r3_fragment_json(source, key=f'{key}-fragment')}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json(f'{key}-claim', source, 1798)}'::jsonb)")
    verdict_id = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{_r4_verdict_json(claim, key=f'{key}-verdict', verdict=verdict, support=[_r4_support(fragment)])}'::jsonb)")
    return lease, other, args, source, claim, verdict_id


def _catalog_chain(db, suffix, args, *, family="government"):
    """A complete, active snapshot with one record and one candidate."""
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json(f'snap-{suffix}', family=family, resource=('model_technical_catalog_il' if family == 'legacy_reference' else None) or '142afde2-6228-49f9-8a29-9b6c3a0cbe40')}'::jsonb)")
    record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-{suffix}', resource=('model_technical_catalog_il' if family == 'legacy_reference' else '142afde2-6228-49f9-8a29-9b6c3a0cbe40'))}'::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    candidate = _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{_catalog_candidate_json(snapshot, record, f'cand-{suffix}')}'::jsonb)")
    return snapshot, record, candidate


# --- defect 1: an unverified verdict could back a catalog link --------------

@pytest.mark.parametrize("verdict_state", ["needs_review", "rejected"])
def test_only_a_verified_verdict_may_back_a_catalog_evidence_link(db, verdict_state):
    """PR1 checked that the verdict EXISTED, never what it SAID.

    `claim_verdicts.verdict` is one of verified / needs_review / rejected. A
    link carrying a `rejected` verdict asserted the opposite of what the
    verifier concluded, and a promotion path reading `verdict_id is not null`
    as "this fact was confirmed" would have promoted a refuted fact.
    """
    lease, _, args, source, claim, verdict = _corrective_evidence(
        db, f"verdict-{verdict_state}", verdict=verdict_state)
    _, _, candidate = _catalog_chain(db, f"verdict-{verdict_state}", args)
    payload = _catalog_link_json(candidate, source, f"link-{verdict_state}",
                                 claim_id=claim, verdict_id=verdict)
    with pytest.raises(AssertionError, match="catalog evidence link verdict is not verified"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "0"

    # The same link with the run's VERIFIED verdict is accepted, so the refusal
    # is about what the verdict says and nothing else.
    _, _, verified_args, verified_source, verified_claim, verified_verdict = _corrective_evidence(
        db, f"verdict-ok-{verdict_state}")
    _, _, verified_candidate = _catalog_chain(db, f"verdict-ok-{verdict_state}", verified_args)
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({verified_args},'{_catalog_link_json(verified_candidate, verified_source, 'link-verified', claim_id=verified_claim, verdict_id=verified_verdict)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{verified_candidate}' and verdict_id='{verified_verdict}'") == "1"


# --- defect 2: the stored locator need not be the claim's locator -----------

def test_a_links_record_locator_must_be_the_claims_own_evidence_locator(db):
    """PR1 accepted any SYNTACTICALLY valid locator and stored it verbatim.

    `r3_canonical_locator(...) is not null` proves a locator is well-formed. It
    proves nothing about whether it is the locator the cited claim was read at,
    so a link could point at a different field of a different record and still
    look like exact provenance.
    """
    lease, _, args, source, claim, verdict = _corrective_evidence(db, "locator")
    _, _, candidate = _catalog_chain(db, "locator", args)
    # A different, perfectly well-formed locator: same document, other span.
    forged = document_span_locator("doc-1", 900, 950, section="Other section").locator_key
    assert db.psql(f"select public.r3_canonical_locator('{forged}') is not null") == "t"
    with pytest.raises(AssertionError, match="record locator does not match the cited claim"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-forged-locator', claim_id=claim, locator=forged)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "0"

    # Omitting it entirely is the supported path: the locator is DERIVED from
    # the claim, so there is nothing for a caller to get wrong.
    derived = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-derived-locator', claim_id=claim, locator=None)}'::jsonb)")
    assert db.psql(f"select record_locator from public.catalog_candidate_evidence_links "
                   f"where id='{derived}'") == db.psql(
        f"select evidence_locator from public.claims where id='{claim}'")


# --- defect 3: the stored source version need not be the source's -----------

@pytest.mark.parametrize("field, value", [
    ("version", "2099.12.31"),
    ("kind", "content_sha256"),
])
def test_a_links_source_version_must_be_the_cited_sources_own_version(db, field, value):
    """Same defect, on the version rather than the locator.

    `r3_source_version_valid` checks the SHAPE of a version identifier. A link
    could therefore claim a fact was read at a version the source was never
    captured at -- which is precisely the kind of claim provenance exists to
    make unfalsifiable.
    """
    lease, _, args, source, claim, verdict = _corrective_evidence(db, f"version-{field}")
    _, _, candidate = _catalog_chain(db, f"version-{field}", args)
    override = {"version": value} if field == "version" else {"kind": value}
    payload = _catalog_link_json(candidate, source, f"link-forged-{field}", claim_id=claim,
                                 version=override.get("version", R3_VERSION[1]),
                                 kind=override.get("kind", R3_VERSION[0]))
    if field == "kind":
        # A content_sha256 kind needs a hash-shaped identifier to pass the R3
        # syntactic gate at all, so the forgery is internally well-formed.
        payload = _catalog_link_json(candidate, source, f"link-forged-{field}", claim_id=claim,
                                     version="e" * 64, kind="content_sha256")
    with pytest.raises(AssertionError, match="source version does not match the cited source"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{payload}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "0"

    # Derived from the source, the link records exactly what was captured.
    derived = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, f'link-derived-{field}', claim_id=claim, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select source_version_kind || '|' || source_version from "
                   f"public.catalog_candidate_evidence_links where id='{derived}'") == db.psql(
        f"select source_version_kind || '|' || source_version_id from public.sources "
        f"where id='{source}'")


def test_an_evidence_link_requires_a_real_claim_and_fails_closed_without_provenance(db):
    """A link with no claim stored exact-fact provenance it could not support.

    PR1 allowed a source-only link that still carried a `record_locator` and a
    `source_version` -- fields that assert WHERE a fact was read and at WHICH
    version, with nothing to check them against. The corrected contract
    requires the claim, so every link's provenance is derivable and checkable.
    """
    lease, _, args, source, claim, _ = _corrective_evidence(db, "needs-claim")
    _, _, candidate = _catalog_chain(db, "needs-claim", args)
    with pytest.raises(AssertionError, match="requires the claim it is evidence for"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-no-claim')}'::jsonb)")

    # A source whose own version was never recorded cannot back a link either:
    # missing provenance fails closed rather than defaulting to the caller's.
    # (R3 already refuses a FOCUSED fragment on an unversioned source, so this
    # claim is deliberately unlocated -- which is also the case that proves the
    # locator must come from the claim rather than from the caller.)
    unversioned = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{_r3_source_json('corrective-unversioned', kind=None, identifier=None)}'::jsonb)")
    unversioned_claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{_r3_claim_json('corrective-unversioned-claim', unversioned, 1798, locator=None, unit=None)}'::jsonb)")
    with pytest.raises(AssertionError, match="states no evidence locator|states no version"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, unversioned, 'link-unversioned', claim_id=unversioned_claim, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "0"


def test_the_legacy_catalog_still_cannot_carry_a_verdict_after_the_correction(db):
    """The PR1 product decision survives the tightening, unchanged."""
    lease, _, args, source, claim, verdict = _corrective_evidence(db, "legacy-still")
    _, _, candidate = _catalog_chain(db, "legacy-still", args, family="legacy_reference")
    with pytest.raises(AssertionError, match="unverified catalog source cannot carry a verdict"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-legacy-verdict', claim_id=claim, verdict_id=verdict)}'::jsonb)")
    # Discovery -- claim-backed, verdict-free -- still works for the legacy
    # family, and now carries provenance derived from the claim and the source
    # rather than caller text.
    link = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-legacy-discovery', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select verdict_id is null from public.catalog_candidate_evidence_links "
                   f"where id='{link}'") == "t"


# --- defect 5: a failed snapshot was not terminal ---------------------------

def test_a_failed_snapshot_is_terminal_and_accepts_no_further_record(db):
    """PR1 refused appends to an ACTIVE snapshot and forgot the FAILED one.

    `activated_at is not null` was the only gate, and a failed snapshot has no
    activation timestamp -- so records could keep arriving after a capture had
    been declared unusable, and the same snapshot could then be activated
    complete as though the failure had never been recorded.
    """
    lease, _ = _evidence_fixture(db, "corrective-failed")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-failed', declared=2)}'::jsonb)")
    _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, 'rec-failed-1')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot, 'validation_state': 'failed'})}'::jsonb)")
    assert db.psql(f"select validation_state, activated_at is null from "
                   f"public.catalog_source_snapshots where id='{snapshot}'") == "failed|t"

    # No further record may be appended...
    with pytest.raises(AssertionError, match="failed catalog snapshot is terminal"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, 'rec-failed-2', upstream='37392', payload={'_id': 37392})}'::jsonb)")
    # ...and it can never become complete, even once its counts would agree.
    with pytest.raises(AssertionError, match="failed catalog snapshot is terminal"):
        _rpc_as_service(db, f"select public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    # Re-declaring the same failure is an idempotent no-op, not an error.
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot, 'validation_state': 'failed'})}'::jsonb)")
    assert db.psql(f"select validation_state, stored_record_count from "
                   f"public.catalog_source_snapshots where id='{snapshot}'") == "failed|1"
    # Not even a direct write can reopen it.
    with pytest.raises(AssertionError, match="terminal|immutable"):
        db.psql(f"update public.catalog_source_snapshots set validation_state='complete' "
                f"where id='{snapshot}'")


def test_snapshot_ingestion_and_activation_require_the_creating_runs_lease(db):
    """A snapshot belongs to the run that opened it.

    PR1 checked only that SOME run held a valid lease, so any concurrently
    leased run could append records to, and activate, a capture it did not
    open -- and the snapshot's `created_by_run_id` would still name the first
    run, making the provenance wrong rather than merely loose.
    """
    lease, other = _evidence_fixture(db, "corrective-owner")
    run_a, worker_a, attempt_a, token_a, _ = lease
    run_b, worker_b, attempt_b, token_b, _ = other
    args_a = f"'{run_a}','{worker_a}',{attempt_a},'{token_a}'"
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args_a},'{_catalog_snapshot_json('snap-owned', declared=1)}'::jsonb)")

    for call in (f"select public.record_catalog_raw_record_guarded({args_b},'{_catalog_record_json(snapshot, 'rec-foreign')}'::jsonb)",
                 f"select public.activate_catalog_snapshot_guarded({args_b},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)"):
        with pytest.raises(AssertionError, match="does not belong to this run"):
            _rpc_as_service(db, call)
    assert db.psql(f"select stored_record_count, activated_at is null from "
                   f"public.catalog_source_snapshots where id='{snapshot}'") == "0|t"

    # The owning run proceeds normally.
    _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args_a},'{_catalog_record_json(snapshot, 'rec-owned')}'::jsonb)")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args_a},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    assert db.psql(f"select activated_at is not null from public.catalog_source_snapshots "
                   f"where id='{snapshot}'") == "t"


def test_a_later_run_may_add_evidence_to_an_existing_candidate_deliberately(db):
    """The ONE cross-run allowance, stated and tested on its own.

    Snapshot ownership is exclusive: only the creating run fills and activates
    a capture. Evidence LINKS are deliberately not, because a later run may
    verify an existing candidate with evidence of its own. That later run's
    source, claim and verdict must all belong to it, and the link records
    which run supplied them -- so the allowance widens who may ADD evidence,
    never who may speak for another run's evidence.
    """
    lease, other, args_a, source_a, claim_a, verdict_a = _corrective_evidence(db, "later-run")
    run_b, worker_b, attempt_b, token_b, _ = other
    args_b = f"'{run_b}','{worker_b}',{attempt_b},'{token_b}'"
    snapshot, record, candidate = _catalog_chain(db, "later-run", args_a)

    # Run B builds its own evidence and links it to run A's candidate.
    source_b = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args_b},'{_r3_source_json('corrective-later-b')}'::jsonb)")
    fragment_b = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args_b},'{_r3_fragment_json(source_b, key='corrective-later-b-fragment')}'::jsonb)")
    claim_b = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args_b},'{_r3_claim_json('corrective-later-b-claim', source_b, 1798)}'::jsonb)")
    verdict_b = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args_b},'{_r4_verdict_json(claim_b, key='corrective-later-b-verdict', support=[_r4_support(fragment_b)])}'::jsonb)")
    link_b = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args_b},'{_catalog_link_json(candidate, source_b, 'link-later-b', claim_id=claim_b, verdict_id=verdict_b, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select run_id from public.catalog_candidate_evidence_links "
                   f"where id='{link_b}'") == run_b

    # But run B still cannot borrow run A's evidence, and still cannot touch
    # run A's snapshot.
    with pytest.raises(AssertionError, match="invalid catalog evidence link source"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args_b},'{_catalog_link_json(candidate, source_a, 'link-borrowed', claim_id=claim_a, verdict_id=verdict_a, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "1"


# --- defect 4/D: the payload digest was the caller's to get right -----------

def test_the_raw_record_digest_is_derived_and_a_supplied_one_is_refused(db):
    """PR1 made PR2 reproduce PostgreSQL's incidental `jsonb::text` rendering.

    The stored hash was compared against `encode(sha256(convert_to(payload::text
    ...)))`, so a correct caller had to predict jsonb's key ordering and
    separator style exactly. That is not a security property, it is a
    formatting coincidence, and the first ingestion path that formatted its
    JSON differently would have failed for no real reason. The digest is now
    derived inside the trusted path and a supplied one is refused outright.
    """
    lease, _ = _evidence_fixture(db, "corrective-hash")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json('snap-hash', declared=1)}'::jsonb)")
    supplied = json.loads(_catalog_record_json(snapshot, "rec-hash"))
    # PR1 REQUIRED this field and compared it against `jsonb::text`; a correct
    # caller had to predict jsonb's key ordering and separator style. Even the
    # value PostgreSQL itself would compute is now refused, because the field
    # is no longer read at all.
    correct = hashlib.sha256(json.dumps(supplied["payload"], separators=(", ", ": ")
                                        ).encode("utf-8")).hexdigest()
    for digest in (correct, "c" * 64):
        with pytest.raises(AssertionError, match="payload digest is derived"):
            _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{json.dumps({**supplied, 'payload_sha256': digest})}'::jsonb)")

    # Without it, the record is stored and the digest is the database's own.
    record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{json.dumps(supplied)}'::jsonb)")
    assert db.psql(f"select payload_sha256 = encode(sha256(convert_to(payload::text, 'UTF8')), 'hex') "
                   f"from public.catalog_raw_records where id='{record}'") == "t"
    # A replay whose payload differs still fails closed on the same identity.
    changed = {**supplied, "payload": {"_id": 36327, "kinuy_mishari": "RAV4 PHEV"}}
    with pytest.raises(AssertionError, match="catalog raw record idempotency conflict"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{json.dumps(changed)}'::jsonb)")


def test_a_catalog_key_must_carry_its_own_domain_and_shape(db):
    """Keys are structural, never free text and never model-authored.

    The database enforces the two halves it can check without depending on one
    language's JSON rendering: a key's DOMAIN prefix and its SHAPE. The full
    derivation from an object's identity is a repository rule -- reproducing
    that digest in SQL would mean reproducing Python's exact JSON formatting in
    SQL, which is the very brittleness this round removes from the payload
    hash.
    """
    lease, _ = _evidence_fixture(db, "corrective-keys")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    payload = json.loads(_catalog_snapshot_json("snap-keys"))
    assert payload["snapshot_key"].startswith("cs1.")

    for bad in ("a-name-i-chose", "snap-keys", "cr1." + payload["snapshot_key"][4:],
                "cs1.NOTHEX" + "0" * 26):
        with pytest.raises(AssertionError, match="catalog_source_snapshots_key_derived"):
            _rpc_as_service(db, f"select public.record_catalog_snapshot_guarded({args},'{json.dumps({**payload, 'snapshot_key': bad})}'::jsonb)")
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{json.dumps(payload)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_source_snapshots where id='{snapshot}'") == "1"


def test_a_rename_cannot_duplicate_a_logical_catalog_identity(db):
    """Natural uniqueness, so the KEY is not the only thing holding identity.

    PR1 keyed every relation solely on a caller-supplied name, so the same
    retrieval, the same reading and the same citation could each be stored
    twice by being called something else. Each now carries a unique index over
    what actually makes it that object.
    """
    lease, _, args, source, claim, verdict = _corrective_evidence(db, "natural")
    snapshot, record, candidate = _catalog_chain(db, "natural", args)

    # Same retrieval, different name -> refused by the schema, not the RPC.
    duplicate = json.loads(_catalog_snapshot_json("snap-natural"))
    duplicate["snapshot_key"] = _catalog_key("catalog.snapshot", "snap-natural-renamed")
    with pytest.raises(AssertionError, match="catalog_source_snapshots_natural_uidx"):
        _rpc_as_service(db, f"select public.record_catalog_snapshot_guarded({args},'{json.dumps(duplicate)}'::jsonb)")

    # Same reading of the same record, different name -> refused.
    twin = json.loads(_catalog_candidate_json(snapshot, record, "cand-natural"))
    twin["candidate_key"] = _catalog_key("catalog.candidate", "cand-natural-renamed")
    with pytest.raises(AssertionError, match="catalog_candidate_variants_natural_uidx"):
        _rpc_as_service(db, f"select public.record_catalog_candidate_guarded({args},'{json.dumps(twin)}'::jsonb)")

    # Same citation of the same claim, different name -> refused.
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-natural', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    with pytest.raises(AssertionError, match="catalog_candidate_evidence_links_natural_uidx"):
        _rpc_as_service(db, f"select public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-natural-renamed', claim_id=claim, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "1"

    # The verdict-bearing citation of the SAME claim is a different statement
    # and is kept alongside the discovery link, by design.
    _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-natural-verified', claim_id=claim, verdict_id=verdict, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_evidence_links "
                   f"where candidate_id='{candidate}'") == "2"


def test_every_catalog_foreign_key_column_is_indexed(db):
    """A referencing column with no index makes every delete-check a scan.

    Enumerated from the catalog rather than listed, so a future migration that
    adds a foreign key without its index fails here instead of at load.
    """
    unindexed = db.psql("""
        select c.conrelid::regclass::text || '.' || a.attname
        from pg_constraint c
        join unnest(c.conkey) with ordinality as k(attnum, ord) on true
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = k.attnum
        where c.contype = 'f'
          and c.conrelid::regclass::text like 'catalog\\_%'
          and k.ord = 1
          and not exists (
            select 1 from pg_index i
            where i.indrelid = c.conrelid and i.indkey[0] = a.attnum)
        order by 1
    """).splitlines()
    assert unindexed == [], f"catalog FK columns without a leading index: {unindexed}"


# --- defect 7: canonical rows could change their facts ----------------------

def test_canonical_rows_are_fully_immutable_until_pr3_adds_field_provenance(db):
    """PR1's trigger froze identity and provenance but not the FACTS.

    `catalog_model_variants` carries ONE `promoted_from_verdict_id` for a row
    of several independent facts -- the year range, the model code, the trim
    and every stated dimension. PR1 allowed those factual columns to be
    rewritten as long as the revision counter advanced, so a row could end up
    stating values its attached verdict had never seen, with an advancing
    counter making it look reviewed.

    Row-level provenance cannot verify a multi-field row, so there is no update
    path at all until PR3 adds FIELD-LEVEL, append-only revision provenance.

    A row-level trigger only fires per row, and the canonical tables are
    empty -- so each case below inserts one inside a transaction that is never
    committed. The failing statement aborts it, which is why the tables are
    still empty afterwards.
    """
    lease, _, args, source, claim, verdict = _corrective_evidence(db, "canonical")
    _, _, candidate = _catalog_chain(db, "canonical", args)
    # Catalog PR3 made the canonical keys DERIVED identities, exactly like the
    # staging keys: `cm1.`/`cv1.` plus 128 bits of the domain-separated digest.
    model_key, variant_key = "cm1." + "a" * 32, "cv1." + "b" * 32
    model = ("insert into public.catalog_models (manufacturer, commercial_model, canonical_key) "
             f"values ('Toyota', 'RAV4', '{model_key}')")
    variant = ("insert into public.catalog_model_variants (model_id, promoted_from_candidate_id, "
               "promoted_from_verdict_id, canonical_key, model_year_start, model_year_end) "
               f"select id, '{candidate}', '{verdict}', '{variant_key}', 2021, 2021 "
               f"from public.catalog_models where canonical_key='{model_key}'")

    for seed, mutation in (
            (model, "update public.catalog_models set revision = revision + 1"),
            (model, "update public.catalog_models set commercial_model = 'RAV4 PHEV'"),
            (model, "delete from public.catalog_models"),
            (f"{model}; {variant}",
             "update public.catalog_model_variants set model_year_end = 2026, revision = 2"),
            (f"{model}; {variant}", "delete from public.catalog_model_variants")):
        with pytest.raises(AssertionError, match="canonical catalog rows are immutable"):
            db.psql(f"begin; {seed}; {mutation}; commit")
    # The aborted transactions left nothing behind.
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"

    # And the trigger no longer offers a revision path that would imply a
    # factual rewrite is safe.
    assert db.psql(
        "select count(*) from pg_proc where proname='forbid_canonical_identity_rewrite' "
        "and prosrc like '%revision must advance%'") == "0"


# --- the raw-record digest: PostgreSQL versus Memory, executably ------------
#
# PR #86 documented the memory digest as reproducing PostgreSQL's rendering.
# It did not: it rendered a Python dict in INSERTION order, while `jsonb`
# normalizes object keys by (key length, then bytes). Two payloads that are one
# value to every JSON reader -- and one `jsonb` row to PostgreSQL -- therefore
# received different memory digests. The cases below pin what each backend
# actually does instead of describing it.

#: Payload pairs that are the SAME value written in different key orders. The
#: middle pair is the one that matters: `"b"` sorts before `"aa"` in
#: PostgreSQL's (length, bytes) order and AFTER it bytewise, so no single
#: rendering can be both.
REORDERED_PAYLOAD_PAIRS = [
    ("flat", {"a": 1, "b": 2}, {"b": 2, "a": 1}),
    ("length_vs_bytes", {"b": 1, "aa": 2}, {"aa": 2, "b": 1}),
    ("nested", {"outer": {"z": 1, "a": 2}, "x": 3}, {"x": 3, "outer": {"a": 2, "z": 1}}),
]


@pytest.mark.parametrize("label, forward, reordered", REORDERED_PAYLOAD_PAIRS)
def test_postgres_stores_one_value_for_either_key_order(db, label, forward, reordered):
    """`jsonb` is a VALUE, so the key order it arrived in is not part of it."""
    forward_text = json.dumps(forward)
    reordered_text = json.dumps(reordered)
    assert db.psql(f"select '{forward_text}'::jsonb = '{reordered_text}'::jsonb") == "t"
    assert db.psql(f"select '{forward_text}'::jsonb::text") == \
        db.psql(f"select '{reordered_text}'::jsonb::text")
    assert db.psql(
        f"select encode(sha256(convert_to('{forward_text}'::jsonb::text,'UTF8')),'hex')") == \
        db.psql(
        f"select encode(sha256(convert_to('{reordered_text}'::jsonb::text,'UTF8')),'hex')")


@pytest.mark.parametrize("label, forward, reordered", REORDERED_PAYLOAD_PAIRS)
def test_a_reordered_payload_replays_onto_the_same_postgres_record(db, label, forward,
                                                                   reordered):
    """Behavioural parity with the memory repository, proven on both sides.

    `tests/test_catalog_persistence.py::test_a_reordered_payload_replays_onto_the_same_memory_record`
    asserts the identical property against the in-memory backend, with the same
    payload pairs. That behaviour -- not a shared digest -- is what replay and
    idempotency actually depend on.
    """
    lease, _ = _evidence_fixture(db, f"reorder-{label}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json(f'snap-reorder-{label}')}'::jsonb)")

    first = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-reorder-{label}', payload=forward)}'::jsonb)")
    again = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-reorder-{label}', payload=reordered)}'::jsonb)")
    assert again == first, "a reordered payload is the same record, not a conflict"
    assert db.psql(f"select stored_record_count from public.catalog_source_snapshots "
                   f"where id='{snapshot}'") == "1"
    # A genuinely different payload under the same identity still fails closed.
    with pytest.raises(AssertionError, match="catalog raw record idempotency conflict"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-reorder-{label}', payload={**forward, 'zz': 9})}'::jsonb)")


@pytest.mark.parametrize("label, forward, reordered", REORDERED_PAYLOAD_PAIRS)
def test_the_raw_record_digest_is_storage_local_and_the_two_backends_differ(db, label,
                                                                            forward,
                                                                            reordered):
    """The digest is local to its storage, and this asserts the inequality.

    PostgreSQL renders `{"a": 1, "b": 2}` (keys by length then bytes, spaced
    separators); the memory backend renders `{"a":1,"b":2}` (keys bytewise,
    compact). Both are order-independent, and they are NOT equal -- which is
    exactly what `backend/catalog/digest.py` now says, and what PR #86
    wrongly claimed the opposite of.

    Asserting the inequality here means the claim cannot quietly come back: a
    future edit that made the memory digest "match" would fail this test and
    have to justify itself.
    """
    lease, _ = _evidence_fixture(db, f"digest-{label}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json(f'snap-digest-{label}')}'::jsonb)")
    record = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{_catalog_record_json(snapshot, f'rec-digest-{label}', payload=forward)}'::jsonb)")

    stored = db.psql(f"select payload_sha256 from public.catalog_raw_records where id='{record}'")
    # PostgreSQL's digest is over ITS rendering, and is order-independent.
    assert stored == db.psql(
        f"select encode(sha256(convert_to('{json.dumps(reordered)}'::jsonb::text,'UTF8')),'hex')")
    # The memory backend's digest is order-independent too...
    assert catalog_payload_digest(forward) == catalog_payload_digest(reordered)
    # ...and is a DIFFERENT value. Storage-local, stated and enforced.
    assert catalog_payload_digest(forward) != stored, (
        "the memory digest must not be claimed equal to PostgreSQL's")
    # The renderings themselves are what differ.
    assert canonical_payload_text(forward) != db.psql(
        f"select '{json.dumps(forward)}'::jsonb::text")


def test_postgres_orders_jsonb_keys_by_length_then_bytes(db):
    """The rule that makes one portable rendering impossible to fake cheaply.

    Documented here because it is the reason the digest is storage-local: a
    Python `sort_keys=True` is bytewise, and the two orders disagree the moment
    a shorter key sorts after a longer one.
    """
    assert db.psql("""select '{"bb":1,"a":2,"ccc":3}'::jsonb::text""") == \
        '{"a": 2, "bb": 1, "ccc": 3}'
    assert db.psql("""select '{"aa":1,"b":2}'::jsonb::text""") == '{"b": 2, "aa": 1}'
    # Bytewise would have put "aa" first; PostgreSQL puts the shorter key first.
    assert canonical_payload_text({"aa": 1, "b": 2}) == '{"aa":1,"b":2}'


# --- the SHARED evidence fixture, submitted through the real RPCs -----------
#
# `backend/testing/evidence_fixtures.py` defines one R3/R4 chain. The memory
# tests in `tests/test_catalog_persistence.py` build their source, fragment,
# claim and verdict from exactly these builders, and the test below submits the
# SAME payloads through the four guarded RPCs against real PostgreSQL.
#
# That is the point. An earlier round of this branch hand-built the memory
# chain and got it wrong in seven places at once -- no `evidence_key`, no
# `task_key`, a locator with no `fragment_type`, no canonical scope identity,
# no unit on a numeric located fact, no `verification_mode`, no
# `verifier_contract_version`. Every one of those is refused here, so the
# memory tests were citing evidence PostgreSQL could not have produced. A
# memory-only assertion cannot catch that; this can.


def _shared_chain(db, args, label):
    """Write one complete shared-fixture chain through the real RPCs."""
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(evidence_fixtures.source_payload(f'{label}-source'))}'::jsonb)")
    fragment = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(evidence_fixtures.fragment_payload(f'{label}-fragment', source))}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload(f'{label}-claim', source))}'::jsonb)")
    verdict = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{json.dumps(evidence_fixtures.verdict_payload(f'{label}-verdict', claim, support=[evidence_fixtures.support_link(fragment)]))}'::jsonb)")
    return source, fragment, claim, verdict


def test_the_shared_evidence_fixture_is_accepted_by_the_real_rpcs(db):
    """The fixture the memory tests use is a chain PostgreSQL actually accepts.

    Four guarded RPCs, the shared payloads, no per-backend adjustment. If a
    builder in `backend/testing/evidence_fixtures.py` drifts out of what the
    contract allows, this fails -- so the memory tests can never again cite a
    shape the database refuses.
    """
    lease, _ = _evidence_fixture(db, "shared-chain")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source, fragment, claim, verdict = _shared_chain(db, args, "shared")

    # Each row landed, carrying the provenance the fixture states.
    assert db.psql(f"select source_version_kind || '|' || source_version_id || '|' || task_key "
                   f"from public.sources where id='{source}'") == (
        f"{evidence_fixtures.SOURCE_VERSION_KIND}|{evidence_fixtures.SOURCE_VERSION_ID}"
        f"|{evidence_fixtures.TASK_KEY}")
    assert db.psql(f"select fragment_type || '|' || locator_key from "
                   f"public.source_evidence_fragments where id='{fragment}'") == (
        f"{evidence_fixtures.FRAGMENT_TYPE}|{evidence_fixtures.LOCATOR}")
    assert db.psql(f"select evidence_locator || '|' || unit from public.claims "
                   f"where id='{claim}'") == f"{evidence_fixtures.LOCATOR}|{evidence_fixtures.FIELD_UNIT}"
    assert db.psql(f"select verdict || '|' || verification_mode || '|' || "
                   f"verifier_contract_version from public.claim_verdicts "
                   f"where id='{verdict}'") == (
        f"verified|{evidence_fixtures.VERIFICATION_MODE}|{evidence_fixtures.VERIFIER_CONTRACT}")
    # The verified verdict carries its durable support link.
    assert db.psql(f"select fragment_id from public.claim_verdict_supports "
                   f"where verdict_id='{verdict}'") == fragment


def test_the_shared_fixture_replays_exactly_and_conflicts_fail_closed(db):
    """Evidence keys are replay identities, in PostgreSQL as in memory."""
    lease, _ = _evidence_fixture(db, "shared-replay")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source, fragment, claim, verdict = _shared_chain(db, args, "replay")

    # An exact replay of every step returns the same row.
    assert _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(evidence_fixtures.source_payload('replay-source'))}'::jsonb)") == source
    assert _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(evidence_fixtures.fragment_payload('replay-fragment', source))}'::jsonb)") == fragment
    assert _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload('replay-claim', source))}'::jsonb)") == claim
    assert _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{json.dumps(evidence_fixtures.verdict_payload('replay-verdict', claim, support=[evidence_fixtures.support_link(fragment)]))}'::jsonb)") == verdict

    # Reusing an evidence key for DIFFERENT content fails closed. The other
    # source gets its own focused fragment first, so the grounding rule is
    # satisfied and the idempotency rule is the one under test.
    other_source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(evidence_fixtures.source_payload('replay-other-source'))}'::jsonb)")
    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(evidence_fixtures.fragment_payload('replay-other-fragment', other_source))}'::jsonb)")
    with pytest.raises(AssertionError, match="idempotency key belongs to a different source"):
        _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload('replay-claim', other_source))}'::jsonb)")
    with pytest.raises(AssertionError, match="evidence fragment idempotency conflict"):
        _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{json.dumps(evidence_fixtures.fragment_payload('replay-fragment', source, text='a different bounded excerpt entirely'))}'::jsonb)")
    with pytest.raises(AssertionError, match="claim verdict idempotency conflict"):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{json.dumps(evidence_fixtures.verdict_payload('replay-verdict', claim, verdict='needs_review', support=[]))}'::jsonb)")


@pytest.mark.parametrize("build, expected", [
    ("fragment_task_mismatch", "evidence fragment task provenance mismatch"),
    ("fragment_without_type", "a focused fragment requires both a type and a locator"),
    ("fragment_type_locator_mismatch", "fragment type does not match the locator kind"),
    ("verified_without_support", "an accepted verdict must cite durable evidence"),
    ("claim_without_unit", "a numeric located fact requires an explicit unit"),
    ("source_without_version", "a located fact requires a versioned source"),
])
def test_the_shared_fixtures_own_rules_are_the_databases_rules(db, build, expected):
    """Each way the fixture could drift, refused by PostgreSQL itself.

    These are the exact defects the hand-built memory chain had. Pinning them
    here means a future edit to the shared builders that reintroduces one is a
    PostgreSQL failure, not a quietly weaker memory test.
    """
    lease, _ = _evidence_fixture(db, f"shared-rule-{build}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    if build == "source_without_version":
        source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(evidence_fixtures.source_payload(f'{build}-source', version_kind=None, version_id=None))}'::jsonb)")
        with pytest.raises(AssertionError, match=expected):
            _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload(f'{build}-claim', source))}'::jsonb)")
        return

    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(evidence_fixtures.source_payload(f'{build}-source'))}'::jsonb)")

    if build == "fragment_task_mismatch":
        payload = evidence_fixtures.fragment_payload(f"{build}-fragment", source, task="another-task")
    elif build == "fragment_without_type":
        payload = evidence_fixtures.fragment_payload(f"{build}-fragment", source, fragment_type=None)
    elif build == "fragment_type_locator_mismatch":
        payload = evidence_fixtures.fragment_payload(f"{build}-fragment", source,
                                                     fragment_type="verbatim_excerpt")
    else:
        payload = None

    if payload is not None:
        with pytest.raises(AssertionError, match=expected):
            _rpc_as_service(db, f"select public.record_evidence_fragment_guarded({args},'{json.dumps(payload)}'::jsonb)")
        return

    _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(evidence_fixtures.fragment_payload(f'{build}-fragment', source))}'::jsonb)")
    if build == "claim_without_unit":
        with pytest.raises(AssertionError, match=expected):
            _rpc_as_service(db, f"select public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload(f'{build}-claim', source, unit=None))}'::jsonb)")
        return

    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{json.dumps(evidence_fixtures.claim_payload(f'{build}-claim', source))}'::jsonb)")
    with pytest.raises(AssertionError, match=expected):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{json.dumps(evidence_fixtures.verdict_payload(f'{build}-verdict', claim, support=[]))}'::jsonb)")


def test_a_catalog_link_cites_a_verdict_built_from_the_shared_fixture(db):
    """The catalog path, end to end, on evidence PostgreSQL really produced."""
    lease, _ = _evidence_fixture(db, "shared-catalog")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source, fragment, claim, verdict = _shared_chain(db, args, "catalog")
    _, _, candidate = _catalog_chain(db, "shared-catalog", args)
    link = _rpc_as_service(db, f"select id from public.link_catalog_candidate_evidence_guarded({args},'{_catalog_link_json(candidate, source, 'link-shared', claim_id=claim, verdict_id=verdict, locator=None, version=None, kind=None)}'::jsonb)")
    assert db.psql(f"select record_locator || '|' || source_version_kind || '|' || source_version "
                   f"from public.catalog_candidate_evidence_links where id='{link}'") == (
        f"{evidence_fixtures.LOCATOR}|{evidence_fixtures.SOURCE_VERSION_KIND}"
        f"|{evidence_fixtures.SOURCE_VERSION_ID}")


# ---------------------------------------------------------------------------
# The verdict REPLAY contract, on the database itself.
# ---------------------------------------------------------------------------
#
# Paired one-for-one with
# `tests/test_catalog_persistence.py` section 4, using the SAME shared-fixture
# builders, so the memory mirror and the database cannot drift apart.

#: (case, expected error or None when the call must be accepted)
VERDICT_REPLAY_CASES = [
    ("reason", "claim verdict idempotency conflict"),
    ("replace", "verdict support links do not match the cited evidence"),
    ("add", "verdict support links do not match the cited evidence"),
    ("remove_one", "verdict support links do not match the cited evidence"),
    ("remove_all", "verdict support links do not match the cited evidence"),
    ("duplicate_first", "verdict support links do not match the cited evidence"),
    ("duplicate_replay", "verdict support links do not match the cited evidence"),
    ("reorder", None),
    ("local_with_support", "a locally settled verdict cites no evidence"),
    ("verified_local_with_support", "a locally settled verdict cites no evidence"),
    ("verified_local_no_support", "an accepted verdict must cite durable evidence"),
]

_SECOND_FRAGMENT_TEXT = evidence_fixtures.FRAGMENT_TEXT + " (a second bounded excerpt)"


def _verdict_scenario(db, args, label):
    """A claim with TWO durable fragments of its own source, ready to cite."""
    F = evidence_fixtures
    source = _rpc_as_service(db, f"select id from public.upsert_source_guarded({args},'{json.dumps(F.source_payload(f'{label}-source'))}'::jsonb)")
    fragment_a = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(F.fragment_payload(f'{label}-fragA', source))}'::jsonb)")
    fragment_b = _rpc_as_service(db, f"select id from public.record_evidence_fragment_guarded({args},'{json.dumps(F.fragment_payload(f'{label}-fragB', source, text=_SECOND_FRAGMENT_TEXT, index=1))}'::jsonb)")
    claim = _rpc_as_service(db, f"select id from public.create_claim_with_source_guarded({args},'{json.dumps(F.claim_payload(f'{label}-claim', source))}'::jsonb)")
    return (claim, F.support_link(fragment_a),
            F.support_link(fragment_b, text=_SECOND_FRAGMENT_TEXT))


def _verdict_calls(case, claim, link_a, link_b):
    """The one or two payload kwargs this case submits, in order."""
    F = evidence_fixtures
    both, only_a = [link_a, link_b], [link_a]
    local = {"mode": "deterministic_local"}
    review = {"verdict": "needs_review"}
    return {
        "reason": ([{"support": only_a}],
                   [{"support": only_a, "reason": "A COMPLETELY DIFFERENT REASON"}]),
        "replace": ([{"support": only_a}], [{"support": [link_b]}]),
        "add": ([{"support": only_a}], [{"support": both}]),
        "remove_one": ([{"support": both, **review}], [{"support": only_a, **review}]),
        "remove_all": ([{"support": only_a, **review}], [{"support": [], **review}]),
        "duplicate_first": ([], [{"support": [link_a, dict(link_a)]}]),
        "duplicate_replay": ([{"support": only_a}],
                             [{"support": [link_a, dict(link_a)]}]),
        "reorder": ([{"support": both}], [{"support": [link_b, link_a]}]),
        "local_with_support": ([], [{"support": only_a, **local, **review}]),
        "verified_local_with_support": ([], [{"support": only_a, **local}]),
        "verified_local_no_support": ([], [{"support": [], **local}]),
    }[case]


def _stored_support(db, run_id, key):
    return db.psql(
        f"select coalesce(string_agg(s.fragment_id::text, ',' order by s.fragment_id::text), '') "
        f"from public.claim_verdict_supports s join public.claim_verdicts v on v.id = s.verdict_id "
        f"where v.run_id = '{run_id}' and v.evidence_key = '{key}'")


@pytest.mark.parametrize("case, expected", VERDICT_REPLAY_CASES)
def test_the_verdict_replay_contract_is_the_databases_own(db, case, expected):
    """Every verdict-replay rule the memory mirror claims, on real PostgreSQL.

    `add` is the one the database itself got wrong: it inserted the added
    support link and then compared `count(*)` of the UNION of the stored and
    cited sets against the CITED length. A cited superset makes those equal,
    so an addition was accepted and the stored support set silently grew --
    while the function's own comment said "a replay that dropped or added
    evidence is a contract failure". The corrective migration compares the
    stored set to the cited set and writes nothing on a replay.
    """
    lease, _ = _evidence_fixture(db, f"replay-{case}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    claim, link_a, link_b = _verdict_scenario(db, args, f"replay-{case}")
    key = f"v-{case}"
    first_calls, last_calls = _verdict_calls(case, claim, link_a, link_b)

    def submit(kwargs):
        payload = evidence_fixtures.verdict_payload(key, claim, **kwargs)
        return _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{json.dumps(payload)}'::jsonb)")

    verdict_id = None
    for kwargs in first_calls:
        verdict_id = submit(kwargs)
    before = _stored_support(db, run_id, key)

    if expected is None:
        for kwargs in last_calls:
            assert submit(kwargs) == verdict_id, "an accepted replay returns the same row"
    else:
        for kwargs in last_calls:
            with pytest.raises(AssertionError, match=expected):
                submit(kwargs)

    # A refused call writes NOTHING: the stored support set is untouched, and
    # a refused first call leaves no verdict row at all.
    assert _stored_support(db, run_id, key) == before
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_id}' "
                   f"and evidence_key='{key}'") == ("1" if first_calls else "0")


# ---------------------------------------------------------------------------
# `support` must be a JSON ARRAY when it is supplied at all.
# ---------------------------------------------------------------------------
#
# Paired with `tests/test_catalog_persistence.py::…_holds_support_to_a_json_array`
# over the SAME `evidence_fixtures.SUPPORT_VALUE_CASES` matrix. The mirror
# collapsed every falsy value into an empty list; these establish, on the real
# database, what it must collapse and what it must refuse.


@pytest.mark.parametrize("label, value, accepted", evidence_fixtures.SUPPORT_VALUE_CASES)
def test_the_verdict_support_field_must_be_a_json_array(db, label, value, accepted):
    """A MISSING key coalesces to `[]`; a supplied non-array is refused.

    The distinction is real jsonb semantics, not a convention: `p_verdict->
    'support'` is SQL NULL only when the key is absent, so `coalesce(...,
    '[]'::jsonb)` fires there alone. A supplied JSON `null` is `'null'::jsonb`,
    which survives the coalesce and fails `jsonb_typeof(...) <> 'array'`.
    """
    lease, _ = _evidence_fixture(db, f"stype-{label}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    claim, _, _ = _verdict_scenario(db, args, f"stype-{label}")
    key = f"v-stype-{label}"
    payload = evidence_fixtures.verdict_payload_with_support(key, claim, value)
    call = f"select id from public.record_claim_verdict_guarded({args},'{json.dumps(payload)}'::jsonb)"

    if accepted:
        verdict_id = _rpc_as_service(db, call)
        assert db.psql(f"select count(*) from public.claim_verdict_supports "
                       f"where verdict_id='{verdict_id}'") == "0"
        return

    with pytest.raises(AssertionError, match=evidence_fixtures.SUPPORT_TYPE_ERROR):
        _rpc_as_service(db, call)
    # A refused FIRST write leaves no verdict row and no support row.
    assert db.psql(f"select count(*) from public.claim_verdicts where run_id='{run_id}' "
                   f"and evidence_key='{key}'") == "0"
    assert db.psql(f"select count(*) from public.claim_verdict_supports s "
                   f"join public.claim_verdicts v on v.id = s.verdict_id "
                   f"where v.run_id='{run_id}'") == "0"


@pytest.mark.parametrize("label, value", evidence_fixtures.REJECTED_SUPPORT_CASES)
def test_a_verdict_replay_holds_support_to_a_json_array(db, label, value):
    """A malformed `support` on a REPLAY changes nothing that is stored."""
    lease, _ = _evidence_fixture(db, f"rstype-{label}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    claim, link_a, _ = _verdict_scenario(db, args, f"rstype-{label}")
    key = f"v-rstype-{label}"
    stored = _rpc_as_service(db, f"select id from public.record_claim_verdict_guarded({args},'{json.dumps(evidence_fixtures.verdict_payload(key, claim, verdict='needs_review', support=[link_a]))}'::jsonb)")
    before = _stored_support(db, run_id, key)
    assert before

    payload = evidence_fixtures.verdict_payload_with_support(key, claim, value)
    with pytest.raises(AssertionError, match=evidence_fixtures.SUPPORT_TYPE_ERROR):
        _rpc_as_service(db, f"select public.record_claim_verdict_guarded({args},'{json.dumps(payload)}'::jsonb)")
    assert _stored_support(db, run_id, key) == before
    assert db.psql(f"select id from public.claim_verdicts where run_id='{run_id}' "
                   f"and evidence_key='{key}'") == stored


# ===========================================================================
# Catalog PR2: a raw record states WHERE in the capture it came from
# ===========================================================================
#
# The column, its closed vocabulary, its position uniqueness and its place in
# the replay identity, against the live schema. The Government ingestion path
# that WRITES these values is proven offline in
# `tests/test_catalog_government_ingestion.py`; what is checked here is what
# PostgreSQL itself enforces, for every writer rather than for one backend.

LOCATOR_MIGRATION = "20260915180000_catalog_raw_record_source_locator.sql"


def _catalog_open_snapshot(db, suffix: str, *, declared: int = 8):
    """A leased run plus a PENDING snapshot that can still take records.

    `_catalog_fixture` activates its snapshot, which correctly freezes it; the
    locator rules below are all about APPENDING, so they need one that is still
    open. `declared` is deliberately larger than any test writes, so nothing
    here can accidentally activate.
    """
    lease, _other = _evidence_fixture(db, f"catalog-{suffix}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{_catalog_snapshot_json(f'open-{suffix}', declared=declared)}'::jsonb)")
    return args, snapshot


def test_catalog_raw_record_locator_column_exists_with_a_safe_default(db):
    """Additive: the column defaults to an empty object, so a record captured
    by a path with no pagination states no position at all."""
    assert db.psql(
        "select data_type || '|' || is_nullable || '|' || column_default "
        "from information_schema.columns where table_schema='public' "
        "and table_name='catalog_raw_records' and column_name='source_locator'"
    ) == "jsonb|NO|'{}'::jsonb"
    _lease, _other, _args, snapshot, _record, _candidate = _catalog_fixture(db, "loc-default")
    stored = _rpc_as_service(db, f"select source_locator from public.catalog_raw_records "
                                 f"where snapshot_id='{snapshot}' limit 1")
    assert stored == "{}"


@pytest.mark.parametrize("label, locator", [
    ("an unknown field", {"page": 1}),
    ("a negative position", {"page_index": -1}),
    ("a text position", {"page_index": "3"}),
    ("a fractional position", {"page_index": 1.5}),
    ("a boolean position", {"page_index": True}),
    ("an object position", {"page_index": {}}),
    ("an array locator", ["page_index", 1]),
    ("a text locator", "page_index=1"),
])
def test_catalog_raw_record_locator_vocabulary_is_closed(db, label, locator):
    """A position that cannot be compared is not a position.

    Refused by the guarded RPC AND by a CHECK constraint, so the rule holds for
    `service_role`'s direct DML too -- which is what makes it a property of the
    schema rather than of one write path.
    """
    args, snapshot = _catalog_open_snapshot(db, f"loc-{label.replace(' ', '-')}")
    payload = _catalog_record_json(snapshot, f"loc-bad-{label}", upstream="99001",
                                   locator=locator)
    with pytest.raises(AssertionError, match="source locator|invalid input|cannot"):
        _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{payload}'::jsonb)")


def test_catalog_raw_record_locator_constraint_holds_for_direct_dml(db):
    """`service_role` holds direct INSERT on this table, so the vocabulary has
    to live in the schema and not only in the function."""
    args, snapshot = _catalog_open_snapshot(db, "loc-dml")
    with pytest.raises(AssertionError, match="locator_allowlisted"):
        db.psql(
            "set role service_role; insert into public.catalog_raw_records "
            "(snapshot_id, resource_id, upstream_record_id, payload, payload_sha256, "
            f"record_key, source_locator) values ('{snapshot}', "
            "'142afde2-6228-49f9-8a29-9b6c3a0cbe40', '99002', '{\"_id\": 99002}'::jsonb, "
            f"repeat('a', 64), '{_catalog_key('catalog.raw_record', 'loc-dml-direct')}', "
            "'{\"page\": 1}'::jsonb)")


def test_catalog_one_capture_position_belongs_to_one_row(db):
    """Two records of one snapshot cannot claim the same place in the capture."""
    args, snapshot = _catalog_open_snapshot(db, "loc-uniq")
    first = _catalog_record_json(snapshot, "loc-uniq-1", upstream="99101",
                                 payload={"_id": 99101}, locator={"capture_index": 7})
    _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{first}'::jsonb)")
    second = _catalog_record_json(snapshot, "loc-uniq-2", upstream="99102",
                                  payload={"_id": 99102}, locator={"capture_index": 7})
    with pytest.raises(AssertionError, match="catalog_raw_records_snapshot_position_uidx"):
        _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{second}'::jsonb)")
    # A record that states NO position collides with nothing.
    third = _catalog_record_json(snapshot, "loc-uniq-3", upstream="99103", payload={"_id": 99103})
    _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{third}'::jsonb)")
    # The first row and the unpositioned one; the colliding one wrote nothing.
    assert db.psql(f"select count(*) from public.catalog_raw_records "
                   f"where snapshot_id='{snapshot}'") == "2"


def test_catalog_raw_record_replay_holds_the_capture_position(db):
    """A replay that MOVES a row is not a retry.

    Same identity, same payload, a different place in the capture: the row
    would then state a provenance it was not written with, so it fails closed
    exactly as a changed payload does.
    """
    args, snapshot = _catalog_open_snapshot(db, "loc-replay")
    payload = _catalog_record_json(snapshot, "loc-replay-1", upstream="99201",
                                   payload={"_id": 99201}, locator={"page_number": 1,
                                                                    "capture_index": 3})
    stored = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{payload}'::jsonb)")
    # Exact replay collapses onto the same row and adds no record.
    assert _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{payload}'::jsonb)") == stored
    moved = _catalog_record_json(snapshot, "loc-replay-1", upstream="99201",
                                 payload={"_id": 99201}, locator={"page_number": 2,
                                                                  "capture_index": 3})
    with pytest.raises(AssertionError, match="idempotency conflict"):
        _rpc_as_service(db, f"select public.record_catalog_raw_record_guarded({args},'{moved}'::jsonb)")
    assert db.psql(f"select source_locator->>'page_number' from public.catalog_raw_records "
                   f"where id='{stored}'") == "1"


def test_catalog_locator_predicate_is_service_path_only(db):
    """A constraint helper is not a browser surface."""
    assert db.psql(
        "select has_function_privilege('service_role',"
        "'public.catalog_source_locator_valid(jsonb)','execute')") == "t"
    for role in ("anon", "authenticated"):
        assert db.psql(
            f"select has_function_privilege('{role}',"
            "'public.catalog_source_locator_valid(jsonb)','execute')") == "f", role


def test_catalog_canonical_tables_stay_empty_through_a_government_shaped_ingestion(db):
    """Catalog PR2 writes snapshots, raw records and candidates -- and nothing
    else. The canonical pair is still empty, still unwritable and now immutable
    outright, so "the canonical catalog starts empty" survives an ingestion.
    """
    lease, _other, args, snapshot, record, candidate = _catalog_fixture(db, "gov-canonical")
    assert db.psql(f"select count(*) from public.catalog_raw_records where snapshot_id='{snapshot}'") == "1"
    assert db.psql(f"select count(*) from public.catalog_candidate_variants where id='{candidate}'") == "1"
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"
        # DELETE is still refused outright, for every role: a promoted fact is
        # append-only and a canonical identity is never removed.
        with pytest.raises(AssertionError, match="permission denied"):
            db.psql(f"set role service_role; delete from public.{table}")
    # A capture that landed cleanly promotes nothing by itself: promotion takes
    # a verified verdict per field, which an ingestion never creates.
    assert db.psql("select count(*) from public.catalog_canonical_field_provenance") == "0"


def test_the_real_government_payloads_are_accepted_by_real_postgresql(db):
    """The shapes Catalog PR2 actually writes, through the real guarded RPCs.

    Every other catalog test here builds a payload by hand, which proves the
    RULES but not that the ingestion path produces payloads those rules accept.
    This one takes a REAL capture of the committed R5 Government fixtures --
    the same bytes, through the same client -- and submits the snapshot, raw
    records and candidates it produces to PostgreSQL unchanged.

    That is what catches a retrieval-metadata object over the durable bound, a
    metadata key the credential screen would reject, a locator outside the
    closed vocabulary, or a candidate dimension the schema does not allow --
    none of which an offline test against the memory repository can see.
    """
    from backend.catalog.government import snapshot as gov_snapshot
    from backend.catalog.government import source as gov_source
    from backend.catalog.government.client import DataGovClient
    from backend.catalog.government import normalize as gov_normalize
    from backend.catalog.government.normalize import read_wltp_record
    from backend.catalog.payloads import (prepare_candidate, prepare_raw_record,
                                          prepare_snapshot)
    from backend.testing.government_capture import FixtureTransport, PINNED_QUERY

    capture = DataGovClient(FixtureTransport(), page_limit=100).capture_resource(
        gov_source.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    lease, _other = _evidence_fixture(db, "catalog-gov-real")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"

    # The snapshot key is DERIVED, never stated by the ingestion payload, so it
    # comes back from the same preparer the repository uses.
    normalization = gov_normalize.read_capture(
        [record for _, record in capture.located_records()], resource_id=capture.resource_id)
    prepared = prepare_snapshot(gov_snapshot.snapshot_payload(capture, normalization))
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args},'{json.dumps(prepared)}'::jsonb)")
    stored = db.psql(f"select content_sha256 || '|' || upstream_version_kind || '|' || "
                     f"declared_record_count from public.catalog_source_snapshots where id='{snapshot}'")
    assert stored == (f"{gov_snapshot.snapshot_content_sha256(capture)}|dataset_version|"
                      f"{capture.reported_total}")
    # The retrieval metadata survived the durable bound AND the credential
    # screen, with its page checksums intact.
    assert db.psql(f"select retrieval_metadata->>'page_chain_sha256' from "
                   f"public.catalog_source_snapshots where id='{snapshot}'") == \
        gov_snapshot.page_chain_digest(tuple(page.body_sha256 for page in capture.pages))
    assert db.psql(f"select jsonb_array_length(retrieval_metadata->'page_checksums') from "
                   f"public.catalog_source_snapshots where id='{snapshot}'") == "3"

    # A representative slice of real rows, with their real capture positions
    # and their real readings. Three, not 233: each psql call is a process, and
    # what is under test is the SHAPE the ingestion produces.
    written = []
    snapshot_row = {"id": snapshot, "snapshot_key": prepared["snapshot_key"]}
    for record_payload, record in list(
            gov_snapshot.raw_record_payloads(capture, snapshot_row))[:3]:
        row_id = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args},'{json.dumps(prepare_raw_record(record_payload))}'::jsonb)")
        written.append((row_id, record))
    assert db.psql(f"select source_locator from public.catalog_raw_records "
                   f"where id='{written[0][0]}'") == \
        '{"page_index": 0, "page_number": 1, "page_offset": 0, "capture_index": 0}'

    for row_id, record in written:
        reading = read_wltp_record(record)
        candidate = reading.candidate_payload(
            {"snapshot_id": snapshot, "id": row_id,
             "record_key": db.psql(f"select record_key from public.catalog_raw_records where id='{row_id}'")})
        _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args},'{json.dumps(prepare_candidate(candidate))}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_variants "
                   f"where snapshot_id='{snapshot}'") == "3"
    assert db.psql(f"select count(*) from public.catalog_candidate_variants "
                   f"where snapshot_id='{snapshot}' and identity_dimensions ? 'body_style'") == "3"

    # Three of 233 is not a complete capture, so the snapshot cannot activate --
    # which is the R5 pagination lesson holding against the real payloads.
    with pytest.raises(AssertionError, match="catalog snapshot is incomplete"):
        _rpc_as_service(db, f"select public.activate_catalog_snapshot_guarded({args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    assert db.psql(f"select activated_at is null from public.catalog_source_snapshots "
                   f"where id='{snapshot}'") == "t"
    # And nothing on this path created a canonical row, a claim or a verdict.
    for table in CATALOG_CANONICAL_TABLES:
        assert db.psql(f"select count(*) from public.{table}") == "0"


# ===========================================================================
# Catalog PR3: bounded database-side aggregation, and FIELD-LEVEL promotion
# ===========================================================================
#
# The two halves this PR adds, held to real PostgreSQL:
#
#   * `20260916090000_catalog_bounded_candidate_queries.sql` -- five bounded
#     aggregations that answer over a snapshot of any size, each gated on an
#     ACTIVE, COMPLETE, USABLE snapshot and each returning the EXACT total;
#   * `20260916120000_catalog_field_level_promotion.sql` -- one append-only
#     provenance row per promoted FACT, plus the two triggers that make a
#     canonical row without complete verified provenance impossible to commit
#     FOR EVERY WRITER, not only for callers of the RPC.
#
# The refusal matrix below is the point of the second migration. Each case is
# one way a canonical fact could be wrong, and each one is a refusal rather
# than a smaller truth.

@pytest.fixture
def pr3_db(db):
    """The shared module DB with every migration a PROMOTION depends on current.

    Same reason `r3_db` and `r4_db` exist: earlier rerun-safety tests
    deliberately re-apply OLDER evidence migrations into this one database, and
    a pre-R4 `create_claim_with_source_guarded` stores no `identity_scope` --
    the column the canonical promotion gate reads to check that a claim is
    scoped to the vehicle being written. The catalog migrations are re-applied
    with them so the promotion path is current too, rather than inheriting
    whatever the previous test left behind.
    """
    for name in ("source_evidence_fragments", "r3_versioned_focused_evidence",
                 "r4_deterministic_verification"):
        db.psql(file=next(m for m in MIGRATIONS if name in m.name))
    for migration in MIGRATIONS:
        if "catalog" in migration.name:
            db.psql(file=migration)
    # And R5 LAST. Re-applying `20260916120000` above restores its own
    # `catalog_run_pending_promotions` -- the definition that joined on "a
    # verified verdict exists" -- so a fixture that stopped there would hand
    # every promotion test the pre-R5 read. Production applies migrations in
    # sequence and never hits this; a fixture that re-applies an older one
    # does, which is exactly the hazard
    # `test_reapplying_the_catalog_promotion_migration_needs_this_one_again`
    # states.
    db.psql(file=next(m for m in MIGRATIONS if "current_verdict_authority" in m.name))
    return db


#: The canonical fields one promotion states, and the register field each is
#: read from. Mirrors `GOVERNMENT_FIELD_SOURCES` in
#: `backend/catalog/government/evidence.py`; the fifth is a namespaced identity
#: dimension, which is what proves a dimension is promoted like any other fact.
PR3_FIELDS = (
    ("model_year_start", 2021, "year", "shnat_yitzur"),
    ("model_year_end", 2021, "year", "shnat_yitzur_end"),
    ("official_model_code", "AXAP54L-ANXGBW", None, "degem_nm"),
    ("trim", "PRIME AWD SE", None, "ramat_gimur"),
    ("identity_dimensions.fuel_type", "plug_in_hybrid", None, "delek_cd"),
)

#: A usable snapshot's own durable reading, as `catalog_readable_snapshot`
#: parses it. One row read, none refused.
PR3_READ_METADATA = {"http_status": 200, "redirect_chain": [],
                     "normalization_contract": "gov.wltp.normalize.1",
                     "normalized_record_count": 1, "normalization_issue_count": 0,
                     "normalization_issues": [], "normalization_issue_records": []}


#: The scope every Government claim in these fixtures is read under, exactly as
#: `backend/catalog/government/source.py` states it for the register.
PR3_MARKET = "IL"


def _pr3_scope(make: str, *, model: str = "RAV4", model_year: int = 2021,
               code: str | None = "AXAP54L-ANXGBW", trim: str | None = "PRIME AWD SE",
               dimensions: dict | None = None) -> dict:
    """The entity, time and identity scope one candidate's claims must state.

    Assembled from the CANDIDATE, through the same two builders production
    uses, because that is exactly what `catalog_check_field_provenance` holds a
    promoted fact to: the claim must be about this canonical model, at this
    model year, narrowed to this vehicle identity and no other.
    """
    identity = {name: value for name, value
                in sorted((dimensions or {}).items())
                if name in ("body_style", "drivetrain", "generation", "transmission")}
    if code is not None:
        identity["model_code"] = code
    if trim is not None:
        identity["trim"] = trim
    model_key = catalog_keys.canonical_model_key(manufacturer=make, commercial_model=model)
    return {"entity": claim_entity_key(model_key, model_year),
            "time_scope": {"model_year": model_year}, "market": PR3_MARKET,
            "geography": PR3_MARKET, "identity": identity}


def _pr3_context(suffix: str, make: str) -> tuple[str, dict]:
    """The locator record identity and the claim scope of one `_pr3_promotable`.

    Recomputed rather than returned, so a test that adds a LATER piece of
    evidence to an existing case states the same two things the fixture did and
    a drift between them shows up as a refusal.
    """
    return (record_locator_id(_catalog_key("catalog.snapshot", f"pr3-snap-{suffix}"), "36327"),
            _pr3_scope(make))


def _pr3_field_evidence(db, args: str, label: str, field_key: str, value,
                        unit: str | None, register_field: str, *,
                        record_id: str, scope: dict,
                        verdict: str = "verified") -> dict[str, str]:
    """A complete R3/R4 chain for ONE promotable field.

    One source per field, deliberately: a source may carry at most four focused
    fragments, and every promoted field needs its own exact locator. Spreading
    them proves a promotion legitimately spans sources -- which is what the
    field-level design is FOR.

    `record_id` is the catalog's durable locator record identity -- the
    snapshot key and the upstream row id, exactly as `record_locator_id`
    assembles it -- so the locator points at the candidate's OWN captured row,
    which is what the promotion trigger checks.
    """
    locator = record_field_locator(record_id, (register_field,)).locator_key
    text = f"{register_field}={value}"
    source = _rpc_as_service(
        db, "select id from public.upsert_source_guarded("
            f"{args},'{_r3_source_json(f'{label}-src', tool_operation=PROMOTABLE_TOOL_OPERATION)}'::jsonb)")
    fragment = _rpc_as_service(
        db, "select id from public.record_evidence_fragment_guarded("
            f"{args},'{_r3_fragment_json(source, text, key=f'{label}-frag', locator=locator)}'::jsonb)")
    claim = _rpc_as_service(
        db, "select id from public.create_claim_with_source_guarded("
            f"{args},'{_r3_claim_json(f'{label}-claim', source, value, locator=locator, unit=unit, field=field_key, **scope)}'::jsonb)")
    support = [{"fragment_id": fragment, "content_hash": fragment_content_hash(text),
                "locator_key": locator}]
    settled = _rpc_as_service(
        db, "select id from public.record_claim_verdict_guarded("
            f"{args},'{_r4_verdict_json(claim, key=f'{label}-verdict', verdict=verdict, support=support)}'::jsonb)")
    return {"source": source, "fragment": fragment, "claim": claim, "verdict": settled,
            "field_key": field_key, "value": value}


def _pr3_promotable(db, suffix: str, *, status: str = "ready_for_review",
                    family: str = "government", fields=PR3_FIELDS,
                    scope: dict | None = None, verified: bool = True):
    """A ready candidate with a VERIFIED evidence link per promotable field.

    Each case gets its OWN manufacturer. `catalog_models_natural_uniq` makes
    (manufacturer, commercial model) a single canonical model -- which is the
    point of the constraint -- so two cases sharing a marque would share a
    canonical model row and one would be revising the other's variant instead
    of establishing its own.
    """
    make = f"Toyota-{suffix}"
    lease, _other = _evidence_fixture(db, f"pr3-{suffix}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot_key = _catalog_key("catalog.snapshot", f"pr3-snap-{suffix}")
    snapshot = _rpc_as_service(
        db, "select id from public.record_catalog_snapshot_guarded("
            f"{args},'{_catalog_snapshot_json(f'pr3-snap-{suffix}', family=family, retrieval_metadata=PR3_READ_METADATA)}'::jsonb)")
    record = _rpc_as_service(
        db, "select id from public.record_catalog_raw_record_guarded("
            f"{args},'{_catalog_record_json(snapshot, f'pr3-rec-{suffix}')}'::jsonb)")
    _rpc_as_service(db, "select id from public.activate_catalog_snapshot_guarded("
                        f"{args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    candidate = _rpc_as_service(
        db, "select id from public.record_catalog_candidate_guarded("
            f"{args},'{_catalog_candidate_json(snapshot, record, f'pr3-cand-{suffix}', status=status, make=make, dimensions={'fuel_type': 'plug_in_hybrid'})}'::jsonb)")
    # The locator record identity of the candidate's OWN captured row, and the
    # scope its evidence must be read under. `36327` is the upstream id
    # `_catalog_record_json` stores by default.
    record_id = record_locator_id(snapshot_key, "36327")
    scope = _pr3_scope(make) if scope is None else scope
    assert record_id == _pr3_context(suffix, make)[0]
    links = {}
    for index, (field_key, value, unit, register_field) in enumerate(fields):
        evidence = _pr3_field_evidence(db, args, f"pr3-{suffix}-{index}", field_key, value,
                                       unit, register_field, record_id=record_id, scope=scope,
                                       verdict="verified" if verified else "needs_review")
        # A link may cite only a VERIFIED verdict, so an unverified case links
        # nothing -- exactly the durable shape a run leaves behind when the
        # Verifier did not confirm what the register said.
        link = None if not verified else _rpc_as_service(
            db, "select id from public.link_catalog_candidate_evidence_guarded("
                f"{args},'{_catalog_link_json(candidate, evidence['source'], f'pr3-link-{suffix}-{index}', claim_id=evidence['claim'], verdict_id=evidence['verdict'], locator=None, version=None, kind=None)}'::jsonb)")
        links[field_key] = {**evidence, "link": link}
    return args, snapshot, record, candidate, links, make


def _pr3_promotion_json(candidate: str, links: dict, make: str, *, key: str,
                        fields=PR3_FIELDS, code: str | None = "AXAP54L-ANXGBW",
                        trim: str | None = "PRIME AWD SE",
                        years: tuple[int, int] = (2021, 2021),
                        dimensions: dict | None = None,
                        entries: list[dict] | None = None) -> str:
    """One promotion payload, with the canonical keys derived as production does.

    The model and variant keys come from `backend/catalog/keys.py`, so a test
    exercises the same identity the repository preparer derives -- including
    `catalog_models_natural_uniq`, which makes one (manufacturer, commercial
    model) exactly one canonical model however it is keyed.

    The PROMOTION key is label-derived, and only that one: an idempotency test
    has to replay deliberately CONFLICTING payloads under one identity, which a
    key derived from the payload's own content could never do.
    """
    model_key = catalog_keys.canonical_model_key(manufacturer=make, commercial_model="RAV4")
    payload = {
        "candidate_id": candidate,
        "model_canonical_key": model_key,
        "canonical_key": catalog_keys.canonical_variant_key(
            model_key=model_key, model_year_start=years[0], model_year_end=years[1],
            official_model_code=code, trim=trim),
        "promotion_key": catalog_keys.derive_key("catalog.promotion", test_label=key),
        "manufacturer": make, "commercial_model": "RAV4",
        "model_year_start": years[0], "model_year_end": years[1],
        "identity_dimensions": {"fuel_type": "plug_in_hybrid"} if dimensions is None
                               else dimensions,
        "fields": entries if entries is not None else [
            {"field_key": field_key, "value": value,
             "evidence_link_id": links[field_key]["link"]}
            for field_key, value, _unit, _register in fields]}
    if code is not None:
        payload["official_model_code"] = code
    if trim is not None:
        payload["trim"] = trim
    return json.dumps(payload)


def _pr3_diff_snapshot(db, suffix: str, rows, *, make: str = "Toyota-diff") -> tuple[str, str]:
    """One ACTIVATED snapshot holding `rows` candidates, in ONE psql round trip.

    `rows` is a sequence of `(official_model_code, status)`. Batched because a
    diff test needs more candidates than a page holds, and a round trip per row
    would make the test's own setup the slowest thing in the suite.
    """
    lease, _other = _evidence_fixture(db, f"pr3-{suffix}")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    snapshot = _rpc_as_service(
        db, "select id from public.record_catalog_snapshot_guarded("
            f"{args},'{_catalog_snapshot_json(f'pr3-diff-{suffix}', declared=len(rows), retrieval_metadata=PR3_READ_METADATA)}'::jsonb)")
    statements = ["set role service_role"]
    for index, (code, status) in enumerate(rows):
        record = _catalog_record_json(snapshot, f"pr3-diff-{suffix}-{index}",
                                      upstream=str(100000 + index),
                                      payload={"_id": 100000 + index, "kinuy_mishari": "RAV4"})
        statements.append("select public.record_catalog_raw_record_guarded("
                          f"{args},'{record}'::jsonb)")
        candidate = _catalog_candidate_json(
            snapshot, "00000000-0000-0000-0000-000000000000",
            f"pr3-diff-{suffix}-{index}", make=make, code=code, status=status, dimensions={})
        # The raw record id is only known inside the database, so the candidate
        # payload is completed there rather than round-tripped out and back.
        statements.append(
            "select public.record_catalog_candidate_guarded("
            f"{args}, jsonb_set(jsonb_set('{candidate}'::jsonb, '{{raw_record_id}}', "
            f"to_jsonb(r.id::text)), '{{snapshot_id}}', to_jsonb(r.snapshot_id::text))) "
            "from public.catalog_raw_records r "
            f"where r.snapshot_id='{snapshot}' and r.upstream_record_id='{100000 + index}'")
    statements.append("select public.activate_catalog_snapshot_guarded("
                      f"{args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    statements.append("reset role")
    # Through a FILE, not `-c`: a hundred-odd statements exceed the argument
    # length a process may be started with, and the failure would read as a
    # PostgreSQL problem rather than as this helper's.
    script = Path(tempfile.mkdtemp()) / f"pr3-diff-{suffix}.sql"
    script.write_text(";\n".join(statements) + ";\n", encoding="utf-8")
    db.psql(file=script)
    return args, snapshot


def _promote(db, args: str, promotion: str) -> str:
    return _rpc_as_service(
        db, f"select id from public.promote_catalog_variant_guarded({args},'{promotion}'::jsonb)")


# --- the bounded aggregations ----------------------------------------------

def test_the_bounded_catalog_queries_answer_only_from_a_usable_snapshot(pr3_db):
    """Active, complete, read, and free of unresolved gaps -- or no answer.

    The Python projection applies the same gate (`snapshot_usability`), and the
    database applying it too is what makes it hold for a direct caller.
    """
    db = pr3_db
    args, snapshot, _record, candidate, _links, make = _pr3_promotable(db, "query")
    rows = db.psql("select manufacturer || '|' || model_count || '|' || variant_count "
                   f"|| '|' || total_count from public.catalog_candidate_manufacturers('{snapshot}')")
    assert rows == f"{make}|1|1|1"
    assert db.psql("select commercial_model || '|' || variant_count || '|' || model_year_start "
                   f"from public.catalog_candidate_models('{snapshot}','{make}')") == "RAV4|1|2021"
    assert db.psql("select model_year || '|' || variant_count from "
                   f"public.catalog_candidate_model_years('{snapshot}','{make}','RAV4')") == "2021|1"
    assert db.psql("select id || '|' || upstream_record_id || '|' || total_count from "
                   f"public.catalog_candidate_variant_page('{snapshot}')") == f"{candidate}|36327|1"
    # A pending snapshot answers nothing at all.
    other = _rpc_as_service(db, "select id from public.record_catalog_snapshot_guarded("
                                f"{args},'{_catalog_snapshot_json('pr3-pending', retrieval_metadata=PR3_READ_METADATA)}'::jsonb)")
    with pytest.raises(AssertionError, match="catalog snapshot is not active"):
        db.psql(f"select * from public.catalog_candidate_manufacturers('{other}')")


def test_the_bounded_catalog_queries_bound_the_page_and_state_the_exact_total(pr3_db):
    """A caller asking for more than the server bound gets the bound.

    And `total_count` is the count of the whole FILTERED set, not of the page,
    so `has_more` is a fact rather than "the page came back full".
    """
    db = pr3_db
    _args, snapshot, _record, _candidate, _links, _make = _pr3_promotable(db, "bound")
    assert db.psql("select limit_applied from (select count(*) as limit_applied from "
                   f"public.catalog_candidate_variant_page('{snapshot}', p_limit => 9999)) t") == "1"
    assert db.psql("select public.catalog_page_limit()") == "200"
    # A page with no rows still states the total, as ONE count row whose every
    # item column is null. Two cases, and the second is the one that matters:
    # a filter that matched nothing, and an OFFSET past the last matching row.
    # Without the count row the second would report a total of 0 for a filter
    # that matched -- and would then say there is nothing more to read.
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(id) "
                   "from public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_manufacturer => 'Hyundai')") == "1|0|0"
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(id) "
                   "from public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_offset => 500)") == "1|1|0"
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(manufacturer) "
                   "from public.catalog_candidate_manufacturers("
                   f"'{snapshot}', p_offset => 500)") == "1|1|0"
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(commercial_model) "
                   "from public.catalog_candidate_models("
                   f"'{snapshot}', p_manufacturer => 'Toyota-bound', p_offset => 500)") == "1|1|0"
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(model_year) "
                   "from public.catalog_candidate_model_years("
                   f"'{snapshot}', p_manufacturer => 'Toyota-bound', "
                   "p_commercial_model => 'RAV4', p_offset => 500)") == "1|1|0"
    # An identity dimension outside the closed vocabulary is refused, never
    # matched loosely.
    with pytest.raises(AssertionError, match="unknown catalog identity dimension"):
        db.psql("select * from public.catalog_candidate_variant_page("
                f"'{snapshot}', p_identity_dimensions => '{{\"horsepower\": \"120\"}}'::jsonb)")
    with pytest.raises(AssertionError, match="unknown catalog candidate status"):
        db.psql(f"select * from public.catalog_candidate_variant_page('{snapshot}', p_status => 'promoted')")


def test_a_snapshot_with_an_unread_row_answers_only_under_acknowledgement(pr3_db):
    """PR2's reading gap, enforced by the database as well as by Python."""
    db = pr3_db
    lease, _other = _evidence_fixture(db, "pr3-gap")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    metadata = {**PR3_READ_METADATA, "normalization_issue_count": 1,
                "normalization_issues": [{"reason": "GOV_NORM_LABEL_CONTRADICTION", "count": 1}],
                "normalization_issue_records": ["36327"]}
    snapshot = _rpc_as_service(db, "select id from public.record_catalog_snapshot_guarded("
                                   f"{args},'{_catalog_snapshot_json('pr3-gap-snap', retrieval_metadata=metadata)}'::jsonb)")
    _rpc_as_service(db, "select id from public.record_catalog_raw_record_guarded("
                        f"{args},'{_catalog_record_json(snapshot, 'pr3-gap-rec')}'::jsonb)")
    _rpc_as_service(db, "select id from public.activate_catalog_snapshot_guarded("
                        f"{args},'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    with pytest.raises(AssertionError, match="rows its vocabulary could not read"):
        db.psql(f"select * from public.catalog_candidate_manufacturers('{snapshot}')")
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(manufacturer) "
                   "from public.catalog_candidate_manufacturers("
                   f"'{snapshot}', p_allow_incomplete => true)") == "1|0|0"
    # A RAW-ONLY snapshot is never answerable, acknowledged or not: it states
    # no identities, so there is no gap it could acknowledge.
    raw_only = {**PR3_READ_METADATA, "normalization_contract": "raw_only",
                "normalized_record_count": 0}
    other = _rpc_as_service(db, "select id from public.record_catalog_snapshot_guarded("
                                f"{args},'{_catalog_snapshot_json('pr3-rawonly', retrieval_metadata=raw_only)}'::jsonb)")
    _rpc_as_service(db, "select id from public.record_catalog_raw_record_guarded("
                        f"{args},'{_catalog_record_json(other, 'pr3-rawonly-rec')}'::jsonb)")
    _rpc_as_service(db, "select id from public.activate_catalog_snapshot_guarded("
                        f"{args},'{json.dumps({'snapshot_id': other})}'::jsonb)")
    for acknowledged in ("false", "true"):
        with pytest.raises(AssertionError, match="states no readable identities"):
            db.psql("select * from public.catalog_candidate_manufacturers("
                    f"'{other}', p_allow_incomplete => {acknowledged})")


# --- the promotion transaction ----------------------------------------------

def test_the_snapshot_diff_is_computed_here_and_states_exact_counts(pr3_db):
    """A whole-resource comparison, with no row leaving the database.

    The COUNTS are exact over every matching row of both snapshots; only the
    ITEM LIST is bounded, and it is dropped WHOLE rather than truncated. Both
    cases are proven against real PostgreSQL, because "it scales" is a claim
    about this function and not about the Python that calls it.
    """
    db = pr3_db
    previous_rows = [(f"CODE-{index:06d}", "candidate") for index in range(140)]
    _args, previous = _pr3_diff_snapshot(db, "diff-a", previous_rows)
    # The newer capture: 10 of the same identities (one of them re-READ), 3 new.
    current_rows = [(f"CODE-{index:06d}", "ambiguous" if index == 0 else "candidate")
                    for index in range(10)] + \
                   [(f"CODE-{900 + index:06d}", "candidate") for index in range(3)]
    _other, current = _pr3_diff_snapshot(db, "diff-b", current_rows)

    counts = db.psql("select distinct added_count || '|' || changed_count || '|' || removed_count "
                     "from public.catalog_snapshot_candidate_diff("
                     f"'{previous}','{current}')")
    # 3 added, 1 re-read, 130 gone -- and 134 items is past the 100 bound, so
    # the list is dropped whole while the counts stay exact.
    assert counts == "3|1|130"
    assert db.psql("select count(*) || '|' || count(state) from "
                   f"public.catalog_snapshot_candidate_diff('{previous}','{current}')") == "1|0"

    # Within the bound the items ARE listed, ordered deterministically, and the
    # counts are the same three numbers.
    small = [(f"CODE-{index:06d}", "candidate") for index in range(5)]
    _third, only_five = _pr3_diff_snapshot(db, "diff-c", small)
    assert db.psql("select string_agg(state || ':' || official_model_code, ',' order by state, "
                   "official_model_code) from public.catalog_snapshot_candidate_diff("
                   f"'{only_five}','{current}')") == (
        "added:CODE-000005,added:CODE-000006,added:CODE-000007,added:CODE-000008,"
        "added:CODE-000009,added:CODE-000900,added:CODE-000901,added:CODE-000902,"
        "changed:CODE-000000")
    assert db.psql("select distinct added_count || '|' || changed_count || '|' || removed_count "
                   f"from public.catalog_snapshot_candidate_diff('{only_five}','{current}')") \
        == "8|1|0"
    # A FIRST ingestion has no previous side, which is not a refusal.
    assert db.psql("select distinct added_count || '|' || removed_count from "
                   f"public.catalog_snapshot_candidate_diff(null,'{only_five}')") == "5|0"
    # And the same readability gate every other answer passes.
    with pytest.raises(AssertionError, match="catalog snapshot is not active"):
        db.psql("select * from public.catalog_snapshot_candidate_diff("
                f"'{previous}','{uuid.uuid4()}')")


def test_the_sql_scope_normalization_is_the_one_r4_stored_the_identity_with(pr3_db):
    """A mirror that drifts is worse than no mirror.

    `claims.identity_scope` is stored NORMALIZED, so the promotion gate has to
    normalize the candidate's raw text the same way to compare them. The two
    implementations are compared over a real vocabulary -- Hebrew marques,
    hyphenated model codes, padded trims -- rather than asserted to look alike.
    """
    db = pr3_db
    vocabulary = ["AXAP54L-ANXGBW", "PRIME AWD SE", "  Hybrid   Premium ", "4WD_A",
                  "טויוטה", "קורולה קרוס", "GR-Sport", "E-CVT", "front-wheel drive", "X１"]
    rendered = ", ".join(f"public.r4_normalized_scope_text('{value}')" for value in vocabulary)
    assert db.psql(f"select concat_ws('|', {rendered})") \
        == "|".join(normalize_field_key(value) for value in vocabulary)
    assert db.psql("select public.r4_normalized_scope_text(null) is null") == "t"


def test_a_promotion_writes_one_provenance_row_per_field_and_the_read_model(pr3_db):
    """The whole point of Catalog PR3, against real PostgreSQL.

    One canonical variant, five promoted facts, and every one of them traceable
    on its own to the candidate, the evidence link, the snapshot, the source,
    the claim, the VERIFIED verdict, the run and worker lease, the source
    version and the exact locator.
    """
    db = pr3_db
    args, snapshot, _record, candidate, links, make = _pr3_promotable(db, "promote")
    promotion = _pr3_promotion_json(candidate, links, make, key="pr3-promote")
    variant = _promote(db, args, promotion)

    assert db.psql("select count(*) from public.catalog_canonical_field_provenance "
                   f"where variant_id='{variant}'") == str(len(PR3_FIELDS))
    for field_key, value, _unit, _register in PR3_FIELDS:
        row = db.psql("select field_value::text || '|' || revision || '|' || "
                      "(source_id = (select source_id from public.catalog_candidate_evidence_links "
                      " where id = evidence_link_id)) || '|' || "
                      "(record_locator = (select evidence_locator from public.claims where id = claim_id)) "
                      "from public.catalog_canonical_field_provenance "
                      f"where variant_id='{variant}' and field_key='{field_key}'")
        expected = json.dumps(value) if not isinstance(value, str) else f'"{value}"'
        assert row == f"{expected}|1|true|true", (field_key, row)
    # The READ MODEL is what "the current canonical value" means, and it is
    # assembled from the highest revision of each field.
    current = db.psql("select manufacturer || '|' || commercial_model || '|' || model_year_start "
                      "|| '|' || official_model_code || '|' || trim || '|' || identity_dimensions::text "
                      f"from public.catalog_canonical_variant_current where variant_id='{variant}'")
    assert current == f'{make}|RAV4|2021|AXAP54L-ANXGBW|PRIME AWD SE|{{"fuel_type": "plug_in_hybrid"}}'
    # The row-level PR1 back-pointer is checked, not decorative: it names a
    # verdict and a candidate that are in this row's OWN field provenance.
    assert db.psql("select count(*) from public.catalog_model_variants v "
                   "join public.catalog_canonical_field_provenance p on p.variant_id = v.id "
                   "and p.verdict_id = v.promoted_from_verdict_id "
                   f"where v.id='{variant}'") >= "1"
    # An EXACT replay is a deterministic no-op: same row, same provenance.
    assert _promote(db, args, promotion) == variant
    assert db.psql("select count(*) from public.catalog_canonical_field_provenance "
                   f"where variant_id='{variant}'") == str(len(PR3_FIELDS))


def test_a_canonical_row_cannot_commit_without_provenance_for_every_field(pr3_db):
    """The DEFERRED gate, exercised by a writer that never called the RPC.

    `service_role` holds direct INSERT on the canonical relations, so this is
    the check that makes the grant safe: a canonical row whose stated fields
    are not all covered by revision-1 provenance cannot COMMIT, whichever path
    wrote it.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "deferred")
    verdict = links["model_year_start"]["verdict"]
    model_key, variant_key = "cm1." + "c" * 32, "cv1." + "c" * 32
    statement = (
        "begin; set role service_role; "
        "insert into public.catalog_models (manufacturer, commercial_model, canonical_key) "
        f"values ('Toyota','RAV4','{model_key}'); "
        "insert into public.catalog_model_variants (model_id, promoted_from_candidate_id, "
        "promoted_from_verdict_id, canonical_key, model_year_start, model_year_end, trim) "
        f"select id, '{candidate}', '{verdict}', '{variant_key}', 2021, 2021, 'PRIME AWD SE' "
        f"from public.catalog_models where canonical_key='{model_key}'; commit")
    with pytest.raises(AssertionError, match="requires verified provenance for every field"):
        db.psql(statement)
    assert db.psql(f"select count(*) from public.catalog_model_variants where canonical_key='{variant_key}'") == "0"
    assert db.psql(f"select count(*) from public.catalog_models where canonical_key='{model_key}'") == "0"
    # A bare canonical MODEL cannot commit either: a model exists because a
    # variant of it was promoted.
    with pytest.raises(AssertionError, match="requires at least one promoted variant"):
        db.psql("begin; set role service_role; "
                "insert into public.catalog_models (manufacturer, commercial_model, canonical_key) "
                f"values ('Toyota','COROLLA','{model_key}'); commit")


def test_a_promoted_fact_is_held_to_its_whole_support_chain_for_every_writer(pr3_db):
    """The BEFORE INSERT gate: a forged provenance row cannot be stored.

    Every case is one way a canonical fact could be wrong, attempted directly
    against the table rather than through the RPC -- which is the only way to
    show that the rule holds for `service_role`'s own DML.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "chain")
    variant = _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-chain"))
    run_id = args.split(",")[0].strip("'")
    year = links["model_year_start"]
    trim = links["trim"]

    def forge(field_key: str, value: str, link_id: str, expected: str):
        with pytest.raises(AssertionError, match=expected):
            db.psql(
                "set role service_role; "
                "insert into public.catalog_canonical_field_provenance "
                "(model_id, variant_id, field_key, field_value, revision, candidate_id, "
                " evidence_link_id, snapshot_id, source_id, claim_id, verdict_id, run_id, "
                " worker_id, attempt, source_version, source_version_kind, record_locator, "
                " promotion_key) "
                f"select v.model_id, v.id, '{field_key}', '{value}'::jsonb, 9, '{candidate}', "
                f" '{link_id}', l.snapshot_id, l.source_id, l.claim_id, l.verdict_id, '{run_id}', "
                f" 'w', 1, l.source_version, l.source_version_kind, l.record_locator, "
                f" '{'cp1.' + 'd' * 32}' "
                "from public.catalog_model_variants v, public.catalog_candidate_evidence_links l "
                f"where v.id='{variant}' and l.id='{link_id}'")

    # The claim states a DIFFERENT field than the one being promoted.
    forge("trim", '"PRIME AWD SE"', year["link"], "claim states a different field")
    # The claim states the right field at a DIFFERENT value.
    forge("trim", '"LIMITED"', trim["link"], "claim states a different value")
    # A field PR2 deliberately left UNMAPPED, verified end to end and still
    # refused. `koah_sus` is the exact case: the register publishes it, the
    # dataset defines no semantics for it, and a verified claim about it -- a
    # perfectly legitimate piece of evidence -- can still never become a
    # canonical fact, because it is outside the closed promotable vocabulary.
    chain_record, chain_scope = _pr3_context("chain", make)
    unmapped = _pr3_field_evidence(db, args, "pr3-chain-unmapped", "koah_sus", "150",
                                   None, "koah_sus", record_id=chain_record, scope=chain_scope)
    unmapped_link = _rpc_as_service(
        db, "select id from public.link_catalog_candidate_evidence_guarded("
            f"{args},'{_catalog_link_json(candidate, unmapped['source'], 'pr3-chain-unmapped-link', claim_id=unmapped['claim'], verdict_id=unmapped['verdict'], locator=None, version=None, kind=None)}'::jsonb)")
    forge("koah_sus", '"150"', unmapped_link,
          "catalog_canonical_field_provenance_field_allowlisted")
    # An IDENTITY field restated differently is a different variant wearing
    # this one's name.
    forge("model_year_start", "2099", year["link"], "claim states a different value")


def test_the_promotion_refusal_matrix(pr3_db):
    """Every way a promotion must fail closed, one case at a time."""
    db = pr3_db
    # 1. a `legacy_reference` source can never support a canonical fact, and
    #    the refusal comes one step EARLIER than promotion: such a candidate
    #    cannot acquire a verified evidence link at all, so there is nothing a
    #    promotion could cite.
    with pytest.raises(AssertionError, match="unverified catalog source cannot carry a verdict"):
        _pr3_promotable(db, "legacy", family="legacy_reference", fields=PR3_FIELDS[:1])
    assert db.psql("select count(*) from public.catalog_candidate_evidence_links l "
                   "join public.catalog_source_snapshots s on s.id = l.snapshot_id "
                   "where s.trust_state = 'unverified' and l.verdict_id is not null") == "0"

    # 2. an AMBIGUOUS candidate is never promotable.
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(
        db, "ambiguous", status="ambiguous")
    with pytest.raises(AssertionError, match="not ready for promotion"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-ambiguous"))

    # 3. a field the canonical row states with no evidence entry at all.
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "missing")
    with pytest.raises(AssertionError, match="do not match the canonical row"):
        _promote(db, args, _pr3_promotion_json(
            candidate, links, make, key="pr3-missing",
            entries=[{"field_key": field_key, "value": value,
                      "evidence_link_id": links[field_key]["link"]}
                     for field_key, value, _u, _r in PR3_FIELDS[:-1]]))

    # 4. an evidence entry for a field the canonical row does not state. The
    #    DIMENSION is dropped from the row while its entry stays, because the
    #    four IDENTITY fields cannot be varied here without tripping refusal 4b
    #    first -- which is itself the point of 4b.
    with pytest.raises(AssertionError, match="do not match the canonical row"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-extra",
                                               dimensions={}))

    # 4b. WRONG VEHICLE: a promotion whose identity is not its candidate's.
    #     The canonical key is derived from these fields, so without this the
    #     CALLER -- not the reviewed candidate -- would decide which vehicle a
    #     verified fact lands on. Each of the four identity fields, one at a
    #     time, plus the marque and the model.
    for label, wrong_make, override in (
            ("no-trim", make, {"trim": None}),
            ("other-trim", make, {"trim": "LIMITED"}),
            ("no-code", make, {"code": None}),
            ("other-years", make, {"years": (2022, 2022)}),
            ("other-make", "Toyota-elsewhere", {})):
        with pytest.raises(AssertionError, match="identity its candidate does not"):
            _promote(db, args, _pr3_promotion_json(candidate, links, wrong_make,
                                                   key=f"pr3-wrong-vehicle-{label}", **override))

    # 5. a promoted value that is not the value the verified claim states.
    with pytest.raises(AssertionError, match="do not match the canonical row"):
        _promote(db, args, _pr3_promotion_json(
            candidate, links, make, key="pr3-value",
            entries=[{"field_key": field_key,
                      "value": "LIMITED" if field_key == "trim" else value,
                      "evidence_link_id": links[field_key]["link"]}
                     for field_key, value, _u, _r in PR3_FIELDS]))

    # 6. an evidence link of ANOTHER candidate.
    other_args, _s, _r, other_candidate, other_links, _other_make = _pr3_promotable(db, "foreign")
    borrowed = [{"field_key": field_key, "value": value,
                 "evidence_link_id": (other_links if field_key == "trim" else links)[field_key]["link"]}
                for field_key, value, _u, _r in PR3_FIELDS]
    with pytest.raises(AssertionError, match="evidence of another candidate"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-foreign",
                                               entries=borrowed))
    # And the canonical model that promotion would have created does not exist:
    # a refused promotion leaves the catalog exactly as it was.
    assert db.psql("select count(*) from public.catalog_models where manufacturer="
                   f"'{make}'") == "0"

    # 7. an idempotency key replayed with DIFFERENT content.
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "replay")
    key = "pr3-replay"
    variant = _promote(db, args, _pr3_promotion_json(candidate, links, make, key=key))
    # The DIMENSION is what varies, because the four identity fields cannot:
    # a promotion whose identity is not its candidate's is refused earlier, by
    # the wrong-vehicle gate above. So this is a replay under one key that
    # states a DIFFERENT SET OF FACTS about the same vehicle -- exactly what an
    # idempotency key exists to catch.
    conflicting = json.loads(_pr3_promotion_json(candidate, links, make, key=key))
    conflicting["fields"] = [entry for entry in conflicting["fields"]
                             if entry["field_key"] != "identity_dimensions.fuel_type"]
    conflicting["identity_dimensions"] = {}
    with pytest.raises(AssertionError, match="idempotency conflict"):
        _promote(db, args, json.dumps(conflicting))
    assert db.psql("select count(*) from public.catalog_canonical_field_provenance "
                   f"where variant_id='{variant}'") == str(len(PR3_FIELDS))

    # 8. a STALE worker. The lease is the first statement of the RPC, so a
    #    superseded worker writes nothing at all.
    run_id = args.split(",")[0].strip("'")
    stale = f"'{run_id}','ghost',1,'not-the-token'"
    with pytest.raises(AssertionError, match="lease"):
        _promote(db, stale, _pr3_promotion_json(candidate, links, make, key="pr3-stale"))


#: The same table `tests/test_catalog_pr3_swarm_promotion.py` drives the
#: in-memory mirror with. One honest, fully verified chain per case, with
#: exactly one thing about WHAT IT IS ABOUT changed.
PR3_SCOPE_REFUSALS = (
    ("wrong-vehicle", "claim is about another vehicle",
     {"entity": "cm1." + "a" * 32 + ":2024"}),
    ("wrong-year", "scoped to another model year", {"time_scope": {"model_year": 1999}}),
    ("no-year", "states no model year scope", {"time_scope": {"as_of": "2026-08"}}),
    ("no-market", "states no market scope", {"market": None}),
    ("wrong-identity", "scoped to another vehicle identity",
     {"identity": {"model_code": "OTHER-CODE", "trim": "PRIME AWD SE"}}),
    ("extra-identity", "scoped to another vehicle identity",
     {"identity": {"model_code": "AXAP54L-ANXGBW", "trim": "PRIME AWD SE",
                   "generation": "XA50"}}),
)


def test_evidence_about_another_vehicle_scope_or_record_is_never_promoted(pr3_db):
    """Sound evidence about the WRONG THING is still refused, by the database.

    Each case below builds a COMPLETE chain -- a versioned government source,
    a focused fragment at the locator, a claim citing it, a `verified` verdict
    supported by that fragment, an evidence link for this exact candidate, and
    a field and value that match the canonical row exactly. The one thing that
    varies is what the claim is ABOUT.

    Without these gates a verified fact about a 1999 Corolla, about another
    market, about a different trim, or read out of a different register row
    could become a canonical fact about this vehicle -- the worst failure this
    table has, and the only one its own provenance could not later reveal.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "scope")
    record_id, scope = _pr3_context("scope", make)
    for label, expected, override in PR3_SCOPE_REFUSALS:
        forged = _pr3_field_evidence(db, args, f"pr3-scope-{label}", "model_year_start",
                                     2021, "year", "shnat_yitzur", record_id=record_id,
                                     scope={**scope, **override})
        link = _rpc_as_service(
            db, "select id from public.link_catalog_candidate_evidence_guarded("
                f"{args},'{_catalog_link_json(candidate, forged['source'], f'pr3-scope-{label}-link', claim_id=forged['claim'], verdict_id=forged['verdict'], locator=None, version=None, kind=None)}'::jsonb)")
        entries = [{"field_key": field_key, "value": value,
                    "evidence_link_id": link if field_key == "model_year_start"
                                        else links[field_key]["link"]}
                   for field_key, value, _u, _r in PR3_FIELDS]
        with pytest.raises(AssertionError, match=expected):
            _promote(db, args, _pr3_promotion_json(candidate, links, make,
                                                   key=f"pr3-scope-{label}", entries=entries))

    # WRONG RECORD: the same claim, read out of a different register row of the
    # same snapshot. A candidate is a reading of ONE captured row, and evidence
    # from another row proves nothing about it.
    elsewhere = _pr3_field_evidence(db, args, "pr3-scope-elsewhere", "model_year_start", 2021,
                                    "year", "shnat_yitzur",
                                    record_id=record_locator_id(
                                        _catalog_key("catalog.snapshot", "pr3-snap-scope"),
                                        "99999"),
                                    scope=scope)
    link = _rpc_as_service(
        db, "select id from public.link_catalog_candidate_evidence_guarded("
            f"{args},'{_catalog_link_json(candidate, elsewhere['source'], 'pr3-scope-elsewhere-link', claim_id=elsewhere['claim'], verdict_id=elsewhere['verdict'], locator=None, version=None, kind=None)}'::jsonb)")
    entries = [{"field_key": field_key, "value": value,
                "evidence_link_id": link if field_key == "model_year_start"
                                    else links[field_key]["link"]}
               for field_key, value, _u, _r in PR3_FIELDS]
    with pytest.raises(AssertionError, match="read from another source record"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make,
                                               key="pr3-scope-elsewhere", entries=entries))
    # Nothing was written by any of it. Scoped to THIS case's own marque: the
    # module shares one database, and earlier tests legitimately promoted their
    # own vehicles into it.
    assert db.psql(f"select count(*) from public.catalog_models where manufacturer='{make}'") == "0"
    assert db.psql("select count(*) from public.catalog_canonical_field_provenance p "
                   f"where p.candidate_id='{candidate}'") == "0"


def test_a_canonical_fact_is_never_promoted_out_of_an_unresolved_conflict(pr3_db):
    """Two verified sources disagreeing is not a value to pick -- it is a wait.

    Promoting either side would settle the disagreement by writing it down,
    which is precisely what a conflict record exists to prevent.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "conflict")
    claim = links["trim"]["claim"]
    db.psql("set role service_role; insert into public.conflicts "
            "(run_id, entity_key, field_key, claim_ids, outcome) values "
            f"('{args.split(',')[0].strip(chr(39))}','vehicle','trim', "
            f"array['{claim}']::uuid[], 'unresolved_needs_review')")
    with pytest.raises(AssertionError, match="unresolved conflict"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-conflict"))
    assert db.psql("select count(*) from public.catalog_model_variants v "
                   "join public.catalog_models m on m.id = v.model_id "
                   f"where m.manufacturer='{make}'") == "0"


def test_the_pending_promotion_read_reconstructs_the_candidate_from_durable_rows(pr3_db):
    """The crash-safety of the whole promotion path, in one read.

    Nothing in a worker's memory says which candidate a claim is evidence FOR.
    This function derives it from rows the server itself wrote -- the claim's
    own locator names one captured upstream row, the row belongs to one active
    snapshot, and the candidate is the reading of that row whose identity scope
    is exactly the claim's -- so a REPLACEMENT worker, which restored completed
    tasks and never re-executed the tool, finds exactly the work the crashed one
    would have done.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, _links, make = _pr3_promotable(db, "pending")
    run_id = args.split(",")[0].strip("'")
    read = ("select %s from public.catalog_run_pending_promotions("
            f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',25)")

    # One row per promoted field, all naming the SAME durable candidate.
    assert db.psql(read % "count(*) || '|' || count(distinct candidate_id)") \
        == f"{len(PR3_FIELDS)}|1"
    assert db.psql(read % "distinct candidate_id") == candidate
    assert db.psql(read % "distinct manufacturer || '|' || commercial_model || '|' || status") \
        == f"{make}|RAV4|ready_for_review"
    # Every field, in the deterministic order the function states.
    assert db.psql(read % "string_agg(field_key, ',')") \
        == ",".join(sorted(name for name, _v, _u, _r in PR3_FIELDS))
    # The claim, its source and its VERIFIED verdict all travel with it, which
    # is what lets the resume link without re-reading anything else.
    assert db.psql(read % "count(*)"
                   ) == db.psql(read % "count(distinct claim_id)")
    assert db.psql("select count(*) from public.catalog_run_pending_promotions("
                   f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',25) p "
                   "join public.claim_verdicts v on v.id = p.verdict_id "
                   "where v.verdict = 'verified'") == str(len(PR3_FIELDS))
    # BOUNDED by candidates, not by rows.
    assert db.psql("select count(*) from public.catalog_run_pending_promotions("
                   f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',0)") == "0"

    # A claim of ANOTHER tool operation is invisible: the association is only
    # ever derived for the one registered Government read.
    assert db.psql("select count(*) from public.catalog_run_pending_promotions("
                   f"'{run_id}','catalog.government_vehicle.get_variants',25)") == "0"
    # And another RUN's evidence is another run's.
    assert db.psql("select count(*) from public.catalog_run_pending_promotions("
                   f"'{uuid.uuid4()}','{PROMOTABLE_TOOL_OPERATION}',25)") == "0"
    with pytest.raises(AssertionError, match="a run and a tool operation are required"):
        db.psql(f"select * from public.catalog_run_pending_promotions('{run_id}','',25)")


def test_the_pending_promotion_read_refuses_what_a_promotion_would_refuse(pr3_db):
    """An unverified verdict, an ambiguous reading and a foreign record.

    Each one makes the candidate invisible to the resume rather than visible
    and then refused: the derivation and the promotion gate agree about what
    may become a canonical fact, so a resumed worker never even proposes one
    the database would reject.
    """
    db = pr3_db
    read = "select count(*) from public.catalog_run_pending_promotions('%s','%s',25)"

    # 1. NO VERIFIED VERDICT. The evidence is durable and the run owes nothing.
    args, _s, _r, _candidate, _links, _make = _pr3_promotable(db, "pending-unverified",
                                                              verified=False)
    unverified_run = args.split(",")[0].strip("'")
    assert db.psql(read % (unverified_run, PROMOTABLE_TOOL_OPERATION)) == "0"

    # 2. AN AMBIGUOUS READING stays ambiguous. Promotion may not overrule a
    #    decision the ingestion made, so the resume does not see it at all.
    args, _s, _r, _candidate, _links, _make = _pr3_promotable(db, "pending-ambiguous",
                                                              status="ambiguous")
    ambiguous_run = args.split(",")[0].strip("'")
    assert db.psql(read % (ambiguous_run, PROMOTABLE_TOOL_OPERATION)) == "0"

    # 3. A LOCATOR NAMING ANOTHER RECORD resolves to no candidate at all.
    args, _snapshot, _record, candidate, _links, make = _pr3_promotable(db, "pending-foreign")
    foreign_run = args.split(",")[0].strip("'")
    before = db.psql(read % (foreign_run, PROMOTABLE_TOOL_OPERATION))
    _pr3_field_evidence(db, args, "pr3-pending-foreign-elsewhere", "model_year_start", 2021,
                        "year", "shnat_yitzur",
                        record_id=record_locator_id(
                            _catalog_key("catalog.snapshot", "pr3-snap-pending-foreign"),
                            "77777"),
                        scope=_pr3_scope(make))
    assert db.psql(read % (foreign_run, PROMOTABLE_TOOL_OPERATION)) == before

    # The identity scope the derivation JOINS on is the one Python builds. A
    # drift here would make every honest resume find nothing, so the two are
    # compared as values rather than reviewed as code.
    dimensions = {"fuel_type": "plug_in_hybrid", "drivetrain": "AWD", "body_style": "SUV"}
    rendered = db.psql("select public.catalog_candidate_identity_scope("
                       f"'{json.dumps(dimensions)}'::jsonb,"
                       "'AXAP54L-ANXGBW','PRIME AWD SE')::text")
    assert json.loads(rendered) == candidate_identity_scope(
        dimensions, "AXAP54L-ANXGBW", "PRIME AWD SE")
    # An absent code and an absent trim are ABSENT KEYS on both sides.
    bare = db.psql("select public.catalog_candidate_identity_scope("
                   "'{}'::jsonb, null, null)::text")
    assert json.loads(bare) == candidate_identity_scope({}, None, None) == {}


def test_a_revision_appends_and_the_read_model_moves_but_the_row_never_does(pr3_db):
    """Canonical UPDATES are append-only revisions, and the view is the answer.

    A later, better source revises a DIMENSION by appending revision 2. The
    canonical row's own columns -- frozen at revision 1, and immutable by
    trigger -- do not move, and the authoritative read model reports the new
    value. That is what keeps one definition of "the current canonical value".
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "revision")
    variant = _promote(db, args, _pr3_promotion_json(candidate, links, make, key="pr3-revision"))
    revision_record, revision_scope = _pr3_context("revision", make)
    revised = _pr3_field_evidence(db, args, "pr3-revision-later",
                                  "identity_dimensions.fuel_type", "electric", None, "delek_cd",
                                  record_id=revision_record, scope=revision_scope)
    link = _rpc_as_service(
        db, "select id from public.link_catalog_candidate_evidence_guarded("
            f"{args},'{_catalog_link_json(candidate, revised['source'], 'pr3-revision-link', claim_id=revised['claim'], verdict_id=revised['verdict'], locator=None, version=None, kind=None)}'::jsonb)")
    payload = json.loads(_pr3_promotion_json(candidate, links, make, key="pr3-revision"))
    payload["promotion_key"] = catalog_keys.derive_key("catalog.promotion",
                                                       test_label="pr3-revision-2")
    payload["identity_dimensions"] = {"fuel_type": "electric"}
    payload["fields"] = [entry for entry in payload["fields"]
                         if entry["field_key"] != "identity_dimensions.fuel_type"]
    payload["fields"].append({"field_key": "identity_dimensions.fuel_type",
                              "value": "electric", "evidence_link_id": link})
    assert _promote(db, args, json.dumps(payload)) == variant
    assert db.psql("select revision || '|' || field_value::text from "
                   "public.catalog_canonical_field_current where "
                   f"variant_id='{variant}' and field_key='identity_dimensions.fuel_type'") \
        == '2|"electric"'
    # The canonical ROW never moved, and could not have: it is immutable.
    assert db.psql(f"select identity_dimensions::text from public.catalog_model_variants where id='{variant}'") \
        == '{"fuel_type": "plug_in_hybrid"}'
    assert db.psql("select identity_dimensions::text from "
                   f"public.catalog_canonical_variant_current where variant_id='{variant}'") \
        == '{"fuel_type": "electric"}'
    # TWO independent barriers, and the order matters: `service_role` has no
    # UPDATE privilege at all, and even a role that did would be stopped by the
    # trigger. Both are asserted, because either one alone could be relaxed by
    # accident.
    with pytest.raises(AssertionError, match="permission denied"):
        db.psql("set role service_role; update public.catalog_model_variants "
                f"set identity_dimensions='{{}}'::jsonb where id='{variant}'")
    with pytest.raises(AssertionError, match="canonical catalog rows are immutable"):
        db.psql("update public.catalog_model_variants "
                f"set identity_dimensions='{{}}'::jsonb where id='{variant}'")
    # And a promoted fact is append-only outright, by privilege AND by trigger.
    with pytest.raises(AssertionError, match="permission denied"):
        db.psql("set role service_role; delete from public.catalog_canonical_field_provenance "
                f"where variant_id='{variant}'")
    with pytest.raises(AssertionError, match="append-only"):
        db.psql(f"delete from public.catalog_canonical_field_provenance where variant_id='{variant}'")


# =============================================================================
# CODE-3 -- the bounded, read-only catalog REVIEW surface, against real SQL
# =============================================================================
#
# The in-memory mirror is exercised by `tests/test_catalog_review_surface.py`.
# What CANNOT be proved there is the half that is SQL: that the database itself
# filters `ready_for_review`, that its `total_count` is exact past the end of a
# page, and that `catalog_canonical_variant_current` answers the bounded
# canonical listing under exactly the columns, filters, ordering and range the
# repository asks for. Those are proved here, against a real cluster.


def test_code3_the_candidate_review_page_is_filtered_to_ready_for_review_in_sql(pr3_db):
    """`p_status` is a DATABASE filter, and every other status is held out.

    Two candidates of the same snapshot, one `ready_for_review` and one not.
    The review page must carry exactly the first, and `total_count` must be the
    count of the FILTERED set -- not of the snapshot.
    """
    db = pr3_db
    args, snapshot, record, ready, _links, _make = _pr3_promotable(db, "code3-status")
    # A SECOND candidate of the same snapshot, left as an ordinary `candidate`.
    # On the same captured row: the snapshot is already active and an active
    # snapshot's raw records are immutable, while a candidate reading of one is
    # exactly what the reconciliation round still writes. A different
    # commercial model gives it its own derived candidate key.
    plain = _rpc_as_service(
        db, "select id from public.record_catalog_candidate_guarded("
            f"{args},'{_catalog_candidate_json(snapshot, record, 'code3-cand-2', status='candidate', make='Toyota-code3-status', model='COROLLA')}'::jsonb)")
    assert db.psql(f"select count(*) from public.catalog_candidate_variants where snapshot_id='{snapshot}'") == "2"

    # The review page: one row, the ready one, and an exact total of 1.
    assert db.psql("select id || '|' || status || '|' || total_count from "
                   "public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_status => 'ready_for_review')") == f"{ready}|ready_for_review|1"
    # The other candidate is in the snapshot and is NOT in the review page.
    assert db.psql("select count(*) from public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_status => 'ready_for_review') where id='{plain}'") == "0"
    # Unfiltered, the snapshot really does hold both -- so the absence above is
    # the filter working, not an empty snapshot.
    assert db.psql("select count(*) from public.catalog_candidate_variant_page("
                   f"'{snapshot}')") == "2"


def test_code3_the_review_page_states_the_exact_total_past_the_end_of_a_page(pr3_db):
    """An offset past the last matching row reports the REAL total, not zero.

    This is what makes CODE-3's `has_more` a fact. Without the count row the
    surface would report `total: 0` for a filter that matched, and would then
    tell an operator there is nothing more to review.
    """
    db = pr3_db
    _args, snapshot, _record, _ready, _links, _make = _pr3_promotable(db, "code3-total")
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(id) "
                   "from public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_status => 'ready_for_review', p_offset => 500)") == "1|1|0"
    # A status that matches nothing is a real zero, and is distinguishable.
    assert db.psql("select count(*) || '|' || max(total_count) || '|' || count(id) "
                   "from public.catalog_candidate_variant_page("
                   f"'{snapshot}', p_status => 'rejected')") == "1|0|0"


def test_code3_the_canonical_view_answers_the_repositorys_bounded_listing(pr3_db):
    """Exactly the query `SupabaseRepository.list_canonical_catalog_variants`
    builds: this column list, these filter columns, this ordering, this range,
    and an exact count -- run against the real view.

    The column list is read from the repository rather than restated, so a
    column added there without a reviewed migration fails HERE.
    """
    db = pr3_db
    from backend.repository.supabase import SupabaseRepository

    columns = SupabaseRepository.CANONICAL_VARIANT_COLUMNS
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "code3-canon")
    variant = _promote(db, args, _pr3_promotion_json(candidate, links, make,
                                                     key="pr3-code3-canon"))

    # Every column the repository selects exists on the view and is readable.
    assert db.psql(f"select count(*) from (select {columns} from "
                   f"public.catalog_canonical_variant_current where variant_id='{variant}') t") == "1"
    # The exact-match filters the repository applies.
    key = db.psql(f"select canonical_key from public.catalog_canonical_variant_current where variant_id='{variant}'")
    assert db.psql("select count(*) from public.catalog_canonical_variant_current "
                   f"where canonical_key='{key}'") == "1"
    assert db.psql("select count(*) from public.catalog_canonical_variant_current "
                   f"where manufacturer='{make}' and commercial_model='RAV4'") == "1"
    # The model-year filter is range CONTAINMENT, exactly as the repository's
    # `lte(model_year_start)` + `gte(model_year_end)` pair expresses it.
    assert db.psql("select count(*) from public.catalog_canonical_variant_current "
                   f"where manufacturer='{make}' and model_year_start <= 2021 "
                   "and model_year_end >= 2021") == "1"
    assert db.psql("select count(*) from public.catalog_canonical_variant_current "
                   f"where manufacturer='{make}' and model_year_start <= 1999 "
                   "and model_year_end >= 1999") == "0"
    # The ordering is by `canonical_key`, which is ASCII by construction, so the
    # page order does not depend on the cluster's text collation -- the reason
    # every other catalog listing orders by a derived key rather than by the
    # Hebrew identity text.
    assert db.psql(f"select canonical_key ~ '^cv1\\.[0-9a-f]{{32}}$' from "
                   f"public.catalog_canonical_variant_current where variant_id='{variant}'") == "t"
    # And the range the repository applies really does bound the answer.
    bounded = int(db.psql("select count(*) from (select 1 from "
                          "public.catalog_canonical_variant_current order by canonical_key "
                          f"limit {SupabaseRepository.MAX_CANONICAL_LIST_ROWS} offset 0) t"))
    assert bounded <= SupabaseRepository.MAX_CANONICAL_LIST_ROWS


def test_code3_the_canonical_view_is_security_invoker_and_grants_nothing_extra(pr3_db):
    """A view is a SHAPE, never a privilege.

    `security_invoker` is what keeps the review surface from reading more than
    its caller could, and `anon` holding no privilege on it is what keeps the
    browser's own Supabase key from reading the canonical catalog directly --
    every CODE-3 read goes through the membership-authorized API instead.
    """
    db = pr3_db
    assert db.psql("select 'security_invoker=true' = any(reloptions) from pg_class "
                   "where relname='catalog_canonical_variant_current'") == "t"
    assert db.psql("select count(*) from information_schema.role_table_grants where "
                   "table_name='catalog_canonical_variant_current' and grantee='anon'") == "0"


# --- migration 20260920000100 (execution usage ledger) ---------------------
#
# The ExecutionUsageLedger invariants, executed against real PostgreSQL:
# component-wise monotonic merge, a versioned and idempotent lease-guarded
# write, the public projection into runs.usage, monotonic legacy writers, the
# storage-level trigger, and service-path-only ACLs.

def _ledger(**values) -> str:
    return json.dumps(values)


def _ledger_worker(db, worker: str):
    run_id = _seed_stale_worker_run(db)
    attempt, token = db.psql(
        f"select attempt, lease_token from public.claim_run_lease('{run_id}', '{worker}', 300)"
    ).split("|")
    return run_id, f"'{run_id}', '{worker}', {attempt}, '{token}'", attempt, token


def test_merge_execution_usage_is_a_component_wise_maximum(db):
    merged = json.loads(db.psql(
        "select public.merge_execution_usage("
        "'{\"model_calls\": 5, \"input_tokens\": 10, \"output_tokens\": 1, \"actual_cost\": 0.5, \"retries\": 0}'::jsonb, "
        "'{\"model_calls\": 2, \"input_tokens\": 3, \"output_tokens\": 9, \"actual_cost\": 0.9, \"retries\": 3, \"tool_calls\": 4}'::jsonb)"
    ))
    assert merged["model_calls"] == 5 and merged["input_tokens"] == 10 and merged["output_tokens"] == 9
    assert float(merged["actual_cost"]) == 0.9 and merged["retries"] == 3 and merged["tool_calls"] == 4
    assert merged["total_tokens"] == 19                       # derived, never maximised alone
    # Order-independent and idempotent.
    reverse = json.loads(db.psql(
        "select public.merge_execution_usage("
        "'{\"model_calls\": 2, \"input_tokens\": 3, \"output_tokens\": 9, \"actual_cost\": 0.9, \"retries\": 3, \"tool_calls\": 4}'::jsonb, "
        "'{\"model_calls\": 5, \"input_tokens\": 10, \"output_tokens\": 1, \"actual_cost\": 0.5, \"retries\": 0}'::jsonb)"
    ))
    assert reverse == merged
    again = json.loads(db.psql(
        f"select public.merge_execution_usage('{json.dumps(merged)}'::jsonb, '{json.dumps(merged)}'::jsonb)"))
    assert again == merged
    # Null and empty contribute nothing; a negative value is refused.
    assert json.loads(db.psql("select public.merge_execution_usage(null, '{}'::jsonb)")) == {}
    with pytest.raises(AssertionError, match="USAGE_LEDGER_INVALID"):
        db.psql("select public.merge_execution_usage('{}'::jsonb, '{\"model_calls\": -1}'::jsonb)")


def test_record_run_usage_guarded_is_versioned_idempotent_and_never_lowers(db):
    run_id, lease, attempt, token = _ledger_worker(db, "worker-LEDGER")
    first = _ledger(model_calls=3, provider_attempts=3, input_tokens=30, output_tokens=5,
                    actual_cost=0.3, tool_calls=2, replans=1, elapsed_seconds=4.5)
    v1 = db.psql(f"select version from public.record_run_usage_guarded({lease}, '{first}'::jsonb)")
    assert v1 == "1"
    # An identical replay advances nothing.
    assert db.psql(f"select version from public.record_run_usage_guarded({lease}, '{first}'::jsonb)") == "1"
    # A record that is BEHIND on every dimension but one merges monotonically:
    # only the advanced dimension moves, and the version moves exactly once.
    behind = _ledger(model_calls=1, provider_attempts=1, input_tokens=2, output_tokens=0,
                     actual_cost=0.01, tool_calls=2, replans=0, elapsed_seconds=1.0, tasks_failed=1)
    row = db.psql(f"select version, ledger from public.record_run_usage_guarded({lease}, '{behind}'::jsonb)")
    version, ledger = row.split("|", 1)
    ledger = json.loads(ledger)
    assert version == "2"
    assert (ledger["model_calls"], ledger["input_tokens"], ledger["tool_calls"], ledger["replans"]) == (3, 30, 2, 1)
    assert float(ledger["actual_cost"]) == 0.3 and float(ledger["elapsed_seconds"]) == 4.5
    assert ledger["tasks_failed"] == 1 and ledger["total_tokens"] == 35
    assert ledger["ledger_version"] == 2 and ledger["schema_version"] == 1
    # The caller never owns the sequence number.
    forged = _ledger(model_calls=3, ledger_version=99)
    assert json.loads(db.psql(
        f"select ledger from public.record_run_usage_guarded({lease}, '{forged}'::jsonb)"))["ledger_version"] == 2
    # runs.usage carries EXACTLY the public projection, merged, never a
    # ledger-only key.
    usage = json.loads(db.psql(f"select usage from public.runs where id='{run_id}'"))
    assert set(usage) == {"model_calls", "input_tokens", "output_tokens", "total_tokens",
                          "estimated_cost", "actual_cost", "retries",
                          "provider_backpressure_events", "agent_steps", "elapsed_seconds"} & set(usage)
    assert usage["model_calls"] == 3 and usage["total_tokens"] == 35
    assert "tool_calls" not in usage and "ledger_version" not in usage
    assert db.psql(f"select attempt, worker_id from public.run_execution_usage where run_id='{run_id}'") == f"{attempt}|worker-LEDGER"


def test_a_stale_worker_cannot_write_the_ledger_and_the_replacement_continues_it(db):
    run_id, lease_a, attempt_a, token_a = _ledger_worker(db, "worker-LA")
    db.psql(f"select public.record_run_usage_guarded({lease_a}, '{_ledger(model_calls=4, tool_calls=1)}'::jsonb)")
    before = db.psql(f"select version, ledger from public.run_execution_usage where run_id='{run_id}'")
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    attempt_b, token_b = db.psql(
        f"select attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-LB', 300)").split("|")
    assert int(attempt_b) == int(attempt_a) + 1
    for stale in (_ledger(model_calls=99), _ledger(model_calls=0)):
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            db.psql(f"select public.record_run_usage_guarded({lease_a}, '{stale}'::jsonb)")
    assert db.psql(f"select version, ledger from public.run_execution_usage where run_id='{run_id}'") == before
    lease_b = f"'{run_id}', 'worker-LB', {attempt_b}, '{token_b}'"
    row = db.psql(f"select version, attempt, ledger->>'model_calls' from public.record_run_usage_guarded({lease_b}, '{_ledger(model_calls=5)}'::jsonb)")
    assert row == f"2|{attempt_b}|5"
    assert db.psql(f"select ledger->>'tool_calls' from public.run_execution_usage where run_id='{run_id}'") == "1"


def _ledger_migration():
    return next(m for m in MIGRATIONS if "execution_usage_ledger" in m.name)


def test_reapplying_the_corrective_migration_out_of_order_needs_the_ledger_migration_again(db):
    """`20260810000600` (re)defines `update_run_usage_guarded` and
    `transition_run_worker_guarded` with their old OVERWRITE bodies, and this
    module proves that migration rerun-safe. Migrations are applied strictly
    in sequence (`MIGRATIONS.md`), so the shipped end state is the monotonic
    one -- but an operator who re-runs `000600` AFTER `20260920000100` would
    quietly get the overwrite back. That hazard is stated here, and so is its
    remedy: `20260920000100` is rerun-safe and must be re-applied last."""
    corrective = next(m for m in MIGRATIONS if "corrective_lease_and_attempt_hardening" in m.name)
    run_id, lease, _, _ = _ledger_worker(db, "worker-ORDER")
    db.psql(file=corrective)
    db.psql(f"select public.update_run_usage_guarded({lease}, '{_ledger(model_calls=6)}'::jsonb)")
    db.psql(f"select public.update_run_usage_guarded({lease}, '{_ledger(model_calls=2)}'::jsonb)")
    assert db.psql(f"select usage->>'model_calls' from public.runs where id='{run_id}'") == "2"   # reverted
    db.psql(file=_ledger_migration())
    db.psql(f"select public.update_run_usage_guarded({lease}, '{_ledger(model_calls=1)}'::jsonb)")
    assert db.psql(f"select usage->>'model_calls' from public.runs where id='{run_id}'") == "2"   # monotonic again


def test_legacy_usage_writers_are_monotonic_too(db):
    # Order-independent within this module: the test above deliberately
    # reverts the writers and restores them; this one states the shipped end
    # state, so it re-applies the (rerun-safe) ledger migration first.
    db.psql(file=_ledger_migration())
    run_id, lease, attempt, token = _ledger_worker(db, "worker-MONO")
    db.psql(f"select public.update_run_usage_guarded({lease}, '{_ledger(model_calls=6, input_tokens=60, output_tokens=0, actual_cost=0.6)}'::jsonb)")
    db.psql(f"select public.update_run_usage_guarded({lease}, '{_ledger(model_calls=2, input_tokens=5, output_tokens=7, actual_cost=0.1)}'::jsonb)")
    usage = json.loads(db.psql(f"select usage from public.runs where id='{run_id}'"))
    assert (usage["model_calls"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (6, 60, 7, 67)
    assert float(usage["actual_cost"]) == 0.6
    db.psql(f"select public.transition_run_worker_guarded('{run_id}', 'running', 'starting', 'worker-MONO', {attempt}, '{token}')")
    db.psql(
        f"select public.transition_run_worker_guarded('{run_id}', 'budget_exhausted', 'running', 'worker-MONO', {attempt}, '{token}', "
        f"null, '{{\"code\": \"MODEL_CALL_LIMIT_REACHED\"}}'::jsonb, false, '{_ledger(model_calls=1, actual_cost=0.05)}'::jsonb, null, now())")
    usage = json.loads(db.psql(f"select usage from public.runs where id='{run_id}'"))
    assert usage["model_calls"] == 6 and float(usage["actual_cost"]) == 0.6
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "budget_exhausted"


def test_the_ledger_row_refuses_any_direct_update_that_lowers_a_counter(db):
    run_id, lease, _, _ = _ledger_worker(db, "worker-TRIG")
    db.psql(f"select public.record_run_usage_guarded({lease}, '{_ledger(model_calls=3, actual_cost=0.3)}'::jsonb)")
    with pytest.raises(AssertionError, match="USAGE_LEDGER_NOT_MONOTONIC"):
        db.psql(f"update public.run_execution_usage set ledger = ledger || '{{\"model_calls\": 2}}'::jsonb, version = version + 1 where run_id='{run_id}'")
    with pytest.raises(AssertionError, match="USAGE_LEDGER_NOT_MONOTONIC"):
        db.psql(f"update public.run_execution_usage set ledger = ledger - 'actual_cost', version = version + 1 where run_id='{run_id}'")
    with pytest.raises(AssertionError, match="USAGE_LEDGER_NOT_MONOTONIC"):
        db.psql(f"update public.run_execution_usage set ledger = ledger || '{{\"model_calls\": 4}}'::jsonb where run_id='{run_id}'")
    with pytest.raises(AssertionError, match="USAGE_LEDGER_NOT_MONOTONIC"):
        db.psql(f"update public.run_execution_usage set version = 0 where run_id='{run_id}'")
    assert db.psql(f"select version, ledger->>'model_calls' from public.run_execution_usage where run_id='{run_id}'") == "1|3"


def test_execution_usage_ledger_is_service_path_only_and_rerun_safe(db):
    for signature in [
        "public.merge_execution_usage(jsonb, jsonb)",
        "public.execution_usage_public_projection(jsonb)",
        "public.record_run_usage_guarded(uuid, text, integer, text, jsonb)",
        "public.update_run_usage_guarded(uuid, text, integer, text, jsonb)",
        "public.transition_run_worker_guarded(uuid, text, text, text, integer, text, jsonb, jsonb, boolean, jsonb, timestamptz, timestamptz)",
    ]:
        assert not _has_execute(db, "anon", signature), signature
        assert not _has_execute(db, "authenticated", signature), signature
        assert _has_execute(db, "service_role", signature), signature
    assert db.psql(
        "select relrowsecurity from pg_class where oid = 'public.run_execution_usage'::regclass") == "t"
    assert db.psql("select count(*) from pg_policies where tablename = 'run_execution_usage'") == "0"
    for role in ("anon", "authenticated"):
        assert db.psql(f"select has_table_privilege('{role}', 'public.run_execution_usage', 'select')") == "f"
    db.psql(file=_ledger_migration())
    db.psql(file=_ledger_migration())
    assert db.psql("select count(*) from pg_proc where proname='record_run_usage_guarded'") == "1"
    assert db.psql("select count(*) from pg_trigger where tgname='run_execution_usage_monotonic'") == "1"


# ---------------------------------------------------------------------------
# 20260920000200: atomic run finalization -- the terminal transition and the
# terminal event commit together, or neither does.
# ---------------------------------------------------------------------------


def _finalizing_worker(db, worker: str):
    """A leased worker whose run is `running`, ready to be finalized."""
    run_id, _lease, attempt, token = _ledger_worker(db, worker)
    db.psql(f"select public.transition_run_worker_guarded('{run_id}', 'running', 'starting', '{worker}', {attempt}, '{token}')")
    return run_id, attempt, token


def _finalize(run_id, status, expected, worker, attempt, token, *, event_type=None,
              message="m", payload='{"product_outcome": {"semantic_status": "partial"}}',
              output="null", error="null", clear_error="false", usage="null"):
    event = "null" if event_type is None else f"'{event_type}'"
    return (
        "select status from public.finalize_run_guarded("
        f"p_run_id => '{run_id}', p_status => '{status}', p_expected_status => {expected}, "
        f"p_worker_id => '{worker}', p_attempt => {attempt}, p_lease_token => '{token}', "
        f"p_output => {output}, p_error => {error}, p_clear_error => {clear_error}, p_usage => {usage}, "
        f"p_finished_at => now(), p_event_type => {event}, p_event_message => '{message}', "
        f"p_event_payload => '{payload}'::jsonb)"
    )


def _terminal_events(db, run_id):
    return db.psql(
        f"select event_type from public.run_events where run_id='{run_id}' "
        "and event_type in ('run_completed','run_partial_success','run_failed','run_cancelled') order by id"
    ).splitlines()


def test_finalize_run_guarded_commits_the_transition_and_the_event_together(db):
    run_id, attempt, token = _finalizing_worker(db, "worker-FIN1")
    status = db.psql(_finalize(run_id, "partial_success", "'running'", "worker-FIN1", attempt, token,
                               event_type="run_partial_success", output='\'{"fields": {"a": 1}}\'::jsonb',
                               error="null", clear_error="true",
                               usage='\'{"model_calls": 2, "input_tokens": 5}\'::jsonb'))
    assert status == "partial_success"
    row = db.psql(f"select status, output, error, finished_at is not null, usage->>'model_calls' from public.runs where id='{run_id}'")
    assert row.startswith("partial_success|") and '"fields"' in row and row.endswith("|t|2")
    assert _terminal_events(db, run_id) == ["run_partial_success"]
    stored = json.loads(db.psql(
        f"select payload from public.run_events where run_id='{run_id}' and event_type='run_partial_success'"))
    assert stored == {"product_outcome": {"semantic_status": "partial"}}


def test_finalize_run_guarded_rejects_a_moved_status_and_records_no_event(db):
    """The blocker at the database boundary: a cancellation request between
    the decision and the write makes the compare-and-set fail, and because
    the event insert comes after it in the same function, nothing is written."""
    run_id, attempt, token = _finalizing_worker(db, "worker-FIN2")
    db.psql(f"update public.runs set status='cancellation_requested' where id='{run_id}'")
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        db.psql(_finalize(run_id, "completed", "'running'", "worker-FIN2", attempt, token,
                          event_type="run_completed"))
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "cancellation_requested"
    assert _terminal_events(db, run_id) == []
    # Re-decided under the state that holds, both land together and agree.
    assert db.psql(_finalize(run_id, "cancelled", "'cancellation_requested'", "worker-FIN2", attempt, token,
                             event_type="run_cancelled", payload='{"code": "RUN_CANCELLED_AFTER_RESULT"}',
                             output='\'{"kept": true}\'::jsonb')) == "cancelled"
    assert db.psql(f"select status, output->>'kept' from public.runs where id='{run_id}'") == "cancelled|true"
    assert _terminal_events(db, run_id) == ["run_cancelled"]


def test_finalize_run_guarded_rejects_a_stale_lease_and_records_no_event(db):
    run_id, attempt, token = _finalizing_worker(db, "worker-FIN3")
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        db.psql(_finalize(run_id, "completed", "'running'", "worker-FIN3", attempt, token,
                          event_type="run_completed"))
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "running"
    assert _terminal_events(db, run_id) == []


def test_finalize_run_guarded_rolls_the_transition_back_when_the_event_cannot_be_written(db):
    """Both or neither: an event insert that fails takes the transition down
    with it, so a terminal run can never be left without its evidence."""
    run_id, attempt, token = _finalizing_worker(db, "worker-FIN4")
    db.psql(
        "create or replace function public._test_refuse_terminal_event() returns trigger language plpgsql as $$ "
        "begin if new.message = 'BOOM' then raise exception 'TEST_EVENT_STORE_DOWN'; end if; return new; end $$; "
        "create trigger _test_refuse_terminal_event before insert on public.run_events "
        "for each row execute function public._test_refuse_terminal_event()"
    )
    try:
        with pytest.raises(AssertionError, match="TEST_EVENT_STORE_DOWN"):
            db.psql(_finalize(run_id, "completed", "'running'", "worker-FIN4", attempt, token,
                              event_type="run_completed", message="BOOM"))
        assert db.psql(f"select status, finished_at is null from public.runs where id='{run_id}'") == "running|t"
        assert _terminal_events(db, run_id) == []
    finally:
        db.psql("drop trigger if exists _test_refuse_terminal_event on public.run_events; "
                "drop function if exists public._test_refuse_terminal_event()")
    # With the store back, the same finalization lands whole.
    assert db.psql(_finalize(run_id, "completed", "'running'", "worker-FIN4", attempt, token,
                             event_type="run_completed")) == "completed"
    assert _terminal_events(db, run_id) == ["run_completed"]


def test_finalize_run_guarded_refuses_non_terminal_and_blind_finalization(db):
    run_id, attempt, token = _finalizing_worker(db, "worker-FIN5")
    with pytest.raises(AssertionError, match="RUN_FINALIZATION_INVALID"):
        db.psql(_finalize(run_id, "waiting", "'running'", "worker-FIN5", attempt, token))
    with pytest.raises(AssertionError, match="RUN_FINALIZATION_INVALID"):
        db.psql(_finalize(run_id, "completed", "null", "worker-FIN5", attempt, token))
    with pytest.raises(AssertionError, match="RUN_FINALIZATION_INVALID"):
        db.psql(_finalize(run_id, "completed", "'running'", "worker-FIN5", attempt, token,
                          event_type="run_completed", payload='[1, 2]'))
    assert db.psql(f"select status from public.runs where id='{run_id}'") == "running"
    assert _terminal_events(db, run_id) == []
    # A budget stop owes no event of its own: the transition alone is fine.
    assert db.psql(_finalize(run_id, "budget_exhausted", "'running'", "worker-FIN5", attempt, token,
                             error='\'{"code": "COST_LIMIT_REACHED", "message": "m"}\'::jsonb')) == "budget_exhausted"
    assert _terminal_events(db, run_id) == []


def test_finalize_run_guarded_is_service_path_only(db):
    signature = "public.finalize_run_guarded(uuid, text, text, text, integer, text, jsonb, jsonb, boolean, jsonb, timestamptz, text, text, jsonb)"
    assert db.psql(f"select has_function_privilege('anon', '{signature}', 'EXECUTE')") == "f"
    assert db.psql(f"select has_function_privilege('authenticated', '{signature}', 'EXECUTE')") == "f"
    assert db.psql(f"select has_function_privilege('service_role', '{signature}', 'EXECUTE')") == "t"
    assert db.psql("select count(*) from pg_proc where proname='finalize_run_guarded'") == "1"



# ===========================================================================
# R5: CURRENT verdict authority -- 20260921000100_current_verdict_authority
# ===========================================================================
#
# `public.claim_verdicts` is append-only, so "a verified verdict exists for
# this claim" stays true forever, including after a re-verification rejected
# it. Every consumer of verified evidence asked exactly that question --
# `catalog_run_pending_promotions` joined on it, and both catalog gates checked
# only what the CITED row said -- so a stale `verified` could authorize a
# canonical fact.
#
# The rule that replaces it lives in
# `backend/engines/swarm_v2/current_verdict.py` and in this migration, and the
# two are pinned together textually by
# `tests/test_current_verdict_authority.py`. What is proven HERE is that
# PostgreSQL itself applies it -- for every writer, including a direct
# `service_role` call that never went through the backend.

def _r5_reverify(db, args: str, claim: str, *, key: str, verdict: str = "rejected",
                 reason: str = "R4_VALUE_MISMATCH") -> str:
    """A NEWER verdict for one claim: what a second verification pass writes."""
    return _rpc_as_service(
        db, "select id from public.record_claim_verdict_guarded("
            f"{args},'{_r4_verdict_json(claim, key=key, verdict=verdict, reason=reason)}'::jsonb)")


def _r5_state(db, claim: str) -> str:
    return db.psql("select state || '|' || coalesce(verdict_id::text,'-') || '|' || "
                   f"support_count from public.claim_current_verdict_state('{claim}')")


def test_the_current_verdict_resolution_is_the_databases_own(db):
    """A newer verdict ends an older `verified`, in the database itself."""
    lease, _other = _evidence_fixture(db, "r5-current")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _source, fragment, claim, verdict = _shared_chain(db, args, "r5-current")

    assert _r5_state(db, claim) == f"supported|{verdict}|1"
    assert db.psql(f"select public.claim_current_verdict_id('{claim}')") == verdict
    assert db.psql("select public.claim_verdict_is_current_support("
                   f"'{claim}','{verdict}')") == "t"

    rejection = _r5_reverify(db, args, claim, key="r5-current-rejected")

    assert _r5_state(db, claim) == f"rejected|{rejection}|0"
    assert db.psql("select public.claim_verdict_is_current_support("
                   f"'{claim}','{verdict}')") == "f"
    # The older row is still there. Append-only history did not change -- what
    # it authorizes did.
    assert db.psql(f"select count(*) from public.claim_verdicts where claim_id='{claim}'") == "2"
    assert db.psql("select count(*) from public.claim_verdicts where "
                   f"claim_id='{claim}' and verdict='verified'") == "1"
    # An unknown claim resolves to NO ROW: "not verified" is a statement about
    # a claim, and this is not one.
    assert db.psql(f"select count(*) from public.claim_current_verdict_state("
                   f"'{uuid.uuid4()}')") == "0"


def test_two_verdicts_of_one_transaction_resolve_fail_closed(db):
    """`created_at` is the TRANSACTION clock, so an exact tie is reachable.

    Both verdicts are written in one implicit transaction, so both carry the
    identical `now()`. Resolving that by id -- or by insertion order -- would
    decide current truth by chance; the non-`verified` verdict wins instead.
    """
    lease, _other = _evidence_fixture(db, "r5-tie")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    source = _rpc_as_service(db, "select id from public.upsert_source_guarded("
                                 f"{args},'{json.dumps(evidence_fixtures.source_payload('r5-tie-source'))}'::jsonb)")
    fragment = _rpc_as_service(db, "select id from public.record_evidence_fragment_guarded("
                                   f"{args},'{json.dumps(evidence_fixtures.fragment_payload('r5-tie-fragment', source))}'::jsonb)")
    claim = _rpc_as_service(db, "select id from public.create_claim_with_source_guarded("
                                f"{args},'{json.dumps(evidence_fixtures.claim_payload('r5-tie-claim', source))}'::jsonb)")
    accepted = json.dumps(evidence_fixtures.verdict_payload(
        "r5-tie-verified", claim, support=[evidence_fixtures.support_link(fragment)]))
    refused = _r4_verdict_json(claim, key="r5-tie-rejected", verdict="rejected",
                               reason="R4_VALUE_MISMATCH")

    # ONE statement, so ONE transaction, so ONE `now()`.
    db.psql("set role service_role; "
            f"select public.record_claim_verdict_guarded({args},'{accepted}'::jsonb); "
            f"select public.record_claim_verdict_guarded({args},'{refused}'::jsonb); "
            "reset role")

    assert db.psql("select count(distinct created_at) from public.claim_verdicts "
                   f"where claim_id='{claim}'") == "1"
    assert db.psql(f"select state from public.claim_current_verdict_state('{claim}')") \
        == "rejected"


def test_a_decided_or_open_contradiction_is_not_current_in_the_database(db):
    """A supersession and an unresolved conflict, both ahead of any verdict."""
    lease, _other = _evidence_fixture(db, "r5-conflict")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _source, _fragment, claim, _verdict = _shared_chain(db, args, "r5-conflict")
    assert _r5_state(db, claim).startswith("supported")

    # An UNRESOLVED conflict covering the claim: nothing is current.
    conflict_claims = f"array['{claim}']::uuid[]"
    _rpc_as_service(db, "insert into public.conflicts"
                        "(run_id, entity_key, field_key, claim_ids, outcome) values "
                        f"('{run_id}','entity','engine_displacement_cc',{conflict_claims},"
                        "'unresolved_needs_review')")
    assert db.psql(f"select state from public.claim_current_verdict_state('{claim}')") \
        == "contested"

    # A RESOLVED one that superseded it outranks even that.
    other_claim = _rpc_as_service(
        db, "select id from public.create_claim_with_source_guarded("
            f"{args},'{json.dumps(evidence_fixtures.claim_payload('r5-conflict-rival', _source, value=1600))}'::jsonb)")
    resolution = _r4_resolution_json([claim, other_claim], key="r5-conflict-resolution",
                                     winner=other_claim, superseded=[claim])
    _rpc_as_service(db, "select id from public.record_conflict_resolution_guarded("
                        f"{args},'{resolution}'::jsonb)")
    assert db.psql(f"select state from public.claim_current_verdict_state('{claim}')") \
        == "superseded"


def test_an_inactive_claim_is_invalidated_in_the_database(db):
    """A claim that is no longer active carries nothing current."""
    lease, _other = _evidence_fixture(db, "r5-inactive")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _source, _fragment, claim, _verdict = _shared_chain(db, args, "r5-inactive")

    _rpc_as_service(db, f"update public.claims set status='superseded' where id='{claim}'")

    assert db.psql(f"select state from public.claim_current_verdict_state('{claim}')") \
        == "invalidated"
    assert db.psql("select verdict_id is null from "
                   f"public.claim_current_verdict_state('{claim}')") == "t"


def test_the_current_verdict_read_is_bounded_and_run_scoped(db):
    lease, other = _evidence_fixture(db, "r5-read")
    run_id, worker, attempt, token, _ = lease
    args = f"'{run_id}','{worker}',{attempt},'{token}'"
    _source, _fragment, claim, _verdict = _shared_chain(db, args, "r5-read")

    assert db.psql("select count(*) from public.claim_current_verdict_states("
                   f"'{run_id}',null,200)") == "1"
    assert db.psql("select state from public.claim_current_verdict_states("
                   f"'{run_id}',array['{claim}']::uuid[],200)") == "supported"
    assert db.psql("select count(*) from public.claim_current_verdict_states("
                   f"'{other[0]}',null,200)") == "0"
    assert db.psql("select count(*) from public.claim_current_verdict_states("
                   f"'{run_id}',null,0)") == "0"
    with pytest.raises(AssertionError, match="a run is required"):
        db.psql("select * from public.claim_current_verdict_states(null,null,200)")


def test_a_catalog_link_may_not_cite_a_verdict_that_is_no_longer_current(pr3_db):
    """The earliest durable gate: a link is provenance, not an opinion."""
    db = pr3_db
    args, _snapshot, _record, candidate, links, _make = _pr3_promotable(db, "r5-link")
    field = sorted(links)[0]
    claim, verdict = links[field]["claim"], links[field]["verdict"]

    _r5_reverify(db, args, claim, key="r5-link-rejected")

    with pytest.raises(AssertionError, match="current supported verdict"):
        _rpc_as_service(
            db, "select id from public.link_catalog_candidate_evidence_guarded("
                f"{args},'{_catalog_link_json(candidate, links[field]['source'], 'r5-link-late', claim_id=claim, verdict_id=verdict, locator=None, version=None, kind=None)}'::jsonb)")


def test_a_stale_verified_verdict_can_never_authorize_a_canonical_fact(pr3_db):
    """THE regression, against the real promotion transaction.

    Every link here was legitimate when it was written -- the link-time gate
    passed, because the verdict WAS current then. The re-verification happens
    afterwards, which is exactly the window a link-time check cannot close.
    """
    db = pr3_db
    args, _snapshot, _record, candidate, links, make = _pr3_promotable(db, "r5-stale")
    for index, field in enumerate(sorted(links)):
        _r5_reverify(db, args, links[field]["claim"], key=f"r5-stale-rejected-{index}")

    with pytest.raises(AssertionError, match="no longer current"):
        _promote(db, args, _pr3_promotion_json(candidate, links, make, key="r5-stale"))

    # Nothing was written: the canonical row and its provenance are one
    # transaction, so a refusal leaves neither.
    assert db.psql("select count(*) from public.catalog_model_variants v "
                   "join public.catalog_models m on m.id = v.model_id "
                   f"where m.manufacturer='{make}'") == "0"
    assert db.psql("select count(*) from public.catalog_canonical_field_provenance p "
                   "join public.catalog_candidate_evidence_links l on l.id = p.evidence_link_id "
                   f"where l.candidate_id='{candidate}'") == "0"


def test_the_pending_promotion_read_resolves_current_truth(pr3_db):
    """The read that drives a resumed worker stops proposing stale work."""
    db = pr3_db
    args, _snapshot, _record, _candidate, links, _make = _pr3_promotable(db, "r5-pending")
    run_id = args.split(",")[0].strip("'")
    read = ("select count(*) from public.catalog_run_pending_promotions("
            f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',25)")
    assert db.psql(read) == str(len(PR3_FIELDS))

    # ONE field is re-verified as needs_review. That field's claim drops out;
    # every other field is untouched.
    field = sorted(links)[0]
    _r5_reverify(db, args, links[field]["claim"], key="r5-pending-review",
                 verdict="needs_review", reason="R4_SOURCE_SILENT")
    assert db.psql(read) == str(len(PR3_FIELDS) - 1)
    assert db.psql("select string_agg(field_key, ',') from "
                   "public.catalog_run_pending_promotions("
                   f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',25)") == \
        ",".join(sorted(name for name, _v, _u, _r in PR3_FIELDS)[1:])
    # And the verdict id the read carries is the CURRENT one for every row.
    assert db.psql("select count(*) from public.catalog_run_pending_promotions("
                   f"'{run_id}','{PROMOTABLE_TOOL_OPERATION}',25) p "
                   "where p.verdict_id = public.claim_current_verdict_id(p.claim_id)") \
        == str(len(PR3_FIELDS) - 1)


def test_reapplying_the_catalog_promotion_migration_needs_this_one_again(db):
    """The rerun hazard, stated where an operator will find it.

    `20260916120000` defines `catalog_run_pending_promotions` with the "a
    verified verdict exists" join R5 replaces, and this module deliberately
    proves that migration rerun-safe. Migrations apply strictly in sequence
    (`MIGRATIONS.md`), so the shipped end state is the R5 one -- but an
    operator who re-runs `20260916120000` afterwards would quietly get the old
    join back. That hazard is stated here, and so is its remedy: this
    migration is rerun-safe and must be re-applied last.
    """
    promotion = next(m for m in MIGRATIONS if "catalog_field_level_promotion" in m.name)
    current = next(m for m in MIGRATIONS if "current_verdict_authority" in m.name)
    installed = ("select pg_get_functiondef(p.oid) from pg_proc p "
                 "join pg_namespace n on n.oid = p.pronamespace "
                 "where n.nspname='public' and p.proname='catalog_run_pending_promotions'")

    db.psql(file=current)
    assert "claim_current_verdict_state" in db.psql(installed)
    db.psql(file=promotion)
    assert "claim_current_verdict_state" not in db.psql(installed)   # reverted
    db.psql(file=current)
    assert "claim_current_verdict_state" in db.psql(installed)       # current again


def test_the_current_verdict_functions_are_service_only(db):
    """A browser role may not ask, let alone answer, this question."""
    for signature in ("public.claim_current_verdict_id(uuid)",
                      "public.claim_current_verdict_state(uuid)",
                      "public.claim_current_verdict_states(uuid,uuid[],integer)",
                      "public.claim_verdict_is_current_support(uuid,uuid)",
                      "public.current_verdict_contract_version()",
                      "public.catalog_check_link_current_verdict()",
                      "public.catalog_check_provenance_current_verdict()"):
        for role in ("anon", "authenticated"):
            assert db.psql(
                f"select has_function_privilege('{role}', '{signature}', 'execute')") == "f"
        assert db.psql(
            f"select has_function_privilege('service_role', '{signature}', 'execute')") == "t"


# ---------------------------------------------------------------------------
# 20260921000200: the immutable run identity, and the last unfenced worker
# writes.
# ---------------------------------------------------------------------------


def _identity_migration():
    return next(m for m in MIGRATIONS if "immutable_run_identity" in m.name)


def _identity_json(run_id: str, workflow_key: str = "swarm_v2", **over) -> str:
    record = {
        "identity_version": "milo-run-identity/1",
        "run_id": run_id,
        "workflow_key": workflow_key,
        "engine_version": "swarm_v2.1",
        "policy_version": "milo-runtime-policy/1",
        "policy_fingerprint": "f" * 64,
        "release_sha": "a" * 40,
        "event_registry_version": "milo-event-registry/1",
    }
    record.update(over)
    return json.dumps(record)


def test_bind_run_identity_writes_once_and_is_idempotent(db):
    run_id = _seed_stale_worker_run(db)
    record = _identity_json(run_id)

    bound = db.psql(f"select run_identity from public.bind_run_identity('{run_id}', '{record}'::jsonb)")
    assert json.loads(bound)["workflow_key"] == "swarm_v2"
    # An identical re-bind is a no-op that returns the same row.
    again = db.psql(f"select run_identity from public.bind_run_identity('{run_id}', '{record}'::jsonb)")
    assert json.loads(again) == json.loads(bound)


def test_a_bound_identity_can_never_be_changed_by_any_path(db):
    """REQUIRED REGRESSION 1, at the only boundary that actually holds.

    An application guard is advisory against a second writer; the trigger is
    not. A V2 run must not be able to become a V1 one through the binder, a
    direct service-role UPDATE, or an erasure.
    """
    run_id = _seed_stale_worker_run(db)
    db.psql(f"select public.bind_run_identity('{run_id}', '{_identity_json(run_id)}'::jsonb)")

    # 1) Through the binder.
    with pytest.raises(AssertionError, match="RUN_IDENTITY_IMMUTABLE"):
        db.psql(f"select public.bind_run_identity('{run_id}', "
                f"'{_identity_json(run_id, 'vehicle_catalog_v1', engine_version='vehicle_catalog_v1.stage3')}'::jsonb)")
    # 2) Through a direct service-role table write.
    with pytest.raises(AssertionError, match="RUN_IDENTITY_IMMUTABLE"):
        db.psql(f"update public.runs set run_identity = "
                f"'{_identity_json(run_id, 'vehicle_catalog_v1', engine_version='vehicle_catalog_v1.stage3')}'::jsonb "
                f"where id='{run_id}'")
    # 3) By erasing it. Dropping an identity is a rewrite too.
    with pytest.raises(AssertionError, match="RUN_IDENTITY_IMMUTABLE"):
        db.psql(f"update public.runs set run_identity = null where id='{run_id}'")

    assert json.loads(db.psql(f"select run_identity from public.runs where id='{run_id}'"))["workflow_key"] == "swarm_v2"
    # Every other column still updates normally: the trigger fences the
    # identity, it does not freeze the row.
    assert db.psql(f"update public.runs set status='queued' where id='{run_id}' returning status") == "queued"


def test_an_identity_naming_another_run_or_a_bad_shape_is_refused(db):
    run_id = _seed_stale_worker_run(db)
    other = _seed_stale_worker_run(db)
    with pytest.raises(AssertionError, match="RUN_IDENTITY_INVALID"):
        db.psql(f"select public.bind_run_identity('{run_id}', '{_identity_json(other)}'::jsonb)")
    with pytest.raises(AssertionError, match="RUN_IDENTITY_INVALID"):
        db.psql(f"select public.bind_run_identity('{run_id}', '[]'::jsonb)")
    with pytest.raises(AssertionError, match="RUN_IDENTITY_INVALID"):
        db.psql(f"select public.bind_run_identity('{run_id}', null)")
    with pytest.raises(AssertionError, match="RUN_NOT_FOUND"):
        db.psql("select public.bind_run_identity('00000000-0000-4000-8000-000000000000', "
                "'{\"run_id\": \"00000000-0000-4000-8000-000000000000\"}'::jsonb)")


def test_the_identity_column_refuses_a_record_that_is_not_one(db):
    """The shape constraint is the part the database can enforce on EVERY
    path into the column, including a direct write that bypasses the binder."""
    run_id = _seed_stale_worker_run(db)
    for broken in ('\'"a string"\'::jsonb',
                   "'{}'::jsonb",
                   f"'{_identity_json(run_id, policy_fingerprint='')}'::jsonb"):
        with pytest.raises(AssertionError, match="runs_run_identity_shape_check"):
            db.psql(f"update public.runs set run_identity = {broken} where id='{run_id}'")
    # A legacy row with no identity at all is untouched by the constraint.
    assert db.psql(f"select run_identity is null from public.runs where id='{run_id}'") == "t"


def _newly_guarded_calls(run_id: str, worker: str, attempt: str, token: str) -> dict[str, str]:
    """The three worker writes that had NO lease fence before 20260921000200."""
    lease = f"'{run_id}', '{worker}', {attempt}, '{token}'"
    return {
        "tool_access_request": (
            f"select id from public.create_tool_access_request_guarded({lease}, "
            "'{\"agent\": \"a\", \"tool\": \"web_search\", \"reason\": \"r\"}'::jsonb)"),
        "tool_grant": (
            f"select id from public.create_tool_grant_guarded({lease}, "
            "'{\"agent\": \"a\", \"tool\": \"web_search\", \"max_searches\": 1, "
            "\"max_rounds\": 1, \"approver_policy\": \"auto\", "
            "\"expires_at\": \"2030-01-01T00:00:00+00:00\"}'::jsonb)"),
        "usage_ledger_row": (
            f"select id from public.append_usage_ledger_guarded({lease}, "
            "'{\"provider\": \"moonshot\", \"model\": \"kimi\", \"call_seq\": 7, "
            "\"decision\": \"settled\", \"actual_cost\": 0.01}'::jsonb)"),
    }


def test_a_stale_worker_cannot_perform_the_newly_guarded_writes(db):
    """REQUIRED REGRESSION 5, for the three paths that had no fence at all.

    The ledger row is the one that mattered most: it is what
    `sum_daily_ledger_cost` reads, so an unfenced append let a replaced worker
    keep charging a run it no longer owned against the live worker's DAILY
    allowance.
    """
    run_id = _seed_stale_worker_run(db)
    worker_a, attempt_a, token_a = db.psql(
        f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-IDA', 300)").split("|")

    # While current, worker A performs every one of them.
    for name, sql in _newly_guarded_calls(run_id, worker_a, attempt_a, token_a).items():
        assert db.psql(sql).strip(), f"live worker blocked on {name}"

    # The lease lapses and a replacement claims it.
    db.psql(f"update public.runs set lease_expires_at = now() - interval '1 minute' where id='{run_id}'")
    db.psql(f"select public.claim_run_lease('{run_id}', 'worker-IDB', 300)")

    for name, sql in _newly_guarded_calls(run_id, worker_a, attempt_a, token_a).items():
        with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
            db.psql(sql)
    # Nothing landed from the stale attempt: one row each, from the live one.
    assert db.psql(f"select count(*) from public.run_usage_ledger where run_id='{run_id}'") == "1"
    assert db.psql(f"select count(*) from public.tool_grants where run_id='{run_id}'") == "1"


def test_a_guarded_ledger_row_is_charged_to_the_fenced_run_not_the_payload(db):
    run_id = _seed_stale_worker_run(db)
    other = _seed_stale_worker_run(db)
    worker, attempt, token = db.psql(
        f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_id}', 'worker-LEDGERID', 300)").split("|")
    row = db.psql(
        f"select run_id from public.append_usage_ledger_guarded('{run_id}', '{worker}', {attempt}, '{token}', "
        f"'{{\"run_id\": \"{other}\", \"provider\": \"moonshot\", \"model\": \"kimi\", \"call_seq\": 9, \"decision\": \"settled\"}}'::jsonb)")
    assert row == run_id
    assert db.psql(f"select count(*) from public.run_usage_ledger where run_id='{other}'") == "0"


def test_a_tool_grant_cannot_mark_another_runs_request_granted(db):
    """The grant path mutates TWO tables. Both are inside one function body
    and under one lease, and the request must belong to THIS run."""
    run_a = _seed_stale_worker_run(db)
    run_b = _seed_stale_worker_run(db)
    worker_a, attempt_a, token_a = db.psql(
        f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_a}', 'worker-GRANTA', 300)").split("|")
    worker_b, attempt_b, token_b = db.psql(
        f"select worker_id, attempt, lease_token from public.claim_run_lease('{run_b}', 'worker-GRANTB', 300)").split("|")

    request_b = db.psql(
        f"select id from public.create_tool_access_request_guarded('{run_b}', '{worker_b}', {attempt_b}, '{token_b}', "
        "'{\"agent\": \"a\", \"tool\": \"web_search\", \"reason\": \"r\"}'::jsonb)")
    with pytest.raises(AssertionError, match="TOOL_GRANT_INVALID"):
        db.psql(
            f"select id from public.create_tool_grant_guarded('{run_a}', '{worker_a}', {attempt_a}, '{token_a}', "
            f"'{{\"request_id\": \"{request_b}\", \"agent\": \"a\", \"tool\": \"web_search\", \"max_searches\": 1, "
            "\"max_rounds\": 1, \"approver_policy\": \"auto\", \"expires_at\": \"2030-01-01T00:00:00+00:00\"}'::jsonb)")
    # Neither table moved: the refusal rolls the status update back with it.
    assert db.psql(f"select status from public.tool_access_requests where id='{request_b}'") == "pending"
    assert db.psql(f"select count(*) from public.tool_grants where run_id='{run_a}'") == "0"


def test_the_run_identity_migration_is_service_only_and_rerun_safe(db):
    for signature in ("public.bind_run_identity(uuid, jsonb)",
                      "public.create_tool_access_request_guarded(uuid, text, integer, text, jsonb)",
                      "public.create_tool_grant_guarded(uuid, text, integer, text, jsonb)",
                      "public.append_usage_ledger_guarded(uuid, text, integer, text, jsonb)"):
        for role in ("anon", "authenticated"):
            assert not _has_execute(db, role, signature), signature
        assert _has_execute(db, "service_role", signature), signature
    for role in ("anon", "authenticated"):
        assert db.psql(f"select has_column_privilege('{role}', 'public.runs', 'run_identity', 'update')") == "f"

    db.psql(file=_identity_migration())
    db.psql(file=_identity_migration())
    assert db.psql("select count(*) from pg_proc where proname='bind_run_identity'") == "1"
    assert db.psql("select count(*) from pg_trigger where tgname='runs_forbid_identity_rewrite'") == "1"
