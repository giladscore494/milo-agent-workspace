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

import inspect
from pathlib import Path

from backend.catalog.contracts import (CANDIDATE_IDENTITY_DIMENSIONS, CANDIDATE_STATUSES,
                                       CANONICAL_DIMENSION_PREFIX,
                                       CANONICAL_OPTIONAL_FIELDS, CANONICAL_REQUIRED_FIELDS,
                                       CANONICAL_VARIANT_FIELDS, CATALOG_SOURCE_FAMILIES,
                                       CATALOG_TRUST_STATES, CONTENT_SHA256_PATTERN,
                                       IDEMPOTENCY_KEY_PATTERN, MAX_RAW_PAYLOAD_CHARS,
                                       MAX_RAW_RECORD_LOCATOR_CHARS,
                                       MAX_RAW_RECORD_LOCATOR_POSITION,
                                       MAX_RETRIEVAL_METADATA_CHARS, RAW_RECORD_LOCATOR_KEYS,
                                       SNAPSHOT_VALIDATION_STATES, TRUST_STATE_BY_FAMILY,
                                       claim_entity_key, is_evidence_family,
                                       record_locator_id, stated_canonical_fields,
                                       stated_identity_dimensions, stated_source_locator,
                                       trust_state_for)
from backend.catalog.diff import DIFF_IDENTITY, MAX_DIFF_ITEMS
from backend.catalog.keys import canonical_variant_key
from backend.catalog.government.normalize import RAW_ONLY_CONTRACT
from backend.catalog.government.projection import MAX_RESULT_ITEMS
from backend.catalog.government.query import TOTAL_COUNT_FIELD

import pytest

MIGRATION = Path("supabase/migrations/20260914200000_catalog_evidence_foundation.sql")
CORRECTION = Path("supabase/migrations/20260915120000_catalog_integrity_corrections.sql")
LOCATOR = Path("supabase/migrations/20260915180000_catalog_raw_record_source_locator.sql")
#: Catalog PR3.
QUERIES = Path("supabase/migrations/20260916090000_catalog_bounded_candidate_queries.sql")
PROMOTION = Path("supabase/migrations/20260916120000_catalog_field_level_promotion.sql")

#: Every relation the catalog namespace adds, and nothing else.
STAGING_TABLES = ("catalog_source_snapshots", "catalog_raw_records",
                  "catalog_candidate_variants", "catalog_candidate_evidence_links")
CANONICAL_TABLES = ("catalog_models", "catalog_model_variants")
#: Catalog PR3: one append-only provenance row per promoted canonical FACT,
#: plus the read model derived from it.
PROVENANCE_TABLES = ("catalog_canonical_field_provenance",)
VIEWS = ("catalog_canonical_field_current", "catalog_canonical_variant_current")
GUARDED_RPCS = ("record_catalog_snapshot_guarded", "record_catalog_raw_record_guarded",
                "activate_catalog_snapshot_guarded", "record_catalog_candidate_guarded",
                "link_catalog_candidate_evidence_guarded")


def sql() -> str:
    """All three catalog migrations, in apply order, as one text.

    Rerun safety and every invariant below are properties of the ORDERED SET,
    not of one file: each later migration replaces function bodies and
    constraints an earlier one introduced, so reading any of them alone would
    describe a schema that never exists.
    """
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in (MIGRATION, CORRECTION, LOCATOR, QUERIES, PROMOTION))


def correction() -> str:
    return CORRECTION.read_text(encoding="utf-8")


def locator() -> str:
    return LOCATOR.read_text(encoding="utf-8")


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
    # Every create is guarded, so a rerun is a no-op. Catalog PR3 adds the
    # append-only field provenance relation to the set.
    assert top_level.count("create table if not exists") == (
        len(STAGING_TABLES) + len(CANONICAL_TABLES) + len(PROVENANCE_TABLES))
    assert "create index if not exists" in top_level
    # The two READ MODEL views are `create or replace`, which is idempotent by
    # definition and never drops the relation it replaces.
    assert top_level.count("create or replace view public.catalog_canonical_") == len(VIEWS)


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


# =============================================================================
# Catalog PR2: the raw-record capture position
# =============================================================================

