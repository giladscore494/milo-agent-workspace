"""Catalog PR1: the SQL and the Python contract state one rule, not two.

Every value in `backend/catalog/contracts.py` is also written into
`20260914200000_catalog_evidence_foundation.sql` -- a closed vocabulary, a
bound, a pinned trust state. Two copies of a rule drift; these tests make a
drift a test failure instead of a silent disagreement between the backend and
the database.

Offline and static: this module reads two files and asserts about their text.
It opens no connection and applies no migration -- `tests/test_migrations_postgres.py`
does that executably against real PostgreSQL.
"""

from pathlib import Path

from backend.catalog.contracts import (CANDIDATE_IDENTITY_DIMENSIONS, CANDIDATE_STATUSES,
                                       CATALOG_SOURCE_FAMILIES, CATALOG_TRUST_STATES,
                                       CONTENT_SHA256_PATTERN, IDEMPOTENCY_KEY_PATTERN,
                                       MAX_RAW_PAYLOAD_CHARS, MAX_RETRIEVAL_METADATA_CHARS,
                                       SNAPSHOT_VALIDATION_STATES, TRUST_STATE_BY_FAMILY,
                                       is_evidence_family, stated_identity_dimensions,
                                       trust_state_for)

import pytest

MIGRATION = Path("supabase/migrations/20260914200000_catalog_evidence_foundation.sql")

#: Every relation the catalog namespace adds, and nothing else.
STAGING_TABLES = ("catalog_source_snapshots", "catalog_raw_records",
                  "catalog_candidate_variants", "catalog_candidate_evidence_links")
CANONICAL_TABLES = ("catalog_models", "catalog_model_variants")
GUARDED_RPCS = ("record_catalog_snapshot_guarded", "record_catalog_raw_record_guarded",
                "activate_catalog_snapshot_guarded", "record_catalog_candidate_guarded",
                "link_catalog_candidate_evidence_guarded")


def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def statements() -> str:
    """The migration's TOP-LEVEL SQL: comments and function bodies removed.

    A guarded RPC legitimately inserts into the staging tables, and the file's
    prose explains why. Neither is a seed, so both are stripped before asking
    whether this migration writes any row of its own.
    """
    body = "".join(line.split("--", 1)[0] + "\n" for line in sql().splitlines())
    parts = body.split("$$")
    # Keep the even segments: everything outside a $$-quoted function body.
    return "".join(parts[index] for index in range(0, len(parts), 2)).lower()


def test_the_closed_vocabularies_are_written_into_the_schema_verbatim():
    """A value the backend accepts and the database does not is a 500 in
    production and a green test suite everywhere else. Both copies here."""
    text = sql()
    assert ", ".join(f"'{family}'" for family in CATALOG_SOURCE_FAMILIES) in text
    assert ", ".join(f"'{state}'" for state in CATALOG_TRUST_STATES) in text
    assert ", ".join(f"'{state}'" for state in SNAPSHOT_VALIDATION_STATES) in text
    assert ", ".join(f"'{status}'" for status in CANDIDATE_STATUSES) in text
    for dimension in CANDIDATE_IDENTITY_DIMENSIONS:
        assert f"'{dimension}'" in text, dimension
    # And no dimension the Python vocabulary does not name is allowlisted in
    # SQL, which is the direction a copy usually drifts.
    allowlist = text.split("where d.key not in (", 1)[1].split(")", 1)[0]
    named = {chunk.strip().strip("'") for chunk in allowlist.split(",") if chunk.strip()}
    assert named == set(CANDIDATE_IDENTITY_DIMENSIONS)


def test_the_durable_bounds_are_the_python_bounds():
    text = sql()
    assert f"between 2 and {MAX_RAW_PAYLOAD_CHARS}" in text
    assert f"> {MAX_RAW_PAYLOAD_CHARS}" in text
    assert f"<= {MAX_RETRIEVAL_METADATA_CHARS}" in text
    assert text.count(CONTENT_SHA256_PATTERN) >= 2
    assert IDEMPOTENCY_KEY_PATTERN in text


def test_the_legacy_catalog_is_pinned_to_unverified_in_the_database():
    """The product decision, as a CHECK constraint.

    `legacy_reference` is the existing aggregated Yeda catalog. It is
    incomplete and holds incorrect values, so it is not canonical and not
    evidence -- and that is not a convention a later release can relax by
    writing a different value, because the column is pinned to the family.
    """
    text = sql()
    assert TRUST_STATE_BY_FAMILY["legacy_reference"] == "unverified"
    assert "catalog_source_snapshots_trust_pinned_to_family" in text
    assert "when 'legacy_reference' then 'unverified' else 'evidence' end" in text
    assert not is_evidence_family("legacy_reference")
    assert all(is_evidence_family(family) for family in ("government", "manufacturer"))
    # And a verdict can never be attached to such a snapshot.
    assert "an unverified catalog source cannot carry a verdict" in text


def test_no_row_of_the_legacy_catalog_is_copied_into_the_migration():
    """Schema only. The 7.3 MB catalog is not imported, quoted or seeded.

    A migration that carried catalog content would make the old data
    canonical by the back door, and would do it in the one file nobody diffs
    line by line.
    """
    top_level = statements()
    for writing in ("insert into", "copy public.", "\\copy"):
        assert writing not in top_level, f"the catalog migration must seed no row at all: {writing}"
    # No SQL statement names the legacy catalog's repository, file or record
    # shape. (The file's prose DOES name it, to say why it is not imported --
    # that is the explanation, not the import.)
    for marker in ("reliabilityaimodelsr2", "model_technical_catalog_il",
                   "technical_variants_il"):
        assert marker not in top_level, marker
    # And the file is schema, so it is small. A migration carrying catalog
    # content could not be: the aggregated catalog alone is 7.3 MB.
    assert MIGRATION.stat().st_size < 80_000


