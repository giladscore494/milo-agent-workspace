-- PR-L2: the register stays in the database WITHOUT its raw payload.
--
-- Every register snapshot has an immutable, sha256-verified Cloud Storage
-- archive with one line per record (PR-D1), and its typed variant rows hold
-- every level-1 and level-1.5 field (PR-L1 / L1b). Once both are in place the
-- stored `catalog_raw_records.payload` is redundant: at ~1.6 KB compressed per
-- row it is the largest single cost of a register row.
--
-- What it changes
-- ---------------
--
--   * `catalog_raw_records.payload` may be NULL -- only for a COMPACTED
--     snapshot. A new raw record still needs a payload (a before-insert
--     trigger; the guarded ingest writers already refuse a non-object).
--     Every row stays: id, snapshot, upstream id, payload_sha256, record_key
--     and source_locator (the archive line) are untouched, so every foreign
--     key, evidence link, field provenance, queue item, reservation and
--     adoption stays valid. Candidates are not touched at all: they hold the
--     identity columns the queue build and the Government Tool filter on.
--   * compact_register_snapshot(snapshot_key, apply): the ONE place the
--     raw records' append-only trigger is suspended for an UPDATE (security
--     definer, search_path pinned, service_role only), like the prunes. It
--     refuses, with a static code and without writing, unless: the snapshot
--     is an activated (active or retained) whole-tozar Government snapshot;
--     its archive is recorded with sha256, byte size and one line per stored
--     row; its capture is count-verified (the newest register unit that
--     captured it) or, with no unit, its stored rows equal its declared rows;
--     its variant build under the CURRENT mapper version is complete and
--     covers every raw row (count equality); and every row's typed variant
--     reads exactly as its payload for everything a reader takes from the
--     payload (catalog_variant_reads_as_payload). `apply = false` is the
--     dry-run. An already compacted snapshot answers `unchanged`.
--   * The readers that took facts from the payload now take them through
--     two helpers that read the payload while it exists and the compacted
--     snapshot's variant row afterwards:
--       catalog_raw_record_code(r, field)      `payload->>field` for the three
--                                               register codes
--       catalog_raw_record_content_sha256(r)   catalog_variant_content_sha256(payload)
--     restated with only those expressions changed: the coverage decisions
--     (Prepare's queue build and its placeholder filter),
--     catalog_variant_coverage_for_batch, catalog_variant_coverage_apply,
--     acquire_catalog_variant_reservations_guarded and
--     catalog_candidate_variant_page (the Government Tool's page).
--   * catalog_compacted_record_reading(): ONE row's typed identity reading
--     (the resolve_variant identity projection and REGISTER_FIELD_ABSENT's
--     "the register states nothing": typed column null AND no parse issue).
--   * record_register_snapshot_archive_from_database(): a Prepare snapshot
--     (captured before PR-D1, no archive) gets its archive written from its
--     stored rows in capture order by PR-D1's writer, then recorded here.
--   * catalog_raw_record_payload_matches(): an archive line fetched from
--     Cloud Storage is checked against the row's payload_sha256 by the
--     database's own jsonb rendering (the digest is storage-local).
--   * catalog_register_snapshot_compactions: one append-only row per
--     compacted snapshot (the mapper version whose rows now answer); like the
--     archive record, history that outlives a prune (no foreign key).
--
-- A compacted snapshot is not rebuilt under a new mapper version (the build
-- needs the payload: CATALOG_VARIANT_SNAPSHOT_COMPACTED); rebuilding one from
-- its archive is a follow-up. Forward-only and rerun-safe.

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
-- 2. The compaction record.
-- ---------------------------------------------------------------------------
create table if not exists public.catalog_register_snapshot_compactions (
  -- Not a foreign key, on purpose (as catalog_register_snapshot_archives): the
  -- record outlives a pruned snapshot, and every catalog key RESTRICTs.
  snapshot_id uuid primary key,
  snapshot_key text not null,
  -- The variant rows that answer for the payload from now on.
  mapper_version text not null check (mapper_version ~ '^[A-Za-z0-9._:-]{1,80}$'),
  raw_rows integer not null check (raw_rows >= 0),
  bytes_before bigint not null check (bytes_before >= 0),
  bytes_after bigint not null check (bytes_after >= 0),
  compacted_at timestamptz not null default now()
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
                where k.snapshot_id = p_record.snapshot_id) end
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
                where k.snapshot_id = p_record.snapshot_id) end
$$;

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
create or replace function public.compact_register_snapshot(p_snapshot_key text, p_apply boolean)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_filters jsonb;
  v_archive public.catalog_register_snapshot_archives%rowtype;
  v_build public.catalog_variant_builds%rowtype;
  v_done public.catalog_register_snapshot_compactions%rowtype;
  v_mapper text := public.catalog_variant_mapper_version();
  v_verified boolean;
  v_raw bigint;
  v_variants bigint;
  v_mismatched bigint;
  v_before bigint;
  v_payload bigint;
  v_after bigint;
  v_updated bigint;
begin
  if p_snapshot_key is null or p_snapshot_key !~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$' or p_apply is null then
    raise exception 'CATALOG_COMPACTION_REQUEST_INVALID: one snapshot key and apply true/false'
      using errcode = '22023';
  end if;
  if p_apply then
    -- A prune, a build and another compaction wait (and are waited for).
    lock table public.catalog_source_snapshots, public.catalog_raw_records, public.catalog_variant_builds,
               public.catalog_variants, public.catalog_register_snapshot_compactions in share row exclusive mode;
  end if;
  select * into v_snapshot from public.catalog_source_snapshots
   where snapshot_key = p_snapshot_key and source_family = 'government';
  if v_snapshot.id is null then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_SNAPSHOT_UNKNOWN',
                              'snapshot_key', p_snapshot_key);
  end if;
  select * into v_done from public.catalog_register_snapshot_compactions where snapshot_id = v_snapshot.id;
  if v_done.snapshot_id is not null then
    return jsonb_build_object('status', 'unchanged', 'snapshot_key', p_snapshot_key,
                              'raw_rows', v_done.raw_rows, 'mapper_version', v_done.mapper_version,
                              'bytes_before', v_done.bytes_before, 'bytes_after', v_done.bytes_after);
  end if;
  v_filters := v_snapshot.retrieval_metadata->'capture_scope'->'filters';
  if v_snapshot.activated_at is null or v_snapshot.validation_state <> 'complete'
     or jsonb_typeof(v_filters) is distinct from 'object'
     or (case when jsonb_typeof(v_filters) = 'object'
              then (select array_agg(k) from jsonb_object_keys(v_filters) k) end) is distinct from array['tozar'] then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE',
                              'snapshot_key', p_snapshot_key);
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
  select * into v_build from public.catalog_variant_builds
   where snapshot_id = v_snapshot.id and mapper_version = v_mapper;
  select count(*) into v_variants from public.catalog_variants
   where snapshot_id = v_snapshot.id and mapper_version = v_mapper;
  if v_build.snapshot_id is null or v_build.completed_at is null
     or v_build.built_rows <> v_raw or v_build.expected_rows <> v_raw or v_variants <> v_raw then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_BUILD_INCOMPLETE',
                              'snapshot_key', p_snapshot_key, 'raw_rows', v_raw, 'variant_rows', v_variants);
  end if;
  select count(*) filter (where v.id is null or not public.catalog_variant_reads_as_payload(r.payload, v)),
         coalesce(sum(pg_column_size(r.payload)), 0)
    into v_mismatched, v_payload
    from public.catalog_raw_records r
    left join public.catalog_variants v
      on v.snapshot_id = r.snapshot_id and v.upstream_record_id = r.upstream_record_id
     and v.mapper_version = v_mapper
   where r.snapshot_id = v_snapshot.id;
  if v_mismatched > 0 then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_TYPED_MISMATCH',
                              'snapshot_key', p_snapshot_key, 'mismatched_rows', v_mismatched);
  end if;
  -- Last, so a dry-run's ARCHIVE_MISSING says every other check passed.
  select * into v_archive from public.catalog_register_snapshot_archives where snapshot_id = v_snapshot.id;
  if v_archive.id is null or v_archive.sha256 !~ '^[0-9a-f]{64}$' or v_archive.byte_size <= 0
     or v_archive.line_count <> v_raw then
    return jsonb_build_object('status', 'refused', 'code', 'CATALOG_COMPACTION_ARCHIVE_MISSING',
                              'snapshot_key', p_snapshot_key);
  end if;
  v_before := public.catalog_register_measured_bytes(v_snapshot.id);
  if not p_apply then
    return jsonb_build_object('status', 'ready', 'snapshot_key', p_snapshot_key, 'raw_rows', v_raw,
                              'mapper_version', v_mapper, 'bytes_before', v_before,
                              'payload_bytes', v_payload);
  end if;

  alter table public.catalog_raw_records disable trigger catalog_raw_records_append_only;
  update public.catalog_raw_records set payload = null
   where snapshot_id = v_snapshot.id and payload is not null;
  get diagnostics v_updated = row_count;
  alter table public.catalog_raw_records enable trigger catalog_raw_records_append_only;

  v_after := public.catalog_register_measured_bytes(v_snapshot.id);
  insert into public.catalog_register_snapshot_compactions
    (snapshot_id, snapshot_key, mapper_version, raw_rows, bytes_before, bytes_after)
  values (v_snapshot.id, v_snapshot.snapshot_key, v_mapper, v_raw, v_before, v_after);
  -- The Register page's measured bytes: the units that captured it.
  update public.catalog_register_capture_units
     set measured_bytes = v_after,
         measurement_method = 'pg_column_size(raw_records+candidates+variants+ledger)', measured_at = now()
   where snapshot_id = v_snapshot.id and status = 'captured';
  return jsonb_build_object('status', 'compacted', 'snapshot_key', p_snapshot_key, 'raw_rows', v_raw,
                            'payloads_removed', v_updated, 'mapper_version', v_mapper,
                            'bytes_before', v_before, 'bytes_after', v_after);
