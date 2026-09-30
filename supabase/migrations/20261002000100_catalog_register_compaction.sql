-- PR-L2: the register stays in the database WITHOUT its raw payload, and a
-- compacted candidate without the identity its variant row already holds.
--
-- Every register snapshot has an immutable, sha256-verified Cloud Storage
-- archive with one line per record (PR-D1), and its typed variant rows hold
-- every level-1 and level-1.5 field (PR-L1 / L1b). Once both are in place the
-- stored `catalog_raw_records.payload` is redundant (the largest single cost
-- of a register row), and so are a candidate's identity columns: the variant
-- row states the same tozar, kinuy_mishari, shnat_yitzur, degem_nm,
-- ramat_gimur and the four PR-V dimensions.
--
-- What it changes
-- ---------------
--
--   * compact_register_snapshot(snapshot_key, apply) -- the ONE place the
--     append-only triggers are suspended for this (security definer,
--     search_path pinned, service_role only), like the prunes. Two modes:
--
--     ACTIVE (the tozar's rank-1 snapshot; "variants" readers). It refuses,
--     with a static code and without writing, unless: the snapshot is an
--     activated whole-tozar Government snapshot; its archive is recorded with
--     sha256, byte size and one line per stored row; its capture is
--     count-verified (the newest register unit) or, with no unit, stored =
--     declared; its build under the CURRENT mapper is complete over every
--     row; every row's variant reads exactly as its payload
--     (catalog_variant_reads_as_payload) AND every candidate's identity reads
--     exactly as its variant (catalog_variant_candidate_reading). Then every
--     payload is NULL and every candidate keeps only its keys (id, snapshot,
--     raw record, candidate_key, status): its identity columns are NULL.
--
--     SUPERSEDED (any older activated snapshot of the tozar; "archive"
--     readers). Once the tozar's active snapshot is built, and nothing live
--     can read the old one (no live run, capture or preparation on it, no
--     startable batch, no open reservation), the old snapshot keeps only the
--     rows something references (a candidate named by a queue item, evidence
--     link, field provenance, promotion or reservation, and its raw record),
--     as skeletons -- payload and identity NULL -- and drops every other row
--     and all its variants. Its archive (written from the stored rows first
--     when it has none) is then the record: `catalog_readable_snapshot`
--     refuses it (CATALOG_SNAPSHOT_ARCHIVED), and the original record of a
--     referenced row is read from the archive, sha256-verified.
--
--   * Readers: the payload facts through catalog_raw_record_code /
--     catalog_raw_record_content_sha256, the candidate identity through the
--     view catalog_candidate_variants_resolved (the table's own columns, the
--     compacted ones filled from the variant row), each reader restated with
--     only those expressions changed. The partial identity indexes skip the
--     compacted rows.
--   * catalog_register_snapshot_compactions: one append-only row per
--     compacted snapshot; history that outlives a prune (no foreign key).
--   * A new raw record still needs its payload, a new candidate its
--     manufacturer and model (before-insert triggers), and no variant is
--     written for a compacted snapshot.
--   * The release read-only role may VACUUM (MAINTAIN, PostgreSQL 17) the two
--     tables compaction shrinks: the operator's space reclamation.
--
-- A compacted snapshot is not rebuilt under a new mapper version: a mapper
-- bump is refused until a rebuild-from-archive exists (the deployed gate's
-- CATALOG_COMPACTION_MAPPER fact; tests/test_register_compaction.py).
-- Forward-only and rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The payload may go; a new record still needs one.
-- ---------------------------------------------------------------------------
alter table public.catalog_raw_records alter column payload drop not null;

create or replace function public.catalog_raw_record_payload_required() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.payload is null then
    raise exception 'CATALOG_RAW_RECORD_PAYLOAD_REQUIRED: a raw record is stored with its payload'
      using errcode = '23502';
  end if;
  return new;
end;
$$;
drop trigger if exists catalog_raw_records_payload_required on public.catalog_raw_records;
create trigger catalog_raw_records_payload_required
  before insert on public.catalog_raw_records
  for each row execute function public.catalog_raw_record_payload_required();

-- ---------------------------------------------------------------------------
-- 1b. A compacted candidate keeps its keys; a new one still states its identity.
-- ---------------------------------------------------------------------------
alter table public.catalog_candidate_variants alter column manufacturer drop not null,
  alter column commercial_model drop not null;

create or replace function public.catalog_candidate_identity_required() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.manufacturer is null or new.commercial_model is null then
    raise exception 'CATALOG_CANDIDATE_IDENTITY_REQUIRED: a candidate is stored with its manufacturer and model'
      using errcode = '23502';
  end if;
  return new;
end;
$$;
drop trigger if exists catalog_candidate_variants_identity_required on public.catalog_candidate_variants;
create trigger catalog_candidate_variants_identity_required
  before insert on public.catalog_candidate_variants
  for each row execute function public.catalog_candidate_identity_required();

-- The identity indexes hold the rows that still state their identity (every
-- new row does): a compacted candidate is read through its snapshot and its
-- variant row, never looked up by name. Rebuilt once, partial.
do $$
begin
  if not exists (select 1 from pg_indexes where schemaname = 'public'
                    and indexname = 'catalog_candidate_variants_natural_uidx'
                    and indexdef like '%WHERE (manufacturer IS NOT NULL)%') then
    drop index if exists public.catalog_candidate_variants_natural_uidx;
    create unique index catalog_candidate_variants_natural_uidx
      on public.catalog_candidate_variants
         (raw_record_id, manufacturer, commercial_model,
          coalesce(model_year_start, -1), coalesce(model_year_end, -1),
          coalesce(official_model_code, ''), coalesce(trim, ''), identity_dimensions)
      where manufacturer is not null;
  end if;
  if not exists (select 1 from pg_indexes where schemaname = 'public'
                    and indexname = 'catalog_candidate_variants_snapshot_identity_idx'
                    and indexdef like '%WHERE (manufacturer IS NOT NULL)%') then
    drop index if exists public.catalog_candidate_variants_snapshot_identity_idx;
    create index catalog_candidate_variants_snapshot_identity_idx
      on public.catalog_candidate_variants
      (snapshot_id, manufacturer collate "C", commercial_model collate "C",
       model_year_start, model_year_end, candidate_key collate "C")
      where manufacturer is not null;
  end if;
  if not exists (select 1 from pg_indexes where schemaname = 'public'
                    and indexname = 'catalog_candidate_variants_identity_idx'
                    and indexdef like '%WHERE (manufacturer IS NOT NULL)%') then
    drop index if exists public.catalog_candidate_variants_identity_idx;
    create index catalog_candidate_variants_identity_idx
      on public.catalog_candidate_variants (manufacturer, commercial_model, model_year_start, model_year_end, status)
      where manufacturer is not null;
  end if;
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. The compaction record.
-- ---------------------------------------------------------------------------
create table if not exists public.catalog_register_snapshot_compactions (
  -- Not a foreign key, on purpose (as catalog_register_snapshot_archives): the
  -- record outlives a pruned snapshot, and every catalog key RESTRICTs. An
  -- active snapshot's compaction ("variants") may be followed, once it is
  -- superseded, by its skeleton ("archive"): at most one of each.
  snapshot_id uuid not null,
  snapshot_key text not null,
  -- Who answers for the removed content from now on: the snapshot's variant
  -- rows of `mapper_version` (an active snapshot), or its archive alone (a
  -- superseded one, kept as referenced skeletons).
  readers text not null check (readers in ('variants', 'archive')),
  mapper_version text check (mapper_version ~ '^[A-Za-z0-9._:-]{1,80}$'),
  raw_rows integer not null check (raw_rows >= 0),
  kept_rows integer not null check (kept_rows between 0 and raw_rows),
  bytes_before bigint not null check (bytes_before >= 0),
  bytes_after bigint not null check (bytes_after >= 0),
  compacted_at timestamptz not null default now(),
  check ((readers = 'variants') = (mapper_version is not null)),
  check (readers = 'archive' or kept_rows = raw_rows),
  primary key (snapshot_id, readers)
);

drop trigger if exists catalog_register_snapshot_compactions_append_only
  on public.catalog_register_snapshot_compactions;
create trigger catalog_register_snapshot_compactions_append_only
  before update or delete on public.catalog_register_snapshot_compactions
  for each row execute function public.forbid_catalog_register_rewrite();

-- ---------------------------------------------------------------------------
-- 3. The two reader helpers: the payload while it exists, the compacted
--    snapshot's variant row afterwards (identical by the compaction's check).
-- ---------------------------------------------------------------------------

-- `payload->>field` for the register codes `tozeret_cd`, `degem_cd`, `sug_degem`.
create or replace function public.catalog_raw_record_code(p_record public.catalog_raw_records, p_field text)
returns text
language sql
stable
set search_path = pg_catalog
as $$
  select case when p_record.payload is not null then p_record.payload->>p_field
         else (select case p_field when 'tozeret_cd' then v.tozeret_cd::text
                                   when 'degem_cd' then v.degem_cd::text
                                   when 'sug_degem' then v.sug_degem end
                 from public.catalog_register_snapshot_compactions k
                 join public.catalog_variants v
                   on v.snapshot_id = k.snapshot_id and v.mapper_version = k.mapper_version
                  and v.upstream_record_id = p_record.upstream_record_id
                where k.snapshot_id = p_record.snapshot_id and k.readers = 'variants') end
$$;

-- catalog_variant_content_sha256(payload): the row's content minus `_id`.
create or replace function public.catalog_raw_record_content_sha256(p_record public.catalog_raw_records)
returns text
language sql
stable
set search_path = pg_catalog
as $$
  select case when p_record.payload is not null then public.catalog_variant_content_sha256(p_record.payload)
         else (select v.content_sha256
                 from public.catalog_register_snapshot_compactions k
                 join public.catalog_variants v
                   on v.snapshot_id = k.snapshot_id and v.mapper_version = k.mapper_version
                  and v.upstream_record_id = p_record.upstream_record_id
                where k.snapshot_id = p_record.snapshot_id and k.readers = 'variants') end
$$;

-- A candidate's identity as its variant row states it: the reading the
-- ingest stored (normalize.read_wltp_record), which the variant mapper takes
-- too -- the text trimmed, blank as absent, the one model year as its range,
-- the four PR-V dimensions. The compaction refuses a snapshot where any
-- candidate differs from this (catalog_candidate_reads_as_variant).
create or replace function public.catalog_variant_candidate_reading(p_variant public.catalog_variants)
returns table (manufacturer text, commercial_model text, model_year integer, official_model_code text,
               "trim" text, identity_dimensions jsonb)
language sql
immutable
set search_path = pg_catalog
as $$
  select nullif(btrim(p_variant.tozar), ''), nullif(btrim(p_variant.kinuy_mishari), ''), p_variant.shnat_yitzur,
         nullif(btrim(p_variant.degem_nm), ''), nullif(btrim(p_variant.ramat_gimur), ''),
         jsonb_strip_nulls(jsonb_build_object(
           'body_style', p_variant.norm_body_style, 'drivetrain', p_variant.norm_drivetrain,
           'fuel_type', p_variant.norm_fuel_type,
           'propulsion_technology', p_variant.norm_propulsion_technology))
$$;

create or replace function public.catalog_candidate_reads_as_variant(
  p_candidate public.catalog_candidate_variants, p_variant public.catalog_variants
) returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce((select p_candidate.manufacturer is not distinct from x.manufacturer
                      and p_candidate.commercial_model is not distinct from x.commercial_model
                      and p_candidate.model_year_start is not distinct from x.model_year
                      and p_candidate.model_year_end is not distinct from x.model_year
                      and p_candidate.official_model_code is not distinct from x.official_model_code
                      and p_candidate.trim is not distinct from x.trim
                      and p_candidate.identity_dimensions is not distinct from x.identity_dimensions
                     from public.catalog_variant_candidate_reading(p_variant) x), false)
$$;

-- The candidates as every reader reads them: the table's own columns, in the
-- table's order (a `%rowtype` of the table takes a row of it), a compacted
-- candidate's identity from its compaction's variant row. A candidate still
-- stating its identity (every uncompacted one) is read as stored.
create or replace view public.catalog_candidate_variants_resolved
with (security_invoker = true) as
select c.id, c.snapshot_id, c.raw_record_id,
       coalesce(c.manufacturer, x.manufacturer) as manufacturer,
       coalesce(c.commercial_model, x.commercial_model) as commercial_model,
       case when c.manufacturer is null then x.model_year else c.model_year_start end as model_year_start,
       case when c.manufacturer is null then x.model_year else c.model_year_end end as model_year_end,
       case when c.manufacturer is null then x.official_model_code else c.official_model_code end
         as official_model_code,
       case when c.manufacturer is null then x.trim else c.trim end as trim,
       case when c.manufacturer is null then coalesce(x.identity_dimensions, c.identity_dimensions)
            else c.identity_dimensions end as identity_dimensions,
       c.status, c.candidate_key, c.created_at
  from public.catalog_candidate_variants c
  left join lateral (
    select y.*
      from public.catalog_register_snapshot_compactions k
      join public.catalog_raw_records r on r.id = c.raw_record_id
      join public.catalog_variants v
        on v.snapshot_id = k.snapshot_id and v.mapper_version = k.mapper_version
       and v.upstream_record_id = r.upstream_record_id
      cross join lateral public.catalog_variant_candidate_reading(v) y
     where c.manufacturer is null and k.snapshot_id = c.snapshot_id and k.readers = 'variants'
  ) x on true;

