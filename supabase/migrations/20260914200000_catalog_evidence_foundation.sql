-- Catalog PR1: an EMPTY, evidence-backed catalog foundation.
--
-- What this migration is for
-- --------------------------
--
-- MILO's durable evidence relations -- public.sources, public.claims,
-- public.source_evidence_fragments, public.claim_verdicts,
-- public.conflicts, public.conflict_resolutions -- are all RUN-SCOPED:
-- every one of them carries `run_id ... on delete cascade`.  That is correct
-- for evidence, which is a record of what one run established.  It is not a
-- catalog: delete the run and the rows go with it, so nothing in this schema
-- can outlive the run that produced it.
--
-- This migration adds the SMALLEST set of long-lived relations that a catalog
-- needs, and links them back to the existing evidence rather than restating
-- it.  There is deliberately no catalog_claims and no catalog_conflicts here:
-- public.claims and public.conflicts already represent those concepts, and a
-- parallel copy would be a second source of truth with its own drift.
--
-- What starts empty, and stays empty in PR1
-- -----------------------------------------
--
-- public.catalog_models and public.catalog_model_variants are the CANONICAL
-- catalog.  They are created empty, nothing is backfilled into them, and no
-- write path in this migration can insert into them -- service_role is
-- granted SELECT only, and INSERT/UPDATE/DELETE are revoked from every role.
-- Canonical promotion is PR3's work and will arrive with its own reviewed
-- migration that grants the privilege it needs.  Until then "the canonical
-- catalog is empty" is enforced by the database, not by convention.
--
-- The existing aggregated catalog is NOT the seed
-- -----------------------------------------------
--
-- The `reliabilityAIModelsR2` JSON catalog is incomplete, holds incorrect
-- values and is missing models and variants.  It is therefore NOT imported,
-- NOT copied into this migration, and NOT canonical.  It is representable
-- here only as a snapshot whose source_family is 'legacy_reference', which a
-- CHECK constraint pins to trust_state 'unverified' -- so it can suggest a
-- candidate to look for and can never carry a verdict, verify a fact, or
-- override a Government or manufacturer statement.  No row of it is copied
-- into this file: this migration contains schema only.
--
-- Additive and forward-only: no existing table, column, RPC, row, index,
-- policy or grant is modified, dropped or rewritten, and nothing is
-- backfilled.  Reverting to the previous schema is a matter of not using
-- these relations; they hold no rows.

-- ---------------------------------------------------------------------------
-- 1. Immutable source snapshots.
-- ---------------------------------------------------------------------------
--
-- One snapshot is one retrieval of one upstream resource at one upstream
-- version.  It is append-only, and it becomes usable only once it is COMPLETE
-- and its own row count agrees with the total the upstream declared -- the
-- R5 Government pagination correction in exactly this form: a capture that
-- holds 200 of 233 rows must never look like a complete one.

