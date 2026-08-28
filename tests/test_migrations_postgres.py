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

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.normalization import (
    SCOPE_NORMALIZATION_VERSION, canonical_scope_hash, canonical_scope_key,
)

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
