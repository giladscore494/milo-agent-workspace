#!/usr/bin/env python3
"""Pure migration-state comparison logic for check-migration-state.sh.

The shell entrypoint owns the (read-only, SELECT-only) remote inspection and
the operator interface; this module owns the decision. Keeping the comparison
here — with no I/O of its own beyond reading local migration filenames — is
what makes every classification directly unit-testable, including the states
that must fail closed.

The authoritative comparison is ALWAYS the complete local migration set
against the remote applied history. A subset of object markers can never
establish that a database is fully migrated: that is precisely the defect
this module exists to make impossible. Markers remain as SECONDARY evidence
and can only ever ADD a drift finding, never remove one.

Nothing here connects to a database, and no value it prints is ever derived
from a connection string or a credential.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# A migration filename is `<version>_<description>.sql` where <version> is
# either a 3-digit sequence number (the historical set) or a 14-digit
# timestamp (`YYYYMMDDHHMMSS`). Anything else is refused rather than guessed
# at: an unparseable version would silently drop a real migration out of the
# comparison, which is the whole class of bug this file prevents.
FILENAME_PATTERN = re.compile(r"^(?P<version>[0-9]{3}|[0-9]{14})_(?P<slug>[A-Za-z0-9][A-Za-z0-9_.-]*)\.sql$")

# The four pre-existing tables of the confirmed historical baseline. This is
# the ONLY partial schema that may be classified as a supported starting
# point; every other partial schema without migration history fails closed.
LEGACY_BASELINE_TABLES = ("conversations", "messages", "runs", "run_events")

# Secondary object evidence: `version -> (kind, object)`. A marker is declared
# only where the migration introduces a STABLE, UNIQUE object. Migrations that
# only grant/revoke privileges, enable RLS, widen a check constraint or
# redefine an existing function in place have no such object and deliberately
# get no marker — an invented one would produce false drift.
#
# Markers never establish "applied". They are checked in ONE direction: when
# the remote history claims a migration is applied and its marker object is
# absent, that disagreement is surfaced as a blocking drift instead of being
# hidden behind a green history row.
MARKERS: dict[str, tuple[str, str]] = {
    "001": ("table", "projects"),
    "002": ("table", "run_checkpoints"),
    "003": ("table", "workflow_proposals"),
    "004": ("table", "supervisor_decisions"),
    "005": ("table", "tool_access_requests"),
    "006": ("view", "stuck_runs"),
    "007": ("table", "project_members"),
    "008": ("column", "workflow_proposals.project_id"),
    "009": ("column", "runs.launch_state"),
    "010": ("column", "runs.usage"),
    "011": ("function", "create_project_from_proposal_with_owner"),
    "012": ("function", "claim_run_lease"),
    "013": ("table", "run_usage_ledger"),
    "014": ("function", "reserve_daily_user_budget"),
    "015": ("table", "model_call_budget_reservations"),
    "20260810000300": ("function", "assert_worker_lease"),
    "20260810000400": ("function", "create_message_and_run_v2"),
    "20260810000600": ("column", "model_call_budget_reservations.attempt"),
    "20260823000100": ("column", "sources.evidence_key"),
    "20260828000100": ("column", "claims.canonical_scope_hash"),
    "20260828000200": ("table", "source_evidence_fragments"),
    "20260902000100": ("column", "sources.source_version_kind"),
    "20260907000100": ("table", "claim_verdicts"),
    "20260914200000": ("table", "catalog_raw_records"),
    "20260915180000": ("column", "catalog_raw_records.source_locator"),
    "20260916090000": ("function", "catalog_snapshot_candidate_diff"),
    "20260916120000": ("table", "catalog_canonical_field_provenance"),
}

STATES = ("empty-schema", "legacy-baseline", "partially-migrated", "fully-migrated", "drift", "unrecognized")


class MigrationStateError(ValueError):
    """A local migration set or a remote observation that cannot be trusted."""


# ---------------------------------------------------------------------------
# local side
# ---------------------------------------------------------------------------
def parse_version(filename: str) -> str:
    """Return the validated migration version encoded in `filename`.

    `001_project_workspace.sql` -> `001`
    `20260916120000_catalog_field_level_promotion.sql` -> `20260916120000`
    """
    match = FILENAME_PATTERN.match(filename)
    if match is None:
        raise MigrationStateError(f"unsupported migration filename: {filename!r}")
    return match.group("version")


def sort_key(version: str) -> tuple[int, int]:
    """Canonical ordering: every 3-digit migration precedes every timestamp.

    Sorting the two families as one integer would interleave them; sorting as
    text happens to work today only because 14-digit strings sort after
    3-digit ones, which is an accident of the current numbering rather than a
    contract. The family is made explicit instead.
    """
    return (0 if len(version) == 3 else 1, int(version))


def local_migrations(migrations_dir: str | Path) -> list[dict[str, str]]:
    """Every local migration, in canonical apply order.

    Fails closed on a malformed filename or a duplicate version ANYWHERE in
    the directory — not merely within the 3-digit set.
    """
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise MigrationStateError(f"migrations directory not found: {directory}")

    entries: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        version = parse_version(path.name)
        if version in seen:
            raise MigrationStateError(
                f"duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        entries.append({"version": version, "file": path.name})

    if not entries:
        raise MigrationStateError(f"no migration files found in {directory}")

    entries.sort(key=lambda entry: sort_key(entry["version"]))
    return entries


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def _marker_disagreements(applied: list[str], markers_present: dict[str, bool]) -> list[str]:
    """Applied migrations whose declared marker object is provably absent."""
    findings = []
    for version in applied:
        if version not in MARKERS:
            continue
        # Absent from the observation means "not probed", not "not there".
        if version in markers_present and markers_present[version] is False:
            kind, obj = MARKERS[version]
            findings.append(f"{version} is recorded as applied but its {kind} '{obj}' is absent")
    return findings


def _finalize(report: dict) -> dict:
    """Derive the counts and the one-line summary the shell entrypoint prints.

    Every classification returns through here, so a new branch cannot forget
    to populate them and leave the operator report silently inconsistent.
    """
    report["applied_count"] = len(report["applied"])
    report["missing_count"] = len(report["missing"])
    report["summary"] = "; ".join(report["reasons"])
    return report


def classify(local: list[dict[str, str]], observation: dict) -> dict:
    """Classify a remote database against the complete local migration set.

    `observation` is what the read-only SQL inspection saw:
        history_available  bool  — supabase_migrations.schema_migrations exists
        applied            list  — migration versions in that table, as stored
        public_table_count int   — relations in the public schema
        legacy_baseline    list  — which LEGACY_BASELINE_TABLES are present
        markers            dict  — version -> bool for each probed marker

    Returns a report whose `state` is one of STATES and whose `blocked` flag
    is true for every condition that cannot be proven safe.
    """
    local_versions = [entry["version"] for entry in local]
    local_index = {entry["version"]: entry["file"] for entry in local}

    history_available = bool(observation.get("history_available"))
    applied_raw = list(observation.get("applied") or [])
    table_count = int(observation.get("public_table_count") or 0)
    baseline_tables = set(observation.get("legacy_baseline") or [])
    markers_present = dict(observation.get("markers") or {})

    reasons: list[str] = []
    report = {
        "state": "unrecognized",
        "blocked": True,
        "reasons": reasons,
        "summary": "",
        "local_total": len(local_versions),
        "applied": [],
        "applied_count": 0,
        "missing": [],
        "missing_count": 0,
        "unexpected": [],
        "marker_disagreements": [],
    }

    baseline_complete = baseline_tables >= set(LEGACY_BASELINE_TABLES)

    # -- no migration history table -----------------------------------------
    if not history_available:
        if table_count == 0:
            report.update(state="empty-schema", blocked=False)
            report["missing"] = [dict(entry) for entry in local]
            reasons.append("no public schema objects and no applied migration history")
            return _finalize(report)
        if baseline_complete and table_count == len(LEGACY_BASELINE_TABLES):
            report.update(state="legacy-baseline", blocked=False)
            report["missing"] = [dict(entry) for entry in local]
            reasons.append("exactly the supported historical four-table baseline, with no migration history")
            return _finalize(report)
        report.update(state="drift")
        reasons.append(
            "migration history is not inspectable while the public schema is already beyond the "
            f"supported legacy baseline ({table_count} public relations); a safe ordered state cannot be proven"
        )
        return _finalize(report)

    # -- migration history present ------------------------------------------
    duplicated = sorted({version for version in applied_raw if applied_raw.count(version) > 1})
    if duplicated:
        report.update(state="drift")
        reasons.append(f"remote migration history records duplicate versions: {', '.join(duplicated)}")
        return _finalize(report)

    unexpected = [version for version in applied_raw if version not in local_index]
    if unexpected:
        report.update(state="drift")
        report["unexpected"] = unexpected
        reasons.append(
            "remote migration history contains versions with no local migration file: "
            + ", ".join(unexpected)
        )
        return _finalize(report)

    applied = sorted(applied_raw, key=sort_key)
    report["applied"] = applied

    # The applied set must be an exact ordered PREFIX of the local sequence.
    # Anything else — a gap, a later migration recorded before an earlier one —
    # means the remote history cannot be replayed by applying a tail, so it is
    # never silently reported as progress.
    prefix = local_versions[: len(applied)]
    if applied != prefix:
        missing_inside = [version for version in prefix if version not in set(applied)]
        report.update(state="drift")
        reasons.append(
            "applied migration history is not an ordered prefix of the local migration sequence; "
            "later migrations are recorded while earlier ones are absent: "
            + (", ".join(missing_inside) if missing_inside else "ordering mismatch")
        )
        return _finalize(report)

    disagreements = _marker_disagreements(applied, markers_present)
    if disagreements:
        report.update(state="drift")
        report["marker_disagreements"] = disagreements
        reasons.append(
            "migration history disagrees with the schema objects it should have created: "
            + "; ".join(disagreements)
        )
        return _finalize(report)

    missing = [dict(entry) for entry in local if entry["version"] not in set(applied)]
    report["missing"] = missing

    if not missing:
        report.update(state="fully-migrated", blocked=False)
        reasons.append(f"all {len(local_versions)} local migrations are recorded in remote migration history")
        return _finalize(report)

    if not applied:
        # History exists but records nothing: only an empty schema or the
        # supported baseline may start here.
        if table_count == 0:
            report.update(state="empty-schema", blocked=False)
            reasons.append("empty migration history and no public schema objects")
            return _finalize(report)
        if baseline_complete and table_count == len(LEGACY_BASELINE_TABLES):
            report.update(state="legacy-baseline", blocked=False)
            reasons.append("empty migration history over exactly the supported four-table baseline")
            return _finalize(report)
        report.update(state="drift")
        reasons.append(
            f"migration history is empty while the public schema already holds {table_count} relations; "
            "schema objects indicate migration activity that history does not record"
        )
        return _finalize(report)

    report.update(state="partially-migrated", blocked=False)
    reasons.append(
        f"{len(applied)} of {len(local_versions)} local migrations applied; "
        f"{len(missing)} pending"
    )
    return _finalize(report)


# ---------------------------------------------------------------------------
# observation input
# ---------------------------------------------------------------------------
# The shell entrypoint speaks a line-oriented TAB format rather than JSON, so
# no tool has to assemble JSON in bash (a quoting bug there would corrupt the
# comparison silently). Every key is explicit and anything unrecognised is
# refused rather than ignored.
#
#   history_available\t0|1
#   public_table_count\t<integer>
#   legacy_baseline\t<table>
#   applied\t<version>
#   marker\t<version>\t0|1
OBSERVATION_KEYS = ("history_available", "public_table_count", "legacy_baseline", "applied", "marker")


def parse_observation_text(text: str) -> dict:
    """Parse the TAB-delimited remote observation the shell entrypoint emits."""
    observation: dict = {
        "history_available": False,
        "public_table_count": 0,
        "legacy_baseline": [],
        "applied": [],
        "markers": {},
    }
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        parts = line.split("\t")
        key = parts[0]
        if key not in OBSERVATION_KEYS:
            raise MigrationStateError(f"unknown observation key on line {lineno}: {key!r}")
        if key == "history_available":
            observation["history_available"] = parts[1].strip() == "1"
        elif key == "public_table_count":
            try:
                observation["public_table_count"] = int(parts[1].strip())
            except (IndexError, ValueError) as exc:
                raise MigrationStateError(f"public_table_count is not an integer on line {lineno}") from exc
        elif key == "legacy_baseline":
            observation["legacy_baseline"].append(parts[1].strip())
        elif key == "applied":
            value = parts[1].strip()
            if value:
                observation["applied"].append(value)
        elif key == "marker":
            if len(parts) < 3:
                raise MigrationStateError(f"marker needs a version and a 0/1 result on line {lineno}")
            observation["markers"][parts[1].strip()] = parts[2].strip() == "1"
    return observation


# ---------------------------------------------------------------------------
# CLI (used by scripts/release/check-migration-state.sh)
# ---------------------------------------------------------------------------
def _marker_probe_plan(local: list[dict[str, str]]) -> list[dict[str, str]]:
    """Only markers for migrations that actually exist locally are probed."""
    plan = []
    for entry in local:
        marker = MARKERS.get(entry["version"])
        if marker is None:
            continue
        kind, obj = marker
        plan.append({"version": entry["version"], "kind": kind, "object": obj})
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migration-state comparison helper (read-only, offline).")
    parser.add_argument("command", choices=("local", "markers", "classify"))
    parser.add_argument("--migrations-dir", required=True)
    parser.add_argument(
        "--observation",
        help="path to the TAB-delimited remote observation ('-' for stdin); required by 'classify'",
    )
    args = parser.parse_args(argv)

    try:
        local = local_migrations(args.migrations_dir)
    except MigrationStateError as exc:
        print(f"migration-state error: {exc}", file=sys.stderr)
        return 2

    if args.command == "local":
        for entry in local:
            print(f"{entry['version']}\t{entry['file']}")
        return 0

    if args.command == "markers":
        for marker in _marker_probe_plan(local):
            print(f"{marker['version']}\t{marker['kind']}\t{marker['object']}")
        return 0

    if not args.observation:
        print("migration-state error: classify requires --observation", file=sys.stderr)
        return 2
    raw = sys.stdin.read() if args.observation == "-" else Path(args.observation).read_text()
    try:
        observation = parse_observation_text(raw)
    except MigrationStateError as exc:
        print(f"migration-state error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(classify(local, observation), separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