create table if not exists public.catalog_source_snapshots (
  id uuid primary key default gen_random_uuid(),
  -- Provenance, not ownership.  ON DELETE RESTRICT, never CASCADE: a run is
  -- what PRODUCED a snapshot, not what the snapshot belongs to, and durable
  -- catalog state must not disappear with it.
  created_by_run_id uuid not null references public.runs(id) on delete restrict,
  source_family text not null,
  trust_state text not null,
  resource_id text not null,
  upstream_version text not null,
  upstream_version_kind text not null,
  content_sha256 text not null,
  retrieved_at timestamptz not null,
  retrieval_metadata jsonb not null default '{}'::jsonb,
  -- Completeness: what the upstream said it had, and what this snapshot holds.
  declared_record_count integer not null,
  stored_record_count integer not null default 0,
  validation_state text not null default 'pending',
  -- NULL until the snapshot is complete AND its counts agree.  A reader asks
  -- for `activated_at is not null`; nothing else makes a snapshot usable.
  activated_at timestamptz,
  -- Stable replay identity, derived by the backend from provenance only.
  snapshot_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_source_snapshots_family_allowlisted
    check (source_family in ('government', 'manufacturer', 'legacy_reference')),
  constraint catalog_source_snapshots_trust_allowlisted
    check (trust_state in ('evidence', 'unverified')),
  -- The legacy catalog can NEVER be evidence, and a primary source can never
  -- be silently downgraded.  Pinned per family, mirroring
  -- TRUST_STATE_BY_FAMILY in backend/catalog/contracts.py.
  constraint catalog_source_snapshots_trust_pinned_to_family
    check (trust_state = case source_family
             when 'legacy_reference' then 'unverified' else 'evidence' end),
  constraint catalog_source_snapshots_validation_allowlisted
    check (validation_state in ('pending', 'complete', 'failed')),
  constraint catalog_source_snapshots_version_kind_valid
    check (public.r3_source_version_valid(upstream_version_kind, upstream_version)),
  constraint catalog_source_snapshots_content_hash_shape
    check (content_sha256 ~ '^[0-9a-f]{64}$'),
  constraint catalog_source_snapshots_key_shape
    check (snapshot_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  constraint catalog_source_snapshots_resource_bounded
    check (char_length(resource_id) between 1 and 200),
  constraint catalog_source_snapshots_counts_non_negative
    check (declared_record_count >= 0 and stored_record_count >= 0),
  constraint catalog_source_snapshots_metadata_bounded
    check (char_length(retrieval_metadata::text) <= 4096),
  -- The activation gate, as a constraint rather than as a code path: a
  -- snapshot may only be active when it is validated complete and holds every
  -- record the upstream declared.
  constraint catalog_source_snapshots_active_only_when_complete
    check (activated_at is null
           or (validation_state = 'complete'
               and stored_record_count = declared_record_count))
);

-- One snapshot per logical retrieval.  A replay collapses onto this row.
create unique index if not exists catalog_source_snapshots_key_uidx
  on public.catalog_source_snapshots(snapshot_key);
create index if not exists catalog_source_snapshots_resource_idx
  on public.catalog_source_snapshots(source_family, resource_id, activated_at);

-- ---------------------------------------------------------------------------
-- 2. Raw source records.
-- ---------------------------------------------------------------------------
--
-- The exact upstream rows a snapshot captured, owned by that snapshot and no
-- other.  Append-only and bounded: the durable representation of one record,
-- never a place to park a dataset.

create table if not exists public.catalog_raw_records (
  id uuid primary key default gen_random_uuid(),
  -- Exact snapshot ownership.  RESTRICT: a snapshot that has records cannot
  -- be removed out from under them.
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  -- Carried on the row as well as on the snapshot so a record states which
  -- upstream object it came from without a join, and so a record can never be
  -- moved to a snapshot describing a different resource.
  resource_id text not null,
  upstream_record_id text not null,
  payload jsonb not null,
  payload_sha256 text not null,
  -- Stable replay identity within the owning snapshot.
  record_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_raw_records_payload_bounded
    check (char_length(payload::text) between 2 and 16384),
  constraint catalog_raw_records_payload_is_object
    check (jsonb_typeof(payload) = 'object'),
  constraint catalog_raw_records_hash_shape
    check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  constraint catalog_raw_records_key_shape
    check (record_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  constraint catalog_raw_records_upstream_id_bounded
    check (char_length(upstream_record_id) between 1 and 200),
  constraint catalog_raw_records_resource_bounded
    check (char_length(resource_id) between 1 and 200)
);

-- Replay identity: one logical record per snapshot.
create unique index if not exists catalog_raw_records_snapshot_key_uidx
  on public.catalog_raw_records(snapshot_id, record_key);
-- The upstream row is addressable by its OWN id inside its snapshot.
create unique index if not exists catalog_raw_records_snapshot_upstream_uidx
  on public.catalog_raw_records(snapshot_id, upstream_record_id);

-- The closed identity-dimension vocabulary, as an IMMUTABLE predicate.
--
-- A CHECK constraint may not contain a subquery, so the rule lives in a
-- function -- the same device `r3_source_version_valid` uses.  It reads no
-- table and is a pure function of its argument, and it mirrors
-- CANDIDATE_IDENTITY_DIMENSIONS in backend/catalog/contracts.py, pinned
-- against that tuple by tests/test_catalog_migration_static.py.
create or replace function public.catalog_identity_dimensions_valid(p_dimensions jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  -- Strictly boolean, never NULL: a CHECK treats NULL as passing, so a
  -- helper that could yield NULL would admit the very rows it exists to
  -- refuse.
  select coalesce(
    p_dimensions is not null
    and jsonb_typeof(p_dimensions) = 'object'
    and not exists (
      select 1 from jsonb_each(p_dimensions) as d(key, value)
      where d.key not in ('body_style', 'drivetrain', 'engine_code', 'fuel_type',
                          'generation', 'market', 'propulsion_technology', 'transmission')
         -- Every stated dimension is EXACT text: never a number, never an
         -- object, never '' and never padded.  There is no 'unknown' value,
         -- because an unstated dimension is an absent key.
         or jsonb_typeof(d.value) <> 'string'
         or char_length(d.value #>> '{}') < 1
         or char_length(d.value #>> '{}') > 120
         or btrim(d.value #>> '{}') <> (d.value #>> '{}')
    ), false)
$$;

-- ---------------------------------------------------------------------------
-- 3. Candidate vehicle identities.
-- ---------------------------------------------------------------------------
--
-- What a raw record appears to identify.  A candidate is a reading of a
-- record, not a canonical fact: it may stay `ambiguous` forever, and nothing
-- here forces it to resolve.  Only STATED dimensions are stored -- an absent
-- dimension is an absent key, never an empty string and never 'unknown'.

create table if not exists public.catalog_candidate_variants (
  id uuid primary key default gen_random_uuid(),
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  raw_record_id uuid not null references public.catalog_raw_records(id) on delete restrict,
  manufacturer text not null,
  commercial_model text not null,
  -- A single model year is the range [y, y].  Both ends are stated or the
  -- range is absent entirely; a half-open range would be a guess.
  model_year_start integer,
  model_year_end integer,
  official_model_code text,
  trim text,
  -- Generation, drivetrain, engine and the rest, ONLY when the source states
  -- them.  The key set is closed by a constraint below.
  identity_dimensions jsonb not null default '{}'::jsonb,
  status text not null default 'candidate',
  candidate_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_candidate_variants_status_allowlisted
    check (status in ('candidate', 'ambiguous', 'rejected', 'ready_for_review')),
  constraint catalog_candidate_variants_manufacturer_bounded
    check (char_length(manufacturer) between 1 and 120 and btrim(manufacturer) = manufacturer),
  constraint catalog_candidate_variants_model_bounded
    check (char_length(commercial_model) between 1 and 200 and btrim(commercial_model) = commercial_model),
  -- An optional identity column is either ABSENT or exact.  '' and a padded
  -- value are refusals, because both read as "stated" while saying nothing.
  constraint catalog_candidate_variants_code_exact
    check (official_model_code is null
           or (char_length(official_model_code) between 1 and 120
               and btrim(official_model_code) = official_model_code)),
  constraint catalog_candidate_variants_trim_exact
    check (trim is null
           or (char_length(trim) between 1 and 120 and btrim(trim) = trim)),
  constraint catalog_candidate_variants_year_range_whole
    check ((model_year_start is null) = (model_year_end is null)),
  constraint catalog_candidate_variants_year_range_ordered
    check (model_year_start is null
           or (model_year_start between 1900 and 2100
               and model_year_end between model_year_start and 2100)),
  constraint catalog_candidate_variants_dimensions_bounded
    check (char_length(identity_dimensions::text) <= 2048),
  -- The closed dimension vocabulary, mirroring CANDIDATE_IDENTITY_DIMENSIONS
  -- in backend/catalog/contracts.py.
  constraint catalog_candidate_variants_dimensions_allowlisted
    check (public.catalog_identity_dimensions_valid(identity_dimensions)),
  constraint catalog_candidate_variants_key_shape
    check (candidate_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$')
);

create unique index if not exists catalog_candidate_variants_snapshot_key_uidx
  on public.catalog_candidate_variants(snapshot_id, candidate_key);
create index if not exists catalog_candidate_variants_identity_idx
  on public.catalog_candidate_variants(manufacturer, commercial_model,
                                       model_year_start, model_year_end, status);

-- ---------------------------------------------------------------------------
-- 4. The canonical catalog -- created EMPTY, and unwritable in PR1.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_models (
  id uuid primary key default gen_random_uuid(),
  manufacturer text not null,
  commercial_model text not null,
  -- The canonical identity.  UNIQUE, so a promotion can never create a second
  -- row for a model that already exists; it must revise the existing one.
  canonical_key text not null,
  revision integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint catalog_models_manufacturer_bounded
    check (char_length(manufacturer) between 1 and 120),
  constraint catalog_models_model_bounded
    check (char_length(commercial_model) between 1 and 200),
  constraint catalog_models_key_shape
    check (canonical_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  constraint catalog_models_revision_positive check (revision >= 1)
);
create unique index if not exists catalog_models_canonical_key_uidx
  on public.catalog_models(canonical_key);

create table if not exists public.catalog_model_variants (
  id uuid primary key default gen_random_uuid(),
  model_id uuid not null references public.catalog_models(id) on delete restrict,
  -- Traceability as a STRUCTURAL requirement, not a convention: a canonical
  -- variant cannot exist without naming the candidate it was promoted from
  -- and the verdict that verified it.  NOT NULL plus a real foreign key, so
  -- there is no shape in which an untraceable canonical fact is storable.
  promoted_from_candidate_id uuid not null
    references public.catalog_candidate_variants(id) on delete restrict,
  promoted_from_verdict_id uuid not null
    references public.claim_verdicts(id) on delete restrict,
  canonical_key text not null,
  model_year_start integer not null,
  model_year_end integer not null,
  official_model_code text,
  trim text,
  identity_dimensions jsonb not null default '{}'::jsonb,
  revision integer not null default 1,
  created_at timestamptz not null default now(),
  constraint catalog_model_variants_key_shape
    check (canonical_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  constraint catalog_model_variants_year_range_ordered
    check (model_year_start between 1900 and 2100
           and model_year_end between model_year_start and 2100),
  constraint catalog_model_variants_dimensions_allowlisted
    check (public.catalog_identity_dimensions_valid(identity_dimensions)),
  constraint catalog_model_variants_revision_positive check (revision >= 1)
);
create unique index if not exists catalog_model_variants_canonical_key_uidx
  on public.catalog_model_variants(canonical_key);
create index if not exists catalog_model_variants_model_idx
  on public.catalog_model_variants(model_id, model_year_start, model_year_end);

-- No silent overwrite of an existing canonical identity.  A revision may
-- change what a canonical row SAYS, but never who it IS: the identity columns
-- and the provenance are frozen, and every revision must advance the counter.
create or replace function public.forbid_canonical_identity_rewrite() returns trigger
language plpgsql as $$
begin
  if tg_table_name = 'catalog_models' then
    if new.canonical_key is distinct from old.canonical_key
       or new.manufacturer is distinct from old.manufacturer
       or new.commercial_model is distinct from old.commercial_model then
      raise exception 'catalog canonical identity is immutable';
    end if;
  else
    if new.canonical_key is distinct from old.canonical_key
       or new.model_id is distinct from old.model_id
       or new.promoted_from_candidate_id is distinct from old.promoted_from_candidate_id
       or new.promoted_from_verdict_id is distinct from old.promoted_from_verdict_id then
      raise exception 'catalog canonical identity is immutable';
    end if;
  end if;
  if new.revision <= old.revision then
    raise exception 'catalog canonical revision must advance';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_models_identity_immutable on public.catalog_models;
create trigger catalog_models_identity_immutable
  before update on public.catalog_models
  for each row execute function public.forbid_canonical_identity_rewrite();

drop trigger if exists catalog_model_variants_identity_immutable on public.catalog_model_variants;
create trigger catalog_model_variants_identity_immutable
  before update on public.catalog_model_variants
  for each row execute function public.forbid_canonical_identity_rewrite();

-- ---------------------------------------------------------------------------
-- 5. Candidate -> existing evidence links.
-- ---------------------------------------------------------------------------
--
-- The join back to the EXISTING evidence system.  A link names real
-- public.sources / public.claims / public.claim_verdicts rows by their own
-- ids: no fact is copied here, and no fact can be recorded without the
-- provenance that produced it.

create table if not exists public.catalog_candidate_evidence_links (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null
    references public.catalog_candidate_variants(id) on delete restrict,
  snapshot_id uuid not null
    references public.catalog_source_snapshots(id) on delete restrict,
  -- The run whose evidence this is.  RESTRICT, so a run that durable catalog
  -- state depends on cannot be deleted and take the evidence with it.
  run_id uuid not null references public.runs(id) on delete restrict,
  source_id uuid not null references public.sources(id) on delete restrict,
  claim_id uuid references public.claims(id) on delete restrict,
  -- NULL unless a verdict verified this claim.  A `legacy_reference`
  -- snapshot's candidate may never carry one (enforced in the guarded RPC),
  -- which is what "the old catalog never verifies a fact" means here.
  verdict_id uuid references public.claim_verdicts(id) on delete restrict,
  -- The EXACT record locator and the EXACT source version the fact was read
  -- at, preserved on the link so provenance never depends on the upstream
  -- still serving the same bytes.
  record_locator text not null,
  source_version text not null,
  source_version_kind text not null,
  link_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_candidate_evidence_links_locator_canonical
    check (public.r3_canonical_locator(record_locator) is not null),
  constraint catalog_candidate_evidence_links_version_valid
    check (public.r3_source_version_valid(source_version_kind, source_version)),
  constraint catalog_candidate_evidence_links_key_shape
    check (link_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  -- A verdict without the claim it judged is not provenance.
  constraint catalog_candidate_evidence_links_verdict_needs_claim
    check (verdict_id is null or claim_id is not null)
);

create unique index if not exists catalog_candidate_evidence_links_key_uidx
  on public.catalog_candidate_evidence_links(candidate_id, link_key);
create index if not exists catalog_candidate_evidence_links_candidate_idx
  on public.catalog_candidate_evidence_links(candidate_id, created_at);
create index if not exists catalog_candidate_evidence_links_run_idx
  on public.catalog_candidate_evidence_links(run_id, source_id);

-- ---------------------------------------------------------------------------
-- 6. RLS and append-only posture.
-- ---------------------------------------------------------------------------

alter table public.catalog_source_snapshots enable row level security;
alter table public.catalog_raw_records enable row level security;
alter table public.catalog_candidate_variants enable row level security;
alter table public.catalog_models enable row level security;
alter table public.catalog_model_variants enable row level security;
alter table public.catalog_candidate_evidence_links enable row level security;
-- No policies on any of them: browser roles (PUBLIC / anon / authenticated)
-- have no access at all, exactly like public.claims,
-- public.source_evidence_fragments and public.claim_verdicts.

-- Raw snapshots, raw records and evidence links are an audit record: once
-- durable they can never be rewritten or removed, by any role.  Same device
-- as public.run_usage_ledger (013) and public.source_evidence_fragments.
create or replace function public.forbid_catalog_source_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'catalog source material is append-only';
end;
$$;

drop trigger if exists catalog_raw_records_append_only on public.catalog_raw_records;
create trigger catalog_raw_records_append_only
  before update or delete on public.catalog_raw_records
  for each row execute function public.forbid_catalog_source_mutation();

drop trigger if exists catalog_candidate_evidence_links_append_only
  on public.catalog_candidate_evidence_links;
create trigger catalog_candidate_evidence_links_append_only
  before update or delete on public.catalog_candidate_evidence_links
  for each row execute function public.forbid_catalog_source_mutation();

-- A snapshot is append-only in EVERYTHING except its own completion.  The
-- stored count, the validation state and the activation timestamp advance as
-- records arrive; identity, provenance and content can never change, and an
-- active snapshot is frozen completely.
create or replace function public.forbid_catalog_snapshot_rewrite() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'catalog source material is append-only';
  end if;
  if old.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable';
  end if;
  if new.id is distinct from old.id
     or new.created_by_run_id is distinct from old.created_by_run_id
     or new.source_family is distinct from old.source_family
     or new.trust_state is distinct from old.trust_state
     or new.resource_id is distinct from old.resource_id
     or new.upstream_version is distinct from old.upstream_version
     or new.upstream_version_kind is distinct from old.upstream_version_kind
     or new.content_sha256 is distinct from old.content_sha256
     or new.retrieved_at is distinct from old.retrieved_at
     or new.retrieval_metadata is distinct from old.retrieval_metadata
     or new.declared_record_count is distinct from old.declared_record_count
     or new.snapshot_key is distinct from old.snapshot_key
     or new.created_at is distinct from old.created_at then
    raise exception 'catalog snapshot identity is immutable';
  end if;
  -- Records only ever accumulate.
  if new.stored_record_count < old.stored_record_count then
    raise exception 'catalog snapshot record count cannot decrease';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_source_snapshots_append_only on public.catalog_source_snapshots;
create trigger catalog_source_snapshots_append_only
  before update or delete on public.catalog_source_snapshots
  for each row execute function public.forbid_catalog_snapshot_rewrite();

-- A candidate's READING may be revised (a later record can move it from
-- ambiguous to ready_for_review), but never its identity or its provenance.
create or replace function public.forbid_catalog_candidate_rewrite() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'catalog source material is append-only';
  end if;
  if new.id is distinct from old.id
     or new.snapshot_id is distinct from old.snapshot_id
     or new.raw_record_id is distinct from old.raw_record_id
     or new.candidate_key is distinct from old.candidate_key
     or new.manufacturer is distinct from old.manufacturer
     or new.commercial_model is distinct from old.commercial_model
     or new.model_year_start is distinct from old.model_year_start
     or new.model_year_end is distinct from old.model_year_end
     or new.official_model_code is distinct from old.official_model_code
     or new.trim is distinct from old.trim
     or new.identity_dimensions is distinct from old.identity_dimensions
     or new.created_at is distinct from old.created_at then
    raise exception 'catalog candidate identity is immutable';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_candidate_variants_identity_immutable
  on public.catalog_candidate_variants;
create trigger catalog_candidate_variants_identity_immutable
  before update or delete on public.catalog_candidate_variants
  for each row execute function public.forbid_catalog_candidate_rewrite();

-- ---------------------------------------------------------------------------
-- 7. The only write paths: lease-guarded, fail-closed, idempotent.
-- ---------------------------------------------------------------------------
--
-- Same posture as every other durable worker write in this schema
-- (20260810000300, 20260823000100, 20260828000200, 20260907000100): the
-- caller presents its run id, worker id, attempt and lease token, and
-- `assert_worker_lease` validates all four atomically -- taking FOR SHARE on
-- the runs row -- before a single byte is written.  A stale, superseded or
-- expired worker writes nothing at all.
--
-- Every one of these is idempotent on a backend-derived identity, and every
-- one FAILS CLOSED when that identity is replayed with different content: a
-- silent overwrite would make "the same key" mean two different things.

create or replace function public.record_catalog_snapshot_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_snapshot jsonb
) returns setof public.catalog_source_snapshots
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_source_snapshots;
  v_key text; v_family text; v_trust text; v_resource text;
  v_version text; v_kind text; v_hash text; v_declared integer;
  v_state text; v_retrieved timestamptz; v_metadata jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_snapshot::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;
  v_key := nullif(p_snapshot->>'snapshot_key', '');
  v_family := nullif(p_snapshot->>'source_family', '');
  v_resource := nullif(p_snapshot->>'resource_id', '');
  v_version := nullif(p_snapshot->>'upstream_version', '');
  v_kind := nullif(p_snapshot->>'upstream_version_kind', '');
  v_hash := nullif(p_snapshot->>'content_sha256', '');
  v_state := coalesce(nullif(p_snapshot->>'validation_state', ''), 'pending');
  v_metadata := coalesce(p_snapshot->'retrieval_metadata', '{}'::jsonb);
  if v_key is null or v_family is null or v_resource is null or v_version is null
     or v_kind is null or v_hash is null then
    raise exception 'invalid catalog snapshot: provenance is incomplete' using errcode = '22023';
  end if;
  begin
    v_declared := (p_snapshot->>'declared_record_count')::integer;
    v_retrieved := (p_snapshot->>'retrieved_at')::timestamptz;
  exception when others then
    raise exception 'invalid catalog snapshot: malformed count or retrieval time' using errcode = '22023';
  end;
  if v_declared is null or v_declared < 0 or v_retrieved is null then
    raise exception 'invalid catalog snapshot: malformed count or retrieval time' using errcode = '22023';
  end if;
  -- The trust state is DERIVED from the family, never accepted from the
  -- caller.  A caller that states one is held to the derived value, so the
  -- legacy catalog cannot be submitted as evidence even by a trusted path.
  v_trust := case v_family when 'legacy_reference' then 'unverified' else 'evidence' end;
  if nullif(p_snapshot->>'trust_state', '') is not null
     and p_snapshot->>'trust_state' <> v_trust then
    raise exception 'catalog trust state is pinned to the source family' using errcode = '22023';
  end if;
  -- A snapshot is never born active.  Activation is a separate, explicit
  -- decision made once its records are in and its counts agree, which is why
  -- this path has no way to set activated_at at all.
  if p_snapshot ? 'activated_at' or p_snapshot ? 'stored_record_count' then
    raise exception 'catalog snapshot activation is not a caller-supplied field' using errcode = '22023';
  end if;

  select * into v_row from public.catalog_source_snapshots where snapshot_key = v_key;
  if v_row.id is null then
    insert into public.catalog_source_snapshots
      (created_by_run_id, source_family, trust_state, resource_id, upstream_version,
       upstream_version_kind, content_sha256, retrieved_at, retrieval_metadata,
       declared_record_count, validation_state, snapshot_key)
    values (p_run_id, v_family, v_trust, v_resource, v_version, v_kind, v_hash,
            v_retrieved, v_metadata, v_declared, v_state, v_key)
    on conflict (snapshot_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_source_snapshots where snapshot_key = v_key;
  end if;

  -- ONE replay invariant: the same snapshot_key may only resolve to the SAME
  -- snapshot.  A replay that changes the resource, the upstream version, the
  -- content digest or the declared completeness is a different retrieval
  -- wearing the same name, and it fails closed rather than being accepted or
  -- silently ignored.  The error carries no payload.
  if v_row.source_family is distinct from v_family
     or v_row.resource_id is distinct from v_resource
     or v_row.upstream_version is distinct from v_version
     or v_row.upstream_version_kind is distinct from v_kind
     or v_row.content_sha256 is distinct from v_hash
     or v_row.declared_record_count is distinct from v_declared then
    raise exception 'catalog snapshot idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

create or replace function public.record_catalog_raw_record_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_record jsonb
) returns setof public.catalog_raw_records
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_raw_records;
  v_snapshot public.catalog_source_snapshots;
  v_key text; v_upstream text; v_payload jsonb; v_hash text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- NOTE: the credential/reasoning marker screen the other catalog RPCs apply
  -- is deliberately NOT applied here. This payload is SOURCE CONTENT captured
  -- verbatim from an upstream register, not backend-authored metadata, and a
  -- keyword screen over it would silently drop legitimate upstream rows whose
  -- own field names happen to collide. What bounds this payload instead is
  -- structural and exact: it must be a JSON object, it must fit the durable
  -- size bound, and PostgreSQL recomputes its digest from the stored bytes.
  v_key := nullif(p_record->>'record_key', '');
  v_upstream := nullif(p_record->>'upstream_record_id', '');
  v_payload := p_record->'payload';
  v_hash := nullif(p_record->>'payload_sha256', '');
  if v_key is null or v_upstream is null or v_hash is null
     or v_payload is null or jsonb_typeof(v_payload) <> 'object' then
    raise exception 'invalid catalog raw record: identity or payload is missing' using errcode = '22023';
  end if;
  if char_length(v_payload::text) > 16384 then
    raise exception 'invalid catalog raw record: payload exceeds the durable bound' using errcode = '22023';
  end if;

  -- The owning snapshot is locked FOR UPDATE, so the stored-record counter
  -- below is exact under concurrency and two writers cannot both observe the
  -- pre-insert count.  Lock order is unchanged from every other guarded RPC:
  -- public.runs (FOR SHARE, inside assert_worker_lease) and only then the
  -- object being written.
  select * into v_snapshot from public.catalog_source_snapshots
    where id = (p_record->>'snapshot_id')::uuid for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog raw record snapshot' using errcode = '23503';
  end if;
  -- An ACTIVE snapshot is a finished, immutable capture.  Appending to one
  -- would change what an already-validated snapshot contains, which is
  -- exactly the property the activation gate exists to guarantee.
  if v_snapshot.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
  end if;
  -- Cross-resource linkage: a record may only join a snapshot of its own
  -- upstream resource.  Belonging to the same run is not enough, and neither
  -- is naming the right snapshot id.
  if nullif(p_record->>'resource_id', '') is distinct from v_snapshot.resource_id then
    raise exception 'catalog raw record resource mismatch' using errcode = '22023';
  end if;
  -- PostgreSQL recomputes the digest from the durable payload, so a stored
  -- hash can never disagree with what sits beside it.
  if encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex') <> v_hash then
    raise exception 'invalid catalog raw record: payload hash does not match the payload' using errcode = '22023';
  end if;

  select * into v_row from public.catalog_raw_records
    where snapshot_id = v_snapshot.id and record_key = v_key;
  if v_row.id is null then
    insert into public.catalog_raw_records
      (snapshot_id, resource_id, upstream_record_id, payload, payload_sha256, record_key)
    values (v_snapshot.id, v_snapshot.resource_id, v_upstream, v_payload, v_hash, v_key)
    on conflict (snapshot_id, record_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      -- The counter advances in the same transaction as the row, so a
      -- snapshot's completeness can never be ahead of or behind its records.
      update public.catalog_source_snapshots
        set stored_record_count = stored_record_count + 1
        where id = v_snapshot.id;
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_raw_records
      where snapshot_id = v_snapshot.id and record_key = v_key;
  end if;

  -- Replay: identical content collapses onto the existing row; different
  -- content under the same identity fails closed.
  if v_row.upstream_record_id is distinct from v_upstream
     or v_row.payload_sha256 is distinct from v_hash
     or v_row.payload is distinct from v_payload then
    raise exception 'catalog raw record idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

create or replace function public.activate_catalog_snapshot_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_activation jsonb
) returns setof public.catalog_source_snapshots
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots;
  v_state text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  v_state := coalesce(nullif(p_activation->>'validation_state', ''), 'complete');
  if v_state not in ('complete', 'failed') then
    raise exception 'invalid catalog snapshot validation state' using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = (p_activation->>'snapshot_id')::uuid for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog snapshot' using errcode = '23503';
  end if;
  -- Replay of an already-decided snapshot: identical decision is a no-op,
  -- a different one fails closed.  An active snapshot is never re-opened.
  if v_snapshot.activated_at is not null then
    if v_state <> 'complete' then
      raise exception 'catalog snapshot activation conflict' using errcode = '22023';
    end if;
    return next v_snapshot;
    return;
  end if;
  if v_state = 'failed' then
    update public.catalog_source_snapshots set validation_state = 'failed'
      where id = v_snapshot.id returning * into v_snapshot;
    return next v_snapshot;
    return;
  end if;
  -- The completeness gate.  A capture holding fewer records than the upstream
  -- declared is NOT a complete snapshot, however honest each record is -- the
  -- R5 Government pagination lesson, enforced here rather than disclosed.
  if v_snapshot.stored_record_count <> v_snapshot.declared_record_count then
    raise exception 'catalog snapshot is incomplete' using errcode = '22023';
  end if;
  update public.catalog_source_snapshots
    set validation_state = 'complete', activated_at = now()
    where id = v_snapshot.id returning * into v_snapshot;
  return next v_snapshot;
end;
$$;

create or replace function public.record_catalog_candidate_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_candidate jsonb
) returns setof public.catalog_candidate_variants
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_candidate_variants;
  v_record public.catalog_raw_records;
  v_key text; v_status text; v_make text; v_model text;
  v_code text; v_trim text; v_dimensions jsonb;
  v_year_start integer; v_year_end integer;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_candidate::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;
  v_key := nullif(p_candidate->>'candidate_key', '');
  v_status := coalesce(nullif(p_candidate->>'status', ''), 'candidate');
  v_make := nullif(p_candidate->>'manufacturer', '');
  v_model := nullif(p_candidate->>'commercial_model', '');
  v_code := nullif(p_candidate->>'official_model_code', '');
  v_trim := nullif(p_candidate->>'trim', '');
  v_dimensions := coalesce(p_candidate->'identity_dimensions', '{}'::jsonb);
  if v_key is null or v_make is null or v_model is null then
    raise exception 'invalid catalog candidate: identity is incomplete' using errcode = '22023';
  end if;
  if v_status not in ('candidate', 'ambiguous', 'rejected', 'ready_for_review') then
    raise exception 'invalid catalog candidate status' using errcode = '22023';
  end if;
  begin
    v_year_start := nullif(p_candidate->>'model_year_start', '')::integer;
    v_year_end := nullif(p_candidate->>'model_year_end', '')::integer;
  exception when others then
    raise exception 'invalid catalog candidate: malformed model year' using errcode = '22023';
  end;
  -- A half-stated range is a guess.  Either the source stated both ends or it
  -- stated no year at all.
  if (v_year_start is null) <> (v_year_end is null) then
    raise exception 'invalid catalog candidate: a model year range must be whole' using errcode = '22023';
  end if;

  select * into v_record from public.catalog_raw_records
    where id = (p_candidate->>'raw_record_id')::uuid;
  if v_record.id is null then
    raise exception 'invalid catalog candidate raw record' using errcode = '23503';
  end if;
  -- Cross-snapshot linkage: a candidate is a reading of ONE record, and it
  -- must be filed under the snapshot that record actually belongs to.
  if nullif(p_candidate->>'snapshot_id', '')::uuid is distinct from v_record.snapshot_id then
    raise exception 'catalog candidate snapshot mismatch' using errcode = '22023';
  end if;

  select * into v_row from public.catalog_candidate_variants
    where snapshot_id = v_record.snapshot_id and candidate_key = v_key;
  if v_row.id is null then
    insert into public.catalog_candidate_variants
      (snapshot_id, raw_record_id, manufacturer, commercial_model, model_year_start,
       model_year_end, official_model_code, trim, identity_dimensions, status, candidate_key)
    values (v_record.snapshot_id, v_record.id, v_make, v_model, v_year_start, v_year_end,
            v_code, v_trim, v_dimensions, v_status, v_key)
    on conflict (snapshot_id, candidate_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_candidate_variants
      where snapshot_id = v_record.snapshot_id and candidate_key = v_key;
  end if;

  -- Replay: the same key must be the same identity.  `status` is excluded on
  -- purpose -- a later record may legitimately move a candidate from
  -- `ambiguous` to `ready_for_review` -- but WHO the candidate is may never
  -- change under a key that already named someone else.
  if v_row.raw_record_id is distinct from v_record.id
     or v_row.manufacturer is distinct from v_make
     or v_row.commercial_model is distinct from v_model
     or v_row.model_year_start is distinct from v_year_start
     or v_row.model_year_end is distinct from v_year_end
     or v_row.official_model_code is distinct from v_code
     or v_row.trim is distinct from v_trim
     or v_row.identity_dimensions is distinct from v_dimensions then
    raise exception 'catalog candidate idempotency conflict' using errcode = '22023';
  end if;
  if v_row.status is distinct from v_status then
    update public.catalog_candidate_variants set status = v_status
      where id = v_row.id returning * into v_row;
  end if;
  return next v_row;
end;
$$;

create or replace function public.link_catalog_candidate_evidence_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_link jsonb
) returns setof public.catalog_candidate_evidence_links
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_candidate_evidence_links;
  v_candidate public.catalog_candidate_variants;
  v_snapshot public.catalog_source_snapshots;
  v_source public.sources;
  v_claim public.claims;
  v_verdict public.claim_verdicts;
  v_key text; v_locator text; v_version text; v_kind text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_link::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;
  v_key := nullif(p_link->>'link_key', '');
  v_locator := nullif(p_link->>'record_locator', '');
  v_version := nullif(p_link->>'source_version', '');
  v_kind := nullif(p_link->>'source_version_kind', '');
  if v_key is null or v_locator is null or v_version is null or v_kind is null then
    raise exception 'invalid catalog evidence link: provenance is incomplete' using errcode = '22023';
  end if;

  select * into v_candidate from public.catalog_candidate_variants
    where id = (p_link->>'candidate_id')::uuid;
  if v_candidate.id is null then
    raise exception 'invalid catalog evidence link candidate' using errcode = '23503';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = v_candidate.snapshot_id;

  -- Cross-RUN linkage: the source must belong to the run holding the lease.
  -- One run may not attach another run's evidence to a candidate.
  select * into v_source from public.sources
    where id = (p_link->>'source_id')::uuid and run_id = p_run_id;
  if v_source.id is null then
    raise exception 'invalid catalog evidence link source' using errcode = '23503';
  end if;

  if nullif(p_link->>'claim_id', '') is not null then
    select * into v_claim from public.claims
      where id = (p_link->>'claim_id')::uuid and run_id = p_run_id;
    if v_claim.id is null then
      raise exception 'invalid catalog evidence link claim' using errcode = '23503';
    end if;
    -- The claim must rest on the source being cited, not merely share a run.
    if v_claim.source_id is distinct from v_source.id then
      raise exception 'catalog evidence link claim source mismatch' using errcode = '22023';
    end if;
  end if;

  if nullif(p_link->>'verdict_id', '') is not null then
    if nullif(p_link->>'claim_id', '') is null then
      raise exception 'catalog evidence link verdict requires its claim' using errcode = '22023';
    end if;
    -- The legacy catalog NEVER verifies a fact.  An unverified snapshot's
    -- candidate may be linked to a source for discovery and comparison, but a
    -- verdict can never be attached to it -- so no amount of replay, and no
    -- future promotion path reading these links, can treat the old catalog as
    -- having confirmed anything.
    if v_snapshot.trust_state <> 'evidence' then
      raise exception 'an unverified catalog source cannot carry a verdict' using errcode = '22023';
    end if;
    select * into v_verdict from public.claim_verdicts
      where id = (p_link->>'verdict_id')::uuid and run_id = p_run_id;
    if v_verdict.id is null then
      raise exception 'invalid catalog evidence link verdict' using errcode = '23503';
    end if;
    if v_verdict.claim_id is distinct from v_claim.id then
      raise exception 'catalog evidence link verdict claim mismatch' using errcode = '22023';
    end if;
  end if;

  select * into v_row from public.catalog_candidate_evidence_links
    where candidate_id = v_candidate.id and link_key = v_key;
  if v_row.id is null then
    insert into public.catalog_candidate_evidence_links
      (candidate_id, snapshot_id, run_id, source_id, claim_id, verdict_id,
       record_locator, source_version, source_version_kind, link_key)
    values (v_candidate.id, v_candidate.snapshot_id, p_run_id, v_source.id,
            nullif(p_link->>'claim_id', '')::uuid, nullif(p_link->>'verdict_id', '')::uuid,
            v_locator, v_version, v_kind, v_key)
    on conflict (candidate_id, link_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_candidate_evidence_links
      where candidate_id = v_candidate.id and link_key = v_key;
  end if;

  -- Replay: the same link must cite the same evidence at the same version and
  -- the same locator.  Re-pointing a link is a new fact, not a retry.
  if v_row.source_id is distinct from v_source.id
     or v_row.claim_id is distinct from nullif(p_link->>'claim_id', '')::uuid
     or v_row.verdict_id is distinct from nullif(p_link->>'verdict_id', '')::uuid
     or v_row.record_locator is distinct from v_locator
     or v_row.source_version is distinct from v_version
     or v_row.source_version_kind is distinct from v_kind then
    raise exception 'catalog evidence link idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- 8. Privileges: service-path only, least privilege, canonical unwritable.
-- ---------------------------------------------------------------------------
--
-- Grants are explicit rather than relying on default privileges or a
-- point-in-time `grant ... on all tables` -- see migrations 20260818000100 /
-- 20260818000200 for why that class of gap must be closed in the migration
-- that introduces the object.
do $$
declare fn text; staging text; canonical text;
begin
  foreach fn in array array[
    -- The pure predicate is listed too: like r3_source_version_valid it is a
    -- constraint helper, not a browser surface, so anon/authenticated lose
    -- the EXECUTE that Supabase default privileges would otherwise grant.
    'public.catalog_identity_dimensions_valid(jsonb)',
    'public.record_catalog_snapshot_guarded(uuid,text,integer,text,jsonb)',
    'public.record_catalog_raw_record_guarded(uuid,text,integer,text,jsonb)',
    'public.activate_catalog_snapshot_guarded(uuid,text,integer,text,jsonb)',
    'public.record_catalog_candidate_guarded(uuid,text,integer,text,jsonb)',
    'public.link_catalog_candidate_evidence_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;

  -- Staging relations: the service path reads and appends, and nothing more.
  -- UPDATE is granted only where a reviewed state transition exists (a
  -- snapshot's completion counters, a candidate's status); the append-only
  -- triggers above constrain exactly which columns those may touch.
  foreach staging in array array[
    'public.catalog_source_snapshots', 'public.catalog_raw_records',
    'public.catalog_candidate_variants', 'public.catalog_candidate_evidence_links'
  ] loop
    execute format('revoke all on table %s from public', staging);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke all on table %s from anon', staging); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke all on table %s from authenticated', staging); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant select, insert on table %s to service_role', staging);
      execute format('revoke delete on table %s from service_role', staging);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname='service_role') then
    -- No UPDATE at all on the two relations that are append-only outright.
    execute 'revoke update on table public.catalog_raw_records from service_role';
    execute 'revoke update on table public.catalog_candidate_evidence_links from service_role';
    -- The two that DO advance (snapshot completion, candidate status) update
    -- only through the guarded RPCs above, which are SECURITY INVOKER and
    -- therefore still bounded by the append-only triggers.
    execute 'grant update on table public.catalog_source_snapshots to service_role';
    execute 'grant update on table public.catalog_candidate_variants to service_role';
  end if;

  -- The canonical catalog: READ ONLY for every role in PR1.
  --
  -- This is what makes "the canonical catalog starts empty and stays empty"
  -- a property of the database rather than a claim about the code.  Canonical
  -- promotion is PR3's work, and PR3 will grant the INSERT it needs in its
  -- own reviewed migration -- so no staging write path added here, and no
  -- backend release, can create a canonical row by accident or by design.
  foreach canonical in array array[
    'public.catalog_models', 'public.catalog_model_variants'
  ] loop
    execute format('revoke all on table %s from public', canonical);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke all on table %s from anon', canonical); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke all on table %s from authenticated', canonical); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant select on table %s to service_role', canonical);
      execute format('revoke insert, update, delete on table %s from service_role', canonical);
    end if;
  end loop;
end $$;