def test_the_locator_vocabulary_is_written_into_the_schema_verbatim():
    """A key the backend accepts and the database refuses is a production 500.

    Checked in both directions, like the identity dimensions: every name the
    Python tuple states appears in the SQL allowlist, and the SQL allowlist
    names nothing the Python tuple does not.
    """
    text = locator()
    for key in RAW_RECORD_LOCATOR_KEYS:
        assert f"'{key}'" in text, key
    allowlist = text.split("where entry.key not in (", 1)[1].split(")", 1)[0]
    named = {chunk.strip().strip("'") for chunk in allowlist.split(",") if chunk.strip()}
    assert named == set(RAW_RECORD_LOCATOR_KEYS)
    assert f"<= {MAX_RAW_RECORD_LOCATOR_CHARS}" in text
    assert str(MAX_RAW_RECORD_LOCATOR_POSITION) in text


def test_a_locator_position_must_be_a_non_negative_whole_number_in_both_copies():
    """The SQL reads the jsonb value as text and requires digits only, which is
    the same rule the Python preparer applies -- a float, a boolean, a string
    and a negative value are refused by both."""
    text = locator()
    assert "jsonb_typeof(entry.value) <> 'number'" in text
    assert "!~ '^[0-9]+$'" in text
    assert stated_source_locator(None) == {}
    assert stated_source_locator({"page_index": 0}) == {"page_index": 0}
    for guess in ({"page": 1}, {"page_index": -1}, {"page_index": "3"}, {"page_index": 1.5},
                  {"page_index": True}, {"page_index": None},
                  {"page_index": MAX_RAW_RECORD_LOCATOR_POSITION + 1}):
        with pytest.raises(ValueError):
            stated_source_locator(guess)


def test_the_locator_migration_is_additive_and_changes_no_other_object():
    """ONE nullable-by-default column on an existing relation.

    No table is created, nothing is backfilled, no existing column or
    constraint outside this one is touched, and the relation's append-only
    trigger is left exactly as it was -- so a locator is written once with its
    row and can never be revised.
    """
    body = "".join(line.split("--", 1)[0] + "\n" for line in locator().splitlines())
    parts = body.split("$$")
    top_level = "".join(parts[index] for index in range(0, len(parts), 2)).lower()
    for forbidden in ("create table", "drop table", "drop column", "delete from", "truncate",
                      "insert into", "update public.", "drop trigger", "create policy"):
        assert forbidden not in top_level, forbidden
    assert "add column if not exists source_locator jsonb not null default '{}'::jsonb" in top_level
    # Only this one relation is altered at all.
    altered = {chunk.split("\n", 1)[0].strip()
               for chunk in top_level.split("alter table ")[1:]}
    assert altered == {"public.catalog_raw_records"}


def test_one_capture_position_belongs_to_one_row():
    text = locator()
    assert "create unique index if not exists catalog_raw_records_snapshot_position_uidx" in text
    assert "where source_locator ? 'capture_index'" in text


def test_the_locator_joins_the_raw_record_replay_identity():
    """A replay that MOVES a row is not a retry."""
    body = locator()
    conflict = body.split("-- Replay: identical content AND identical position", 1)[1]
    assert "v_row.source_locator is distinct from v_locator" in conflict
    assert "catalog raw record idempotency conflict" in conflict
    # And every PR1 refusal the corrected body carried is still present.
    for rule in ("payload digest is derived, not supplied",
                 "this catalog snapshot does not belong to this run",
                 "a failed catalog snapshot is terminal",
                 "an active catalog snapshot is immutable",
                 "catalog raw record resource mismatch",
                 "payload exceeds the durable bound"):
        assert rule in body, rule


def test_the_locator_predicate_is_immutable_strictly_boolean_and_service_only():
    text = locator()
    predicate = text.split("create or replace function public.catalog_source_locator_valid", 1)[1]
    predicate = predicate.split("$$;", 1)[0]
    assert "immutable" in predicate and "set search_path = pg_catalog" in predicate
    # A CHECK treats NULL as passing, so the helper may never yield one.
    assert "coalesce(" in predicate and ", false)" in predicate
    assert "'public.catalog_source_locator_valid(jsonb)'" in text


# =============================================================================
# Catalog PR3: the promotable vocabulary and the page bound, in both copies
# =============================================================================

def queries() -> str:
    return QUERIES.read_text(encoding="utf-8")


def promotion() -> str:
    return PROMOTION.read_text(encoding="utf-8")


