-- Catalog PR1 corrective round: make stored provenance TRUTHFUL.
--
-- Forward-only. The merged migration
-- `20260914200000_catalog_evidence_foundation.sql` is not rewritten; this file
-- tightens what it established, on relations that are still empty, before any
-- Government ingestion can come to rely on them.
--
-- What was wrong
-- --------------
--
-- PR1 validated provenance SYNTACTICALLY and stored it VERBATIM. A link's
-- `record_locator` had to parse (`r3_canonical_locator`) and its
-- `source_version` had to be shaped like a version (`r3_source_version_valid`),
-- and that was all. Neither was ever compared against the claim and the source
-- the link cited. Observed on the merged schema, one accepted row:
--
--     verdict_id       -> a verdict whose own value is 'rejected'
--     record_locator   -> ["document_span","doc-1",[],"Other section",900,950]
--       while the cited claim's evidence_locator is
--                         ["record_field","rec-1",["engine_displacement_cc"],...]
--     source_version   -> 2099.12.31
--       while the cited source was captured at dataset_version 2026.08.1
--
-- A field that is merely well-formed is not provenance. Everything below turns
-- those three fields from things a caller STATES into things the database
-- DERIVES, and makes a stated one an assertion that is checked.
--
-- Four more gaps, same round:
--
--   * a `failed` snapshot was not terminal: `activated_at is not null` was the
--     only append gate, so a failed capture kept taking records and could then
--     be activated `complete` (observed: complete|2|t);
--   * any leased run could fill and activate a snapshot another run opened,
--     leaving `created_by_run_id` naming a run that did not do the work;
--   * the raw-record digest had to be predicted by the caller from
--     PostgreSQL's incidental `jsonb::text` rendering -- a formatting
--     coincidence, not a security property;
--   * a canonical row's FACTS could be rewritten while the single row-level
--     `promoted_from_verdict_id` stayed attached, so a row could state values
--     that its verdict had never seen.
--
-- What this migration does NOT do: it does not ingest anything, does not
-- populate the canonical tables, does not register a tool, and does not
-- implement promotion. The canonical catalog is still empty and still
-- unwritable, and is now immutable outright (see §6).

-- ---------------------------------------------------------------------------
-- 1. Identity keys are STRUCTURAL, and the schema says so.
-- ---------------------------------------------------------------------------
--
-- PR1 let the caller name every durable object, so the same logical object
-- could be stored twice under two names and an idempotency identity could come
-- from anything -- including a generated string. Keys are now derived by the
-- domain-separated builders in `backend/catalog/keys.py`, and the database
-- enforces the two halves of that it can check cheaply and without ever
-- depending on one language's JSON formatting:
--
--   * the SHAPE and DOMAIN of a key (below), so free text and a key from the
--     wrong domain are both refused here and not only in the backend;
--   * the NATURAL identity of the row (§2), so even a wrong key cannot
--     duplicate a logical object.
--
-- The full derivation is checked in the repository, where the builder lives.
-- Reproducing that digest in SQL would mean reproducing Python's exact JSON
-- rendering in SQL -- precisely the brittleness §4 removes.

-- Every `add constraint` below is preceded by a drop of BOTH the PR1 name it
-- replaces and its own, so replaying this migration -- or replaying the whole
-- ordered catalog set, which is what rerun safety actually means here -- is a
-- no-op rather than a duplicate-object error.
alter table public.catalog_source_snapshots
  drop constraint if exists catalog_source_snapshots_key_shape,
  drop constraint if exists catalog_source_snapshots_key_derived,
  add constraint catalog_source_snapshots_key_derived
    check (snapshot_key ~ '^cs1\.[0-9a-f]{32}$');

alter table public.catalog_raw_records
  drop constraint if exists catalog_raw_records_key_shape,
  drop constraint if exists catalog_raw_records_key_derived,
  add constraint catalog_raw_records_key_derived
    check (record_key ~ '^cr1\.[0-9a-f]{32}$');

