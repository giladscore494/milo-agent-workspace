"""PR-OPS1 (P50): the maintenance RPCs' own timeouts, pinned statically.

Migration 20261004000100 sets each whole-snapshot RPC's statement_timeout
(PostgREST 14.5 hoists it before the call) and, for the two that take table
locks, a 5 s lock_timeout. `create or replace function` RESETS a function's
settings, so a later restatement that forgets them would quietly bring back
the 8 s failure: the newest definition of each function must be followed by
exactly these settings. tests/test_register_rpc_timeouts_postgres.py checks
the applied result and the PostgREST call path.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))
TIMEOUTS = REPO / "supabase" / "migrations" / "20261004000100_catalog_maintenance_rpc_timeouts.sql"

#: name -> (the argument types as the migration names them, {setting: value}).
PINNED = {
    "compact_register_snapshot": ("text, boolean, text, uuid",
                                  {"statement_timeout": "300s", "lock_timeout": "5s"}),
    "prune_register_snapshots": ("text[], text", {"statement_timeout": "300s", "lock_timeout": "5s"}),
    "record_register_snapshot_archive_from_database": ("uuid, text, bigint, text, integer",
                                                       {"statement_timeout": "300s"}),
    "catalog_register_snapshot_archivable": ("uuid", {"statement_timeout": "300s"}),
    "prepare_work_scope_queue": ("uuid, text, integer, text, jsonb", {"statement_timeout": "300s"}),
    "record_catalog_variants": ("uuid, text, jsonb", {"statement_timeout": "300s"}),
}


def _sql(path: Path) -> str:
    """The migration without its comments."""
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


def _alters(text: str, name: str, args: str) -> dict[str, str]:
    return dict(re.findall(rf"alter function public\.{name}\({re.escape(args)}\)\s+set (\w+) = '([^']+)';", text))


def test_the_migration_sets_exactly_the_pinned_settings():
    text = _sql(TIMEOUTS)
    for name, (args, settings) in PINNED.items():
        assert _alters(text, name, args) == settings, name
    # Nothing else is altered: no other function, no role, no database setting.
    assert len(re.findall(r"^alter function ", text, re.M)) == sum(len(s) for _a, s in PINNED.values())
    assert not re.search(r"\balter (role|database|system)\b", text, re.I)
    assert "catalog_work_scope_coverage_decisions" not in re.sub(r"do \$\$.*", "", text, flags=re.S), \
        "the coverage decisions are called only inside Prepare and must stay inlinable"


def test_no_later_restatement_drops_a_pinned_setting():
    """The newest `create or replace` of each pinned function comes BEFORE the
    migration that sets its timeouts, or states them itself."""
    for name, (_args, settings) in PINNED.items():
        newest = None
        for path in MIGRATIONS:
            for match in re.finditer(rf"create or replace function public\.{name}\(", _sql(path), re.I):
                newest = (path, match.start())
        assert newest is not None, name
        path, start = newest
        if path.name < TIMEOUTS.name:
            continue
        header = _sql(path)[start:].split("$$", 1)[0]
        for setting, value in settings.items():
            assert re.search(rf"set {setting} (=|to) '{re.escape(value)}'", header), (name, path.name, setting)


def test_the_setting_is_never_on_a_helper_postgrest_does_not_call():
    """A statement_timeout only reaches the statement through PostgREST's hoist
    of the CALLED function: a SET on an inner helper does nothing but block
    inlining. The coverage decisions carry none in any migration."""
    for path in MIGRATIONS:
        text = _sql(path)
        for match in re.finditer(r"create or replace function public\.catalog_work_scope_coverage_decisions\(",
                                 text):
            header = text[match.start():].split("$$", 1)[0]
            assert not re.search(r"\bset (statement_timeout|lock_timeout)\b", header), path.name
        assert not re.search(r"alter function public\.catalog_work_scope_coverage_decisions", text), path.name