def test_the_promotable_field_vocabulary_is_written_into_the_schema_verbatim():
    """A field the backend would promote and the database would refuse is a
    500 in production and a green test suite everywhere else."""
    text = promotion()
    # The four variant columns, named as a closed IN list.
    assert ", ".join(f"'{name}'" for name in
                     (*CANONICAL_REQUIRED_FIELDS, *CANONICAL_OPTIONAL_FIELDS)) in text
    # And one namespaced entry per dimension of the closed vocabulary -- with
    # no dimension in SQL that the Python vocabulary does not name, which is
    # the direction a copy usually drifts.
    allowlist = text.split("and substring(p_field_key from 21) in", 1)[1].split("))", 1)[0]
    named = {chunk.strip().strip("(").strip().strip("'")
             for chunk in allowlist.split(",")}
    assert {name for name in named if name} == set(CANDIDATE_IDENTITY_DIMENSIONS)
    assert CANONICAL_DIMENSION_PREFIX == "identity_dimensions."
    assert f"'{CANONICAL_DIMENSION_PREFIX}%'" in text
    # 21 is the position after the prefix: an off-by-one here would rename
    # every dimension.
    assert len(CANONICAL_DIMENSION_PREFIX) + 1 == 21
    assert set(CANONICAL_VARIANT_FIELDS) == set(CANONICAL_REQUIRED_FIELDS) \
        | set(CANONICAL_OPTIONAL_FIELDS) \
        | {f"{CANONICAL_DIMENSION_PREFIX}{name}" for name in CANDIDATE_IDENTITY_DIMENSIONS}


def test_the_stated_field_derivation_is_the_same_rule_in_both_languages():
    """`catalog_canonical_stated_fields` and `stated_canonical_fields` decide
    the same thing: which fields a canonical row states."""
    text = promotion()
    for name in (*CANONICAL_REQUIRED_FIELDS, *CANONICAL_OPTIONAL_FIELDS):
        assert f"select '{name}'" in text or f"'{name}'::text as field_key" in text
    # An optional column is stated only when it is not null, in both copies.
    assert "where p_official_model_code is not null" in text
    assert "where p_trim is not null" in text
    stated = stated_canonical_fields({"model_year_start": 2021, "model_year_end": 2021,
                                      "official_model_code": None, "trim": "SE",
                                      "identity_dimensions": {"fuel_type": "petrol"}})
    assert stated == {"model_year_start": 2021, "model_year_end": 2021, "trim": "SE",
                      "identity_dimensions.fuel_type": "petrol"}


def test_the_page_bound_and_the_usability_markers_are_the_python_ones():
    text = queries()
    assert f"select {MAX_RESULT_ITEMS}" in text
    # The snapshot gate reads the SAME durable metadata keys PR2 writes and
    # `parse_normalization_state` parses.
    for key in ("normalization_contract", "normalization_issue_count"):
        assert f"->>'{key}'" in text, key
    assert f"= '{RAW_ONLY_CONTRACT}'" in text
    # And the ordering is collation-free in BOTH the index and the query, so a
    # differently configured cluster cannot reorder a page.
    assert text.count('collate "C"') >= 8


def test_the_canonical_pair_becomes_insertable_and_nothing_more():
    """The exact grant PR3 adds, and the two it does not."""
    text = promotion()
    assert "grant select, insert on table %s to service_role" in text
    assert "revoke update, delete on table %s from service_role" in text
    for forbidden in ("grant update on table public.catalog_model",
                      "grant delete on table public.catalog_model",
                      "grant all on table public.catalog_model"):
        assert forbidden not in text
    # A view is a NEW object, so Supabase default privileges hand it
    # everything: the revoke has to come first.
    assert "revoke all on %s from service_role" in text
    assert "security_invoker = true" in text


def test_the_two_triggers_that_hold_for_every_writer_are_present():
    text = promotion()
    assert "before insert on public.catalog_canonical_field_provenance" in text
    assert "create constraint trigger catalog_model_variants_require_field_provenance" in text
    assert "deferrable initially deferred" in text
    # The chain each promoted fact is held to, named refusal by refusal.
    for refusal in ("cites evidence of another candidate",
                    "catalog candidate is not ready for promotion",
                    "an unverified catalog source cannot support a canonical fact",
                    "canonical field provenance verdict is not verified",
                    "claim states a different field",
                    "claim states a different value",
                    "claim is in an unresolved conflict",
                    "contradicts the canonical identity",
                    "requires verified provenance for every field it states",
                    "requires at least one promoted variant"):
        assert refusal in text, refusal