def test_the_canonical_tables_are_created_empty_and_unwritable_in_pr1():
    """`grant select` and nothing else: PR1 cannot create a canonical row.

    Promotion is PR3's work and will arrive with the privilege it needs. Until
    then the emptiness of the canonical catalog is enforced by the database
    rather than asserted about the code.
    """
    text = sql()
    for table in CANONICAL_TABLES:
        assert f"create table if not exists public.{table}" in text
        assert f"'public.{table}'" in text
    assert "grant select on table %s to service_role" in text
    assert "revoke insert, update, delete on table %s from service_role" in text
    # Traceability is structural: a canonical variant cannot exist without the
    # candidate it came from and the verdict that verified it.
    assert "promoted_from_candidate_id uuid not null" in text
    assert "promoted_from_verdict_id uuid not null" in text
    assert "references public.claim_verdicts(id) on delete restrict" in text


def test_every_durable_write_is_lease_guarded_and_service_only():
    text = sql()
    assert text.count("perform public.assert_worker_lease") == len(GUARDED_RPCS)
    assert text.count("set search_path = pg_catalog") == len(GUARDED_RPCS) + 1  # + the predicate
    for rpc in GUARDED_RPCS:
        assert f"create or replace function public.{rpc}" in text
        assert f"'public.{rpc}(uuid,text,integer,text,jsonb)'" in text
    assert "revoke execute on function %s from public" in text
    assert "revoke execute on function %s from anon" in text
    assert "revoke execute on function %s from authenticated" in text
    assert "grant execute on function %s to service_role" in text


def test_every_new_table_enables_rls_and_carries_no_policy():
    text = sql()
    for table in STAGING_TABLES + CANONICAL_TABLES:
        assert f"alter table public.{table} enable row level security" in text
    assert "create policy" not in text.lower(), "catalog tables are service-path only"


def test_raw_source_material_is_append_only():
    text = sql()
    assert "catalog source material is append-only" in text
    for trigger in ("catalog_raw_records_append_only",
                    "catalog_candidate_evidence_links_append_only",
                    "catalog_source_snapshots_append_only"):
        assert f"create trigger {trigger}" in text
    assert "revoke delete on table %s from service_role" in text
    assert "revoke update on table public.catalog_raw_records from service_role" in text
    assert ("revoke update on table public.catalog_candidate_evidence_links "
            "from service_role") in text


def test_every_replay_identity_fails_closed_on_different_content():
    """Idempotent replay, never a silent overwrite."""
    text = sql()
    for conflict in ("catalog snapshot idempotency conflict",
                     "catalog raw record idempotency conflict",
                     "catalog candidate idempotency conflict",
                     "catalog evidence link idempotency conflict"):
        assert conflict in text, conflict
    assert text.count("create unique index if not exists catalog_") >= 6


def test_the_activation_gate_is_the_r5_pagination_lesson():
    """A capture holding fewer records than declared is not complete.

    Exactly the defect the R5 Government pagination follow-up corrected, made
    structurally impossible here instead of disclosed in prose.
    """
    text = sql()
    assert "catalog_source_snapshots_active_only_when_complete" in text
    assert "stored_record_count = declared_record_count" in text
    assert "catalog snapshot is incomplete" in text
    assert "an active catalog snapshot is immutable" in text


def test_the_migration_is_additive_and_forward_only():
    """No existing object is touched, and nothing is destructive."""
    top_level = statements()
    for forbidden in ("drop table", "drop column", "delete from", "truncate",
                      "alter table public.runs", "alter table public.sources",
                      "alter table public.claims", "alter table public.claim_verdicts",
                      "alter table public.source_evidence_fragments"):
        assert forbidden not in top_level, forbidden
    # Every create is guarded, so a rerun is a no-op.
    assert top_level.count("create table if not exists") == len(STAGING_TABLES) + len(CANONICAL_TABLES)
    assert "create index if not exists" in top_level


def test_no_table_or_function_name_is_ever_built_from_a_payload():
    """No dynamic SQL from model or user input.

    `format()` appears only in the privilege block, and only over literal
    names declared in this file -- never over anything a caller supplies.
    """
    text = sql()
    for rpc_body in text.split("create or replace function public.")[1:]:
        body = rpc_body.split("$$;", 1)[0]
        if "assert_worker_lease" not in body:
            continue
        assert "execute format" not in body
        assert "execute '" not in body
        assert "quote_ident" not in body


def test_the_python_contract_refuses_a_guessed_identity_dimension():
    """An unstated dimension is an absent key -- never '' and never 'unknown'."""
    assert stated_identity_dimensions(None) == {}
    assert stated_identity_dimensions({"drivetrain": "awd"}) == {"drivetrain": "awd"}
    for guess in ({"drivetrain": ""}, {"drivetrain": " awd"}, {"drivetrain": None},
                  {"horsepower": "302"}, {"drivetrain": 4}):
        with pytest.raises(ValueError):
            stated_identity_dimensions(guess)
    with pytest.raises(ValueError):
        trust_state_for("something_else")
