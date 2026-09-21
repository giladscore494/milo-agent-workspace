"""Remote migration-state classification must cover EVERY local migration.

The defect this file exists to prevent: `check-migration-state.sh` used to
classify a remote database from a hard-coded list of object markers covering
only migrations `001`–`015`. Every migration added since is timestamped, so a
database carrying all fifteen numeric markers was reported `fully-migrated`
even when ten real migrations had never been applied. The production
database is exactly that shape, which means the tool's green answer was the
most dangerous possible answer: it said "nothing to apply" about a database
missing a third of its schema.

The authoritative comparison is now the COMPLETE local migration set against
`supabase_migrations.schema_migrations`. Object markers are secondary: they
can only ever ADD a drift finding.

Nothing here contacts a database. The shell entrypoint is run for real
against a mocked `psql` that records every statement it is asked to run, so
"read-only" is proved from the statements themselves rather than asserted.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO / "supabase" / "migrations"
SCRIPT = REPO / "scripts" / "release" / "check-migration-state.sh"

sys.path.insert(0, str(REPO / "scripts" / "release"))

from migration_state import (  # noqa: E402
    LEGACY_BASELINE_TABLES,
    MARKERS,
    MigrationStateError,
    classify,
    local_migrations,
    parse_version,
    sort_key,
)

# The production migration history observed by an independent authenticated
# READ-ONLY inspection: applied through 20260823000100, nothing after it.
PRODUCTION_APPLIED = [
    "001", "002", "003", "004", "005", "006", "007", "008",
    "009", "010", "011", "012", "013", "014", "015",
    "20260706192500",
    "20260810000100",
    "20260810000200",
    "20260810000300",
    "20260810000400",
    "20260810000500",
    "20260810000600",
    "20260818000100",
    "20260818000200",
    "20260823000100",
]

# The local migrations production has never applied.
PRODUCTION_PENDING = [
    "20260828000100",
    "20260828000200",
    "20260902000100",
    "20260907000100",
    "20260914200000",
    "20260915120000",
    "20260915180000",
    "20260916090000",
    "20260916120000",
    "20260920000100",
    "20260920000200",
    "20260921000100",
]


@pytest.fixture(scope="module")
def local() -> list[dict[str, str]]:
    return local_migrations(MIGRATIONS_DIR)


# ---------------------------------------------------------------------------
# version parsing — the local side must cover every migration family
# ---------------------------------------------------------------------------
def test_three_digit_and_timestamped_versions_both_parse():
    assert parse_version("001_project_workspace.sql") == "001"
    assert parse_version("20260916120000_catalog_field_level_promotion.sql") == "20260916120000"


@pytest.mark.parametrize(
    "name",
    [
        "no_version.sql",
        "1_short.sql",
        "0001_four_digits.sql",
        "202609161200_twelve_digits.sql",
        "001-dash-separator.sql",
        "001_project_workspace.txt",
        "001_.sql",
    ],
)
def test_malformed_migration_filenames_fail_closed(name):
    """An unparseable version would silently drop a real migration."""
    with pytest.raises(MigrationStateError):
        parse_version(name)


def test_duplicate_versions_fail_closed_across_the_whole_directory(tmp_path):
    """Uniqueness is checked over every migration, not only the 001–015 set."""
    (tmp_path / "001_a.sql").write_text("select 1;")
    (tmp_path / "20260916120000_a.sql").write_text("select 1;")
    (tmp_path / "20260916120000_b.sql").write_text("select 1;")
    with pytest.raises(MigrationStateError, match="duplicate migration version 20260916120000"):
        local_migrations(tmp_path)


def test_local_set_is_every_repository_migration_in_canonical_order(local):
    on_disk = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert [entry["file"] for entry in local] == sorted(on_disk, key=lambda n: sort_key(parse_version(n)))
    assert len(local) == len(on_disk)
    # Both families are really present — the bug was a comparison that saw
    # only one of them.
    assert any(len(entry["version"]) == 3 for entry in local)
    assert any(len(entry["version"]) == 14 for entry in local)


def test_every_declared_marker_belongs_to_a_real_local_migration(local):
    versions = {entry["version"] for entry in local}
    assert set(MARKERS) <= versions, sorted(set(MARKERS) - versions)


# ---------------------------------------------------------------------------
# classification — pure comparison
# ---------------------------------------------------------------------------
def observation(
    *,
    history: bool = True,
    applied: list[str] | None = None,
    table_count: int | None = None,
    baseline: list[str] | None = None,
    markers: dict[str, bool] | None = None,
) -> dict:
    applied = list(applied or [])
    return {
        "history_available": history,
        "applied": applied,
        "public_table_count": 40 if table_count is None else table_count,
        "legacy_baseline": list(LEGACY_BASELINE_TABLES) if baseline is None else baseline,
        "markers": {version: True for version in applied if version in MARKERS} | dict(markers or {}),
    }


def test_production_history_is_partially_migrated_and_never_fully_migrated(local):
    """The mandatory fixture: the real production history, ending at 20260823000100."""
    report = classify(local, observation(applied=PRODUCTION_APPLIED))

    assert report["state"] == "partially-migrated"
    assert report["state"] != "fully-migrated"
    assert report["blocked"] is False


def test_production_history_reports_exactly_the_pending_migrations(local):
    report = classify(local, observation(applied=PRODUCTION_APPLIED))

    assert [entry["version"] for entry in report["missing"]] == PRODUCTION_PENDING
    assert report["missing_count"] == len(PRODUCTION_PENDING)
    # Every pending migration is reported by FILE too, so the operator can
    # apply the tail without reconstructing filenames from versions.
    assert [entry["file"] for entry in report["missing"]] == [
        f"{version}_" + next(
            e["file"].split("_", 1)[1] for e in local if e["version"] == version
        )
        for version in PRODUCTION_PENDING
    ]


def test_production_history_is_not_fully_migrated_even_with_every_numeric_marker(local):
    """The exact false green this redesign removes.

    All fifteen 001–015 markers present, every one of their objects really
    there — and every timestamped migration since absent. The old marker-only
    comparison answered `fully-migrated` here.
    """
    numeric_markers = {version: True for version in MARKERS if len(version) == 3}
    report = classify(local, observation(applied=PRODUCTION_APPLIED, markers=numeric_markers))

    assert report["state"] == "partially-migrated"
    assert report["missing_count"] == len(PRODUCTION_PENDING)


def test_complete_local_history_is_fully_migrated(local):
    report = classify(local, observation(applied=[entry["version"] for entry in local]))

    assert report["state"] == "fully-migrated"
    assert report["blocked"] is False
    assert report["missing"] == []
    assert report["applied_count"] == report["local_total"] == len(local)


def test_legacy_baseline_is_recognised(local):
    report = classify(
        local,
        observation(history=False, table_count=len(LEGACY_BASELINE_TABLES)),
    )

    assert report["state"] == "legacy-baseline"
    assert report["blocked"] is False
    # Nothing is applied yet, so the whole ordered set is pending.
    assert report["missing_count"] == len(local)


def test_empty_history_over_the_baseline_is_still_the_baseline(local):
    report = classify(
        local,
        observation(applied=[], table_count=len(LEGACY_BASELINE_TABLES)),
    )
    assert report["state"] == "legacy-baseline"
    assert report["blocked"] is False


def test_empty_schema_is_recognised(local):
    report = classify(local, observation(history=False, table_count=0, baseline=[]))

    assert report["state"] == "empty-schema"
    assert report["blocked"] is False
    assert report["missing_count"] == len(local)


def test_arbitrary_partial_schema_is_not_inferred_to_be_the_legacy_baseline(local):
    """`legacy-baseline` is the four-table baseline, not 'some tables exist'."""
    report = classify(
        local,
        observation(history=False, table_count=12, baseline=list(LEGACY_BASELINE_TABLES)),
    )

    assert report["state"] == "drift"
    assert report["blocked"] is True


def test_gap_in_applied_history_fails_closed(local):
    """A later migration recorded while an earlier one is absent."""
    applied = [v for v in PRODUCTION_APPLIED if v != "20260810000300"] + ["20260828000100"]
    report = classify(local, observation(applied=applied))

    assert report["state"] == "drift"
    assert report["blocked"] is True
    assert "20260810000300" in report["summary"]
    assert "ordered prefix" in report["summary"]


def test_unexpected_remote_version_fails_closed(local):
    report = classify(local, observation(applied=PRODUCTION_APPLIED + ["20991231235959"]))

    assert report["state"] == "drift"
    assert report["blocked"] is True
    assert report["unexpected"] == ["20991231235959"]


def test_duplicate_remote_version_fails_closed(local):
    report = classify(local, observation(applied=PRODUCTION_APPLIED + ["20260823000100"]))

    assert report["state"] == "drift"
    assert report["blocked"] is True


def test_history_object_disagreement_fails_closed(local):
    """History says applied; the object it must have created is absent."""
    report = classify(
        local,
        observation(applied=PRODUCTION_APPLIED, markers={"20260823000100": False}),
    )

    assert report["state"] == "drift"
    assert report["blocked"] is True
    assert report["marker_disagreements"]
    assert "sources.evidence_key" in report["marker_disagreements"][0]


def test_complete_history_with_a_missing_object_is_not_fully_migrated(local):
    """A green history row never outranks a provably absent object."""
    report = classify(
        local,
        observation(
            applied=[entry["version"] for entry in local],
            markers={"20260916120000": False},
        ),
    )

    assert report["state"] == "drift"
    assert report["state"] != "fully-migrated"
    assert report["blocked"] is True


def test_uninspectable_history_beyond_the_baseline_fails_closed(local):
    """Past the supported baseline, 'cannot look' is never 'nothing to do'."""
    report = classify(local, observation(history=False, table_count=40))

    assert report["state"] == "drift"
    assert report["blocked"] is True
    assert "cannot be proven" in report["summary"]


def test_populated_schema_with_empty_history_fails_closed(local):
    report = classify(local, observation(applied=[], table_count=40))

    assert report["state"] == "drift"
    assert report["blocked"] is True
    assert "does not record" in report["summary"]


def test_markers_alone_never_establish_fully_migrated(local):
    """Every marker object present, empty history: still not 'applied'."""
    report = classify(
        local,
        observation(applied=[], table_count=40, markers={version: True for version in MARKERS}),
    )

    assert report["state"] != "fully-migrated"
    assert report["blocked"] is True


# ---------------------------------------------------------------------------
# the shell entrypoint, run for real against a mocked psql
# ---------------------------------------------------------------------------
# The mock is a tiny read-only catalog: it answers the SELECTs the script
# issues and records every statement, so "never mutates" and "never prints the
# connection string" are proved from what actually ran.
MOCK_PSQL = r'''#!/usr/bin/env python3
import json
import os
import re
import sys

argv = sys.argv[1:]
with open(os.environ["MOCK_PSQL_ARGV"], "a") as handle:
    handle.write(json.dumps(argv) + "\n")

query = ""
for index, arg in enumerate(argv):
    if arg == "-c" and index + 1 < len(argv):
        query = argv[index + 1]
with open(os.environ["MOCK_PSQL_LOG"], "a") as handle:
    handle.write(query.replace("\n", " ") + "\n")

state = json.load(open(os.environ["MOCK_PSQL_STATE"]))
normalized = " ".join(query.split())

# Injected failure: this is how a real psql reports a query it could not
# run (permission denied, a revoked grant, a dropped relation mid-read).
# The tool must never read a nonzero exit as "the database answered
# nothing".
for needle in state.get("fail_on", []):
    if needle in normalized:
        print("ERROR:  permission denied for relation", file=sys.stderr)
        raise SystemExit(1)


def emit(*rows):
    for row in rows:
        print(row)
    raise SystemExit(0)


if normalized == "select 1":
    emit("1")

if "table_schema='supabase_migrations'" in normalized:
    emit(*(["1"] if state["history_exists"] else []))

if "from supabase_migrations.schema_migrations" in normalized:
    if not state["history_exists"]:
        print("ERROR:  relation does not exist", file=sys.stderr)
        raise SystemExit(1)
    emit(*sorted(state["applied"]))

if normalized.startswith("select count(*) from information_schema.tables"):
    emit(str(len(state["tables"])))

if "like 'milo_%'" in normalized:
    emit("")

match = re.search(r"information_schema\.tables .* table_name='([^']+)'", normalized)
if match:
    emit(*(["1"] if match.group(1) in state["tables"] else []))

match = re.search(r"information_schema\.views .* table_name='([^']+)'", normalized)
if match:
    emit(*(["1"] if match.group(1) in state["views"] else []))

match = re.search(r"information_schema\.columns .* table_name='([^']+)' and column_name='([^']+)'", normalized)
if match:
    emit(*(["1"] if f"{match.group(1)}.{match.group(2)}" in state["columns"] else []))

match = re.search(r"pg_proc .* p\.proname='([^']+)'", normalized)
if match:
    emit(*(["1"] if match.group(1) in state["functions"] else []))

print("ERROR:  unmocked query", file=sys.stderr)
raise SystemExit(1)
'''

SECRET_PASSWORD = "sup3r-s3cret-pg-password"
DB_URL = f"postgresql://readonly_user:{SECRET_PASSWORD}@db.example-project.supabase.co:5432/postgres"


def catalog_for(applied: list[str], *, absent_markers: tuple[str, ...] = ()) -> dict:
    """A fake catalog consistent with `applied`, built from the marker map."""
    catalog = {"tables": set(LEGACY_BASELINE_TABLES), "views": set(), "columns": set(), "functions": set()}
    for version in applied:
        if version in absent_markers or version not in MARKERS:
            continue
        kind, obj = MARKERS[version]
        catalog[{"table": "tables", "view": "views", "column": "columns", "function": "functions"}[kind]].add(obj)
    # A real database has far more relations than markers; the count only has
    # to be "clearly beyond the four-table baseline".
    catalog["tables"] |= {f"supporting_relation_{index}" for index in range(12)}
    return {key: sorted(value) for key, value in catalog.items()}


class RemoteRun:
    """One invocation of check-migration-state.sh against the mocked psql."""

    def __init__(self, tmp_path: Path, state: dict) -> None:
        self.dir = tmp_path
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bin = tmp_path / "bin"
        self.bin.mkdir(exist_ok=True)
        psql = self.bin / "psql"
        psql.write_text(MOCK_PSQL)
        psql.chmod(psql.stat().st_mode | stat.S_IEXEC)

        self.log = tmp_path / "statements.log"
        self.argv_log = tmp_path / "argv.log"
        self.state_file = tmp_path / "state.json"
        self.state_file.write_text(json.dumps(state))
        self.json_report = tmp_path / "report.json"

    def run(self) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "MOCK_PSQL_LOG": str(self.log),
                "MOCK_PSQL_ARGV": str(self.argv_log),
                "MOCK_PSQL_STATE": str(self.state_file),
                "MILO_READONLY_DB_URL": DB_URL,
            }
        )
        return subprocess.run(
            [
                "bash", str(SCRIPT),
                "--database-url-env", "MILO_READONLY_DB_URL",
                "--json-output", str(self.json_report),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

    def statements(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.strip()]


def remote_run(tmp_path: Path, applied: list[str], **kwargs) -> tuple[subprocess.CompletedProcess, RemoteRun]:
    state = {"history_exists": True, "applied": list(applied), **catalog_for(applied, **kwargs)}
    run = RemoteRun(tmp_path, state)
    return run.run(), run


def test_shell_reports_production_history_as_partially_migrated(tmp_path):
    result, _ = remote_run(tmp_path, PRODUCTION_APPLIED)

    assert "classified as partially-migrated" in result.stdout
    assert "fully-migrated" not in result.stdout.split("Reminder:")[0].replace("partially-migrated", "")
    assert result.returncode == 0


def test_shell_names_every_pending_migration(tmp_path):
    result, _ = remote_run(tmp_path, PRODUCTION_APPLIED)

    missing_line = next(line for line in result.stdout.splitlines() if "remote:missing" in line)
    for version in PRODUCTION_PENDING:
        assert version in missing_line, f"{version} not reported as pending"
    assert f"{len(PRODUCTION_PENDING)} local migration(s) not present" in missing_line


def test_shell_reports_complete_history_as_fully_migrated(tmp_path, local):
    result, _ = remote_run(tmp_path, [entry["version"] for entry in local])

    assert "classified as fully-migrated" in result.stdout
    assert result.returncode == 0


def test_shell_fails_closed_on_a_gap_in_history(tmp_path):
    applied = [v for v in PRODUCTION_APPLIED if v != "20260810000300"] + ["20260828000100"]
    result, _ = remote_run(tmp_path, applied)

    assert "[BLOCKED] remote:state" in result.stdout
    assert result.returncode != 0


def test_shell_fails_closed_on_an_unexpected_remote_version(tmp_path):
    result, _ = remote_run(tmp_path, PRODUCTION_APPLIED + ["20991231235959"])

    assert "[BLOCKED]" in result.stdout
    assert result.returncode != 0


def test_shell_fails_closed_when_history_and_objects_disagree(tmp_path):
    result, _ = remote_run(tmp_path, PRODUCTION_APPLIED, absent_markers=("20260823000100",))

    assert "[BLOCKED]" in result.stdout
    assert "sources.evidence_key" in result.stdout
    assert result.returncode != 0


def test_shell_recognises_the_legacy_baseline(tmp_path):
    state = {
        "history_exists": False,
        "applied": [],
        "tables": sorted(LEGACY_BASELINE_TABLES),
        "views": [],
        "columns": [],
        "functions": [],
    }
    result = RemoteRun(tmp_path, state).run()

    assert "classified as legacy-baseline" in result.stdout
    assert result.returncode == 0


def test_shell_recognises_an_empty_schema(tmp_path):
    state = {"history_exists": False, "applied": [], "tables": [], "views": [], "columns": [], "functions": []}
    result = RemoteRun(tmp_path, state).run()

    assert "classified as empty-schema" in result.stdout
    assert result.returncode == 0


def test_shell_fails_closed_when_history_is_uninspectable_beyond_the_baseline(tmp_path):
    state = {
        "history_exists": False,
        "applied": [],
        **catalog_for(PRODUCTION_APPLIED),
    }
    result = RemoteRun(tmp_path, state).run()

    assert "[BLOCKED] remote:state" in result.stdout
    assert result.returncode != 0


MUTATION_VERBS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "comment", "copy", "vacuum", "refresh", "call", "do",
)


def sql_skeleton(statement: str) -> str:
    """The statement with every quoted literal removed.

    Marker probes legitimately carry object names like
    `p.proname='create_project_from_proposal_with_owner'` — a mutation verb
    inside a string literal is data, not a command. Stripping literals is what
    makes the scan below check the SQL rather than the object names.
    """
    return re.sub(r"'[^']*'", "''", statement).lower()


def test_remote_inspection_issues_read_only_statements_only(tmp_path):
    """Every statement the tool runs against the remote database is a SELECT."""
    _, run = remote_run(tmp_path, PRODUCTION_APPLIED)

    statements = run.statements()
    assert statements, "the tool issued no statements at all"
    for statement in statements:
        skeleton = sql_skeleton(statement)
        assert skeleton.lstrip().startswith("select"), statement
        # No statement chaining: a trailing `; update …` would still start
        # with `select`.
        assert ";" not in skeleton.rstrip().rstrip(";"), statement
        for verb in MUTATION_VERBS:
            assert not re.search(rf"\b{verb}\b", skeleton), (
                f"mutation verb {verb!r} reached the database in: {statement}"
            )


def test_the_read_only_statement_scan_would_catch_a_real_mutation():
    """The scan above is only meaningful if it rejects an actual mutation."""
    skeleton = sql_skeleton("select 1; drop table public.runs")
    assert any(re.search(rf"\b{verb}\b", skeleton) for verb in MUTATION_VERBS)
    assert ";" in skeleton
    # …while a marker probe naming a function is still accepted.
    probe = sql_skeleton(
        "select 1 from pg_proc p where p.proname='create_project_from_proposal_with_owner'"
    )
    assert not any(re.search(rf"\b{verb}\b", probe) for verb in MUTATION_VERBS)


def test_remote_inspection_reads_the_migration_history_table(tmp_path):
    """The applied side must come from history, not from object markers."""
    _, run = remote_run(tmp_path, PRODUCTION_APPLIED)

    assert any("supabase_migrations.schema_migrations" in s for s in run.statements())


@pytest.mark.parametrize(
    "applied,absent",
    [
        (PRODUCTION_APPLIED, ()),
        (PRODUCTION_APPLIED + ["20991231235959"], ()),
        (PRODUCTION_APPLIED, ("20260823000100",)),
    ],
)
def test_the_connection_string_never_appears_in_output_or_reports(tmp_path, applied, absent):
    result, run = remote_run(tmp_path, applied, absent_markers=absent)

    report = run.json_report.read_text()
    for stream in (result.stdout, result.stderr, report):
        assert SECRET_PASSWORD not in stream
        assert DB_URL not in stream
        assert "readonly_user" not in stream
    # The JSON report is real JSON and carries the classification.
    assert json.loads(report)["script"] == "check-migration-state"


def test_the_tool_states_that_it_never_applies_a_migration(tmp_path):
    result, _ = remote_run(tmp_path, PRODUCTION_APPLIED)

    assert "NEVER applies migrations" in result.stdout


# ---------------------------------------------------------------------------
# the documented strict apply order must be the real one
# ---------------------------------------------------------------------------
# BLOCKER 2: docs/production-readiness/MIGRATIONS.md says migrations are
# applied "strictly in this sequence" and then omitted five real migrations —
# 20260823000100, 20260828000100, 20260828000200, 20260902000100 and
# 20260907000100. An operator following that table exactly would have skipped
# them. The table is not allowed to depend on a human remembering to update
# it, so it is checked against the directory here.
MIGRATIONS_DOC = REPO / "docs" / "production-readiness" / "MIGRATIONS.md"
ORDER_HEADING = "## Order (apply strictly in this sequence)"


def documented_order() -> list[str]:
    """The filenames in the strict-order table, in the order they appear."""
    text = MIGRATIONS_DOC.read_text()
    assert ORDER_HEADING in text, f"missing authoritative section: {ORDER_HEADING}"
    section = text.split(ORDER_HEADING, 1)[1].split("\n## ", 1)[0]

    files: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[1] == "File":
            continue
        assert cells[1].startswith("`") and cells[1].endswith("`"), (
            f"strict-order row does not name a file in backticks: {line}"
        )
        files.append(cells[1].strip("`"))
    assert files, "the strict-order table has no rows"
    return files


def test_every_migration_file_appears_in_the_strict_order_table(local):
    documented = documented_order()
    on_disk = {entry["file"] for entry in local}

    missing = sorted(on_disk - set(documented))
    assert not missing, (
        "migrations missing from the strict apply order in "
        f"docs/production-readiness/MIGRATIONS.md: {missing}"
    )


def test_the_strict_order_table_names_only_real_migration_files(local):
    """A row naming a file that does not exist (an abbreviation, a typo,
    a deleted migration) is as broken as an omitted one."""
    on_disk = {entry["file"] for entry in local}

    unknown = sorted(name for name in documented_order() if name not in on_disk)
    assert not unknown, f"strict-order rows naming no real migration file: {unknown}"


def test_each_migration_appears_exactly_once_in_the_strict_order_table():
    documented = documented_order()

    duplicated = sorted({name for name in documented if documented.count(name) > 1})
    assert not duplicated, f"migrations listed more than once: {duplicated}"


def test_the_documented_order_matches_the_canonical_apply_order(local):
    """Docs and tooling may not claim two different sequences."""
    assert documented_order() == [entry["file"] for entry in local]


def test_the_documented_order_matches_the_generated_migration_plan(local, tmp_path):
    """The plan the operator tooling writes is the same sequence as the docs."""
    plan_path = tmp_path / "migration-plan.json"
    result = subprocess.run(
        ["bash", str(SCRIPT), "--plan-output", str(plan_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    plan = json.loads(plan_path.read_text())["migrations"]
    assert [entry["order"] for entry in plan] == list(range(1, len(plan) + 1))
    assert [entry["file"] for entry in plan] == documented_order()
    assert [entry["version"] for entry in plan] == [entry["version"] for entry in local]


def test_the_five_previously_omitted_migrations_are_documented():
    """The exact regression: these five were absent from the strict order."""
    documented = documented_order()
    for name in (
        "20260823000100_lease_guarded_evidence_writes.sql",
        "20260828000100_canonical_scope_conflict_identity.sql",
        "20260828000200_source_evidence_fragments.sql",
        "20260902000100_r3_versioned_focused_evidence.sql",
        "20260907000100_r4_deterministic_verification.sql",
    ):
        assert name in documented, f"{name} is missing from the strict apply order"


def test_omitting_a_migration_from_the_table_is_actually_detected(local):
    """The invariant above is only worth having if it fails when violated."""
    documented = documented_order()
    weakened = [name for name in documented if name != documented[-1]]

    assert set(entry["file"] for entry in local) - set(weakened)


# ---------------------------------------------------------------------------
# a failed inspection is never an observation
# ---------------------------------------------------------------------------
# "Could not read the migration history" and "the migration history is empty"
# are different states, and the tool used to be unable to tell them apart:
# rows were read through a process substitution, whose exit status the parent
# shell never sees, and single-value probes ran inside `$( )` in a condition,
# where a failure looks exactly like an empty result.
#
# That mattered most for the case below: an empty applied history over the
# four baseline tables legitimately classifies a database as `legacy-baseline`
# — the state whose documented remedy is "apply the whole ordered set". A
# SELECT that merely FAILED must never be able to produce that answer.
HISTORY_READ = "from supabase_migrations.schema_migrations"
HISTORY_EXISTS_PROBE = "table_schema='supabase_migrations'"
CLASSIFICATIONS = ("legacy-baseline", "partially-migrated", "fully-migrated", "empty-schema")


def failing_run(tmp_path: Path, state: dict, fail_on: list[str]) -> tuple[subprocess.CompletedProcess, RemoteRun]:
    run = RemoteRun(tmp_path, {**state, "fail_on": list(fail_on)})
    return run.run(), run


def assert_refused_to_classify(result: subprocess.CompletedProcess) -> None:
    """Nonzero, BLOCKED, and no state claimed from an incomplete inspection."""
    assert result.returncode != 0, result.stdout
    assert "[BLOCKED]" in result.stdout
    assert "remote inspection did not complete" in result.stdout
    for state in CLASSIFICATIONS:
        assert f"classified as {state}" not in result.stdout, (
            f"classified as {state} from an inspection that did not complete"
        )
    # Never leak the connection string on a failure path.
    for stream in (result.stdout, result.stderr):
        assert SECRET_PASSWORD not in stream
        assert DB_URL not in stream


def production_like_state() -> dict:
    return {"history_exists": True, "applied": list(PRODUCTION_APPLIED), **catalog_for(PRODUCTION_APPLIED)}


def legacy_baseline_state() -> dict:
    """Exactly the supported four-table baseline — and nothing else."""
    return {
        "history_exists": True,
        "applied": [],
        "tables": sorted(LEGACY_BASELINE_TABLES),
        "views": [],
        "columns": [],
        "functions": [],
    }


def test_history_read_failure_over_a_production_like_schema_fails_closed(tmp_path):
    """The relation exists; reading its rows fails. Not an empty history."""
    result, _ = failing_run(tmp_path, production_like_state(), [HISTORY_READ])

    assert "[BLOCKED] remote:history-read" in result.stdout
    assert_refused_to_classify(result)


def test_history_read_failure_over_a_legacy_baseline_schema_fails_closed(tmp_path):
    """The dangerous one: an assumed-empty history would say `legacy-baseline`."""
    result, _ = failing_run(tmp_path, legacy_baseline_state(), [HISTORY_READ])

    assert "[BLOCKED] remote:history-read" in result.stdout
    assert "legacy-baseline" not in result.stdout
    assert_refused_to_classify(result)


def test_history_existence_probe_failure_fails_closed(tmp_path):
    """Failing to learn WHETHER history exists is not 'there is no history'."""
    result, _ = failing_run(tmp_path, production_like_state(), [HISTORY_EXISTS_PROBE])

    assert "[BLOCKED] remote:history-probe" in result.stdout
    assert_refused_to_classify(result)


def test_a_failed_marker_probe_fails_closed(tmp_path, local):
    """A probe that failed is not an observed absence.

    Recording it as absent would manufacture drift against a complete
    history; dropping it silently would let `fully-migrated` stand on an
    inspection that did not finish.
    """
    complete = [entry["version"] for entry in local]
    state = {"history_exists": True, "applied": complete, **catalog_for(complete)}
    result, _ = failing_run(tmp_path, state, ["p.proname='claim_run_lease'"])

    assert "[BLOCKED] remote:marker-probe" in result.stdout
    assert "fully-migrated" not in result.stdout
    assert_refused_to_classify(result)


def test_a_failed_baseline_table_probe_fails_closed(tmp_path):
    result, _ = failing_run(tmp_path, legacy_baseline_state(), ["table_name='run_events'"])

    assert "[BLOCKED] remote:baseline-probe" in result.stdout
    assert_refused_to_classify(result)


def test_a_failed_relation_count_fails_closed(tmp_path):
    result, _ = failing_run(tmp_path, production_like_state(), ["select count(*)"])

    assert "[BLOCKED] remote:schema-shape" in result.stdout
    assert_refused_to_classify(result)


def test_a_failed_unexpected_relation_probe_fails_closed(tmp_path):
    """Even the advisory probe: an unanswered question stays unanswered."""
    result, _ = failing_run(tmp_path, production_like_state(), ["like 'milo_%'"])

    assert "[BLOCKED] remote:unexpected-probe" in result.stdout
    assert_refused_to_classify(result)


def test_connectivity_failure_fails_closed(tmp_path):
    result, _ = failing_run(tmp_path, production_like_state(), ["select 1"])

    assert result.returncode != 0
    assert "[BLOCKED] remote:connection" in result.stdout
    for state in CLASSIFICATIONS:
        assert f"classified as {state}" not in result.stdout
    for stream in (result.stdout, result.stderr):
        assert SECRET_PASSWORD not in stream


def test_the_failure_injection_is_real(tmp_path):
    """Without an injected failure these same fixtures classify normally.

    Otherwise the assertions above could pass for the wrong reason.
    """
    production, _ = failing_run(tmp_path / "production", production_like_state(), [])
    assert production.returncode == 0
    assert "classified as partially-migrated" in production.stdout

    baseline, _ = failing_run(tmp_path / "baseline", legacy_baseline_state(), [])
    assert baseline.returncode == 0
    assert "classified as legacy-baseline" in baseline.stdout
