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

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

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