alter table public.catalog_candidate_variants
  drop constraint if exists catalog_candidate_variants_key_shape,
  drop constraint if exists catalog_candidate_variants_key_derived,
  add constraint catalog_candidate_variants_key_derived
    check (candidate_key ~ '^cc1\.[0-9a-f]{32}$');

alter table public.catalog_candidate_evidence_links
  drop constraint if exists catalog_candidate_evidence_links_key_shape,
  drop constraint if exists catalog_candidate_evidence_links_key_derived,
  add constraint catalog_candidate_evidence_links_key_derived
    check (link_key ~ '^cl1\.[0-9a-f]{32}$');

-- ---------------------------------------------------------------------------
-- 2. Natural uniqueness: a rename cannot duplicate a logical identity.
-- ---------------------------------------------------------------------------

-- One retrieval of one resource at one version with one content identity.
-- Two captures that returned different bytes are two snapshots; two that
-- returned the same bytes at the same version are one, whatever they are
-- called.
create unique index if not exists catalog_source_snapshots_natural_uidx
  on public.catalog_source_snapshots
     (source_family, resource_id, upstream_version_kind, upstream_version, content_sha256);

-- One READING of one raw record. The nullable identity columns are collapsed
-- with sentinels a real value can never take (`-1` is outside the 1900-2100
-- year range, and '' is refused by the exactness constraints), so two
-- identical readings collide instead of both being stored.
create unique index if not exists catalog_candidate_variants_natural_uidx
  on public.catalog_candidate_variants
     (raw_record_id, manufacturer, commercial_model,
      coalesce(model_year_start, -1), coalesce(model_year_end, -1),
      coalesce(official_model_code, ''), coalesce(trim, ''), identity_dimensions);