-- ---------------------------------------------------------------------------
-- 4. The losslessness check: does the typed variant read EXACTLY as the
--    payload for everything a reader takes from it?
-- ---------------------------------------------------------------------------
--
-- One identity field, as the Government query layer reads it
-- (query.identity_projection / unstated_fields): a TEXT field is stated when
-- it is a non-blank JSON string, a WHOLE field when it is a JSON integer; a
-- missing, null or blank value is "the register states nothing"; anything
-- else is stated but dropped from the projection. From the variant: stated
-- when the typed column is not null, "nothing" when it is null WITHOUT a
-- parse issue for the field. Blank is tested conservatively (ASCII/locale
-- space only): a value this reads as non-blank but the mapper blanked makes
-- the row not lossless, so the snapshot is refused, never misread.
create or replace function public.catalog_variant_field_reads_as(
  p_raw jsonb, p_value text, p_issue boolean, p_whole boolean
) returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce(case
    when p_raw is null or jsonb_typeof(p_raw) = 'null' then p_value is null
    when p_value is not null then
      case when p_whole then jsonb_typeof(p_raw) = 'number' and p_raw::text ~ '^-?[0-9]+$'
                             and p_raw::text = p_value
           else jsonb_typeof(p_raw) = 'string' and (p_raw #>> '{}') = p_value end
    when jsonb_typeof(p_raw) = 'string' and (p_raw #>> '{}') ~ '^[[:space:]]*$' then not p_issue
    -- Stated but not readable (the mapper recorded why): the projection drops
    -- it too, unless it is exactly the kind the projection keeps.
    else p_issue
         and not (p_whole and jsonb_typeof(p_raw) = 'number' and p_raw::text ~ '^-?[0-9]+$')
         and not (not p_whole and jsonb_typeof(p_raw) = 'string')
  end, false)
$$;

create or replace function public.catalog_variant_reads_as_payload(p_payload jsonb, p_variant public.catalog_variants)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce(
    p_payload is not null
    and p_variant.content_sha256 = public.catalog_variant_content_sha256(p_payload)
    -- The register codes, exactly as `payload->>field` renders them.
    and p_variant.tozeret_cd::text is not distinct from p_payload->>'tozeret_cd'
    and p_variant.degem_cd::text is not distinct from p_payload->>'degem_cd'
    and p_variant.sug_degem is not distinct from p_payload->>'sug_degem'
    -- The identity projection (query.IDENTITY_RECORD_FIELD_TYPES).
    and public.catalog_variant_field_reads_as(p_payload->'tozar', p_variant.tozar, 'tozar' = any(i.f), false)
    and public.catalog_variant_field_reads_as(p_payload->'kinuy_mishari', p_variant.kinuy_mishari,
                                              'kinuy_mishari' = any(i.f), false)
    and public.catalog_variant_field_reads_as(p_payload->'shnat_yitzur', p_variant.shnat_yitzur::text,
                                              'shnat_yitzur' = any(i.f), true)
    and public.catalog_variant_field_reads_as(p_payload->'degem_nm', p_variant.degem_nm, 'degem_nm' = any(i.f), false)
    and public.catalog_variant_field_reads_as(p_payload->'ramat_gimur', p_variant.ramat_gimur,
                                              'ramat_gimur' = any(i.f), false)
    and public.catalog_variant_field_reads_as(p_payload->'delek_cd', p_variant.delek_cd::text,
                                              'delek_cd' = any(i.f), true)
    and public.catalog_variant_field_reads_as(p_payload->'delek_nm', p_variant.delek_nm, 'delek_nm' = any(i.f), false),
    false)
    from (select coalesce(array_agg(e->>'field'), '{}'::text[]) as f
            from jsonb_array_elements(p_variant.parse_issues) e) i
$$;

-- ---------------------------------------------------------------------------
-- 5. The compaction.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_compaction_answer(
  p_status text, p_done public.catalog_register_snapshot_compactions
) returns jsonb
language sql
immutable
set search_path = pg_catalog
as $$
  select jsonb_build_object('status', p_status, 'snapshot_key', p_done.snapshot_key,
                            'mode', case p_done.readers when 'archive' then 'superseded' else 'active' end,
                            'raw_rows', p_done.raw_rows, 'kept_rows', p_done.kept_rows,
                            'mapper_version', p_done.mapper_version,
                            'bytes_before', p_done.bytes_before, 'bytes_after', p_done.bytes_after)
$$;

create or replace function public.compact_register_snapshot(p_snapshot_key text, p_apply boolean)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_filters jsonb;
  v_active uuid;
  v_superseded boolean;
  v_archive public.catalog_register_snapshot_archives%rowtype;
  v_build public.catalog_variant_builds%rowtype;
  v_done public.catalog_register_snapshot_compactions%rowtype;
  v_mapper text := public.catalog_variant_mapper_version();
  v_terminal text[] := array['completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted'];
  v_verified boolean;
  v_raw bigint;
  v_variants bigint;
  v_mismatched bigint;
  v_before bigint;
  v_payload bigint;
  v_after bigint;
  v_updated bigint;
  v_kept bigint;
begin
  if p_snapshot_key is null or p_snapshot_key !~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$' or p_apply is null then
    raise exception 'CATALOG_COMPACTION_REQUEST_INVALID: one snapshot key and apply true/false'
      using errcode = '22023';
  end if;
  if p_apply then
    -- A prune, a build and another compaction wait (and are waited for). The
    -- prune's order, raw records first: a raw-record writer inserts there
    -- before it updates its snapshot, so no order here can deadlock it.
    lock table public.catalog_raw_records, public.catalog_source_snapshots, public.catalog_candidate_variants,
               public.catalog_variant_builds, public.catalog_variants,
               public.catalog_register_snapshot_compactions in share row exclusive mode;
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
   where snapshot_key = p_snapshot_key and source_family = 'government';
  if v_snapshot.id is null then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_SNAPSHOT_UNKNOWN',
                              'snapshot_key', p_snapshot_key);
  end if;
  -- A skeleton is final; an active snapshot's compaction is final while it
  -- is active (checked below, once its rank is known).
  select * into v_done from public.catalog_register_snapshot_compactions
   where snapshot_id = v_snapshot.id and readers = 'archive';
  if v_done.snapshot_id is not null then
    return public.catalog_compaction_answer('unchanged', v_done);
  end if;
  v_filters := v_snapshot.retrieval_metadata->'capture_scope'->'filters';
  if v_snapshot.activated_at is null or v_snapshot.validation_state <> 'complete'
     or jsonb_typeof(v_filters) is distinct from 'object'
     or (case when jsonb_typeof(v_filters) = 'object'
              then (select array_agg(k) from jsonb_object_keys(v_filters) k) end) is distinct from array['tozar'] then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE',
                              'snapshot_key', p_snapshot_key);
  end if;
  -- The tozar's active snapshot: rank 1 in retention's order (activated_at
  -- desc, then id), as the variant build ranks it.
  select s.id into v_active from public.catalog_source_snapshots s
   where s.source_family = 'government' and s.activated_at is not null
     and s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' = v_filters->>'tozar'
   order by s.activated_at desc, s.id limit 1;
  v_superseded := v_active is distinct from v_snapshot.id;
  if not v_superseded then
    select * into v_done from public.catalog_register_snapshot_compactions
     where snapshot_id = v_snapshot.id and readers = 'variants';
    if v_done.snapshot_id is not null then
      return public.catalog_compaction_answer('unchanged', v_done);
    end if;
  end if;
  select count(*) into v_raw from public.catalog_raw_records where snapshot_id = v_snapshot.id;
  -- The NEWEST register unit that captured it decides; a Prepare snapshot no
  -- unit captured passes the stored = declared gate.
  select u.count_verified into v_verified from public.catalog_register_capture_units u
   where u.snapshot_id = v_snapshot.id order by u.updated_at desc, u.id desc limit 1;
  if (found and v_verified is not true)
     or v_snapshot.stored_record_count <> v_snapshot.declared_record_count
     or v_snapshot.stored_record_count <> v_raw then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_COUNT_UNVERIFIED',
                              'snapshot_key', p_snapshot_key);
  end if;

  if v_superseded then
    -- The active snapshot serves the tozar (its build under the current mapper
    -- is complete), and nothing live can still read this one.
    if not exists (select 1 from public.catalog_variant_builds b
                    where b.snapshot_id = v_active and b.mapper_version = v_mapper and b.completed_at is not null) then
      return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_BUILD_INCOMPLETE',
                                'snapshot_key', p_snapshot_key, 'mode', 'superseded');
    end if;
    if exists (select 1 from public.runs r where r.id = v_snapshot.created_by_run_id
                  and r.status <> all (v_terminal))
       or exists (select 1 from public.catalog_snapshot_adoptions a join public.runs r on r.id = a.adopted_by_run_id
                   where a.snapshot_id = v_snapshot.id and r.status <> all (v_terminal))
       or exists (select 1 from public.catalog_work_scope_units u
                    join public.catalog_work_scope_preparations wp on wp.id = u.preparation_id
                    join public.runs r on r.id = wp.prepared_by_run_id
                   where u.snapshot_id = v_snapshot.id and r.status <> all (v_terminal))
       -- A batch its plan can still start (the head revision of an open plan,
       -- never completed), or a run on one of its batches still going.
       or exists (select 1 from public.catalog_work_scope_batches b
                    join public.catalog_work_scopes w on w.id = b.work_scope_id
                   where b.snapshot_id = v_snapshot.id and w.closed_at is null
                     and b.revision = w.head_revision and b.scope_digest = w.head_digest
                     and not exists (select 1 from public.catalog_work_scope_batch_runs br
                                       join public.runs r on r.id = br.run_id
                                      where br.batch_id = b.id and r.status in ('completed', 'partial_success')))
       or exists (select 1 from public.catalog_work_scope_batches b
                    join public.catalog_work_scope_batch_runs br on br.batch_id = b.id
                    join public.runs r on r.id = br.run_id
                   where b.snapshot_id = v_snapshot.id and r.status <> all (v_terminal))
       or exists (select 1 from public.run_checkpoints rc join public.runs r on r.id = rc.run_id
                   where rc.artifacts->'government'->>'snapshot_key' = v_snapshot.snapshot_key
                     and r.status <> all (v_terminal))
       -- A capture-job run that is not a register capture: a Prepare can be
       -- reading this snapshot for reuse before it records any unit.
       or exists (select 1 from public.runs r
                   where r.run_identity->>'workflow_key' = 'operator_capture' and r.status <> all (v_terminal)
                     and not exists (select 1 from public.catalog_register_capture_groups g where g.run_id = r.id))
       or exists (select 1 from public.catalog_variant_reservations vr
                    join public.catalog_candidate_variants c on c.id = vr.candidate_id
                   where c.snapshot_id = v_snapshot.id) then
      return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_SNAPSHOT_IN_USE',
                                'snapshot_key', p_snapshot_key, 'mode', 'superseded');
    end if;
    -- The rows something references stay, as skeletons.
    select count(*) into v_kept from public.catalog_raw_records r
     where r.snapshot_id = v_snapshot.id
       and exists (select 1 from public.catalog_candidate_variants c
                    where c.raw_record_id = r.id and public.catalog_candidate_referenced(c.id));
    select coalesce(sum(pg_column_size(r.payload)), 0) into v_payload
      from public.catalog_raw_records r where r.snapshot_id = v_snapshot.id;
  else
    select * into v_build from public.catalog_variant_builds
     where snapshot_id = v_snapshot.id and mapper_version = v_mapper;
    select count(*) into v_variants from public.catalog_variants
     where snapshot_id = v_snapshot.id and mapper_version = v_mapper;
    if v_build.snapshot_id is null or v_build.completed_at is null
       or v_build.built_rows <> v_raw or v_build.expected_rows <> v_raw or v_variants <> v_raw then
      return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_BUILD_INCOMPLETE',
                                'snapshot_key', p_snapshot_key, 'raw_rows', v_raw, 'variant_rows', v_variants);
    end if;
    -- Every row's variant reads as its payload, and every candidate's
    -- identity as its variant: then every reader answers identically.
    select count(*) filter (where v.id is null or not public.catalog_variant_reads_as_payload(r.payload, v)),
           coalesce(sum(pg_column_size(r.payload)), 0)
      into v_mismatched, v_payload
      from public.catalog_raw_records r
      left join public.catalog_variants v
        on v.snapshot_id = r.snapshot_id and v.upstream_record_id = r.upstream_record_id
       and v.mapper_version = v_mapper
     where r.snapshot_id = v_snapshot.id;
    select v_mismatched + count(*) into v_mismatched
      from public.catalog_candidate_variants c
      join public.catalog_raw_records r on r.id = c.raw_record_id
      left join public.catalog_variants v
        on v.snapshot_id = r.snapshot_id and v.upstream_record_id = r.upstream_record_id
       and v.mapper_version = v_mapper
     where c.snapshot_id = v_snapshot.id
       and (v.id is null or not public.catalog_candidate_reads_as_variant(c, v));
    if v_mismatched > 0 then
      return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_TYPED_MISMATCH',
                                'snapshot_key', p_snapshot_key, 'mismatched_rows', v_mismatched);
    end if;
    v_kept := v_raw;
  end if;
  -- Last, so a dry-run's ARCHIVE_MISSING says every other check passed.
  select * into v_archive from public.catalog_register_snapshot_archives where snapshot_id = v_snapshot.id;
  if v_archive.id is null or v_archive.sha256 !~ '^[0-9a-f]{64}$' or v_archive.byte_size <= 0
     or v_archive.line_count <> v_raw then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_ARCHIVE_MISSING',
                              'snapshot_key', p_snapshot_key,
                              'mode', case when v_superseded then 'superseded' else 'active' end);
  end if;
  v_before := public.catalog_register_measured_bytes(v_snapshot.id);
  if not p_apply then
    return jsonb_build_object('status', 'ready', 'snapshot_key', p_snapshot_key,
                              'mode', case when v_superseded then 'superseded' else 'active' end,
                              'raw_rows', v_raw, 'kept_rows', v_kept,
                              'mapper_version', case when not v_superseded then v_mapper end,
                              'bytes_before', v_before, 'payload_bytes', v_payload);
  end if;

  alter table public.catalog_raw_records disable trigger catalog_raw_records_append_only;
  alter table public.catalog_candidate_variants disable trigger catalog_candidate_variants_identity_immutable;
  if v_superseded then
    -- Its variants no longer answer for anything: the active snapshot serves
    -- the tozar, the archive answers for the kept rows.
    alter table public.catalog_variants disable trigger catalog_variants_append_only;
    delete from public.catalog_variants where snapshot_id = v_snapshot.id;
    alter table public.catalog_variants enable trigger catalog_variants_append_only;
    delete from public.catalog_variant_builds where snapshot_id = v_snapshot.id;
    delete from public.catalog_candidate_variants c
     where c.snapshot_id = v_snapshot.id and not public.catalog_candidate_referenced(c.id);
    delete from public.catalog_raw_records r
     where r.snapshot_id = v_snapshot.id
       and not exists (select 1 from public.catalog_candidate_variants c where c.raw_record_id = r.id);
  end if;
  update public.catalog_raw_records set payload = null
   where snapshot_id = v_snapshot.id and payload is not null;
  get diagnostics v_updated = row_count;
  update public.catalog_candidate_variants
     set manufacturer = null, commercial_model = null, model_year_start = null, model_year_end = null,
         official_model_code = null, trim = null, identity_dimensions = '{}'::jsonb
   where snapshot_id = v_snapshot.id and manufacturer is not null;
  alter table public.catalog_candidate_variants enable trigger catalog_candidate_variants_identity_immutable;
  alter table public.catalog_raw_records enable trigger catalog_raw_records_append_only;

  v_after := public.catalog_register_measured_bytes(v_snapshot.id);
  insert into public.catalog_register_snapshot_compactions
    (snapshot_id, snapshot_key, readers, mapper_version, raw_rows, kept_rows, bytes_before, bytes_after)
  values (v_snapshot.id, v_snapshot.snapshot_key, case when v_superseded then 'archive' else 'variants' end,
          case when not v_superseded then v_mapper end, v_raw, v_kept, v_before, v_after);
  -- The Register page's measured bytes: the units that captured it.
  update public.catalog_register_capture_units
     set measured_bytes = v_after,
         measurement_method = 'pg_column_size(raw_records+candidates+variants+ledger)', measured_at = now()
   where snapshot_id = v_snapshot.id and status = 'captured';
  return jsonb_build_object('status', 'compacted', 'snapshot_key', p_snapshot_key,
                            'mode', case when v_superseded then 'superseded' else 'active' end,
                            'raw_rows', v_raw, 'kept_rows', v_kept, 'payloads_removed', v_updated,
                            'mapper_version', case when not v_superseded then v_mapper end,
                            'bytes_before', v_before, 'bytes_after', v_after);
end;
$$;

-- A candidate something outside the snapshot's own rows points at: a
-- promotion, an evidence link, a field provenance, a queue item or a claim.
create or replace function public.catalog_candidate_referenced(p_candidate_id uuid)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select exists (select 1 from public.catalog_model_variants x where x.promoted_from_candidate_id = p_candidate_id)
      or exists (select 1 from public.catalog_candidate_evidence_links x where x.candidate_id = p_candidate_id)
      or exists (select 1 from public.catalog_canonical_field_provenance x where x.candidate_id = p_candidate_id)
      or exists (select 1 from public.catalog_work_scope_queue_items x where x.candidate_id = p_candidate_id)
      or exists (select 1 from public.catalog_variant_reservations x where x.candidate_id = p_candidate_id)
$$;

-- The tozar's other activated snapshots not yet compacted: what the capture
-- job compacts, superseded, right after it compacts the active one.
create or replace function public.catalog_register_superseded_snapshots(p_snapshot_key text)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce(jsonb_agg(jsonb_build_object('id', o.id, 'snapshot_key', o.snapshot_key,
                                               'retrieval_metadata', o.retrieval_metadata)
                            order by o.activated_at desc, o.id), '[]'::jsonb)
    from public.catalog_source_snapshots s
    join public.catalog_source_snapshots o
      on o.source_family = 'government' and o.activated_at is not null and o.id <> s.id
     and o.retrieval_metadata->'capture_scope'->'filters'->>'tozar'
         = s.retrieval_metadata->'capture_scope'->'filters'->>'tozar'
   where s.snapshot_key = p_snapshot_key and s.source_family = 'government'
     and s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' is not null
     and not exists (select 1 from public.catalog_register_snapshot_compactions k
                      where k.snapshot_id = o.id and k.readers = 'archive')
$$;