def test_the_locator_and_entity_conventions_are_one_definition_in_both_languages():
    """A promoted fact is bound to its vehicle by two assembled strings.

    If SQL spelled either of them differently the gate would refuse every
    honest promotion, so the two copies are pinned rather than reviewed.
    """
    text = promotion()
    assert "create or replace function public.catalog_record_locator_id(" in text
    assert "create or replace function public.catalog_claim_entity_key(" in text
    # Python assembles them here, and the SQL body must be the same two joins.
    assert record_locator_id("cs1.aa", "36327") == "cs1.aa:36327"
    assert claim_entity_key("cm1.bb", 2021) == "cm1.bb:2021"
    assert "p_snapshot_key || ':' || p_upstream_record_id" in text
    assert "p_model_canonical_key || ':' || p_model_year::text" in text
    # And the R4 scope normalization, whose four steps must be the four
    # `_normalize_text` applies, in that order.
    assert "normalize(p_value, NFKC)" in text
    assert "translate(lower(" in text and "'_-', '  '" in text
    assert "regexp_replace(" in text


def test_the_scope_and_run_gates_name_every_refusal_they_make():
    """Every WRONG-VEHICLE, WRONG-SCOPE and WRONG-RUN refusal, by name."""
    text = promotion()
    for refusal in ("cites a candidate for another vehicle",
                    "cites a candidate for another variant",
                    "claim states no model year scope",
                    "claim is scoped to another model year",
                    "claim is about another vehicle",
                    "claim states no market scope",
                    "claim is scoped to another vehicle identity",
                    "cites evidence read from another source record",
                    "locator does not match its cited claim",
                    "was not promoted by its linking run",
                    "support chain spans more than one run",
                    "catalog promotion is not one act of one run",
                    "scope disagrees with this variant",
                    "catalog promotion states an identity its candidate does not",
                    "catalog canonical variant identity conflict"):
        assert refusal in text, refusal
    # `identity_dimensions` is a REVISABLE FACT, in all four places that decide
    # what a canonical variant IS.
    assert "identity_dimensions" not in text.split(
        "create or replace function public.catalog_canonical_identity_field", 1)[1].split("$$;", 1)[0]
    natural = text.split("create unique index if not exists "
                         "catalog_model_variants_natural_uidx", 1)[1].split(";", 1)[0]
    assert "identity_dimensions" not in natural
    assert "identity_dimensions" not in inspect.signature(canonical_variant_key).parameters


def test_the_snapshot_diff_is_bounded_in_its_list_and_exact_in_its_counts():
    """The comparison runs in the database, and says so in its shape."""
    text = queries()
    assert "create or replace function public.catalog_snapshot_candidate_diff(" in text
    body = text.split("create or replace function public.catalog_snapshot_candidate_diff",
                      1)[1].split("$$;", 1)[0]
    # Both sides pass the SAME readability gate every other answer passes.
    assert body.count("perform public.catalog_readable_snapshot(") == 2
    # The counts are aggregates over the whole paired set, never over the page.
    assert "count(*) filter (where p.state = 'added')" in body
    assert "full outer join before b" in body
    # The list is bounded by the page bound, and dropped WHOLE past it.
    assert "least(coalesce(p_limit, 100), public.catalog_page_limit())" in body
    assert "from counted c) <= v_limit" in body
    assert MAX_DIFF_ITEMS == 100
    # The identity is the COMPLETE stated identity, dimensions included.
    for name in DIFF_IDENTITY:
        assert name in body, name


def test_every_aggregation_states_its_total_even_with_no_rows():
    """A page past the last row must state the total, not infer zero from its
    own emptiness."""
    text = queries()
    # One anchored left join per paged aggregation, so an empty page is still
    # exactly one COUNT ROW.
    assert text.count("left join page p on true") == 4
    assert text.count("from (select 1) as anchor") == 4
    assert TOTAL_COUNT_FIELD == "total_count"


def test_the_promotion_rpc_is_lease_guarded_and_names_no_dynamic_object():
    text = promotion()
    body = text.split("create or replace function public.promote_catalog_variant_guarded", 1)[1]
    body = body.split("$$;", 1)[0]
    # The lease is the FIRST statement, exactly like every other durable
    # catalog write.
    statements = [line.strip() for line in body.split("begin", 1)[1].splitlines() if line.strip()]
    assert statements[0].startswith("perform public.assert_worker_lease(")
    assert "unsafe catalog payload rejected" in body
    # No dynamic SQL and no object name assembled from data, anywhere.
    for forbidden in ("execute format(", "quote_ident(", "||' from '||"):
        assert forbidden not in body, forbidden