end;
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
     where k.snapshot_id = p_snapshot_id and v.upstream_record_id = p_upstream_record_id);
end;
$$;

-- An archive line (the record's canonical JSON) is the stored row's content
-- exactly when the database's own rendering of it has the row's digest.
create or replace function public.catalog_raw_record_payload_matches(p_raw_record_id uuid, p_line text)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce((select r.payload_sha256 = encode(sha256(convert_to(p_line::jsonb::text, 'UTF8')), 'hex')
                     from public.catalog_raw_records r where r.id = p_raw_record_id), false)
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
create or replace function public.record_register_snapshot_archive_from_database(
  p_snapshot_id uuid, p_gcs_uri text, p_byte_size bigint, p_sha256 text, p_line_count integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_filters jsonb;
  v_row public.catalog_register_snapshot_archives%rowtype;
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
  if p_line_count is null or v_rows <> p_line_count or v_positions <> v_rows
     or v_snapshot.declared_record_count <> p_line_count or v_snapshot.stored_record_count <> p_line_count
     or (v_rows > 0 and (v_low <> 0 or v_high <> v_rows - 1)) then
    raise exception 'CATALOG_CAPTURE_COUNT_MISMATCH: the archive does not hold exactly the stored rows'
      using errcode = '22023';
  end if;
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
-- 8. The readers, restated with only the payload expressions changed:
--      r.payload->>'<code>'                         -> catalog_raw_record_code(r, '<code>')
--      catalog_variant_content_sha256(r.payload)    -> catalog_raw_record_content_sha256(r)
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
        join public.catalog_candidate_variants c on c.id = i.candidate_id
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
    join public.catalog_candidate_variants c
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
      join public.catalog_candidate_variants c on c.id = (e->>'candidate_id')::uuid
      join public.catalog_raw_records r on r.id = c.raw_record_id
  ),
  peers as (
    select k.identity_key, k.content
      from (select distinct x.manufacturer, x.commercial_model, x.model_year_start,
                   x.model_year_end, x.official_model_code, x.trim from x) as w
      join public.catalog_candidate_variants c
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
        join public.catalog_candidate_variants c on c.id = (e #>> '{}')::uuid
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
      from public.catalog_candidate_variants c
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
    from public.catalog_candidate_variants c
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
  -- The trigger's function is called by its trigger only.
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'revoke execute on function public.catalog_raw_record_payload_required() from service_role';
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
  end loop;
end $$;