-- One piece of evidence cited once for one candidate. `verdict_id` is part of
-- the identity because a discovery link and the verification of the same claim
-- are two different statements, and both are worth keeping.
create unique index if not exists catalog_candidate_evidence_links_natural_uidx
  on public.catalog_candidate_evidence_links
     (candidate_id, claim_id, coalesce(verdict_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- ---------------------------------------------------------------------------
-- 3. Cross-table identity, enforced STRUCTURALLY.
-- ---------------------------------------------------------------------------
--
-- PR1 checked "this candidate's snapshot is its record's snapshot" inside one
-- RPC. A check that lives only in a function holds only for callers who go
-- through that function; `service_role` holds direct DML on these tables
-- (§7), so the invariant needs to be in the schema.

-- Order matters on a rerun: the composite foreign keys DEPEND on the unique
-- constraints they reference, so the dependants are dropped first and re-added
-- last. Written as explicit steps rather than one `drop ... , add ...` per
-- table for exactly that reason.
alter table public.catalog_candidate_variants
  drop constraint if exists catalog_candidate_variants_record_snapshot_fk;
alter table public.catalog_candidate_evidence_links
  drop constraint if exists catalog_candidate_evidence_links_candidate_snapshot_fk;

alter table public.catalog_raw_records
  drop constraint if exists catalog_raw_records_id_snapshot_uniq,
  add constraint catalog_raw_records_id_snapshot_uniq unique (id, snapshot_id);
alter table public.catalog_candidate_variants
  drop constraint if exists catalog_candidate_variants_id_snapshot_uniq,
  add constraint catalog_candidate_variants_id_snapshot_uniq unique (id, snapshot_id);

-- A candidate's snapshot must BE its raw record's snapshot -- not merely equal
-- it at the moment some function looked.
alter table public.catalog_candidate_variants
  add constraint catalog_candidate_variants_record_snapshot_fk
    foreign key (raw_record_id, snapshot_id)
    references public.catalog_raw_records(id, snapshot_id) on delete restrict;

-- A link's snapshot must BE its candidate's snapshot.
alter table public.catalog_candidate_evidence_links
  add constraint catalog_candidate_evidence_links_candidate_snapshot_fk
    foreign key (candidate_id, snapshot_id)
    references public.catalog_candidate_variants(id, snapshot_id) on delete restrict;

-- Every referencing column that is not already the leading column of an index
-- gets one. A foreign key without a supporting index makes the referenced
-- side's delete-check a sequential scan and hides the join cost until the
-- table is large.
create index if not exists catalog_source_snapshots_run_idx
  on public.catalog_source_snapshots(created_by_run_id);
create index if not exists catalog_candidate_variants_record_idx
  on public.catalog_candidate_variants(raw_record_id);
create index if not exists catalog_candidate_evidence_links_snapshot_idx
  on public.catalog_candidate_evidence_links(snapshot_id);
create index if not exists catalog_candidate_evidence_links_source_idx
  on public.catalog_candidate_evidence_links(source_id);
create index if not exists catalog_candidate_evidence_links_claim_idx
  on public.catalog_candidate_evidence_links(claim_id);
create index if not exists catalog_candidate_evidence_links_verdict_idx
  on public.catalog_candidate_evidence_links(verdict_id);
create index if not exists catalog_model_variants_candidate_idx
  on public.catalog_model_variants(promoted_from_candidate_id);
create index if not exists catalog_model_variants_verdict_idx
  on public.catalog_model_variants(promoted_from_verdict_id);

-- ---------------------------------------------------------------------------
-- 4. An evidence link must cite a real claim.
-- ---------------------------------------------------------------------------
--
-- PR1 allowed a source-only link that still carried a `record_locator` and a
-- `source_version` -- fields that assert WHERE a fact was read and at WHICH
-- version, with nothing to check them against. Requiring the claim is what
-- makes every link's provenance derivable, and therefore checkable.
--
-- Discovery is unaffected in substance: a legacy-reference row that suggests a
-- candidate is a CLAIM from that source, and it simply never acquires a
-- verdict. What is gone is the shape in which a link asserted exact fact
-- provenance with no fact behind it.
alter table public.catalog_candidate_evidence_links
  alter column claim_id set not null;

-- ---------------------------------------------------------------------------
-- 5. Snapshot lifecycle: pending -> (active-complete | failed), both terminal.
-- ---------------------------------------------------------------------------

alter table public.catalog_source_snapshots
  drop constraint if exists catalog_source_snapshots_failed_is_never_active,
  add constraint catalog_source_snapshots_failed_is_never_active
    check (validation_state <> 'failed' or activated_at is null);

-- Replaces the PR1 trigger function of the same name. A snapshot may still
-- advance its completion counters and its decision while PENDING; once it is
-- decided -- active or failed -- it is frozen. `failed` is terminal: it is the
-- record that a capture was found unusable, and a capture cannot become
-- trustworthy later by being written to again.
create or replace function public.forbid_catalog_snapshot_rewrite() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'catalog source material is append-only';
  end if;
  if old.validation_state = 'failed' then
    raise exception 'a failed catalog snapshot is terminal';
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
  if new.stored_record_count < old.stored_record_count then
    raise exception 'catalog snapshot record count cannot decrease';
  end if;
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Canonical rows are immutable outright until PR3 adds field provenance.
-- ---------------------------------------------------------------------------
--
-- WHY THIS IS NOT MERELY CAUTIOUS. `catalog_model_variants` carries ONE
-- `promoted_from_candidate_id` and ONE `promoted_from_verdict_id` for a row of
-- several independent facts -- the year range, the model code, the trim and
-- every stated identity dimension. Row-level provenance cannot verify a
-- multi-field row: a verdict that confirmed the drivetrain says nothing about
-- the model year beside it.
--
-- PR1's trigger froze identity and provenance but allowed the FACTUAL columns
-- to be rewritten as long as the revision counter advanced. That is exactly
-- the shape in which a row ends up stating values its attached verdict never
-- saw -- and the advancing counter made it look reviewed.
--
-- So there is no update path at all. PR3 MUST add FIELD-LEVEL, append-only
-- revision provenance -- one provenance row per fact, not one per canonical
-- row -- before any insert is enabled; a row-level FK is not sufficient and
-- must not be treated as if it were.
create or replace function public.forbid_canonical_identity_rewrite() returns trigger
language plpgsql as $$
begin
  raise exception 'canonical catalog rows are immutable';
end;
$$;

drop trigger if exists catalog_models_identity_immutable on public.catalog_models;
create trigger catalog_models_identity_immutable
  before update or delete on public.catalog_models
  for each row execute function public.forbid_canonical_identity_rewrite();

drop trigger if exists catalog_model_variants_identity_immutable on public.catalog_model_variants;
create trigger catalog_model_variants_identity_immutable
  before update or delete on public.catalog_model_variants
  for each row execute function public.forbid_canonical_identity_rewrite();

-- ---------------------------------------------------------------------------
-- 7. The corrected write paths.
-- ---------------------------------------------------------------------------
--
-- These replace the PR1 functions of the same names and signatures, so nothing
-- that calls them changes. The lease posture is unchanged --
-- `assert_worker_lease` is still the first statement of every one -- and every
-- PR1 refusal still holds. What is added is referential truth.
--
-- A note on the boundary, stated exactly: these are SECURITY INVOKER, and
-- `service_role` retains direct DML on the staging tables. So the accurate
-- claim is that the REPOSITORY'S CATALOG WRITE PATH goes through these RPCs --
-- not that they are the only way to write these tables from the database.
-- Constraints and triggers, which hold for every writer, carry the invariants
-- that must not depend on which path was used; see §1, §2, §3, §5 and §6.

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
  -- own field names happen to collide. What bounds it instead is structural:
  -- object shape, the durable size bound, and a digest this function derives.
  v_key := nullif(p_record->>'record_key', '');
  v_upstream := nullif(p_record->>'upstream_record_id', '');
  v_payload := p_record->'payload';
  if v_key is null or v_upstream is null
     or v_payload is null or jsonb_typeof(v_payload) <> 'object' then
    raise exception 'invalid catalog raw record: identity or payload is missing' using errcode = '22023';
  end if;
  -- The digest is DERIVED, never supplied. PR1 compared a caller's hash
  -- against `jsonb::text`, which made every ingestion path responsible for
  -- reproducing PostgreSQL's key ordering and separator style exactly -- a
  -- formatting coincidence dressed up as an integrity check. A caller that
  -- still sends one is refused rather than silently ignored, so nothing
  -- depends on a value that is no longer read.
  if p_record ? 'payload_sha256' then
    raise exception 'catalog raw record payload digest is derived, not supplied' using errcode = '22023';
  end if;
  if char_length(v_payload::text) > 16384 then
    raise exception 'invalid catalog raw record: payload exceeds the durable bound' using errcode = '22023';
  end if;
  v_hash := encode(sha256(convert_to(v_payload::text, 'UTF8')), 'hex');

  -- The owning snapshot is locked FOR UPDATE, so the stored-record counter
  -- below is exact under concurrency. Lock order is unchanged: public.runs
  -- (FOR SHARE, inside assert_worker_lease) and only then the object written.
  select * into v_snapshot from public.catalog_source_snapshots
    where id = (p_record->>'snapshot_id')::uuid for update;
  if v_snapshot.id is null then
    raise exception 'invalid catalog raw record snapshot' using errcode = '23503';
  end if;
  -- A snapshot belongs to the run that opened it. PR1 accepted any valid
  -- lease, so a concurrently leased run could fill a capture it did not open
  -- while `created_by_run_id` kept naming the first run.
  if v_snapshot.created_by_run_id is distinct from p_run_id then
    raise exception 'this catalog snapshot does not belong to this run' using errcode = '22023';
  end if;
  -- Both decided states are terminal. PR1 gated on `activated_at` alone, so a
  -- FAILED capture kept accepting records.
  if v_snapshot.validation_state = 'failed' then
    raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
  end if;
  if v_snapshot.activated_at is not null then
    raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
  end if;
  if nullif(p_record->>'resource_id', '') is distinct from v_snapshot.resource_id then
    raise exception 'catalog raw record resource mismatch' using errcode = '22023';
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
  -- Only the creating run decides its own capture.
  if v_snapshot.created_by_run_id is distinct from p_run_id then
    raise exception 'this catalog snapshot does not belong to this run' using errcode = '22023';
  end if;

  -- `failed` is TERMINAL. Re-declaring the same failure is an idempotent
  -- no-op; anything else is refused, so a capture found unusable can never
  -- become complete afterwards.
  if v_snapshot.validation_state = 'failed' then
    if v_state <> 'failed' then
      raise exception 'a failed catalog snapshot is terminal' using errcode = '22023';
    end if;
    return next v_snapshot;
    return;
  end if;
  -- An active snapshot is equally settled: replaying `complete` is a no-op,
  -- and it can never be failed after the fact.
  if v_snapshot.activated_at is not null then
    if v_state <> 'complete' then
      raise exception 'an active catalog snapshot is immutable' using errcode = '22023';
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
  -- The completeness gate: a capture holding fewer records than the upstream
  -- declared is not a complete snapshot -- the R5 Government pagination lesson.
  if v_snapshot.stored_record_count <> v_snapshot.declared_record_count then
    raise exception 'catalog snapshot is incomplete' using errcode = '22023';
  end if;
  update public.catalog_source_snapshots
    set validation_state = 'complete', activated_at = now()
    where id = v_snapshot.id returning * into v_snapshot;
  return next v_snapshot;
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
  v_verdict_id uuid;
  v_key text; v_locator text; v_version text; v_kind text;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_link::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;
  v_key := nullif(p_link->>'link_key', '');
  if v_key is null then
    raise exception 'invalid catalog evidence link: provenance is incomplete' using errcode = '22023';
  end if;

  select * into v_candidate from public.catalog_candidate_variants
    where id = (p_link->>'candidate_id')::uuid;
  if v_candidate.id is null then
    raise exception 'invalid catalog evidence link candidate' using errcode = '23503';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
    where id = v_candidate.snapshot_id;

  -- Cross-RUN linkage: the cited source must belong to the run holding the
  -- lease. A LATER run may add evidence to an existing candidate -- that is
  -- deliberate, and separately tested -- but it may only ever cite evidence
  -- of its own.
  select * into v_source from public.sources
    where id = (p_link->>'source_id')::uuid and run_id = p_run_id;
  if v_source.id is null then
    raise exception 'invalid catalog evidence link source' using errcode = '23503';
  end if;

  -- A link must cite a real CLAIM. Without one there is nothing for the
  -- locator to be the locator OF, and PR1's source-only link therefore stored
  -- exact-fact provenance it could not support.
  if nullif(p_link->>'claim_id', '') is null then
    raise exception 'a catalog evidence link requires the claim it is evidence for' using errcode = '22023';
  end if;
  select * into v_claim from public.claims
    where id = (p_link->>'claim_id')::uuid and run_id = p_run_id;
  if v_claim.id is null then
    raise exception 'invalid catalog evidence link claim' using errcode = '23503';
  end if;
  -- The claim must rest on the source being cited, not merely share a run.
  if v_claim.source_id is distinct from v_source.id then
    raise exception 'catalog evidence link claim source mismatch' using errcode = '22023';
  end if;

  -- Provenance is DERIVED from the evidence, never taken from the caller.
  v_locator := v_claim.evidence_locator;
  v_kind := v_source.source_version_kind;
  v_version := v_source.source_version_id;
  if v_locator is null or btrim(v_locator) = '' then
    raise exception 'the cited claim states no evidence locator' using errcode = '22023';
  end if;
  if v_kind is null or btrim(v_kind) = '' or v_version is null or btrim(v_version) = '' then
    raise exception 'the cited source states no version to pin this link to' using errcode = '22023';
  end if;
  -- A caller MAY state them, and is then held to them. Stating a different
  -- locator or version is a factual disagreement with the evidence, not a
  -- formatting preference, so it fails closed instead of being overwritten.
  if nullif(p_link->>'record_locator', '') is not null
     and p_link->>'record_locator' is distinct from v_locator then
    raise exception 'catalog evidence link record locator does not match the cited claim' using errcode = '22023';
  end if;
  if (nullif(p_link->>'source_version', '') is not null
      and p_link->>'source_version' is distinct from v_version)
     or (nullif(p_link->>'source_version_kind', '') is not null
         and p_link->>'source_version_kind' is distinct from v_kind) then
    raise exception 'catalog evidence link source version does not match the cited source' using errcode = '22023';
  end if;

  v_verdict_id := nullif(p_link->>'verdict_id', '')::uuid;
  if v_verdict_id is not null then
    -- The legacy catalog NEVER verifies a fact.
    if v_snapshot.trust_state <> 'evidence' then
      raise exception 'an unverified catalog source cannot carry a verdict' using errcode = '22023';
    end if;
    select * into v_verdict from public.claim_verdicts
      where id = v_verdict_id and run_id = p_run_id;
    if v_verdict.id is null then
      raise exception 'invalid catalog evidence link verdict' using errcode = '23503';
    end if;
    if v_verdict.claim_id is distinct from v_claim.id then
      raise exception 'catalog evidence link verdict claim mismatch' using errcode = '22023';
    end if;
    -- What the verdict SAYS, not merely that it exists. PR1 accepted a
    -- `needs_review` or `rejected` verdict as backing, so a link could assert
    -- the opposite of what the verifier concluded.
    if v_verdict.verdict is distinct from 'verified' then
      raise exception 'catalog evidence link verdict is not verified' using errcode = '22023';
    end if;
  end if;

  select * into v_row from public.catalog_candidate_evidence_links
    where candidate_id = v_candidate.id and link_key = v_key;
  if v_row.id is null then
    insert into public.catalog_candidate_evidence_links
      (candidate_id, snapshot_id, run_id, source_id, claim_id, verdict_id,
       record_locator, source_version, source_version_kind, link_key)
    values (v_candidate.id, v_candidate.snapshot_id, p_run_id, v_source.id,
            v_claim.id, v_verdict_id, v_locator, v_version, v_kind, v_key)
    on conflict (candidate_id, link_key) do nothing
    returning * into v_row;
    if v_row.id is not null then
      return next v_row;
      return;
    end if;
    select * into v_row from public.catalog_candidate_evidence_links
      where candidate_id = v_candidate.id and link_key = v_key;
  end if;

  -- Replay: the same link must cite the same evidence. The locator and the
  -- version are derived, so they can only differ here if the underlying claim
  -- or source changed -- which is itself a conflict, not a retry.
  if v_row.source_id is distinct from v_source.id
     or v_row.claim_id is distinct from v_claim.id
     or v_row.verdict_id is distinct from v_verdict_id
     or v_row.record_locator is distinct from v_locator
     or v_row.source_version is distinct from v_version
     or v_row.source_version_kind is distinct from v_kind then
    raise exception 'catalog evidence link idempotency conflict' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;


-- ---------------------------------------------------------------------------
-- 8. A verdict REPLAY may not change the durable support set.
-- ---------------------------------------------------------------------------
--
-- `record_claim_verdict_guarded` (from
-- `20260907000100_r4_deterministic_verification.sql`) says, in its own
-- comment, that "the stored link set must be EXACTLY what was cited: a replay
-- that dropped or added evidence is a contract failure". It caught DROPPED and
-- missed ADDED.
--
-- The reason is the shape of the check. It inserted every cited link first --
-- `on conflict (verdict_id, fragment_id) do nothing` -- and only then compared
--
--     count(*) from claim_verdict_supports where verdict_id = v_row.id
--
-- against `jsonb_array_length(v_support)`. That count is the count of the
-- UNION of the already-stored set and the newly-cited set. When the cited set
-- is a strict SUPERSET of the stored one the union equals the cited set, the
-- counts match, and the call succeeds -- having already inserted the extra
-- link. Observed on the merged schema: a verdict stored citing fragment A,
-- replayed citing [A, B], returned the SAME verdict id with its stored support
-- silently grown to {A, B}.
--
-- So a replay could mutate the durable evidence a stored verdict rests on,
-- which is exactly what an idempotent write must never do.
--
-- The corrected function keeps every message and every lineage rule and
-- changes only the set comparison:
--
--   * the cited links are validated for lineage BEFORE anything is written;
--   * a cited list with a repeated fragment is not a set, and is refused
--     whether it is the first write or a replay;
--   * on a REPLAY nothing is inserted at all -- the stored set and the cited
--     set are compared, and any difference (added, removed or replaced) is
--     refused, so a refused replay leaves the stored support untouched;
--   * input ORDER remains irrelevant: support is stored as relational rows,
--     so set membership and cardinality are the identity, not sequence.
--
-- Forward-only: this replaces the function body in place. The
-- `claim_verdicts` / `claim_verdict_supports` relations are unchanged and no
-- row is written by this migration.
--
-- THE COMPATIBILITY BOUNDARY, stated exactly. This is NOT a pure widening:
--
--   * a valid FIRST write is still accepted, unchanged;
--   * an EXACT replay -- same identity, same support set, in any order -- is
--     still accepted and still returns the same row;
--   * a replay that MUTATED the stored support set is now REFUSED. Adding
--     evidence to an existing verdict used to succeed (see above), and that
--     acceptance is deliberately withdrawn: it let an idempotent write change
--     the durable evidence a stored verdict rests on.
--
-- Nothing depends on the withdrawn behaviour today -- these relations are
-- empty on this branch and no caller adds support on replay -- but the change
-- is a rejection where there was an acceptance, and is recorded as one.

create or replace function public.record_claim_verdict_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_verdict jsonb
) returns setof public.claim_verdicts
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.claim_verdicts;
  v_claim public.claims;
  v_key text; v_verdict text; v_reason text; v_mode text; v_contract text;
  v_support jsonb; v_link jsonb; v_fragment public.source_evidence_fragments;
  v_source public.sources;
  v_count integer; v_cited integer; v_replay boolean;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_verdict::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_verdict::text) like '%secret sentinel%'
     or lower(p_verdict::text) like '%chain of thought%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_verdict->>'evidence_key', '') is null then
    raise exception 'invalid claim verdict: evidence_key is required' using errcode = '22023';
  end if;
  v_key := p_verdict->>'evidence_key';
  v_verdict := p_verdict->>'verdict';
  v_reason := p_verdict->>'reason';
  v_mode := p_verdict->>'verification_mode';
  v_contract := p_verdict->>'verifier_contract_version';
  v_support := coalesce(p_verdict->'support', '[]'::jsonb);
  if jsonb_typeof(v_support) <> 'array' then
    raise exception 'invalid claim verdict: support must be an array' using errcode = '22023';
  end if;
  -- The per-source durable fragment bound is also the bound on how much
  -- evidence one verdict may cite: a verdict rests on fragments of ONE source.
  if jsonb_array_length(v_support) > 4 then
    raise exception 'claim verdict cites more evidence than a source can hold' using errcode = '22023';
  end if;

  -- Claim-bound only, and same-run only.
  select * into v_claim from public.claims
    where id = (p_verdict->>'claim_id')::uuid and run_id = p_run_id;
  if v_claim.id is null then
    raise exception 'invalid claim verdict claim' using errcode = '23503';
  end if;
  -- A locally settled verdict compares no evidence, so it may cite none.
  if v_mode = 'deterministic_local' and jsonb_array_length(v_support) > 0 then
    raise exception 'a locally settled verdict cites no evidence' using errcode = '22023';
  end if;
  -- An ACCEPTED verdict must be bound to durable evidence. This is the durable
  -- half of "forged, cross-source or missing support links fail closed".
  if v_verdict = 'verified' and jsonb_array_length(v_support) = 0 then
    raise exception 'an accepted verdict must cite durable evidence' using errcode = '22023';
  end if;

  select * into v_source from public.sources
    where id = v_claim.source_id and run_id = p_run_id;
  if v_source.id is null then
    raise exception 'invalid claim verdict source' using errcode = '23503';
  end if;

  -- Every cited link is validated BEFORE anything is written, so a forged or
  -- cross-source citation can never reach the stored set.
  for v_link in select value from jsonb_array_elements(v_support) as entry(value) loop
    select * into v_fragment from public.source_evidence_fragments
      where id = (v_link->>'fragment_id')::uuid and run_id = p_run_id;
    if v_fragment.id is null then
      -- A support link naming no durable fragment of this run is forged.
      raise exception 'verdict support link does not name durable evidence' using errcode = '23503';
    end if;
    -- The lineage every support link must satisfy: the evidence must belong to
    -- the CLAIM'S OWN source, and to the same task that captured that source.
    -- Same run is deliberately not enough.
    if v_fragment.source_id is distinct from v_claim.source_id then
      raise exception 'verdict support link belongs to another source' using errcode = '22023';
    end if;
    if v_fragment.task_key is distinct from v_source.task_key then
      raise exception 'verdict support link task provenance mismatch' using errcode = '22023';
    end if;
    if v_fragment.content_hash is distinct from (v_link->>'content_hash') then
      raise exception 'verdict support link content hash mismatch' using errcode = '22023';
    end if;
    if v_fragment.locator_key is distinct from nullif(v_link->>'locator_key', '') then
      raise exception 'verdict support link locator mismatch' using errcode = '22023';
    end if;
  end loop;

  -- Support is a SET. A list naming one fragment twice cites one fragment, so
  -- it can never produce two stored rows -- it is a caller bug, not a set.
  select count(distinct (value->>'fragment_id')::uuid) into v_cited
    from jsonb_array_elements(v_support) as entry(value);
  if v_cited is distinct from jsonb_array_length(v_support) then
    raise exception 'verdict support links do not match the cited evidence' using errcode = '22023';
  end if;

  insert into public.claim_verdicts
    (run_id, claim_id, evidence_key, verdict, reason, verification_mode,
     verifier_contract_version)
  values (p_run_id, v_claim.id, v_key, v_verdict, v_reason, v_mode, v_contract)
  on conflict (run_id, evidence_key) do nothing
  returning * into v_row;
  v_replay := v_row.id is null;

  if v_replay then
    -- Exact replay: the row already exists. ONE replay invariant -- an
    -- existing row may only be returned when it is the SAME logical verdict.
    -- Reusing an evidence_key for a different claim or decision is a caller
    -- bug, never a silent no-op. The error carries no stored evidence.
    select * into v_row from public.claim_verdicts
      where run_id = p_run_id and evidence_key = v_key;
    if v_row.claim_id is distinct from v_claim.id
       or v_row.verdict is distinct from v_verdict
       or v_row.reason is distinct from v_reason
       or v_row.verification_mode is distinct from v_mode
       or v_row.verifier_contract_version is distinct from v_contract then
      raise exception 'claim verdict idempotency conflict' using errcode = '22023';
    end if;

    -- A REPLAY WRITES NO SUPPORT. The stored set must already BE the cited
    -- set: equal cardinality and every cited link present. Added, removed and
    -- replaced evidence all fail here, and because nothing was inserted first,
    -- a refused replay leaves the stored support exactly as it was. Order is
    -- not compared -- these are relational rows, not a sequence.
    select count(*) into v_count from public.claim_verdict_supports
      where verdict_id = v_row.id;
    if v_count is distinct from jsonb_array_length(v_support)
       or exists (select 1 from jsonb_array_elements(v_support) as entry(value)
                    where not exists (
                      select 1 from public.claim_verdict_supports s
                        where s.verdict_id = v_row.id
                          and s.fragment_id = (entry.value->>'fragment_id')::uuid)) then
      raise exception 'verdict support links do not match the cited evidence' using errcode = '22023';
    end if;
    return next v_row;
    return;
  end if;

  -- A NEW verdict: store exactly the cited set.
  for v_link in select value from jsonb_array_elements(v_support) as entry(value) loop
    select * into v_fragment from public.source_evidence_fragments
      where id = (v_link->>'fragment_id')::uuid and run_id = p_run_id;
    insert into public.claim_verdict_supports
      (run_id, verdict_id, fragment_id, content_hash, locator_key)
    values (p_run_id, v_row.id, v_fragment.id, v_fragment.content_hash,
            v_fragment.locator_key)
    on conflict (verdict_id, fragment_id) do nothing;
  end loop;

  -- The stored link set must be EXACTLY what was cited.
  select count(*) into v_count from public.claim_verdict_supports
    where verdict_id = v_row.id;
  if v_count <> jsonb_array_length(v_support) then
    raise exception 'verdict support links do not match the cited evidence' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;