-- ---------------------------------------------------------------------------
-- 6. One compacted row's typed reading (resolve_variant, REGISTER_FIELD_ABSENT,
--    the batch preparation's duplicate check). Null when the snapshot is not
--    compacted: the caller reads the payload, as before.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_compacted_record_reading(
  p_snapshot_id uuid, p_upstream_record_id text, p_allow_incomplete boolean default false
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  return (
    select jsonb_build_object(
             'upstream_record_id', v.upstream_record_id, 'mapper_version', v.mapper_version,
             'content_sha256', v.content_sha256,
             'fields', jsonb_build_object(
               'tozar', v.tozar, 'kinuy_mishari', v.kinuy_mishari, 'shnat_yitzur', v.shnat_yitzur,
               'degem_nm', v.degem_nm, 'ramat_gimur', v.ramat_gimur, 'delek_cd', v.delek_cd,
               'delek_nm', v.delek_nm, 'tozeret_cd', v.tozeret_cd::text, 'degem_cd', v.degem_cd::text,
               'sug_degem', v.sug_degem),
             'parse_issue_fields', (select coalesce(jsonb_agg(distinct e->>'field'), '[]'::jsonb)
                                      from jsonb_array_elements(v.parse_issues) e))
      from public.catalog_register_snapshot_compactions k
      join public.catalog_variants v
        on v.snapshot_id = k.snapshot_id and v.mapper_version = k.mapper_version
     where k.snapshot_id = p_snapshot_id and k.readers = 'variants'
       and v.upstream_record_id = p_upstream_record_id);
end;
$$;

-- An archive line (the record's canonical JSON) is the stored row's content
-- exactly when the database's own rendering of it has the row's digest:
-- one row, and a page of an archive being written (the lines at capture
-- indexes p_first.., before any payload is removed): how many do NOT match.
create or replace function public.catalog_raw_record_payload_matches(p_raw_record_id uuid, p_line text)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce((select r.payload_sha256 = encode(sha256(convert_to(p_line::jsonb::text, 'UTF8')), 'hex')
                     from public.catalog_raw_records r where r.id = p_raw_record_id), false)
$$;

create or replace function public.catalog_raw_record_lines_mismatched(p_snapshot_id uuid, p_first integer,
                                                                     p_lines text[])
returns integer
language sql
stable
set search_path = pg_catalog
as $$
  select count(*)::integer
    from unnest(p_lines) with ordinality as l(line, n)
    left join public.catalog_raw_records r
      on r.snapshot_id = p_snapshot_id and r.source_locator ? 'capture_index'
     and (r.source_locator->>'capture_index')::integer = p_first + l.n::integer - 1
   where r.id is null or r.payload is null
      or r.payload_sha256 <> encode(sha256(convert_to(l.line::jsonb::text, 'UTF8')), 'hex')
$$;

-- ---------------------------------------------------------------------------
-- 7. The archive of a Prepare snapshot, written from its stored rows.
-- ---------------------------------------------------------------------------
--
-- PR-D1's writer builds the object from the stored payloads in capture order
-- (`source_locator.capture_index`, which every row of such a snapshot has,
-- contiguous from 0) and uploads it create-only; this records it. No lease:
-- like the variant build, it is idempotent and checked here. The recording
-- run is the snapshot's own writer run (whose rows the object holds).
-- Its stored rows, once they are exactly an archive's lines: an activated
-- whole-tozar Government snapshot whose rows carry capture indexes 0..n-1 and
-- whose declared and stored counts are n. Checked BEFORE the object is written
-- and again when it is recorded.
create or replace function public.catalog_register_snapshot_archivable(p_snapshot_id uuid)
returns integer
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_filters jsonb;
  v_rows bigint;
  v_positions bigint;
  v_low integer;
  v_high integer;
begin
  select * into v_snapshot from public.catalog_source_snapshots
   where id = p_snapshot_id and source_family = 'government';
  v_filters := v_snapshot.retrieval_metadata->'capture_scope'->'filters';
  if v_snapshot.id is null or v_snapshot.activated_at is null
     or jsonb_typeof(v_filters) is distinct from 'object' or (v_filters->>'tozar') is null then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: only an activated whole-tozar Government snapshot is archived'
      using errcode = '22023';
  end if;
  select count(*), count(distinct (r.source_locator->>'capture_index')::integer)
           filter (where r.source_locator ? 'capture_index'),
         min((r.source_locator->>'capture_index')::integer), max((r.source_locator->>'capture_index')::integer)
    into v_rows, v_positions, v_low, v_high
    from public.catalog_raw_records r where r.snapshot_id = p_snapshot_id;
  if v_positions <> v_rows or v_snapshot.declared_record_count <> v_rows
     or v_snapshot.stored_record_count <> v_rows
     or (v_rows > 0 and (v_low <> 0 or v_high <> v_rows - 1)) then
    raise exception 'CATALOG_CAPTURE_COUNT_MISMATCH: the stored rows are not exactly an archive''s lines'
      using errcode = '22023';
  end if;
  return v_rows;
end;
$$;

-- PR-D1's writer builds the object from the stored payloads in capture order
-- and uploads it create-only (after the check above and after every line is
-- checked against its row); this records it. No lease: like the variant
-- build, it is idempotent and checked here. The recording run is the
-- snapshot's own writer run (whose rows the object holds).
create or replace function public.record_register_snapshot_archive_from_database(
  p_snapshot_id uuid, p_gcs_uri text, p_byte_size bigint, p_sha256 text, p_line_count integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_row public.catalog_register_snapshot_archives%rowtype;
begin
  if p_line_count is null or public.catalog_register_snapshot_archivable(p_snapshot_id) <> p_line_count then
    raise exception 'CATALOG_CAPTURE_COUNT_MISMATCH: the archive does not hold exactly the stored rows'
      using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots where id = p_snapshot_id;
  if p_gcs_uri is null or right(p_gcs_uri, char_length(v_snapshot.snapshot_key) + 10)
       <> '/' || v_snapshot.snapshot_key || '.jsonl.gz'
     or strpos(p_gcs_uri, '/register/' || v_snapshot.resource_id || '/') = 0 then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: the archive object does not name this snapshot'
      using errcode = '22023';
  end if;
  select * into v_row from public.catalog_register_snapshot_archives where snapshot_id = p_snapshot_id;
  if found then
    if v_row.sha256 = p_sha256 and v_row.gcs_uri = p_gcs_uri and v_row.byte_size = p_byte_size then
      return to_jsonb(v_row);
    end if;
    raise exception 'CATALOG_ARCHIVE_CONFLICT: a different archive is already recorded for that snapshot'
      using errcode = '23505';
  end if;
  insert into public.catalog_register_snapshot_archives
    (snapshot_id, snapshot_key, gcs_uri, byte_size, sha256, line_count, recorded_by_run_id)
  values (v_snapshot.id, v_snapshot.snapshot_key, p_gcs_uri, p_byte_size, p_sha256, p_line_count,
          v_snapshot.created_by_run_id)
  returning * into v_row;
  return to_jsonb(v_row);
end;
$$;

-- ---------------------------------------------------------------------------
-- 8. The readers, restated with only these expressions changed:
--      r.payload->>'<code>'                         -> catalog_raw_record_code(r, '<code>')
--      catalog_variant_content_sha256(r.payload)    -> catalog_raw_record_content_sha256(r)
--      from / join public.catalog_candidate_variants -> public.catalog_candidate_variants_resolved
-- ---------------------------------------------------------------------------

-- catalog_variant_coverage_for_batch: restated from 20260927000100.
create or replace function public.catalog_variant_coverage_for_batch(
  p_batch_id uuid, p_level text
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_batch public.catalog_work_scope_batches;
  v_include boolean;
begin
  if p_level is distinct from 'register' then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select * into v_batch from public.catalog_work_scope_batches where id = p_batch_id;
  if v_batch.id is null then
    return null;
  end if;
  select coalesce(rev.scope->'include_unresolved' = 'true'::jsonb, false) into v_include
    from public.catalog_work_scope_revisions rev
   where rev.work_scope_id = v_batch.work_scope_id and rev.revision = v_batch.revision;
  return jsonb_build_object(
    'batch_id', v_batch.id,
    'level', p_level,
    'include_unresolved', coalesce(v_include, false),
    'vocabulary_version', public.catalog_vocabulary_version(),
    'items', coalesce((
      select jsonb_agg(jsonb_build_object(
               'batch_position', i.batch_position, 'candidate_id', c.id,
               'candidate_key', c.candidate_key, 'upstream_record_id', r.upstream_record_id,
               'variant_identity_key', k.identity_key, 'content_sha256', k.content,
               'status', cv.status, 'reason_code', cv.reason_code,
               'last_run_id', cv.last_run_id,
               'decision', public.catalog_variant_coverage_decision(
                             cv.status, cv.content_sha256, cv.vocabulary_version, k.content,
                             coalesce(v_include, false)))
               order by i.batch_position)
        from public.catalog_work_scope_queue_items i
        join public.catalog_candidate_variants_resolved c on c.id = i.candidate_id
        join public.catalog_raw_records r on r.id = c.raw_record_id
        cross join lateral (
          select public.catalog_variant_identity_key(
                   c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
                   c.official_model_code, c.trim, public.catalog_raw_record_code(r, 'tozeret_cd'), public.catalog_raw_record_code(r, 'degem_cd'),
                   public.catalog_raw_record_code(r, 'sug_degem'), c.identity_dimensions) as identity_key,
                 public.catalog_raw_record_content_sha256(r) as content) as k
        left join public.catalog_variant_coverage cv
          on cv.variant_identity_key = k.identity_key and cv.level = p_level
       where i.batch_id = v_batch.id), '[]'::jsonb));
end;
$$;

-- catalog_variant_coverage_apply: restated from 20260927000100.
create or replace function public.catalog_variant_coverage_apply(
  p_run_id uuid, p_level text, p_entries jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_status text;
  v_snapshot_id uuid;
  v_snapshot_key text;
  v_count integer;
  v_written integer;
  v_released integer;
  v_collisions integer;
begin
  if p_level is distinct from 'register' then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  if p_entries is null or jsonb_typeof(p_entries) <> 'array'
     or jsonb_array_length(p_entries) not between 0 and 200
     or exists (select 1 from jsonb_array_elements(p_entries) as e
                 where jsonb_typeof(e) <> 'object'
                    or (select array_agg(k order by k collate "C") from jsonb_object_keys(e) as k)
                         is distinct from array['candidate_id', 'status']
                    or jsonb_typeof(e->'candidate_id') <> 'string'
                    or e->>'candidate_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    or e->>'status' is null
                    or e->>'status' not in ('enriched', 'unresolved_ambiguous',
                                            'unresolved_not_found', 'failed', 'pending')) then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select r.status into v_status from public.runs r where r.id = p_run_id;
  if v_status is null or v_status not in ('completed', 'partial_success') then
    raise exception 'CATALOG_COVERAGE_RUN_NOT_FINISHED' using errcode = '55000';
  end if;
  select b.snapshot_id, b.snapshot_key into v_snapshot_id, v_snapshot_key
    from public.catalog_work_scope_batch_runs br
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.run_id = p_run_id;
  if v_snapshot_id is null then
    raise exception 'CATALOG_COVERAGE_RUN_UNBOUND' using errcode = '22023';
  end if;
  select count(*) into v_count
    from (select distinct (e->>'candidate_id')::uuid as id
            from jsonb_array_elements(p_entries) as e) as wanted
    join public.catalog_candidate_variants_resolved c
      on c.id = wanted.id and c.snapshot_id = v_snapshot_id;
  if v_count <> (select count(distinct e->>'candidate_id') from jsonb_array_elements(p_entries) as e) then
    raise exception 'CATALOG_COVERAGE_CANDIDATE_INVALID' using errcode = '22023';
  end if;

  -- One row per identity key (`coverage.settle_keys`). A duplicate group --
  -- rows whose content minus `_id` is IDENTICAL -- is ONE variant, settled by
  -- the strongest status any of its rows earned. Rows that share a key while
  -- their content DIFFERS -- among the entries, or anywhere else in the run's
  -- snapshot -- are a key collision: none of them is picked; the key is
  -- recorded `failed` with CATALOG_COVERAGE_KEY_COLLISION and the hash of
  -- every content involved. Nothing is refused: the write succeeds, and the
  -- run is untouched either way.
  --
  -- The snapshot's rows are read by the identity columns every row of a key
  -- states (the snapshot identity index), never the whole snapshot.
  with x as (
    select public.catalog_variant_identity_key(
             c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
             c.official_model_code, c.trim, public.catalog_raw_record_code(r, 'tozeret_cd'), public.catalog_raw_record_code(r, 'degem_cd'),
             public.catalog_raw_record_code(r, 'sug_degem'), c.identity_dimensions) as identity_key,
           public.catalog_raw_record_content_sha256(r) as content,
           e->>'status' as status,
           c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
           c.official_model_code, c.trim
      from jsonb_array_elements(p_entries) as e
      join public.catalog_candidate_variants_resolved c on c.id = (e->>'candidate_id')::uuid
      join public.catalog_raw_records r on r.id = c.raw_record_id
  ),
  peers as (
    select k.identity_key, k.content
      from (select distinct x.manufacturer, x.commercial_model, x.model_year_start,
                   x.model_year_end, x.official_model_code, x.trim from x) as w
      join public.catalog_candidate_variants_resolved c
        on c.snapshot_id = v_snapshot_id
       and c.manufacturer = w.manufacturer and c.commercial_model = w.commercial_model
       and c.model_year_start is not distinct from w.model_year_start
       and c.model_year_end is not distinct from w.model_year_end
       and c.official_model_code is not distinct from w.official_model_code
       and c.trim is not distinct from w.trim
      join public.catalog_raw_records r on r.id = c.raw_record_id
      cross join lateral (
        select public.catalog_variant_identity_key(
                 c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
                 c.official_model_code, c.trim, public.catalog_raw_record_code(r, 'tozeret_cd'),
                 public.catalog_raw_record_code(r, 'degem_cd'), public.catalog_raw_record_code(r, 'sug_degem'),
                 c.identity_dimensions) as identity_key,
               public.catalog_raw_record_content_sha256(r) as content) as k
  ),
  contents as (
    -- `union` leaves each (key, content) pair once.
    select u.identity_key, count(*) as distinct_contents,
           encode(sha256(convert_to(string_agg(u.content, ',' order by u.content collate "C"),
                                    'UTF8')), 'hex') as collision_content,
           min(u.content collate "C") as only_content
      from (select x.identity_key, x.content from x
            union
            select p.identity_key, p.content from peers p
             where p.identity_key in (select x.identity_key from x)) as u
     group by u.identity_key
  ),
  strongest as (
    select x.identity_key,
           (array_agg(x.status order by public.catalog_variant_coverage_rank(x.status) desc,
                      x.status collate "C"))[1] as status
      from x
     group by x.identity_key
  ),
  settled as (
    select s.identity_key,
           case when k.distinct_contents > 1 then 'failed' else s.status end as status,
           case when k.distinct_contents > 1 then k.collision_content
                else k.only_content end as content,
           case when k.distinct_contents > 1
                then 'CATALOG_COVERAGE_KEY_COLLISION' end as reason_code
      from strongest s
      join contents k on k.identity_key = s.identity_key
  ),
  written as (
    insert into public.catalog_variant_coverage as cv
      (variant_identity_key, level, status, last_run_id, snapshot_key, content_sha256,
       vocabulary_version, reason_code)
    select t.identity_key, p_level, t.status, p_run_id, v_snapshot_key, t.content,
           public.catalog_vocabulary_version(), t.reason_code
      from settled t
     order by t.identity_key collate "C"
    on conflict (variant_identity_key, level) do update
       set status = excluded.status,
           last_run_id = excluded.last_run_id,
           snapshot_key = excluded.snapshot_key,
           content_sha256 = excluded.content_sha256,
           vocabulary_version = excluded.vocabulary_version,
           reason_code = excluded.reason_code,
           updated_at = now()
     where cv.content_sha256 is distinct from excluded.content_sha256
        or public.catalog_variant_coverage_rank(excluded.status)
             > public.catalog_variant_coverage_rank(cv.status)
        or (public.catalog_variant_coverage_rank(excluded.status)
              = public.catalog_variant_coverage_rank(cv.status)
            and (cv.status, cv.last_run_id, cv.snapshot_key, cv.vocabulary_version,
                 cv.reason_code)
                is distinct from (excluded.status, excluded.last_run_id, excluded.snapshot_key,
                                  excluded.vocabulary_version, excluded.reason_code))
    returning 1
  )
  select (select count(*) from written),
         (select count(*) from settled where settled.reason_code is not null)
    into v_written, v_collisions;
  delete from public.catalog_variant_reservations
   where run_id = p_run_id and level = p_level;
  get diagnostics v_released = row_count;
  return jsonb_build_object(
    'run_id', p_run_id, 'level', p_level, 'snapshot_key', v_snapshot_key,
    'entries', jsonb_array_length(p_entries), 'written', v_written,
    'released', v_released, 'collisions', v_collisions);
end;
$$;

-- acquire_catalog_variant_reservations_guarded: restated from 20260927000100.
create or replace function public.acquire_catalog_variant_reservations_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text, p_level text,
  p_candidate_ids jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_status text;
  v_batch public.catalog_work_scope_batches;
  v_include boolean;
  v_item record;
  v_row public.catalog_variant_reservations;
  v_ledger public.catalog_variant_coverage;
  v_decision text;
  v_owner text;
  v_answer jsonb := '[]'::jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_level is distinct from 'register' then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select r.status into v_status from public.runs r where r.id = p_run_id;
  if v_status in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                  'budget_exhausted') then
    raise exception 'CATALOG_COVERAGE_RUN_FINISHED' using errcode = '55000';
  end if;
  if p_candidate_ids is null or jsonb_typeof(p_candidate_ids) <> 'array'
     or jsonb_array_length(p_candidate_ids) not between 1 and 20
     or exists (select 1 from jsonb_array_elements(p_candidate_ids) as e
                 where jsonb_typeof(e) <> 'string'
                    or e #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
     or (select count(distinct e #>> '{}') from jsonb_array_elements(p_candidate_ids) as e)
          <> jsonb_array_length(p_candidate_ids) then
    raise exception 'CATALOG_COVERAGE_INVALID' using errcode = '22023';
  end if;
  select b.* into v_batch
    from public.catalog_work_scope_batch_runs br
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.run_id = p_run_id;
  if v_batch.id is null then
    raise exception 'CATALOG_COVERAGE_RUN_UNBOUND' using errcode = '22023';
  end if;
  -- Every named item must be an item of the run's own batch.
  if (select count(*) from jsonb_array_elements(p_candidate_ids) as e
        join public.catalog_work_scope_queue_items i
          on i.batch_id = v_batch.id and i.candidate_id = (e #>> '{}')::uuid)
     <> jsonb_array_length(p_candidate_ids) then
    raise exception 'CATALOG_COVERAGE_CANDIDATE_INVALID' using errcode = '22023';
  end if;
  select coalesce(rev.scope->'include_unresolved' = 'true'::jsonb, false) into v_include
    from public.catalog_work_scope_revisions rev
   where rev.work_scope_id = v_batch.work_scope_id and rev.revision = v_batch.revision;
  v_include := coalesce(v_include, false);

  for v_item in
    select x.* from (
      select c.id as candidate_id, r.upstream_record_id,
             public.catalog_variant_identity_key(
               c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
               c.official_model_code, c.trim, public.catalog_raw_record_code(r, 'tozeret_cd'), public.catalog_raw_record_code(r, 'degem_cd'),
               public.catalog_raw_record_code(r, 'sug_degem'), c.identity_dimensions) as identity_key,
             public.catalog_raw_record_content_sha256(r) as content
        from jsonb_array_elements(p_candidate_ids) as e
        join public.catalog_candidate_variants_resolved c on c.id = (e #>> '{}')::uuid
        join public.catalog_raw_records r on r.id = c.raw_record_id) as x
     order by x.identity_key collate "C", x.candidate_id
  loop
    -- 1. The row, claimed or locked. A concurrent claim of the same key waits
    --    here for the other transaction and then sees its committed row.
    insert into public.catalog_variant_reservations
      (variant_identity_key, level, run_id, attempt, batch_id, candidate_id, content_sha256)
    values (v_item.identity_key, p_level, p_run_id, p_attempt, v_batch.id,
            v_item.candidate_id, v_item.content)
    on conflict (variant_identity_key, level) do nothing;
    select * into v_row from public.catalog_variant_reservations
     where variant_identity_key = v_item.identity_key and level = p_level
       for update;
    -- 2. The ledger, read with the row locked.
    select * into v_ledger from public.catalog_variant_coverage
     where variant_identity_key = v_item.identity_key and level = p_level;
    v_decision := public.catalog_variant_coverage_decision(
      v_ledger.status, v_ledger.content_sha256, v_ledger.vocabulary_version, v_item.content,
      v_include);
    v_owner := null;
    if v_decision <> 'queue' then
      -- Settled: nothing to claim. A claim this run holds on it is given back.
      delete from public.catalog_variant_reservations
       where id = v_row.id and run_id = p_run_id;
    elsif v_row.run_id = p_run_id then
      update public.catalog_variant_reservations
         set attempt = p_attempt, updated_at = now()
       where id = v_row.id;
      v_decision := 'reserved';
    else
      v_owner := public.catalog_variant_reservation_state(v_row.run_id);
      if v_owner = 'dead' then
        update public.catalog_variant_reservations
           set previous_run_id = v_row.run_id, run_id = p_run_id, attempt = p_attempt,
               batch_id = v_batch.id, candidate_id = v_item.candidate_id,
               content_sha256 = v_item.content, reserved_at = now(), updated_at = now()
         where id = v_row.id;
        v_decision := 'reserved';
      elsif v_owner = 'settling' then
        v_decision := 'settlement_pending';
      else
        v_decision := 'reserved_by_other';
      end if;
    end if;
    v_answer := v_answer || jsonb_build_object(
      'candidate_id', v_item.candidate_id, 'upstream_record_id', v_item.upstream_record_id,
      'variant_identity_key', v_item.identity_key, 'decision', v_decision,
      'owner_run_id', case when v_decision in ('reserved_by_other', 'settlement_pending')
                           then v_row.run_id end);
  end loop;
  return jsonb_build_object('run_id', p_run_id, 'level', p_level, 'batch_id', v_batch.id,
                            'items', v_answer);
end;
$$;

-- catalog_candidate_variant_page: restated from 20260927000100.
create or replace function public.catalog_candidate_variant_page(
  p_snapshot_id uuid,
  p_manufacturer text default null,
  p_commercial_model text default null,
  p_model_year integer default null,
  p_official_model_code text default null,
  p_trim text default null,
  p_identity_dimensions jsonb default null,
  p_status text default null,
  p_limit integer default 50,
  p_offset integer default 0,
  p_allow_incomplete boolean default false,
  p_register_manufacturer_code text default null,
  p_register_model_code text default null,
  p_vehicle_type_code text default null
) returns table (
  id uuid, snapshot_id uuid, raw_record_id uuid, manufacturer text,
  commercial_model text, model_year_start integer, model_year_end integer,
  official_model_code text, "trim" text, identity_dimensions jsonb, status text,
  candidate_key text, upstream_record_id text, resource_id text,
  source_locator jsonb, payload_sha256 text, register_manufacturer_code text,
  register_model_code text, vehicle_type_code text, total_count bigint
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer; v_offset integer; v_dimensions jsonb;
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  if p_status is not null
     and p_status not in ('candidate', 'ambiguous', 'rejected', 'ready_for_review') then
    raise exception 'unknown catalog candidate status' using errcode = '22023';
  end if;
  if p_identity_dimensions is not null
     and not public.catalog_identity_dimensions_valid(p_identity_dimensions) then
    raise exception 'unknown catalog identity dimension' using errcode = '22023';
  end if;
  v_dimensions := coalesce(p_identity_dimensions, '{}'::jsonb);
  v_limit := greatest(1, least(coalesce(p_limit, 50), public.catalog_page_limit()));
  v_offset := greatest(0, coalesce(p_offset, 0));
  return query
  with matched as (
    select c.id, c.snapshot_id, c.raw_record_id, c.manufacturer, c.commercial_model,
           c.model_year_start, c.model_year_end, c.official_model_code, c.trim,
           c.identity_dimensions, c.status, c.candidate_key,
           r.upstream_record_id, r.resource_id, r.source_locator, r.payload_sha256,
           public.catalog_raw_record_code(r, 'tozeret_cd') as register_manufacturer_code,
           public.catalog_raw_record_code(r, 'degem_cd') as register_model_code,
           public.catalog_raw_record_code(r, 'sug_degem') as vehicle_type_code
      from public.catalog_candidate_variants_resolved c
      join public.catalog_raw_records r
        on r.id = c.raw_record_id and r.snapshot_id = c.snapshot_id
     where c.snapshot_id = p_snapshot_id
       and (p_manufacturer is null or c.manufacturer = p_manufacturer)
       and (p_commercial_model is null or c.commercial_model = p_commercial_model)
       and (p_official_model_code is null or c.official_model_code = p_official_model_code)
       and (p_trim is null or c.trim = p_trim)
       and (p_status is null or c.status = p_status)
       and (p_model_year is null
            or (c.model_year_start is not null
                and p_model_year between c.model_year_start and c.model_year_end))
       and c.identity_dimensions @> v_dimensions
       and (p_register_manufacturer_code is null
            or public.catalog_raw_record_code(r, 'tozeret_cd') = p_register_manufacturer_code)
       and (p_register_model_code is null
            or public.catalog_raw_record_code(r, 'degem_cd') = p_register_model_code)
       and (p_vehicle_type_code is null
            or public.catalog_raw_record_code(r, 'sug_degem') = p_vehicle_type_code)
  ), page as (
    select m.*
      from matched m
     order by m.manufacturer collate "C", m.commercial_model collate "C",
              m.model_year_start, m.model_year_end,
              coalesce(m.official_model_code, '') collate "C",
              coalesce(m.trim, '') collate "C", m.candidate_key collate "C"
     limit v_limit offset v_offset
  )
  select p.id, p.snapshot_id, p.raw_record_id, p.manufacturer, p.commercial_model,
         p.model_year_start, p.model_year_end, p.official_model_code, p.trim,
         p.identity_dimensions, p.status, p.candidate_key, p.upstream_record_id,
         p.resource_id, p.source_locator, p.payload_sha256, p.register_manufacturer_code,
         p.register_model_code, p.vehicle_type_code,
         (select count(*) from matched)
    from (select 1) as anchor
    left join page p on true
   order by p.manufacturer collate "C" nulls last, p.commercial_model collate "C",
            p.model_year_start, p.model_year_end,
            coalesce(p.official_model_code, '') collate "C",
            coalesce(p.trim, '') collate "C", p.candidate_key collate "C";
end;
$$;

-- catalog_work_scope_coverage_decisions: restated from 20260930000200.
create or replace function public.catalog_work_scope_coverage_decisions(
  p_snapshot_id uuid, p_from integer, p_to integer, p_include_unresolved boolean
) returns table (candidate_id uuid, upstream_record_id text, decision text)
language sql
stable
set search_path = pg_catalog
as $$
  select c.id, r.upstream_record_id,
         -- P27: a placeholder source record is never queued, whatever the ledger says.
         case when public.catalog_is_placeholder_identity(c.commercial_model, c.official_model_code)
              then 'excluded_placeholder_source_record'
              else public.catalog_variant_coverage_decision(cv.status, cv.content_sha256,
                                                            cv.vocabulary_version,
                                                            public.catalog_raw_record_content_sha256(r),
                                                            p_include_unresolved) end
    from public.catalog_candidate_variants_resolved c
    join public.catalog_raw_records r on r.id = c.raw_record_id
    left join public.catalog_variant_coverage cv
      on cv.level = 'register'
     and cv.variant_identity_key = public.catalog_variant_identity_key(
           c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
           c.official_model_code, c.trim, public.catalog_raw_record_code(r, 'tozeret_cd'), public.catalog_raw_record_code(r, 'degem_cd'),
           public.catalog_raw_record_code(r, 'sug_degem'), c.identity_dimensions)
   where c.snapshot_id = p_snapshot_id and c.status = 'candidate'
     and (p_from is null or c.model_year_start >= p_from)
     and (p_to is null or c.model_year_end <= p_to);
$$;

-- catalog_readable_snapshot: restated from 20260916090000; refuses a superseded snapshot kept as skeletons.
create or replace function public.catalog_readable_snapshot(
  p_snapshot_id uuid, p_allow_incomplete boolean default false
) returns public.catalog_source_snapshots
language plpgsql stable
set search_path = pg_catalog
as $$
declare
  v_row public.catalog_source_snapshots;
  v_contract text;
  v_issues integer;
begin
  select * into v_row from public.catalog_source_snapshots
    where id = p_snapshot_id and activated_at is not null and validation_state = 'complete';
  if v_row.id is null then
    raise exception 'catalog snapshot is not active' using errcode = '22023';
  end if;
  -- PR-L2: a superseded snapshot kept as referenced skeletons is read from its
  -- archive only (compaction.source_record), never browsed here.
  if exists (select 1 from public.catalog_register_snapshot_compactions k
              where k.snapshot_id = v_row.id and k.readers = 'archive') then
    raise exception 'CATALOG_SNAPSHOT_ARCHIVED: the snapshot is kept as referenced rows; its archive is the record'
      using errcode = '55000';
  end if;
  v_contract := v_row.retrieval_metadata->>'normalization_contract';
  if v_contract is null or v_contract = 'raw_only' then
    -- No reading at all, or an explicit raw-only contract. Neither can state
    -- what it is missing, so neither is answerable under any acknowledgement.
    raise exception 'catalog snapshot states no readable identities' using errcode = '22023';
  end if;
  begin
    v_issues := (v_row.retrieval_metadata->>'normalization_issue_count')::integer;
  exception when others then
    raise exception 'catalog snapshot reading state is malformed' using errcode = '22023';
  end;
  if v_issues is null then
    raise exception 'catalog snapshot reading state is malformed' using errcode = '22023';
  end if;
  if v_issues > 0 and not coalesce(p_allow_incomplete, false) then
    raise exception 'catalog snapshot holds rows its vocabulary could not read'
      using errcode = '22023';
  end if;
  return v_row;
end;
$$;

-- catalog_candidate_manufacturers: restated from 20260916090000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_candidate_manufacturers(
  p_snapshot_id uuid, p_limit integer default 50, p_offset integer default 0,
  p_allow_incomplete boolean default false
) returns table (
  manufacturer text, model_count integer, variant_count integer,
  ambiguous_variant_count integer, total_count bigint
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer; v_offset integer;
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  v_limit := greatest(1, least(coalesce(p_limit, 50), public.catalog_page_limit()));
  v_offset := greatest(0, coalesce(p_offset, 0));
  return query
  with grouped as (
    select c.manufacturer as name,
           count(distinct c.commercial_model)::integer as models,
           count(*)::integer as variants,
           count(*) filter (where c.status = 'ambiguous')::integer as ambiguous
      from public.catalog_candidate_variants_resolved c
     where c.snapshot_id = p_snapshot_id
     group by c.manufacturer
  ), page as (
    select g.name, g.models, g.variants, g.ambiguous
      from grouped g
     order by g.name collate "C"
     limit v_limit offset v_offset
  )
  select p.name, p.models, p.variants, p.ambiguous, (select count(*) from grouped)
    from (select 1) as anchor
    left join page p on true
   order by p.name collate "C" nulls last;
end;
$$;

-- catalog_candidate_models: restated from 20260916090000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_candidate_models(
  p_snapshot_id uuid, p_manufacturer text, p_limit integer default 50,
  p_offset integer default 0, p_allow_incomplete boolean default false
) returns table (
  manufacturer text, commercial_model text, variant_count integer,
  ambiguous_variant_count integer, model_year_start integer, model_year_end integer,
  total_count bigint
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer; v_offset integer;
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  if p_manufacturer is null or btrim(p_manufacturer) = '' then
    raise exception 'a manufacturer is required' using errcode = '22023';
  end if;
  v_limit := greatest(1, least(coalesce(p_limit, 50), public.catalog_page_limit()));
  v_offset := greatest(0, coalesce(p_offset, 0));
  return query
  with grouped as (
    select c.commercial_model as name,
           count(*)::integer as variants,
           count(*) filter (where c.status = 'ambiguous')::integer as ambiguous,
           min(c.model_year_start) as first_year,
           max(c.model_year_end) as last_year
      from public.catalog_candidate_variants_resolved c
     where c.snapshot_id = p_snapshot_id and c.manufacturer = p_manufacturer
     group by c.commercial_model
  ), page as (
    -- The manufacturer travels INSIDE the page, not as a parameter beside it,
    -- so the count row's every item column is null and the shape is uniform.
    select p_manufacturer as make, g.name, g.variants, g.ambiguous,
           g.first_year, g.last_year
      from grouped g
     order by g.name collate "C"
     limit v_limit offset v_offset
  )
  select p.make, p.name, p.variants, p.ambiguous, p.first_year, p.last_year,
         (select count(*) from grouped)
    from (select 1) as anchor
    left join page p on true
   order by p.name collate "C" nulls last;
end;
$$;

-- catalog_candidate_model_years: restated from 20260916090000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_candidate_model_years(
  p_snapshot_id uuid, p_manufacturer text, p_commercial_model text,
  p_limit integer default 50, p_offset integer default 0,
  p_allow_incomplete boolean default false
) returns table (
  manufacturer text, commercial_model text, model_year integer,
  variant_count integer, ambiguous_variant_count integer, total_count bigint
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer; v_offset integer;
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  if p_manufacturer is null or btrim(p_manufacturer) = ''
     or p_commercial_model is null or btrim(p_commercial_model) = '' then
    raise exception 'a manufacturer and a commercial model are required' using errcode = '22023';
  end if;
  v_limit := greatest(1, least(coalesce(p_limit, 50), public.catalog_page_limit()));
  v_offset := greatest(0, coalesce(p_offset, 0));
  return query
  with expanded as (
    select y.year as model_year, c.status
      from public.catalog_candidate_variants_resolved c
      cross join lateral generate_series(c.model_year_start, c.model_year_end) as y(year)
     where c.snapshot_id = p_snapshot_id
       and c.manufacturer = p_manufacturer
       and c.commercial_model = p_commercial_model
       and c.model_year_start is not null
  ), grouped as (
    select e.model_year as year,
           count(*)::integer as variants,
           count(*) filter (where e.status = 'ambiguous')::integer as ambiguous
      from expanded e group by e.model_year
  ), page as (
    select p_manufacturer as make, p_commercial_model as model, g.year,
           g.variants, g.ambiguous
      from grouped g
     order by g.year
     limit v_limit offset v_offset
  )
  select p.make, p.model, p.year, p.variants, p.ambiguous,
         (select count(*) from grouped)
    from (select 1) as anchor
    left join page p on true
   order by p.year nulls last;
end;
$$;

-- catalog_snapshot_candidate_diff: restated from 20260916090000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_snapshot_candidate_diff(
  p_previous_snapshot_id uuid, p_snapshot_id uuid, p_limit integer default 100,
  p_allow_incomplete boolean default false
) returns table (
  state text, manufacturer text, commercial_model text,
  model_year_start integer, model_year_end integer,
  official_model_code text, "trim" text, changed_fields text[],
  upstream_record_id text,
  added_count bigint, changed_count bigint, removed_count bigint
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer;
begin
  -- Both sides must be READABLE snapshots, under the same gate every other
  -- answer in this file passes: an unread or incomplete capture cannot state
  -- what it is missing, so it cannot state what changed either. A null
  -- previous side is the FIRST ingestion -- everything is added.
  if p_previous_snapshot_id is not null then
    perform public.catalog_readable_snapshot(p_previous_snapshot_id, p_allow_incomplete);
  end if;
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  v_limit := greatest(0, least(coalesce(p_limit, 100), public.catalog_page_limit()));
  return query
  with reduced as (
    select distinct on (c.snapshot_id, c.manufacturer, c.commercial_model,
                        c.model_year_start, c.model_year_end,
                        coalesce(c.official_model_code, ''), coalesce(c.trim, ''),
                        c.identity_dimensions)
           c.snapshot_id, c.manufacturer, c.commercial_model, c.model_year_start,
           c.model_year_end, c.official_model_code, c.trim, c.identity_dimensions,
           c.status, r.upstream_record_id
      from public.catalog_candidate_variants_resolved c
      join public.catalog_raw_records r
        on r.id = c.raw_record_id and r.snapshot_id = c.snapshot_id
     where c.snapshot_id = p_snapshot_id
        or c.snapshot_id = p_previous_snapshot_id
     order by c.snapshot_id, c.manufacturer, c.commercial_model, c.model_year_start,
              c.model_year_end, coalesce(c.official_model_code, ''),
              coalesce(c.trim, ''), c.identity_dimensions, c.candidate_key collate "C"
  ), before as (
    select * from reduced where snapshot_id = p_previous_snapshot_id
  ), after as (
    select * from reduced where snapshot_id = p_snapshot_id
  ), paired as (
    select case when b.snapshot_id is null then 'added'
                when a.snapshot_id is null then 'removed'
                when a.status is distinct from b.status then 'changed'
                else 'unchanged' end as state,
           coalesce(a.manufacturer, b.manufacturer) as manufacturer,
           coalesce(a.commercial_model, b.commercial_model) as commercial_model,
           coalesce(a.model_year_start, b.model_year_start) as model_year_start,
           coalesce(a.model_year_end, b.model_year_end) as model_year_end,
           coalesce(a.official_model_code, b.official_model_code) as official_model_code,
           coalesce(a.trim, b.trim) as trim,
           coalesce(a.upstream_record_id, b.upstream_record_id) as upstream_record_id
      from after a
      full outer join before b
        on b.manufacturer = a.manufacturer
       and b.commercial_model = a.commercial_model
       and b.model_year_start is not distinct from a.model_year_start
       and b.model_year_end is not distinct from a.model_year_end
       -- The optional identity columns are NEVER the empty string (a CHECK on
       -- the table refuses one), so coalescing to it is an injective way to
       -- join on "both absent" without a three-way comparison.
       and coalesce(b.official_model_code, '') = coalesce(a.official_model_code, '')
       and coalesce(b.trim, '') = coalesce(a.trim, '')
       and b.identity_dimensions = a.identity_dimensions
  ), counted as (
    select count(*) filter (where p.state = 'added') as added,
           count(*) filter (where p.state = 'changed') as changed,
           count(*) filter (where p.state = 'removed') as removed
      from paired p
  ), page as (
    select p.state, p.manufacturer, p.commercial_model, p.model_year_start,
           p.model_year_end, p.official_model_code, p.trim,
           case when p.state = 'changed' then array['status']::text[]
                else '{}'::text[] end as changed_fields,
           p.upstream_record_id
      from paired p
     where p.state <> 'unchanged'
       and (select c.added + c.changed + c.removed from counted c) <= v_limit
     order by p.state, p.manufacturer collate "C", p.commercial_model collate "C",
              p.model_year_start, p.model_year_end,
              coalesce(p.official_model_code, '') collate "C",
              coalesce(p.trim, '') collate "C", p.upstream_record_id collate "C"
     limit v_limit
  )
  select d.state, d.manufacturer, d.commercial_model, d.model_year_start,
         d.model_year_end, d.official_model_code, d.trim, d.changed_fields,
         d.upstream_record_id, c.added, c.changed, c.removed
    from counted c
    left join page d on true
   order by d.state nulls last, d.manufacturer collate "C", d.commercial_model collate "C",
            d.model_year_start, d.model_year_end,
            coalesce(d.official_model_code, '') collate "C",
            coalesce(d.trim, '') collate "C", d.upstream_record_id collate "C";
end;
$$;

-- catalog_run_pending_promotions: restated from 20260921000100; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_run_pending_promotions(
  p_run_id uuid, p_tool_operation text, p_limit integer default 25
) returns table (
  candidate_id uuid, candidate_key text, status text,
  snapshot_id uuid, snapshot_key text, source_family text, resource_id text,
  raw_record_id uuid, upstream_record_id text, record_key text,
  manufacturer text, commercial_model text,
  model_year_start integer, model_year_end integer,
  official_model_code text, "trim" text, identity_dimensions jsonb,
  claim_id uuid, source_id uuid, verdict_id uuid,
  field_key text, field_value jsonb
)
language plpgsql stable
set search_path = pg_catalog
as $$
declare v_limit integer;
begin
  if p_run_id is null or p_tool_operation is null or btrim(p_tool_operation) = '' then
    raise exception 'a run and a tool operation are required' using errcode = '22023';
  end if;
  v_limit := greatest(0, least(coalesce(p_limit, 25), public.catalog_page_limit()));
  return query
  with evidence as (
    -- ONE run, ONE registered tool operation, and the CURRENT verdict state.
    -- A `needs_review` or `rejected` verdict is a real answer and it is an
    -- answer against promoting; so is a newer verdict that replaced an older
    -- `verified`, a superseded claim, an unresolved contradiction and a
    -- `verified` that cites no durable evidence.
    select c.id as claim_id, c.source_id, c.field_key, c.value as field_value,
           coalesce(c.identity_scope, '{}'::jsonb) as identity_scope,
           public.r3_canonical_locator(c.evidence_locator)->>1 as locator_record,
           cv.verdict_id as verdict_id
      from public.claims c
      join public.sources s on s.id = c.source_id and s.run_id = p_run_id
      join lateral public.claim_current_verdict_state(c.id) cv on cv.state = 'supported'
     where c.run_id = p_run_id
       and c.status = 'active'
       and s.tool_operation = p_tool_operation
       and c.evidence_locator is not null
  ), located as (
    select e.claim_id, e.source_id, e.field_key, e.field_value, e.identity_scope,
           e.verdict_id, r.id as raw_record_id, r.record_key, r.upstream_record_id,
           sn.id as snapshot_id, sn.snapshot_key, sn.source_family, sn.resource_id
      from evidence e
      join public.catalog_source_snapshots sn
        on sn.trust_state = 'evidence'
       and sn.activated_at is not null
       and sn.validation_state = 'complete'
      join public.catalog_raw_records r
        on r.snapshot_id = sn.id
       and public.catalog_record_locator_id(sn.snapshot_key, r.upstream_record_id)
           = e.locator_record
  ), matched as (
    select l.claim_id, l.source_id, l.field_key, l.field_value, l.verdict_id,
           l.raw_record_id, l.record_key, l.upstream_record_id, l.snapshot_id,
           l.snapshot_key, l.source_family, l.resource_id,
           cand.id as candidate_id, cand.candidate_key, cand.status,
           cand.manufacturer, cand.commercial_model,
           cand.model_year_start, cand.model_year_end,
           cand.official_model_code, cand.trim as candidate_trim,
           cand.identity_dimensions
      from located l
      join public.catalog_candidate_variants_resolved cand
        on cand.snapshot_id = l.snapshot_id
       and cand.raw_record_id = l.raw_record_id
     where cand.status in ('candidate', 'ready_for_review')
       and public.catalog_candidate_identity_scope(
             cand.identity_dimensions, cand.official_model_code, cand.trim)
           = l.identity_scope
  ), unambiguous as (
    select m.claim_id from matched m
     group by m.claim_id having count(distinct m.candidate_id) = 1
  ), chosen as (
    select m.candidate_key
      from matched m join unambiguous u on u.claim_id = m.claim_id
     group by m.candidate_key
     order by m.candidate_key collate "C"
     limit v_limit
  )
  select m.candidate_id, m.candidate_key, m.status, m.snapshot_id, m.snapshot_key,
         m.source_family, m.resource_id, m.raw_record_id, m.upstream_record_id,
         m.record_key, m.manufacturer, m.commercial_model, m.model_year_start,
         m.model_year_end, m.official_model_code, m.candidate_trim,
         m.identity_dimensions, m.claim_id, m.source_id, m.verdict_id,
         m.field_key, m.field_value
    from matched m
    join unambiguous u on u.claim_id = m.claim_id
    join chosen ch on ch.candidate_key = m.candidate_key
   order by m.candidate_key collate "C", m.field_key collate "C", m.claim_id;
end;
$$;

-- work_scope_batch_for_run: restated from 20260923000100; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.work_scope_batch_for_run(p_run_id uuid)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'binding', to_jsonb(br),
    'batch', to_jsonb(b),
    'items', coalesce((select jsonb_agg(jsonb_build_object(
                         'position', i.position, 'batch_position', i.batch_position,
                         'candidate_id', i.candidate_id, 'candidate_key', i.candidate_key,
                         'manufacturer', c.manufacturer, 'commercial_model', c.commercial_model,
                         'model_year_start', c.model_year_start,
                         'model_year_end', c.model_year_end,
                         'official_model_code', c.official_model_code, 'trim', c.trim,
                         'status', c.status, 'snapshot_id', c.snapshot_id)
                         order by i.batch_position)
                       from public.catalog_work_scope_queue_items i
                       join public.catalog_candidate_variants_resolved c on c.id = i.candidate_id
                      where i.batch_id = b.id), '[]'::jsonb))
    from public.catalog_work_scope_batch_runs br
    join public.catalog_work_scope_batches b on b.id = br.batch_id
   where br.run_id = p_run_id;
$$;

-- prepare_work_scope_queue: restated from 20260930000200; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.prepare_work_scope_queue(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_preparation jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_scope_id uuid;
  v_revision integer;
  v_digest text;
  v_plan public.catalog_work_scopes;
  v_rev public.catalog_work_scope_revisions;
  v_workflow text;
  v_existing public.catalog_work_scope_preparations;
  v_units jsonb;
  v_unit jsonb;
  v_plan_units jsonb;
  v_count integer;
  v_from integer;
  v_to integer;
  v_max integer;
  v_size integer;
  v_i integer;
  v_state text;
  v_marque text;
  v_snapshot public.catalog_source_snapshots;
  v_reason text;
  v_readable integer;
  v_ambiguous integer;
  v_eligible integer;
  v_take integer;
  v_budget integer;
  v_decided jsonb := '[]'::jsonb;
  v_prepared integer := 0;
  v_queued integer := 0;
  v_batches integer := 0;
  v_preparation public.catalog_work_scope_preparations;
  v_unit_row public.catalog_work_scope_units;
  v_ids uuid[];
  v_keys text[];
  v_position integer := 0;
  v_batch_number integer := 0;
  v_batch_id uuid;
  v_in_batch integer;
  v_b integer;
  v_j integer;
  v_submitted jsonb;
  v_stored jsonb;
  -- PR-Z: the coverage ledger's part of the decision.
  v_include boolean;
  v_vocabulary text := public.catalog_vocabulary_version();
  v_enriched integer;
  v_unresolved integer;
  v_placeholder integer;
  v_excluded jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  -- Only an operator capture run prepares. A paid product run holds a lease
  -- too, and must never be able to reach Government preparation.
  if not exists (select 1 from public.runs r where r.id = p_run_id
                  and r.run_identity->>'workflow_key' = 'operator_capture') then
    raise exception 'WORK_SCOPE_PREPARATION_RUN_INVALID' using errcode = '42501';
  end if;
  if p_preparation is null or jsonb_typeof(p_preparation) <> 'object'
     or (select array_agg(k order by k collate "C") from jsonb_object_keys(p_preparation) as k)
          is distinct from array['revision', 'scope_digest', 'units', 'work_scope_id']
     or jsonb_typeof(p_preparation->'revision') <> 'number'
     or p_preparation->>'revision' !~ '^[0-9]{1,9}$'
     or jsonb_typeof(p_preparation->'scope_digest') <> 'string'
     or p_preparation->>'scope_digest' !~ '^[0-9a-f]{64}$'
     or jsonb_typeof(p_preparation->'units') <> 'array'
     or jsonb_typeof(p_preparation->'work_scope_id') <> 'string' then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end if;
  begin
    v_scope_id := (p_preparation->>'work_scope_id')::uuid;
  exception when invalid_text_representation then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end;
  v_revision := (p_preparation->>'revision')::integer;
  v_digest := p_preparation->>'scope_digest';
  v_units := p_preparation->'units';

  -- The plan row lock serializes preparation with revisions and bindings.
  select * into v_plan from public.catalog_work_scopes where id = v_scope_id for update;
  if v_plan.id is null then
    raise exception 'WORK_SCOPE_NOT_FOUND' using errcode = 'P0002';
  end if;
  select p.workflow_key into v_workflow from public.projects p where p.id = v_plan.project_id;
  if v_workflow is distinct from 'swarm_v2' then
    raise exception 'WORK_SCOPE_WORKFLOW_UNSUPPORTED' using errcode = '22023';
  end if;
  if v_plan.closed_at is not null then
    raise exception 'WORK_SCOPE_NOT_EDITABLE' using errcode = '55000';
  end if;
  -- A stale digest fails closed: only the head revision is ever prepared.
  if v_plan.head_revision is distinct from v_revision
     or v_plan.head_digest is distinct from v_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  select * into v_rev from public.catalog_work_scope_revisions
   where work_scope_id = v_scope_id and revision = v_revision;
  if v_rev.id is null or v_rev.digest is distinct from v_digest then
    raise exception 'WORK_SCOPE_STALE' using errcode = '40001';
  end if;
  v_plan_units := v_rev.scope->'units';
  v_count := jsonb_array_length(v_plan_units);
  v_from := nullif(v_rev.scope->'model_years'->>'from', '')::integer;
  v_to := nullif(v_rev.scope->'model_years'->>'to', '')::integer;
  v_max := (v_rev.scope->>'max_items')::integer;
  v_size := (v_rev.scope->>'batch_size')::integer;
  -- PR-Z: an explicit request on the revision itself to queue again what the
  -- ledger records as unresolved.
  v_include := coalesce(v_rev.scope->'include_unresolved' = 'true'::jsonb, false);

  -- The submission must name exactly the revision's units, in its order.
  if jsonb_array_length(v_units) <> v_count then
    raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
  end if;

  -- A revision is prepared once. The same submission again is a replay and
  -- answers with what was stored; a different one is refused.
  select * into v_existing from public.catalog_work_scope_preparations
   where work_scope_id = v_scope_id and revision = v_revision;
  if v_existing.id is not null then
    select coalesce(jsonb_agg(jsonb_build_object(
             'unit_key', u.unit_key, 'snapshot_id', u.snapshot_id::text,
             'register_marque', u.register_marque,
             'captured', u.state in ('prepared', 'vocabulary_insufficient'))
             order by u.priority), '[]'::jsonb)
      into v_stored from public.catalog_work_scope_units u
     where u.preparation_id = v_existing.id;
    select coalesce(jsonb_agg(jsonb_build_object(
             'unit_key', e->>'unit_key', 'snapshot_id', e->>'snapshot_id',
             'register_marque', e->>'register_marque',
             'captured', e->>'state' = 'captured')
             order by (e->>'priority')::integer), '[]'::jsonb)
      into v_submitted from jsonb_array_elements(v_units) as e;
    if v_stored is distinct from v_submitted then
      raise exception 'WORK_SCOPE_ALREADY_PREPARED' using errcode = '23505';
    end if;
    return public.work_scope_preparation_summary(v_existing.id, true);
  end if;

  -- Pass 1: decide every unit, and count, before writing anything.
  v_budget := v_max;
  for v_i in 0 .. v_count - 1 loop
    v_unit := v_units->v_i;
    if jsonb_typeof(v_unit) <> 'object'
       or (select array_agg(k order by k collate "C") from jsonb_object_keys(v_unit) as k)
            is distinct from array['priority', 'reason_code', 'register_marque',
                                   'snapshot_id', 'state', 'unit_key']
       or v_unit->>'unit_key' is distinct from v_plan_units->>v_i
       or jsonb_typeof(v_unit->'priority') <> 'number'
       or v_unit->>'priority' is distinct from (v_i + 1)::text then
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_state := v_unit->>'state';
    v_marque := case when jsonb_typeof(v_unit->'register_marque') = 'string'
                     then v_unit->>'register_marque' end;
    v_readable := 0; v_ambiguous := 0; v_eligible := 0; v_take := 0; v_reason := null;
    v_enriched := 0; v_unresolved := 0; v_placeholder := 0; v_excluded := '[]'::jsonb;
    v_snapshot := null;
    if v_state = 'register_unverified' then
      if jsonb_typeof(v_unit->'register_marque') <> 'null'
         or jsonb_typeof(v_unit->'snapshot_id') <> 'null'
         or jsonb_typeof(v_unit->'reason_code') <> 'null' then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      v_reason := 'WORK_SCOPE_REGISTER_UNVERIFIED';
    elsif v_state in ('captured', 'snapshot_unusable') then
      if v_marque is null or char_length(v_marque) not between 1 and 120
         or jsonb_typeof(v_unit->'snapshot_id') <> 'string' then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      begin
        select * into v_snapshot from public.catalog_source_snapshots
         where id = (v_unit->>'snapshot_id')::uuid;
      exception when invalid_text_representation then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end;
      -- The snapshot must be an ACTIVE Government WLTP capture that declares
      -- exactly this unit's register marque.
      if v_snapshot.id is null or v_snapshot.source_family <> 'government'
         or v_snapshot.resource_id <> '142afde2-6228-49f9-8a29-9b6c3a0cbe40'
         or v_snapshot.activated_at is null
         or v_snapshot.retrieval_metadata->'capture_scope'->'filters'->>'tozar'
              is distinct from v_marque then
        raise exception 'WORK_SCOPE_UNIT_SNAPSHOT_INVALID' using errcode = '22023';
      end if;
      -- Two units never share one snapshot: a candidate is queued once.
      if v_decided @> jsonb_build_array(jsonb_build_object('snapshot_id', v_snapshot.id)) then
        raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
      end if;
      if v_state = 'snapshot_unusable' then
        if jsonb_typeof(v_unit->'reason_code') <> 'string'
           or v_unit->>'reason_code' not in (
             'GOV_PROJECTION_SNAPSHOT_INCOMPLETE', 'GOV_PROJECTION_SNAPSHOT_NOT_READ',
             'GOV_PROJECTION_RESOURCE_NOT_NORMALIZED', 'GOV_PROJECTION_SNAPSHOT_STATE_INVALID') then
          raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
        end if;
        v_reason := v_unit->>'reason_code';
      else
        if jsonb_typeof(v_unit->'reason_code') <> 'null' then
          raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
        end if;
        -- The catalog's own readability gate, applied by the database.
        begin
          perform public.catalog_readable_snapshot(v_snapshot.id, false);
        exception when others then
          raise exception 'WORK_SCOPE_UNIT_SNAPSHOT_INVALID' using errcode = '22023';
        end;
        select count(*) filter (where c.status <> 'ambiguous'),
               count(*) filter (where c.status = 'ambiguous'),
               -- P27: a placeholder is not queueable, so it is not eligible.
               count(*) filter (where c.status = 'candidate' and not public.catalog_is_placeholder_identity(c.commercial_model, c.official_model_code))
          into v_readable, v_ambiguous, v_eligible
          from public.catalog_candidate_variants_resolved c
         where c.snapshot_id = v_snapshot.id
           and (v_from is null or c.model_year_start >= v_from)
           and (v_to is null or c.model_year_end <= v_to);
        if v_ambiguous > v_readable then
          v_state := 'vocabulary_insufficient';
          v_reason := 'WORK_SCOPE_VOCABULARY_INSUFFICIENT';
        else
          v_state := 'prepared';
          -- PR-Z: never pay twice. A queueable candidate the coverage ledger
          -- already settles at this level -- enriched from the same content,
          -- or known unresolved under the same content and vocabulary -- is
          -- left out BEFORE the plan's limit is spent, and counted with its
          -- register id. `eligible_count` keeps counting every queueable row.
          select count(*) filter (where d.decision = 'excluded_already_enriched'),
                 count(*) filter (where d.decision = 'excluded_known_unresolved'),
                 count(*) filter (where d.decision = 'excluded_placeholder_source_record'),
                 coalesce(jsonb_agg(jsonb_build_object('upstream_record_id', d.upstream_record_id,
                                                       'reason', d.decision)
                                    order by d.upstream_record_id collate "C")
                            filter (where d.decision <> 'queue'), '[]'::jsonb)
            into v_enriched, v_unresolved, v_placeholder, v_excluded
            from public.catalog_work_scope_coverage_decisions(v_snapshot.id, v_from, v_to,
                                                               v_include) as d;
          v_take := least(v_eligible - v_enriched - v_unresolved, v_budget);
          v_budget := v_budget - v_take;
          v_prepared := v_prepared + 1;
          v_queued := v_queued + v_take;
          v_batches := v_batches + (v_take + v_size - 1) / v_size;
        end if;
      end if;
    else
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_decided := v_decided || jsonb_build_object(
      'priority', v_i + 1, 'unit_key', v_unit->>'unit_key', 'register_marque', v_marque,
      'state', v_state, 'reason_code', v_reason,
      'snapshot_id', v_snapshot.id, 'snapshot_key', v_snapshot.snapshot_key,
      'capture_scope_key', v_snapshot.retrieval_metadata->'capture_scope'->>'scope_key',
      'readable', v_readable, 'ambiguous', v_ambiguous, 'eligible', v_eligible,
      'take', v_take, 'enriched', v_enriched, 'unresolved', v_unresolved,
      'placeholder', v_placeholder, 'excluded', v_excluded);
  end loop;

  -- Pass 2: write the decision, all of it or none of it.
  insert into public.catalog_work_scope_preparations
    (work_scope_id, revision, scope_digest, prepared_by_run_id, unit_count,
     prepared_unit_count, queued_item_count, batch_count)
  values (v_scope_id, v_revision, v_digest, p_run_id, v_count, v_prepared, v_queued, v_batches)
  returning * into v_preparation;

  for v_i in 0 .. v_count - 1 loop
    v_unit := v_decided->v_i;
    insert into public.catalog_work_scope_units
      (preparation_id, work_scope_id, revision, priority, unit_key, register_marque, state,
       reason_code, snapshot_id, snapshot_key, capture_scope_key, readable_count,
       ambiguous_count, eligible_count, queued_count)
    values (v_preparation.id, v_scope_id, v_revision, (v_unit->>'priority')::integer,
            v_unit->>'unit_key', v_unit->>'register_marque', v_unit->>'state',
            v_unit->>'reason_code', (v_unit->>'snapshot_id')::uuid, v_unit->>'snapshot_key',
            v_unit->>'capture_scope_key', (v_unit->>'readable')::integer,
            (v_unit->>'ambiguous')::integer, (v_unit->>'eligible')::integer,
            (v_unit->>'take')::integer)
    returning * into v_unit_row;
    -- PR-Z: what the ledger left out of this unit, counts AND register ids,
    -- recorded with the preparation and immutable like it.
    if v_unit_row.state = 'prepared' then
      insert into public.catalog_work_scope_unit_coverage
        (preparation_id, unit_id, unit_key, level, vocabulary_version, include_unresolved,
         excluded_already_enriched, excluded_known_unresolved, excluded_placeholder,
         excluded_records)
      values (v_preparation.id, v_unit_row.id, v_unit_row.unit_key, 'register', v_vocabulary,
              v_include, (v_unit->>'enriched')::integer, (v_unit->>'unresolved')::integer,
              (v_unit->>'placeholder')::integer, v_unit->'excluded');
    end if;
    v_take := (v_unit->>'take')::integer;
    if v_take = 0 then
      continue;
    end if;
    -- The canonical candidate order, the one every catalog read uses.
    select array_agg(q.id order by q.ord), array_agg(q.candidate_key order by q.ord)
      into v_ids, v_keys
      from (select c.id, c.candidate_key,
                   row_number() over (order by c.manufacturer collate "C",
                                      c.commercial_model collate "C",
                                      c.model_year_start, c.model_year_end,
                                      coalesce(c.official_model_code, '') collate "C",
                                      coalesce(c.trim, '') collate "C",
                                      c.candidate_key collate "C") as ord
              from public.catalog_candidate_variants_resolved c
             where c.snapshot_id = v_unit_row.snapshot_id and c.status = 'candidate'
               and (v_from is null or c.model_year_start >= v_from)
               and (v_to is null or c.model_year_end <= v_to)
               -- PR-Z: only what the ledger does not already settle.
               and c.id in (select d.candidate_id
                              from public.catalog_work_scope_coverage_decisions(
                                     v_unit_row.snapshot_id, v_from, v_to, v_include) as d
                             where d.decision = 'queue')) as q
     where q.ord <= v_take;
    if coalesce(array_length(v_ids, 1), 0) <> v_take then
      raise exception 'WORK_SCOPE_PREPARATION_INVALID' using errcode = '22023';
    end if;
    v_b := 0;
    while v_b * v_size < v_take loop
      v_in_batch := least(v_size, v_take - v_b * v_size);
      v_batch_number := v_batch_number + 1;
      insert into public.catalog_work_scope_batches
        (preparation_id, work_scope_id, revision, scope_digest, batch_number, unit_id,
         unit_key, snapshot_id, snapshot_key, item_count, first_position)
      values (v_preparation.id, v_scope_id, v_revision, v_digest, v_batch_number,
              v_unit_row.id, v_unit_row.unit_key, v_unit_row.snapshot_id,
              v_unit_row.snapshot_key, v_in_batch, v_position + 1)
      returning id into v_batch_id;
      for v_j in 1 .. v_in_batch loop
        v_position := v_position + 1;
        insert into public.catalog_work_scope_queue_items
          (preparation_id, batch_id, position, batch_position, unit_key, snapshot_id,
           candidate_id, candidate_key)
        values (v_preparation.id, v_batch_id, v_position, v_j, v_unit_row.unit_key,
                v_unit_row.snapshot_id, v_ids[v_b * v_size + v_j], v_keys[v_b * v_size + v_j]);
      end loop;
      v_b := v_b + 1;
    end loop;
  end loop;
  return public.work_scope_preparation_summary(v_preparation.id, false);
end;
$$;

-- catalog_check_field_provenance: restated from 20260916120000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.catalog_check_field_provenance() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_link public.catalog_candidate_evidence_links;
  v_candidate public.catalog_candidate_variants;
  v_snapshot public.catalog_source_snapshots;
  v_claim public.claims;
  v_verdict public.claim_verdicts;
  v_variant public.catalog_model_variants;
  v_model public.catalog_models;
  v_source public.sources;
  v_record public.catalog_raw_records;
  v_expected jsonb;
  v_identity jsonb;
  v_year integer;
begin
  select * into v_link from public.catalog_candidate_evidence_links
    where id = new.evidence_link_id;
  if v_link.id is null then
    raise exception 'canonical field provenance cites no catalog evidence link'
      using errcode = '23503';
  end if;

  select * into v_variant from public.catalog_model_variants where id = new.variant_id;
  if v_variant.id is null then
    raise exception 'canonical field provenance cites no canonical variant'
      using errcode = '23503';
  end if;

  select * into v_candidate from public.catalog_candidate_variants_resolved where id = new.candidate_id;
  if v_candidate.id is null then
    raise exception 'canonical field provenance cites no catalog candidate'
      using errcode = '23503';
  end if;
  -- The evidence must be evidence FOR THIS CANDIDATE. A link of another
  -- candidate, or of another snapshot, proves nothing about this one.
  if v_link.candidate_id is distinct from new.candidate_id then
    raise exception 'canonical field provenance cites evidence of another candidate'
      using errcode = '22023';
  end if;
  if v_candidate.snapshot_id is distinct from v_link.snapshot_id then
    raise exception 'canonical field provenance snapshot mismatch' using errcode = '22023';
  end if;
  -- An AMBIGUOUS, rejected or still-unreviewed candidate is not promotable.
  -- Ambiguity is a first-class answer in this schema; promoting one would be
  -- inventing the identity the source declined to state.
  if v_candidate.status is distinct from 'ready_for_review' then
    raise exception 'catalog candidate is not ready for promotion' using errcode = '22023';
  end if;

  select * into v_snapshot from public.catalog_source_snapshots where id = v_link.snapshot_id;
  if v_snapshot.id is null or v_snapshot.activated_at is null
     or v_snapshot.validation_state <> 'complete' then
    raise exception 'catalog promotion requires an active complete snapshot'
      using errcode = '22023';
  end if;
  -- The legacy catalog can never support a canonical fact. Pinned per family
  -- in `catalog_source_snapshots_trust_pinned_to_family`, so this is a
  -- restatement of a constraint rather than a second opinion.
  if v_snapshot.trust_state <> 'evidence' then
    raise exception 'an unverified catalog source cannot support a canonical fact'
      using errcode = '22023';
  end if;

  -- The link must already carry a VERIFIED verdict. `link_catalog_candidate_
  -- evidence_guarded` refuses anything else at link time; it is checked again
  -- here because a promotion is a different act and must not inherit a
  -- guarantee it did not make.
  if v_link.verdict_id is null then
    raise exception 'canonical field provenance cites unverified evidence'
      using errcode = '22023';
  end if;
  select * into v_verdict from public.claim_verdicts where id = v_link.verdict_id;
  if v_verdict.id is null or v_verdict.verdict is distinct from 'verified' then
    raise exception 'canonical field provenance verdict is not verified'
      using errcode = '22023';
  end if;
  if v_verdict.claim_id is distinct from v_link.claim_id then
    raise exception 'canonical field provenance verdict claim mismatch' using errcode = '22023';
  end if;

  select * into v_claim from public.claims where id = v_link.claim_id;
  if v_claim.id is null then
    raise exception 'canonical field provenance cites no claim' using errcode = '23503';
  end if;
  if v_claim.source_id is distinct from v_link.source_id then
    raise exception 'canonical field provenance claim source mismatch' using errcode = '22023';
  end if;
  if v_claim.status is distinct from 'active' then
    raise exception 'canonical field provenance cites a claim that is not active'
      using errcode = '22023';
  end if;
  -- THE FIELD GATE. The verified claim must support the EXACT field and the
  -- EXACT value being promoted. A verdict that confirmed the drivetrain says
  -- nothing about the model year beside it, and this is where that stops being
  -- a comment and becomes a refusal.
  if v_claim.field_key is distinct from new.field_key then
    raise exception 'canonical field provenance claim states a different field'
      using errcode = '22023';
  end if;
  if v_claim.value is distinct from new.field_value then
    raise exception 'canonical field provenance claim states a different value'
      using errcode = '22023';
  end if;

  -- An unresolved conflict means two verified sources disagree and nobody has
  -- decided. Promoting either side would be picking a winner by writing it
  -- down, so promotion waits for the resolution rather than creating one.
  if exists (select 1 from public.conflicts c
              where c.outcome = 'unresolved_needs_review'
                and c.claim_ids @> array[v_claim.id]) then
    raise exception 'canonical field provenance claim is in an unresolved conflict'
      using errcode = '22023';
  end if;

  -- An identity field's value is frozen by the canonical variant key: a
  -- revision restating one differently would be a different variant wearing
  -- this one's name.
  if public.catalog_canonical_identity_field(new.field_key) then
    v_expected := public.catalog_canonical_stated_fields(
      v_variant.model_year_start, v_variant.model_year_end,
      v_variant.official_model_code, v_variant.trim, '{}'::jsonb) -> new.field_key;
    if v_expected is distinct from new.field_value then
      raise exception 'canonical field provenance contradicts the canonical identity'
        using errcode = '22023';
    end if;
  end if;

  -- ---------------------------------------------------------------------
  -- THE VEHICLE. The candidate must be a reading OF THIS CANONICAL ROW.
  -- ---------------------------------------------------------------------
  --
  -- Everything above proves the evidence is sound. None of it proves the
  -- evidence is about the vehicle being written. Without the three gates
  -- below, a verified, located, conflict-free fact about one car could be
  -- promoted onto another, which is the worst failure this table has.
  select * into v_model from public.catalog_models where id = v_variant.model_id;
  if v_model.id is null then
    raise exception 'canonical field provenance cites no canonical model' using errcode = '23503';
  end if;
  if v_candidate.manufacturer is distinct from v_model.manufacturer
     or v_candidate.commercial_model is distinct from v_model.commercial_model then
    raise exception 'canonical field provenance cites a candidate for another vehicle'
      using errcode = '22023';
  end if;
  -- The four identity fields, which a revision may never restate differently
  -- (`catalog_canonical_identity_field`). A later snapshot's candidate may
  -- revise a DIMENSION of this variant; it may not be a candidate for a
  -- different year range, code or trim and still support this row.
  if v_candidate.model_year_start is distinct from v_variant.model_year_start
     or v_candidate.model_year_end is distinct from v_variant.model_year_end
     or v_candidate.official_model_code is distinct from v_variant.official_model_code
     or v_candidate.trim is distinct from v_variant.trim then
    raise exception 'canonical field provenance cites a candidate for another variant'
      using errcode = '22023';
  end if;

  -- ---------------------------------------------------------------------
  -- THE TIME SCOPE, and THE ENTITY.
  -- ---------------------------------------------------------------------
  if v_claim.time_scope->>'model_year' is null
     or (v_claim.time_scope->>'model_year') !~ '^[0-9]{4}$' then
    raise exception 'canonical field provenance claim states no model year scope'
      using errcode = '22023';
  end if;
  v_year := (v_claim.time_scope->>'model_year')::integer;
  if v_year < v_variant.model_year_start or v_year > v_variant.model_year_end then
    raise exception 'canonical field provenance claim is scoped to another model year'
      using errcode = '22023';
  end if;
  -- The claim's entity must be THIS canonical model at THAT model year. Exact
  -- rather than normalized: the model key is a digest this schema stores, so
  -- there is nothing to fold and nothing to guess.
  if v_claim.entity_key is distinct from
       public.catalog_claim_entity_key(v_model.canonical_key, v_year) then
    raise exception 'canonical field provenance claim is about another vehicle'
      using errcode = '22023';
  end if;

  -- ---------------------------------------------------------------------
  -- THE MARKET AND GEOGRAPHY.
  -- ---------------------------------------------------------------------
  --
  -- A vehicle fact is a fact somewhere. A claim that names no market states a
  -- value that cannot be compared to any other, and a canonical catalog built
  -- out of unscoped values is a catalog that silently mixes markets.
  if nullif(btrim(coalesce(v_claim.market, '')), '') is null
     or nullif(btrim(coalesce(v_claim.geography, '')), '') is null then
    raise exception 'canonical field provenance claim states no market scope'
      using errcode = '22023';
  end if;

  -- ---------------------------------------------------------------------
  -- THE IDENTITY SCOPE.
  -- ---------------------------------------------------------------------
  --
  -- The identity a claim narrows itself to must be EXACTLY the identity the
  -- candidate states: an extra dimension means the evidence is about a
  -- narrower vehicle than this row, a missing one means it is about a wider
  -- one, and neither is evidence for THIS variant. The key set is compared
  -- first and exactly, because a key is never normalized.
  v_identity := coalesce(v_claim.identity_scope, '{}'::jsonb);
  if v_identity is distinct from public.catalog_candidate_identity_scope(
       v_candidate.identity_dimensions, v_candidate.official_model_code,
       v_candidate.trim) then
    raise exception 'canonical field provenance claim is scoped to another vehicle identity'
      using errcode = '22023';
  end if;

  -- ---------------------------------------------------------------------
  -- THE SOURCE RECORD the evidence was read from.
  -- ---------------------------------------------------------------------
  --
  -- A candidate is a READING of one captured upstream row. The evidence that
  -- promotes it must have been read from THAT row: a locator pointing into a
  -- different record of the same snapshot is evidence about a different
  -- vehicle wearing this candidate's link.
  select * into v_record from public.catalog_raw_records
    where id = v_candidate.raw_record_id;
  if v_record.id is null then
    raise exception 'canonical field provenance cites no source record' using errcode = '23503';
  end if;
  if v_claim.evidence_locator is distinct from v_link.record_locator then
    raise exception 'canonical field provenance locator does not match its cited claim'
      using errcode = '22023';
  end if;
  if public.r3_canonical_locator(v_link.record_locator)->>1 is distinct from
       public.catalog_record_locator_id(v_snapshot.snapshot_key, v_record.upstream_record_id) then
    raise exception 'canonical field provenance cites evidence read from another source record'
      using errcode = '22023';
  end if;

  -- ---------------------------------------------------------------------
  -- ONE RUN. The lease's authority, carried down to the stored fact.
  -- ---------------------------------------------------------------------
  --
  -- `promote_catalog_variant_guarded` proves the promoting run holds a valid
  -- worker lease before it writes anything. A trigger cannot assert a lease --
  -- it does not know the token -- but it CAN refuse to let a promotion be
  -- attributed to a run other than the one that gathered, verified and linked
  -- the evidence. Together the two make the whole support chain one leased
  -- act, for every writer, including a direct INSERT that bypasses the RPC.
  select * into v_source from public.sources where id = v_link.source_id;
  if v_source.id is null then
    raise exception 'canonical field provenance cites no source' using errcode = '23503';
  end if;
  if new.run_id is distinct from v_link.run_id then
    raise exception 'canonical field provenance was not promoted by its linking run'
      using errcode = '22023';
  end if;
  if v_source.run_id is distinct from new.run_id
     or v_claim.run_id is distinct from new.run_id
     or v_verdict.run_id is distinct from new.run_id then
    raise exception 'canonical field provenance support chain spans more than one run'
      using errcode = '22023';
  end if;
  -- One promotion is ONE act: every field it writes shares its run, its
  -- worker, its attempt, its candidate and its variant.
  if exists (select 1 from public.catalog_canonical_field_provenance p
              where p.promotion_key = new.promotion_key
                and (p.run_id is distinct from new.run_id
                     or p.worker_id is distinct from new.worker_id
                     or p.attempt is distinct from new.attempt
                     or p.candidate_id is distinct from new.candidate_id
                     or p.variant_id is distinct from new.variant_id)) then
    raise exception 'catalog promotion is not one act of one run' using errcode = '22023';
  end if;
  -- Every fact about one canonical variant is read under ONE scope. The first
  -- promoted field fixes it and every later field and every later REVISION is
  -- held to it, so a variant can never accumulate facts about two markets, two
  -- model years or two vehicle identities.
  if exists (select 1 from public.catalog_canonical_field_provenance p
              where p.variant_id = new.variant_id
                and (p.entity_key is distinct from v_claim.entity_key
                     or p.market is distinct from v_claim.market
                     or p.geography is distinct from v_claim.geography
                     or p.time_scope is distinct from coalesce(v_claim.time_scope, '{}'::jsonb)
                     or p.identity_scope is distinct from v_identity)) then
    raise exception 'canonical field provenance scope disagrees with this variant'
      using errcode = '22023';
  end if;

  -- Derived, never taken from the caller; a caller that states one is held to it.
  if new.source_id is not null and new.source_id is distinct from v_link.source_id then
    raise exception 'canonical field provenance source does not match its evidence link'
      using errcode = '22023';
  end if;
  if new.claim_id is not null and new.claim_id is distinct from v_link.claim_id then
    raise exception 'canonical field provenance claim does not match its evidence link'
      using errcode = '22023';
  end if;
  if new.verdict_id is not null and new.verdict_id is distinct from v_link.verdict_id then
    raise exception 'canonical field provenance verdict does not match its evidence link'
      using errcode = '22023';
  end if;
  if new.record_locator is not null and new.record_locator is distinct from v_link.record_locator then
    raise exception 'canonical field provenance locator does not match its evidence link'
      using errcode = '22023';
  end if;
  if (new.source_version is not null and new.source_version is distinct from v_link.source_version)
     or (new.source_version_kind is not null
         and new.source_version_kind is distinct from v_link.source_version_kind) then
    raise exception 'canonical field provenance version does not match its evidence link'
      using errcode = '22023';
  end if;
  if (new.entity_key is not null and new.entity_key is distinct from v_claim.entity_key)
     or (new.market is not null and new.market is distinct from v_claim.market)
     or (new.geography is not null and new.geography is distinct from v_claim.geography)
     or (new.time_scope is not null
         and new.time_scope is distinct from coalesce(v_claim.time_scope, '{}'::jsonb))
     or (new.identity_scope is not null and new.identity_scope is distinct from v_identity) then
    raise exception 'canonical field provenance scope does not match its cited claim'
      using errcode = '22023';
  end if;
  new.entity_key := v_claim.entity_key;
  new.market := v_claim.market;
  new.geography := v_claim.geography;
  new.time_scope := coalesce(v_claim.time_scope, '{}'::jsonb);
  new.identity_scope := v_identity;
  new.model_id := v_variant.model_id;
  new.snapshot_id := v_link.snapshot_id;
  new.source_id := v_link.source_id;
  new.claim_id := v_link.claim_id;
  new.verdict_id := v_link.verdict_id;
  new.record_locator := v_link.record_locator;
  new.source_version := v_link.source_version;
  new.source_version_kind := v_link.source_version_kind;
  return new;
end;
$$;

-- promote_catalog_variant_guarded: restated from 20260916120000; only its candidate reads go through catalog_candidate_variants_resolved.
create or replace function public.promote_catalog_variant_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_promotion jsonb
) returns setof public.catalog_model_variants
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_variant public.catalog_model_variants;
  v_model public.catalog_models;
  v_candidate public.catalog_candidate_variants;
  v_link public.catalog_candidate_evidence_links;
  v_entry jsonb;
  v_promotion_key text; v_variant_key text; v_model_key text;
  v_manufacturer text; v_commercial_model text;
  v_year_start integer; v_year_end integer;
  v_code text; v_trim text; v_dimensions jsonb;
  v_requested jsonb; v_stored jsonb; v_field_key text;
  v_anchor_verdict uuid; v_revision integer;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_promotion::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:' then
    raise exception 'unsafe catalog payload rejected' using errcode = '22023';
  end if;

  v_promotion_key := nullif(p_promotion->>'promotion_key', '');
  v_variant_key := nullif(p_promotion->>'canonical_key', '');
  v_model_key := nullif(p_promotion->>'model_canonical_key', '');
  v_manufacturer := nullif(p_promotion->>'manufacturer', '');
  v_commercial_model := nullif(p_promotion->>'commercial_model', '');
  v_code := nullif(p_promotion->>'official_model_code', '');
  v_trim := nullif(p_promotion->>'trim', '');
  v_dimensions := coalesce(p_promotion->'identity_dimensions', '{}'::jsonb);
  if v_promotion_key is null or v_variant_key is null or v_model_key is null
     or v_manufacturer is null or v_commercial_model is null then
    raise exception 'invalid catalog promotion: identity is incomplete' using errcode = '22023';
  end if;
  begin
    v_year_start := (p_promotion->>'model_year_start')::integer;
    v_year_end := (p_promotion->>'model_year_end')::integer;
  exception when others then
    raise exception 'invalid catalog promotion: model year range is not whole'
      using errcode = '22023';
  end;
  if v_year_start is null or v_year_end is null then
    raise exception 'invalid catalog promotion: model year range is not whole'
      using errcode = '22023';
  end if;
  if jsonb_typeof(p_promotion->'fields') <> 'array'
     or jsonb_array_length(p_promotion->'fields') = 0 then
    raise exception 'invalid catalog promotion: no promoted fields' using errcode = '22023';
  end if;

  select * into v_candidate from public.catalog_candidate_variants_resolved
    where id = (p_promotion->>'candidate_id')::uuid;
  if v_candidate.id is null then
    raise exception 'invalid catalog promotion candidate' using errcode = '23503';
  end if;
  if v_candidate.status is distinct from 'ready_for_review' then
    raise exception 'catalog candidate is not ready for promotion' using errcode = '22023';
  end if;
  -- The promotion may not state an identity its own candidate does not. The
  -- caller derives the canonical key from these five fields, so without this
  -- the caller -- not the reviewed candidate -- would decide which vehicle a
  -- verified fact lands on.
  if v_candidate.manufacturer is distinct from v_manufacturer
     or v_candidate.commercial_model is distinct from v_commercial_model
     or v_candidate.model_year_start is distinct from v_year_start
     or v_candidate.model_year_end is distinct from v_year_end
     or v_candidate.official_model_code is distinct from v_code
     or v_candidate.trim is distinct from v_trim then
    raise exception 'catalog promotion states an identity its candidate does not'
      using errcode = '22023';
  end if;

  -- The requested field set, as one object, so the comparisons below are one
  -- equality rather than a loop that could exit early and leave a partial view.
  select coalesce(jsonb_object_agg(t.entry->>'field_key', t.entry->'value'), '{}'::jsonb)
    into v_requested
    from jsonb_array_elements(p_promotion->'fields') as t(entry);
  if v_requested <> public.catalog_canonical_stated_fields(
       v_year_start, v_year_end, v_code, v_trim, v_dimensions) then
    raise exception 'promoted catalog fields do not match the canonical row'
      using errcode = '22023';
  end if;

  -- REPLAY. The promotion key names this exact candidate, this exact variant
  -- and this exact set of field values, so anything already stored under it
  -- must be all three -- or it is a different promotion wearing the same name.
  select coalesce(jsonb_object_agg(p.field_key, p.field_value), '{}'::jsonb)
    into v_stored
    from public.catalog_canonical_field_provenance p
   where p.promotion_key = v_promotion_key;
  if v_stored <> '{}'::jsonb then
    if v_stored <> v_requested then
      raise exception 'catalog promotion idempotency conflict' using errcode = '22023';
    end if;
    select v.* into v_variant from public.catalog_model_variants v
      join public.catalog_canonical_field_provenance p on p.variant_id = v.id
     where p.promotion_key = v_promotion_key limit 1;
    if v_variant.canonical_key is distinct from v_variant_key
       or exists (select 1 from public.catalog_canonical_field_provenance p
                   where p.promotion_key = v_promotion_key
                     and p.candidate_id is distinct from v_candidate.id) then
      raise exception 'catalog promotion idempotency conflict' using errcode = '22023';
    end if;
    return next v_variant;
    return;
  end if;

  select * into v_model from public.catalog_models where canonical_key = v_model_key;
  if v_model.id is null then
    insert into public.catalog_models (manufacturer, commercial_model, canonical_key)
    values (v_manufacturer, v_commercial_model, v_model_key)
    on conflict (canonical_key) do nothing
    returning * into v_model;
    if v_model.id is null then
      select * into v_model from public.catalog_models where canonical_key = v_model_key;
    end if;
  end if;
  if v_model.manufacturer is distinct from v_manufacturer
     or v_model.commercial_model is distinct from v_commercial_model then
    raise exception 'catalog canonical model identity conflict' using errcode = '22023';
  end if;

  -- The anchor verdict: the PR1 row-level back-pointer, taken from THIS
  -- promotion's own evidence rather than chosen. The deferred trigger holds it
  -- to being one of the row's provenance verdicts.
  select l.verdict_id into v_anchor_verdict
    from jsonb_array_elements(p_promotion->'fields') as t(entry)
    join public.catalog_candidate_evidence_links l
      on l.id = (t.entry->>'evidence_link_id')::uuid
   order by t.entry->>'field_key'
   limit 1;
  if v_anchor_verdict is null then
    raise exception 'catalog promotion cites no verified evidence link' using errcode = '22023';
  end if;

  -- The canonical variant IDENTITY is the model, the year range, the code and
  -- the trim -- and `identity_dimensions` is deliberately not part of it (see
  -- `canonical_variant_key` in `backend/catalog/keys.py`). A later, better
  -- source revising a dimension therefore APPENDS a revision to this same
  -- variant instead of naming a second vehicle, which is also why
  -- `catalog_model_variants_natural_uidx` is unique on exactly those four.
  select * into v_variant from public.catalog_model_variants where canonical_key = v_variant_key;
  if v_variant.id is null then
    insert into public.catalog_model_variants
      (model_id, promoted_from_candidate_id, promoted_from_verdict_id, canonical_key,
       model_year_start, model_year_end, official_model_code, trim, identity_dimensions)
    values (v_model.id, v_candidate.id, v_anchor_verdict, v_variant_key,
            v_year_start, v_year_end, v_code, v_trim, v_dimensions)
    returning * into v_variant;
  else
    -- A key is a caller-derived string. The stored row is the authority on who
    -- it is, so a promotion presenting this key for a different vehicle is a
    -- conflict rather than a revision of the row that already has the name.
    if v_variant.model_id is distinct from v_model.id
       or v_variant.model_year_start is distinct from v_year_start
       or v_variant.model_year_end is distinct from v_year_end
       or v_variant.official_model_code is distinct from v_code
       or v_variant.trim is distinct from v_trim then
      raise exception 'catalog canonical variant identity conflict' using errcode = '22023';
    end if;
  end if;

  -- One provenance row per promoted fact, at the next revision of that field.
  for v_entry in select t.entry from jsonb_array_elements(p_promotion->'fields') as t(entry)
                  order by t.entry->>'field_key'
  loop
    v_field_key := v_entry->>'field_key';
    select * into v_link from public.catalog_candidate_evidence_links
      where id = (v_entry->>'evidence_link_id')::uuid;
    if v_link.id is null then
      raise exception 'invalid catalog promotion evidence link' using errcode = '23503';
    end if;
    select coalesce(max(p.revision), 0) + 1 into v_revision
      from public.catalog_canonical_field_provenance p
     where p.variant_id = v_variant.id and p.field_key = v_field_key;
    -- EVERY promoted field gets a row, including one whose value did not
    -- change: a later snapshot re-stating a fact is a re-verification of it,
    -- and skipping it would make a promotion's stored field set differ from
    -- the set its idempotency key was derived over -- which would turn the
    -- next honest replay into a spurious conflict.
    insert into public.catalog_canonical_field_provenance
      (model_id, variant_id, field_key, field_value, revision, candidate_id,
       evidence_link_id, snapshot_id, source_id, claim_id, verdict_id, run_id,
       worker_id, attempt, source_version, source_version_kind, record_locator,
       promotion_key)
    values (v_model.id, v_variant.id, v_field_key, v_entry->'value', v_revision,
            v_candidate.id, v_link.id, v_link.snapshot_id, v_link.source_id,
            v_link.claim_id, v_link.verdict_id, p_run_id, p_worker_id, p_attempt,
            v_link.source_version, v_link.source_version_kind, v_link.record_locator,
            v_promotion_key);
  end loop;

  return next v_variant;
end;
$$;

-- prune_register_snapshots: restated from 20261001000100; only its lock order changes: raw records first.
create or replace function public.prune_register_snapshots(p_snapshot_keys text[], p_digest text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_keys text[];
  v_ids uuid[];
  v_items text[];
  v_build_ids uuid[];
  v_build_versions text[];
  v_digest text;
  v_variants bigint;
  v_builds bigint;
  v_count bigint;
  v_repointed bigint;
  v_candidates bigint;
  v_records bigint;
  v_snapshots bigint;
begin
  if p_snapshot_keys is null or p_digest is null or p_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'CATALOG_PRUNE_REQUEST_INVALID: a key list and a digest are required'
      using errcode = '22023';
  end if;
  -- Lock the source and variant tables first, so the lists cannot move under the check.
  -- Raw records first, the compaction's order (a raw-record writer inserts there before
  -- it updates its snapshot, so neither can deadlock it).
  lock table public.catalog_raw_records, public.catalog_source_snapshots,
             public.catalog_candidate_variants, public.catalog_variant_builds,
             public.catalog_variants, public.catalog_variant_coverage in share row exclusive mode;
  select coalesce(array_agg(p.snapshot_key order by p.snapshot_key collate "C"), '{}'),
         coalesce(array_agg(p.snapshot_id), '{}')
    into v_keys, v_ids
    from public.catalog_register_prunable_snapshots() p;
  select coalesce(array_agg(b.item order by b.item collate "C"), '{}'),
         coalesce(array_agg(b.snapshot_id order by b.item collate "C"), '{}'),
         coalesce(array_agg(b.mapper_version order by b.item collate "C"), '{}')
    into v_items, v_build_ids, v_build_versions
    from public.catalog_register_prunable_variant_builds() b;
  v_digest := public.catalog_register_prune_digest(v_keys || v_items);
  if v_digest <> p_digest
     or public.catalog_register_prune_digest(p_snapshot_keys) <> p_digest then
    raise exception 'CATALOG_PRUNE_DIGEST_MISMATCH: the prunable list changed or the digest is not its digest'
      using errcode = '40001';
  end if;
  if cardinality(v_ids) = 0 and cardinality(v_items) = 0 then
    return jsonb_build_object('snapshots', 0, 'raw_records', 0, 'candidates', 0, 'variants', 0,
                              'variant_builds', 0, 'ledger_repointed', 0, 'digest', v_digest);
  end if;
  -- Ledger rows at the variant levels that name a pruned snapshot: where its
  -- tozar's CURRENT build states the key (a newer snapshot's partial build
  -- wrote them, then was superseded), they are re-pointed to that build, by
  -- the build's own rule; any other is kept unchanged, as history.
  with cur as (
    select distinct on (b.tozar) b.tozar, b.snapshot_id, b.snapshot_key
      from public.catalog_variant_builds b
     where b.mapper_version = public.catalog_variant_mapper_version() and b.completed_at is not null
     order by b.tozar, b.activated_at desc, b.snapshot_id
  ),
  named as (
    select cv.id, cv.variant_identity_key as k, c.snapshot_id as cur_id, c.snapshot_key as cur_key
      from public.catalog_variant_coverage cv
      join public.catalog_source_snapshots ps on ps.snapshot_key = cv.snapshot_key and ps.id = any(v_ids)
      join cur c on c.tozar = ps.retrieval_metadata->'capture_scope'->'filters'->>'tozar'
     where cv.level in ('identity', 'government_fields')
  ),
  contents as (
    select n.k, n.cur_id, n.cur_key, count(distinct v.content_sha256) as distinct_contents,
           min(v.content_sha256 collate "C") as only_content,
           encode(sha256(convert_to(string_agg(distinct v.content_sha256, ','
                                               order by v.content_sha256), 'UTF8')), 'hex') as collision_content
      from (select distinct k, cur_id, cur_key from named) n
      join public.catalog_variants v
        on v.snapshot_id = n.cur_id and v.variant_identity_key = n.k
       and v.mapper_version = public.catalog_variant_mapper_version()
     group by n.k, n.cur_id, n.cur_key
  )
  update public.catalog_variant_coverage cv
     set status = case when x.distinct_contents > 1 then 'failed' else 'enriched' end,
         last_run_id = s.created_by_run_id, snapshot_key = x.cur_key,
         content_sha256 = case when x.distinct_contents > 1 then x.collision_content else x.only_content end,
         vocabulary_version = public.catalog_vocabulary_version(),
         reason_code = case when x.distinct_contents > 1 then 'CATALOG_COVERAGE_KEY_COLLISION' end,
         updated_at = now()
    from named n
    join contents x on x.k = n.k and x.cur_id = n.cur_id
    join public.catalog_source_snapshots s on s.id = x.cur_id
   where cv.id = n.id;
  get diagnostics v_repointed = row_count;

  alter table public.catalog_variants disable trigger catalog_variants_append_only;
  delete from public.catalog_variants where snapshot_id = any(v_ids);
  get diagnostics v_variants = row_count;
  delete from public.catalog_variants v
   using unnest(v_build_ids, v_build_versions) as u(snapshot_id, mapper_version)
   where v.snapshot_id = u.snapshot_id and v.mapper_version = u.mapper_version;
  get diagnostics v_count = row_count;
  v_variants := v_variants + v_count;
  alter table public.catalog_variants enable trigger catalog_variants_append_only;
  delete from public.catalog_variant_builds where snapshot_id = any(v_ids);
  get diagnostics v_builds = row_count;
  delete from public.catalog_variant_builds b
   using unnest(v_build_ids, v_build_versions) as u(snapshot_id, mapper_version)
   where b.snapshot_id = u.snapshot_id and b.mapper_version = u.mapper_version;
  get diagnostics v_count = row_count;
  v_builds := v_builds + v_count;
  alter table public.catalog_candidate_variants disable trigger catalog_candidate_variants_identity_immutable;
  alter table public.catalog_raw_records disable trigger catalog_raw_records_append_only;
  alter table public.catalog_source_snapshots disable trigger catalog_source_snapshots_append_only;
  delete from public.catalog_candidate_variants where snapshot_id = any(v_ids);
  get diagnostics v_candidates = row_count;
  delete from public.catalog_raw_records where snapshot_id = any(v_ids);
  get diagnostics v_records = row_count;
  delete from public.catalog_source_snapshots where id = any(v_ids);
  get diagnostics v_snapshots = row_count;
  alter table public.catalog_candidate_variants enable trigger catalog_candidate_variants_identity_immutable;
  alter table public.catalog_raw_records enable trigger catalog_raw_records_append_only;
  alter table public.catalog_source_snapshots enable trigger catalog_source_snapshots_append_only;
  return jsonb_build_object('snapshots', v_snapshots, 'raw_records', v_records,
                            'candidates', v_candidates, 'variants', v_variants,
                            'variant_builds', v_builds, 'ledger_repointed', v_repointed, 'digest', v_digest);
end;
$$;

-- ---------------------------------------------------------------------------
-- 8b. No variant is written for a compacted snapshot: its payloads are gone
--     (the build needs them), and its rows answer through their compaction.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_variant_compacted_guard() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if exists (select 1 from public.catalog_register_snapshot_compactions k where k.snapshot_id = new.snapshot_id) then
    raise exception 'CATALOG_VARIANT_SNAPSHOT_COMPACTED: a compacted snapshot is not built again'
      using errcode = '55000';
  end if;
  return new;
end;
$$;
drop trigger if exists catalog_variants_compacted_guard on public.catalog_variants;
create trigger catalog_variants_compacted_guard
  before insert on public.catalog_variants
  for each row execute function public.catalog_variant_compacted_guard();

-- ---------------------------------------------------------------------------
-- 8c. Operator maintenance: what must be quiet before a VACUUM (FULL) of the
--     compacted tables (it holds ACCESS EXCLUSIVE while it rewrites them),
--     and the deployed gate's mapper check (a compacted snapshot is read
--     through its compaction's mapper version; a bump would hide it).
-- ---------------------------------------------------------------------------
create or replace function public.catalog_register_maintenance_blockers()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select jsonb_build_object(
    'live_runs', (select count(*) from public.runs r
                   where r.status not in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out',
                                          'budget_exhausted')),
    -- Every register capture with a unit not yet terminal, stale or not: a
    -- VACUUM FULL must never wait on, or be waited on by, a capture.
    'live_register_groups', (select count(*) from public.catalog_register_capture_groups g
                              where exists (select 1 from public.catalog_register_capture_units u
                                             where u.group_id = g.id and u.status in ('requested', 'capturing'))))
$$;

create or replace function public.catalog_register_compaction_mapper_mismatches()
returns integer
language sql
stable
set search_path = pg_catalog
as $$
  select count(*)::integer from public.catalog_register_snapshot_compactions k
   where k.readers = 'variants' and k.mapper_version <> public.catalog_variant_mapper_version()
     -- A compacted snapshot since pruned, or kept as skeletons, is read from its archive.
     and exists (select 1 from public.catalog_source_snapshots s where s.id = k.snapshot_id)
     and not exists (select 1 from public.catalog_register_snapshot_compactions a
                      where a.snapshot_id = k.snapshot_id and a.readers = 'archive')
$$;

-- ---------------------------------------------------------------------------
-- 9. RLS and privileges (PR-D1's pattern).
-- ---------------------------------------------------------------------------
alter table public.catalog_register_snapshot_compactions enable row level security;

do $$
declare
  fn text;
  ro record;
  v_service text[] := array[
    'public.catalog_raw_record_payload_required()',
    'public.catalog_candidate_identity_required()',
    'public.catalog_variant_compacted_guard()',
    'public.catalog_variant_candidate_reading(public.catalog_variants)',
    'public.catalog_candidate_reads_as_variant(public.catalog_candidate_variants,public.catalog_variants)',
    'public.catalog_compaction_answer(text,public.catalog_register_snapshot_compactions)',
    'public.catalog_candidate_referenced(uuid)',
    'public.catalog_register_superseded_snapshots(text)',
    'public.catalog_raw_record_lines_mismatched(uuid,integer,text[])',
    'public.catalog_register_snapshot_archivable(uuid)',
    'public.catalog_register_maintenance_blockers()',
    'public.catalog_register_compaction_mapper_mismatches()',
    'public.catalog_raw_record_code(public.catalog_raw_records,text)',
    'public.catalog_raw_record_content_sha256(public.catalog_raw_records)',
    'public.catalog_variant_field_reads_as(jsonb,text,boolean,boolean)',
    'public.catalog_variant_reads_as_payload(jsonb,public.catalog_variants)',
    'public.compact_register_snapshot(text,boolean)',
    'public.catalog_compacted_record_reading(uuid,text,boolean)',
    'public.catalog_raw_record_payload_matches(uuid,text)',
    'public.record_register_snapshot_archive_from_database(uuid,text,bigint,text,integer)'];
begin
  foreach fn in array v_service loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke execute on function %s from anon', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke execute on function %s from authenticated', fn);
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
      execute format('grant execute on function %s to service_role', fn);
    end if;
  end loop;
  -- The triggers' functions are called by their triggers only.
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke execute on function public.catalog_raw_record_payload_required() from service_role';
    execute 'revoke execute on function public.catalog_candidate_identity_required() from service_role';
    execute 'revoke execute on function public.catalog_variant_compacted_guard() from service_role';
  end if;

  -- The view every candidate reader reads (security invoker: the caller's
  -- own privileges on the tables and the reading function apply).
  execute 'revoke all on table public.catalog_candidate_variants_resolved from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table public.catalog_candidate_variants_resolved from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table public.catalog_candidate_variants_resolved from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant select on table public.catalog_candidate_variants_resolved to service_role';
  end if;

  execute 'revoke all on table public.catalog_register_snapshot_compactions from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table public.catalog_register_snapshot_compactions from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table public.catalog_register_snapshot_compactions from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    -- Written only by compact_register_snapshot (security definer).
    execute 'grant select on table public.catalog_register_snapshot_compactions to service_role';
    execute 'revoke insert, update, delete, truncate on table public.catalog_register_snapshot_compactions from service_role';
  end if;

  -- The read-only release roles, exactly as 20260929000100 selects them: the
  -- compaction record (retention and coverage read their tables directly).
  for ro in
    select r.rolname from pg_roles r
     where r.rolcanlogin and not r.rolsuper
       and r.rolname not in ('postgres', 'service_role', 'authenticator', 'anon', 'authenticated')
       and (r.rolname = 'supabase_read_only_user'
            or (r.rolbypassrls and r.rolname like 'milo\_release\_readonly\_%')
            or (r.rolbypassrls and pg_has_role(r.oid, 'pg_read_all_data', 'MEMBER')))
  loop
    execute format('grant select on table public.catalog_register_snapshot_compactions to %I', ro.rolname);
    execute format('grant select on table public.catalog_candidate_variants_resolved to %I', ro.rolname);
    execute format('grant execute on function public.catalog_variant_candidate_reading(public.catalog_variants) to %I',
                   ro.rolname);
    execute format('grant execute on function public.catalog_register_maintenance_blockers() to %I', ro.rolname);
    execute format('grant execute on function public.catalog_register_compaction_mapper_mismatches() to %I',
                   ro.rolname);
    -- The operator's space reclamation (register-retention, operation
    -- vacuum-full) runs VACUUM (FULL, ANALYZE) as the release read-only role:
    -- MAINTAIN on exactly the two tables compaction shrinks (PostgreSQL 17;
    -- older servers have no such privilege and are left as they are).
    if ro.rolname like 'milo\_release\_readonly\_%' and current_setting('server_version_num')::integer >= 170000 then
      execute format('grant maintain on table public.catalog_raw_records, public.catalog_candidate_variants to %I',
                     ro.rolname);
    end if;
  end loop;
end $$;
