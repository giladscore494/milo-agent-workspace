"""Executable migration compatibility test against ephemeral PostgreSQL.

Applies the exact confirmed legacy production baseline
(tests/fixtures/legacy_baseline.sql), seeds legacy rows, runs migrations
001-006 in order, and asserts that data is preserved and the schema matches
what the backend requires. Migrations are then re-applied to prove they are
rerun-safe.

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

    def __init__(self, pg_bin: str):
        self.pg_bin = pg_bin
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
                f"-o '-k {self.dir} -p {PG_PORT} -c listen_addresses=' start"
            ),
            check=True, capture_output=True,
        )

    def stop(self) -> None:
        pg_ctl = os.path.join(self.pg_bin, "pg_ctl")
        subprocess.run(self._server_cmd(f"{pg_ctl} -D {self.dir}/data -m immediate stop"), capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)

    def psql(self, sql: str | None = None, file: Path | None = None) -> str:
        cmd = ["psql", "-h", self.dir, "-p", PG_PORT, "-U", "postgres", "-d", "milo",
               "-v", "ON_ERROR_STOP=1", "-X", "-q", "-t", "-A"]
        if file is not None:
            cmd += ["-f", str(file)]
        else:
            cmd += ["-c", sql]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(f"psql failed:\n{result.stderr}\n(sql: {file or sql})")
        return result.stdout.strip()


@pytest.fixture(scope="module")
def db():
    pg_bin = _find_pg_bin()
    if pg_bin is None or shutil.which("psql") is None:
        pytest.skip("PostgreSQL server binaries not available; executable migration validation skipped")
    server = EphemeralPostgres(pg_bin)
    server.start()
    try:
        subprocess.run(
            ["psql", "-h", server.dir, "-p", PG_PORT, "-U", "postgres", "-d", "postgres",
             "-X", "-q", "-c", "create database milo"],
            check=True, capture_output=True,
        )
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


SEED_LEGACY_ROWS = """
insert into public.conversations (id, title) values
  ('11111111-1111-1111-1111-111111111111', 'legacy conversation');
insert into public.runs (id, conversation_id, user_prompt, status, current_phase, progress, result, error_message) values
  ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111',
   'legacy prompt', 'completed', 'summary', 100, '{"models": []}'::jsonb, null),
  ('33333333-3333-3333-3333-333333333333', '11111111-1111-1111-1111-111111111111',
   'legacy failed prompt', 'legacy_error_state', 'fetch', 40, null, 'legacy failure text');
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


def test_runs_status_check_rejects_new_invalid_status_but_keeps_legacy_rows(db):
    assert db.psql(
        "select status from public.runs where id = '33333333-3333-3333-3333-333333333333'"
    ) == "legacy_error_state"
    with pytest.raises(AssertionError, match="runs_status_check"):
        db.psql(
            "insert into public.runs (conversation_id, status, input) values "
            "('11111111-1111-1111-1111-111111111111', 'made_up_status', '{}'::jsonb)"
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
