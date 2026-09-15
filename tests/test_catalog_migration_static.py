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
CORRECTION = Path("supabase/migrations/20260915120000_catalog_integrity_corrections.sql")

#: Every relation the catalog namespace adds, and nothing else.
STAGING_TABLES = ("catalog_source_snapshots", "catalog_raw_records",
                  "catalog_candidate_variants", "catalog_candidate_evidence_links")
CANONICAL_TABLES = ("catalog_models", "catalog_model_variants")
GUARDED_RPCS = ("record_catalog_snapshot_guarded", "record_catalog_raw_record_guarded",
                "activate_catalog_snapshot_guarded", "record_catalog_candidate_guarded",
                "link_catalog_candidate_evidence_guarded")


def sql() -> str:
    """Both catalog migrations, as one text.

    Rerun safety and every invariant below are properties of the ORDERED SET,
    not of one file: the corrective migration replaces function bodies and
    constraints the foundation migration introduced, so reading either alone
    would describe a schema that never exists.
    """
    return MIGRATION.read_text(encoding="utf-8") + "\n" + CORRECTION.read_text(encoding="utf-8")


def correction() -> str:
    return CORRECTION.read_text(encoding="utf-8")


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
    """Every catalog RPC leads with the lease guard, in both migrations.

    Three of the five are REDEFINED by the corrective migration, so the lease
    guard has to be present in each definition rather than counted once: a
    corrected body that dropped it would still leave the original file's copy
    in the concatenated text.
    """
    text = sql()
    for body in text.split("create or replace function public.")[1:]:
        name = body.split("(", 1)[0]
        if not name.endswith("_guarded"):
            continue
        statements = [line.strip() for line in body.split("as $$", 1)[1].splitlines()
                      if line.strip() and not line.strip().startswith("--")]
        first = statements[statements.index("begin") + 1]
        assert first.startswith("perform public.assert_worker_lease"), name
        assert "set search_path = pg_catalog" in body.split("as $$", 1)[0], name
    for rpc in GUARDED_RPCS:
        assert f"create or replace function public.{rpc}" in text
        assert f"'public.{rpc}(uuid,text,integer,text,jsonb)'" in text
    assert "revoke execute on function %s from public" in text
    assert "revoke execute on function %s from anon" in text
    assert "revoke execute on function %s from authenticated" in text
    assert "grant execute on function %s to service_role" in text


def test_the_corrective_migration_states_the_authorization_boundary_exactly():
    """No claim that the RPCs are the only database write path.

    They are SECURITY INVOKER and `service_role` keeps direct DML on the
    staging tables, so the accurate statement is about the REPOSITORY's write
    path. Anything that must hold for every writer is a constraint or a
    trigger, not a function body -- which is why the cross-table identity
    checks moved into the schema in this round.
    """
    body = correction()
    assert "security definer" not in body.lower(), (
        "converting these RPCs to SECURITY DEFINER is a separate, reviewed decision")
    assert "REPOSITORY'S CATALOG WRITE PATH goes through these RPCs" in body
    assert "not that they are the only way to write these tables" in body
    # And the schema, not a function, carries the cross-table invariants.
    for constraint in ("catalog_candidate_variants_record_snapshot_fk",
                       "catalog_candidate_evidence_links_candidate_snapshot_fk"):
        assert constraint in body


def test_the_corrective_migration_derives_provenance_rather_than_trusting_it():
    """The defect this round exists for, pinned in the SQL."""
    body = correction()
    for rule in ("catalog evidence link verdict is not verified",
                 "record locator does not match the cited claim",
                 "source version does not match the cited source",
                 "requires the claim it is evidence for",
                 "the cited claim states no evidence locator",
                 "the cited source states no version to pin this link to",
                 "a failed catalog snapshot is terminal",
                 "does not belong to this run",
                 "payload digest is derived, not supplied",
                 "canonical catalog rows are immutable"):
        assert rule in body, rule
    # The claim is structurally required, not merely checked in a function.
    assert "alter column claim_id set not null" in body
    # And the locator/version are read from the evidence, not the payload.
    assert "v_locator := v_claim.evidence_locator;" in body
    assert "v_kind := v_source.source_version_kind;" in body
    assert "v_version := v_source.source_version_id;" in body


def test_canonical_rows_have_no_update_path_and_pr3_is_told_why():
    """Row-level provenance cannot verify a multi-field row."""
    body = correction()
    assert "revision must advance" not in body
    assert "FIELD-LEVEL, append-only" in body
    assert "a row-level FK is not sufficient" in body
    trigger = body.split("create or replace function public.forbid_canonical_identity_rewrite", 1)[1]
    trigger = trigger.split("$$;", 1)[0]
    assert "raise exception 'canonical catalog rows are immutable'" in trigger
    assert "before update or delete on public.catalog_models" in body
    assert "before update or delete on public.catalog_model_variants" in body


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
