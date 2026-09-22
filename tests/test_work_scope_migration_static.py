"""The work-scope migration and its backend twin state ONE set of rules.

`20260922000100_catalog_work_scopes.sql` restates, in SQL, the contract
version, the pinned source, the record's closed key set and every hard bound
that `backend/catalog/scope/contract.py` defines -- because the database must
refuse a bad plan on paths that never come through the backend. Two copies of
a rule can drift; this module is what stops them. The executable half of the
proof is `tests/test_migrations_postgres.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from backend.catalog.scope import contract as wsc
from backend.catalog.scope import directory as mdir
from backend.catalog.scope import interpret as wi
from backend.repository.supabase import SupabaseRepository
from backend.testing.memory_repository import MemoryRepository

MIGRATION = Path("supabase/migrations/20260922000100_catalog_work_scopes.sql")
SQL = MIGRATION.read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    start = SQL.index(f"create or replace function public.{name}(")
    return SQL[start:SQL.index("$$;", start)]


def test_the_contract_version_is_one_string():
    assert f"select '{wsc.WORK_SCOPE_CONTRACT}'::text" in SQL


def test_the_pinned_source_is_the_contracts_source():
    body = _function_body("catalog_work_scope_record_valid")
    pairs = dict(re.findall(r"'(family|package_id|resource_id)', '([^']+)'", body))
    assert pairs == dict(wsc.WORK_SCOPE_SOURCE)


def test_the_closed_key_sets_are_the_contracts():
    body = _function_body("catalog_work_scope_record_valid")
    arrays = re.findall(r"array\[([^\]]+)\]", body)
    stated = [sorted(re.findall(r"'([a-z_]+)'", array)) for array in arrays]
    assert sorted(wsc.RECORD_KEYS) in stated
    assert sorted(wsc.MODEL_YEAR_KEYS) in stated


def test_every_hard_bound_is_the_contracts_bound():
    body = _function_body("catalog_work_scope_record_valid")
    assert f"::integer not between 1 and {wsc.MAX_BATCH_SIZE}" in body
    assert f"::integer not between 1 and {wsc.MAX_WORK_SCOPE_ITEMS}" in body
    assert f"jsonb_array_length(p_scope->'units') not between 1 and {wsc.MAX_STORED_UNITS}" in body
    assert f"not between {wsc.MIN_MODEL_YEAR} and {wsc.MAX_MODEL_YEAR}" in body
    assert f"char_length(scope_text) between 2 and {wsc.MAX_SCOPE_TEXT_CHARS}" in SQL
    assert f"char_length(instruction) between 1 and {wi.MAX_INSTRUCTION_CHARS}" in SQL
    # The unit-key SHAPE, exactly.
    assert f"'^{wsc._UNIT_KEY.pattern}$'" in body
    # The stored directory version bound admits the version in force.
    assert "char_length(p_scope->>'directory_version') not between 1 and 80" in body
    assert len(mdir.DIRECTORY_VERSION) <= 80
    # Every directory entry fits under the database's unit ceiling.
    assert wsc.MAX_UNITS <= wsc.MAX_STORED_UNITS


def test_the_digest_is_derived_exactly_as_the_backend_derives_it():
    assert "encode(sha256(convert_to(p_text, 'UTF8')), 'hex')" in SQL
    assert "check (digest = public.catalog_work_scope_text_digest(scope_text))" in SQL
    assert "check (scope = scope_text::jsonb)" in SQL


def test_every_function_the_migration_creates_is_in_its_privilege_block():
    created = set(re.findall(r"create or replace function public\.([a-z_]+)\(", SQL))
    block = SQL[SQL.index("6. RLS and privileges"):]
    granted = set(re.findall(r"'public\.([a-z_]+)\(", block))
    assert created == granted, created ^ granted


def test_the_migration_is_a_plan_and_nothing_that_executes():
    """Drafts only: no run, no snapshot, no status but `draft`."""
    lowered = SQL.lower()
    assert "public.runs" not in lowered
    assert "catalog_source_snapshots" not in lowered
    assert "check (status in ('draft'))" in lowered
    assert "drop table" not in lowered and "delete from" not in lowered


def test_every_refusal_the_sql_raises_by_name_is_mapped_by_both_repositories():
    raised = set(re.findall(r"raise exception '(WORK_SCOPE_[A-Z_]+)'", SQL))
    mapped = {code for code, _message, _status in SupabaseRepository._WORK_SCOPE_REFUSALS}
    mapped |= {"WORK_SCOPE_CONVERSATION_NOT_FOUND", "WORK_SCOPE_NOT_FOUND"}
    # Trigger-only refusals are integrity backstops no backend path can reach;
    # everything the two WRITERS raise is mapped.
    backstops = {"WORK_SCOPE_REVISION_IMMUTABLE", "WORK_SCOPE_IMMUTABLE",
                 "WORK_SCOPE_HEAD_INVALID", "WORK_SCOPE_REVISION_OUT_OF_SEQUENCE"}
    assert raised - backstops <= mapped, raised - backstops - mapped
    memory = Path("backend/testing/memory_repository.py").read_text(encoding="utf-8")
    for code in mapped - {"WORK_SCOPE_CONVERSATION_NOT_FOUND", "WORK_SCOPE_NOT_FOUND"}:
        assert code in memory, code


def test_the_memory_mirror_implements_every_supabase_work_scope_method():
    names = ("create_work_scope", "revise_work_scope", "get_work_scope", "open_work_scope",
             "list_work_scope_revisions", "catalog_canonical_manufacturer_coverage")
    for name in names:
        assert callable(getattr(SupabaseRepository, name)), name
        assert callable(getattr(MemoryRepository, name)), name
    assert MemoryRepository.MAX_WORK_SCOPE_REVISION_ROWS == \
        SupabaseRepository.MAX_WORK_SCOPE_REVISION_ROWS


def test_the_repository_selects_only_columns_the_migration_defines():
    for columns, table in ((SupabaseRepository.WORK_SCOPE_COLUMNS, "catalog_work_scopes"),
                           (SupabaseRepository.WORK_SCOPE_REVISION_COLUMNS,
                            "catalog_work_scope_revisions")):
        definition = SQL[SQL.index(f"create table if not exists public.{table} ("):]
        definition = definition[:definition.index("\n);")]
        for column in (name.strip() for name in columns.split(",")):
            assert re.search(rf"^\s+{column} ", definition, re.M), (table, column)


def test_a_valid_record_stays_valid_json_the_database_can_cast():
    text = wsc.scope_from_fields({"units": ["toyota"], "model_year_from": None,
                                  "model_year_to": None, "max_items": 1,
                                  "batch_size": 1}).canonical_text()
    assert json.loads(text)["units"] == ["toyota"]
