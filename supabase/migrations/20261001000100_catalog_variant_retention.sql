-- PR-L1b: storage and review follow-ups for PR-L1 (catalog variants) before
-- capturing more of the register. Applied together with 20260929000100,
-- 20260930000100 and 20260930000200 (none of them is in production yet).
--
-- What it changes
-- ---------------
--
--   * Retention: a SUPERSEDED snapshot with variants is prunable again.
--     catalog_register_prunable_snapshots() keeps PR-D1's rule (the two
--     latest activations per tozar; every snapshot referenced by evidence,
--     claims, runs, work scopes, the latest captured unit or a live writer)
--     except that the coverage ledger keeps a snapshot only through a
--     `register`-level row, and variants keep only the CURRENT build of a
--     tozar (the one catalog_variants_current serves). Ledger rows at
--     `identity` / `government_fields` naming a pruned snapshot never block
--     the prune and are never deleted: where the tozar's current build states
--     the same key (e.g. a newer snapshot's partial build wrote the row, then
--     was superseded) the prune RE-POINTS the row to that build, by the
--     build's own rule; otherwise the row is KEPT AS HISTORY, unchanged (the
--     register no longer states that key; a captured snapshot's archive, which
--     a prune never touches, keeps it citable).
--   * Retention: the variant rows of an OLD mapper version are prunable once
--     the same snapshot's build under the current mapper version is complete
--     (catalog_register_prunable_variant_builds()). The dry-run lists both,
--     with estimated bytes, and the digest covers both: a variant build's item
--     is `<snapshot_key> <mapper_version>` (a snapshot key has no space).
--   * prune_register_snapshots() deletes, in its one transaction: the variants
--     and variant builds, then candidates, raw records and snapshots as
--     before. The variants' append-only trigger is suspended ONLY there, like
--     the source tables' (and re-enabled before it returns).
--   * Eligibility: only the ACTIVE snapshot of a tozar (rank 1: the latest
--     activation) is built: CATALOG_VARIANT_SNAPSHOT_SUPERSEDED otherwise. A
--     Prepare snapshot no register unit captured stays eligible: its stored
--     rows equal its declared rows (the existing gate) and no capture unit
--     recorded it count-unverified.
--   * Row size: the `equipment` document becomes two 19-bit masks
--     (`equipment_stated`: which indicators the register states,
--     `equipment_on`: which of those are 1) and `equipment_sources`, the five
--     *_makor_hatkana texts in `catalog_variant_equipment_keys()` order (null
--     when the row states none). Same information, same closed-list check on
--     the mapper's document; existing rows are converted IN PLACE under the
--     same mapper version (none exist in production). Reads return the same
--     `equipment` document (catalog_variant_equipment()).
--   * The discovery tree resolves the tozar's current snapshot first and reads
--     by snapshot id (the tree index), never the whole table.
--   * Measured bytes: a snapshot's measured storage includes its variants and
--     its ledger rows at the two variant levels; a completed build refreshes
--     the measurement of the register units that captured it, and a unit
--     captured on a snapshot that is already built is measured the same way.
--   * A mapper version is one token (it is half of a digest item).
--
-- Nothing else moves: no raw record, candidate, content hash, Prepare, run or
-- `register`-level ledger row. Forward-only and rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. Compact equipment storage.
-- ---------------------------------------------------------------------------

-- The closed key list, in bit order: 19 indicators (bit i-1), then the five
-- sources (equipment_sources[i-19]). `variants.EQUIPMENT_FIELDS`, pinned by a test.
create or replace function public.catalog_variant_equipment_keys()
returns text[]
language sql
immutable
set search_path = pg_catalog
as $$
  select array[
    'bakarat_mehirut_isa', 'bakarat_shyut_adaptivit_ind', 'bakarat_stiya_activ_s',
    'bakarat_stiya_menativ_ind', 'blima_otomatit_nesia_leahor',
    'blimat_hirum_lifnei_holhei_regel_ofanaim', 'hayshaney_hagorot_ind',
    'hayshaney_lahatz_avir_batzmigim_ind', 'hitnagshut_cad_shetah_met',
    'maarechet_ezer_labalam_ind', 'matzlemat_reverse_ind', 'nitur_merhak_milfanim_ind',
    'shlita_automatit_beorot_gvohim_ind', 'teura_automatit_benesiya_kadima_ind',
    'zihuy_beshetah_nistar_ind', 'zihuy_holchey_regel_ind',
    'zihuy_matzav_hitkarvut_mesukenet_ind', 'zihuy_rechev_do_galgali',
    'zihuy_tamrurey_tnua_ind',
    'bakarat_stiya_menativ_makor_hatkana', 'nitur_merhak_milfanim_makor_hatkana',
    'shlita_automatit_beorot_gvohim_makor_hatkana', 'zihuy_holchey_regel_makor_hatkana',
    'zihuy_tamrurey_tnua_makor_hatkana']::text[]
$$;

-- A (valid) equipment document's indicator mask: stated (p_on false) or 1 (p_on true).
create or replace function public.catalog_variant_equipment_mask(p_equipment jsonb, p_on boolean)
returns integer
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce(sum(1 << (k.i::integer - 1)), 0)::integer
    from unnest((public.catalog_variant_equipment_keys())[1:19]) with ordinality as k(name, i)
   where p_equipment ? k.name and (not p_on or p_equipment->>k.name = '1')
$$;

-- A (valid) equipment document's five sources, or null when it states none.
create or replace function public.catalog_variant_equipment_source_texts(p_equipment jsonb)
returns text[]
language sql
immutable
set search_path = pg_catalog
as $$
  select case when p_equipment ?| (public.catalog_variant_equipment_keys())[20:24] then
    array(select p_equipment->>k.name
            from unnest((public.catalog_variant_equipment_keys())[20:24]) with ordinality as k(name, i)
           order by k.i) end
$$;

create or replace function public.catalog_variant_equipment_sources_valid(p_sources text[])
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select p_sources is null
      or (array_ndims(p_sources) = 1 and array_lower(p_sources, 1) = 1 and cardinality(p_sources) = 5
          and exists (select 1 from unnest(p_sources) s where s is not null)
          and not exists (select 1 from unnest(p_sources) s where char_length(s) not between 1 and 80))
$$;

-- The stored columns -> the mapper's document, exactly (the browser's `equipment`).
create or replace function public.catalog_variant_equipment(p_stated integer, p_on integer, p_sources text[])
returns jsonb
language sql
immutable
set search_path = pg_catalog
as $$
  select coalesce(jsonb_object_agg(e.name, e.value), '{}'::jsonb)
    from (select k.name, to_jsonb(case when p_on & (1 << (k.i::integer - 1)) <> 0 then 1 else 0 end) as value
            from unnest((public.catalog_variant_equipment_keys())[1:19]) with ordinality as k(name, i)
           where p_stated & (1 << (k.i::integer - 1)) <> 0
          union all
          select k.name, to_jsonb(p_sources[k.i])
            from unnest((public.catalog_variant_equipment_keys())[20:24]) with ordinality as k(name, i)
           where p_sources[k.i] is not null) e
$$;

-- The view expands v.*: it is rebuilt around the new columns below.
drop view if exists public.catalog_variants_current;

alter table public.catalog_variants add column if not exists equipment_stated integer not null default 0;
alter table public.catalog_variants add column if not exists equipment_on integer not null default 0;
alter table public.catalog_variants add column if not exists equipment_sources text[];
alter table public.catalog_variants drop constraint if exists catalog_variants_equipment_check;

do $$
begin
  if exists (select 1 from information_schema.columns
              where table_schema = 'public' and table_name = 'catalog_variants' and column_name = 'equipment') then
    -- In place, same mapper version: the same information, re-encoded.
    alter table public.catalog_variants disable trigger catalog_variants_append_only;
    update public.catalog_variants
       set equipment_stated = public.catalog_variant_equipment_mask(equipment, false),
           equipment_on = public.catalog_variant_equipment_mask(equipment, true),
           equipment_sources = public.catalog_variant_equipment_source_texts(equipment)
     where equipment <> '{}'::jsonb;
    alter table public.catalog_variants enable trigger catalog_variants_append_only;
    alter table public.catalog_variants drop column equipment;
  end if;
end $$;

alter table public.catalog_variants add constraint catalog_variants_equipment_check
  check (equipment_stated between 0 and 524287 and equipment_on >= 0
         and (equipment_on & ~equipment_stated) = 0
         and public.catalog_variant_equipment_sources_valid(equipment_sources));

create or replace view public.catalog_variants_current
with (security_invoker = true) as
  select v.*
    from public.catalog_variants v
    join (select distinct on (b.tozar) b.snapshot_id
            from public.catalog_variant_builds b
           where b.mapper_version = public.catalog_variant_mapper_version()
             and b.completed_at is not null
           order by b.tozar, b.activated_at desc, b.snapshot_id) cur
      on cur.snapshot_id = v.snapshot_id
   where v.mapper_version = public.catalog_variant_mapper_version();

-- ---------------------------------------------------------------------------
-- 2. Measured bytes: what a snapshot's rows occupy, and with its ledger rows.
-- ---------------------------------------------------------------------------

-- Raw records + candidates (PR-D1) + variants of every mapper version: what a
-- prune of the snapshot frees (heap sizes; indexes excluded, a floor).
create or replace function public.catalog_register_snapshot_bytes(p_snapshot_id uuid)
returns bigint
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce((select sum(pg_column_size(r.*)) from public.catalog_raw_records r
                    where r.snapshot_id = p_snapshot_id), 0)
       + coalesce((select sum(pg_column_size(c.*)) from public.catalog_candidate_variants c
                    where c.snapshot_id = p_snapshot_id), 0)
       + coalesce((select sum(pg_column_size(v.*)) from public.catalog_variants v
                    where v.snapshot_id = p_snapshot_id), 0)
$$;

-- The above plus the ledger rows at the two variant levels that name the
-- snapshot (read through the ledger's key index): the Register page's number.
create or replace function public.catalog_register_measured_bytes(p_snapshot_id uuid)
returns bigint
language sql
stable
set search_path = pg_catalog
as $$
  select public.catalog_register_snapshot_bytes(p_snapshot_id)
       + coalesce((select sum(pg_column_size(cv.*))
                     from public.catalog_variant_coverage cv
                     join public.catalog_source_snapshots s on s.id = p_snapshot_id
                    where cv.level in ('identity', 'government_fields')
                      and cv.snapshot_key = s.snapshot_key
                      and cv.variant_identity_key in (select v.variant_identity_key from public.catalog_variants v
                                                       where v.snapshot_id = p_snapshot_id
                                                         and v.variant_identity_key is not null)), 0)
$$;

-- record_register_unit_status (20260929000100) measures raw records +
-- candidates under that label. For a snapshot that is already built (a
-- capture that reuses a content-addressed snapshot) that is not the whole
-- storage: this trigger takes the full measurement instead.
create or replace function public.catalog_register_unit_measure() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if new.snapshot_id is not null and new.measurement_method = 'pg_column_size(raw_records+candidates)' then
    new.measured_bytes := public.catalog_register_measured_bytes(new.snapshot_id);
    new.measurement_method := 'pg_column_size(raw_records+candidates+variants+ledger)';
  end if;
  return new;
end;
$$;
drop trigger if exists catalog_register_capture_units_measure on public.catalog_register_capture_units;
create trigger catalog_register_capture_units_measure
  before insert or update on public.catalog_register_capture_units
  for each row execute function public.catalog_register_unit_measure();

alter table public.catalog_variant_builds drop constraint if exists catalog_variant_builds_mapper_version_token;
alter table public.catalog_variant_builds add constraint catalog_variant_builds_mapper_version_token
  check (mapper_version ~ '^[A-Za-z0-9._:-]{1,80}$');

-- ---------------------------------------------------------------------------
-- 3. The build: rank-1 eligibility, compact equipment, the measurement.
--    Otherwise exactly 20260930000100's function.
-- ---------------------------------------------------------------------------
create or replace function public.record_catalog_variants(
  p_snapshot_id uuid, p_mapper_version text, p_rows jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_filters jsonb;
  v_build public.catalog_variant_builds%rowtype;
  v_inserted integer;
  v_matched integer;
  v_keys integer;
begin
  if p_mapper_version is distinct from public.catalog_variant_mapper_version() then
    raise exception 'CATALOG_VARIANT_MAPPER_MISMATCH: that is not the current mapper version'
      using errcode = '22023';
  end if;
  if p_rows is null or jsonb_typeof(p_rows) <> 'array' or jsonb_array_length(p_rows) > 500
     or exists (select 1 from jsonb_array_elements(p_rows) e
                 where jsonb_typeof(e) <> 'object' or jsonb_typeof(e->'upstream_record_id') <> 'string')
     or (select count(*) <> count(distinct e->>'upstream_record_id') from jsonb_array_elements(p_rows) e) then
    raise exception 'CATALOG_VARIANT_ROWS_INVALID: invalid variant rows' using errcode = '22023';
  end if;
  if exists (select 1 from jsonb_to_recordset(p_rows) as t(equipment jsonb)
              where not public.catalog_variant_equipment_valid(coalesce(t.equipment, '{}'::jsonb))) then
    raise exception 'CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN: an equipment key or value is outside the closed list'
      using errcode = '22023';
  end if;
  select * into v_snapshot from public.catalog_source_snapshots where id = p_snapshot_id;
  v_filters := v_snapshot.retrieval_metadata->'capture_scope'->'filters';
  -- A Prepare snapshot no register unit captured passes: its stored rows
  -- equal its declared rows, and no unit recorded it count-unverified.
  if v_snapshot.id is null or v_snapshot.source_family <> 'government' or v_snapshot.activated_at is null
     or v_snapshot.validation_state <> 'complete'
     or v_snapshot.stored_record_count <> v_snapshot.declared_record_count
     or jsonb_typeof(v_filters) is distinct from 'object'
     or (case when jsonb_typeof(v_filters) = 'object'
              then (select array_agg(k) from jsonb_object_keys(v_filters) k) end) is distinct from array['tozar']
     -- The NEWEST register unit that captured this (content-addressed)
     -- snapshot decides: a later verified capture clears an older mismatch.
     or (select u.count_verified from public.catalog_register_capture_units u
          where u.snapshot_id = p_snapshot_id
          order by u.updated_at desc, u.id desc limit 1) is false then
    raise exception 'CATALOG_VARIANT_SNAPSHOT_INELIGIBLE: only an active, count-verified, whole-tozar Government snapshot is built'
      using errcode = '22023';
  end if;
  -- Rank 1 only (retention's order: activated_at desc, then id): a superseded
  -- snapshot is never built, by a capture or a backfill.
  if exists (select 1 from public.catalog_source_snapshots s
              where s.source_family = 'government' and s.activated_at is not null and s.id <> v_snapshot.id
                and s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' = v_filters->>'tozar'
                and (s.activated_at > v_snapshot.activated_at
                     or (s.activated_at = v_snapshot.activated_at and s.id < v_snapshot.id))) then
    raise exception 'CATALOG_VARIANT_SNAPSHOT_SUPERSEDED: only the active snapshot of a tozar is built'
      using errcode = '22023';
  end if;

  insert into public.catalog_variant_builds
    (snapshot_id, mapper_version, snapshot_key, tozar, activated_at, expected_rows, completed_at)
  values (v_snapshot.id, p_mapper_version, v_snapshot.snapshot_key, v_filters->>'tozar',
          v_snapshot.activated_at, v_snapshot.stored_record_count,
          case when v_snapshot.stored_record_count = 0 then now() end)
  on conflict (snapshot_id, mapper_version) do nothing;
  select * into v_build from public.catalog_variant_builds
   where snapshot_id = p_snapshot_id and mapper_version = p_mapper_version for update;

  select count(*) into v_matched
    from jsonb_array_elements(p_rows) e
    join public.catalog_raw_records r
      on r.snapshot_id = p_snapshot_id and r.upstream_record_id = e->>'upstream_record_id';
  if v_matched <> jsonb_array_length(p_rows) then
    raise exception 'CATALOG_VARIANT_ROWS_INVALID: a row names no record of that snapshot'
      using errcode = '22023';
  end if;

  with x as (
    select t.*, coalesce(t.equipment, '{}'::jsonb) as doc from jsonb_to_recordset(p_rows) as t(
      upstream_record_id text, vehicle_segment text,
      tozar text, tozeret_cd integer, tozeret_nm text, tozeret_eretz_nm text, degem_cd integer,
      degem_nm text, sug_degem text, kinuy_mishari text, shnat_yitzur integer, ramat_gimur text,
      delek_cd integer, delek_nm text, norm_fuel_type text, norm_propulsion_technology text,
      norm_drivetrain text, norm_body_style text,
      nefah_manoa integer, koah_sus integer, hanaa_cd integer, hanaa_nm text,
      technologiat_hanaa_cd integer, technologiat_hanaa_nm text, automatic_ind smallint,
      sug_tkina_cd integer, sug_tkina_nm text, sug_mamir_cd integer, sug_mamir_nm text, merkav text,
      mispar_dlatot integer, mispar_moshavim integer, mishkal_kolel integer,
      kosher_grira_im_blamim integer, kosher_grira_bli_blamim integer,
      kamut_co2_city numeric, kamut_co2_hway numeric, co2_wltp numeric, nox_wltp numeric,
      co_wltp numeric, hc_wltp numeric, kvutzat_zihum integer, madad_yarok numeric,
      nikud_betihut numeric, ramat_eivzur_betihuty integer, mispar_kariot_avir integer,
      abs_ind smallint, bakarat_yatzivut_ind smallint, equipment jsonb, parse_issues jsonb)
  )
  insert into public.catalog_variants (
    snapshot_id, snapshot_key, upstream_record_id, content_sha256, archive_line, mapper_version,
    variant_identity_key, vehicle_segment,
    tozar, tozeret_cd, tozeret_nm, tozeret_eretz_nm, degem_cd, degem_nm, sug_degem, kinuy_mishari,
    shnat_yitzur, ramat_gimur, delek_cd, delek_nm, norm_fuel_type, norm_propulsion_technology,
    norm_drivetrain, norm_body_style, nefah_manoa, koah_sus, hanaa_cd, hanaa_nm,
    technologiat_hanaa_cd, technologiat_hanaa_nm, automatic_ind, sug_tkina_cd, sug_tkina_nm,
    sug_mamir_cd, sug_mamir_nm, merkav, mispar_dlatot, mispar_moshavim, mishkal_kolel,
    kosher_grira_im_blamim, kosher_grira_bli_blamim, kamut_co2_city, kamut_co2_hway, co2_wltp,
    nox_wltp, co_wltp, hc_wltp, kvutzat_zihum, madad_yarok, nikud_betihut, ramat_eivzur_betihuty,
    mispar_kariot_avir, abs_ind, bakarat_yatzivut_ind, equipment_stated, equipment_on,
    equipment_sources, parse_issues)
  select v_snapshot.id, v_snapshot.snapshot_key, r.upstream_record_id,
         public.catalog_variant_content_sha256(r.payload),
         case when a.snapshot_id is not null and r.source_locator ? 'capture_index'
              then (r.source_locator->>'capture_index')::integer + 1 end,
         p_mapper_version,
         case when c.id is not null then public.catalog_variant_identity_key(
                c.manufacturer, c.commercial_model, c.model_year_start, c.model_year_end,
                c.official_model_code, c.trim, r.payload->>'tozeret_cd', r.payload->>'degem_cd',
                r.payload->>'sug_degem', c.identity_dimensions) end,
         x.vehicle_segment,
         x.tozar, x.tozeret_cd, x.tozeret_nm, x.tozeret_eretz_nm, x.degem_cd, x.degem_nm, x.sug_degem,
         x.kinuy_mishari, x.shnat_yitzur, x.ramat_gimur, x.delek_cd, x.delek_nm, x.norm_fuel_type,
         x.norm_propulsion_technology, x.norm_drivetrain, x.norm_body_style, x.nefah_manoa,
         x.koah_sus, x.hanaa_cd, x.hanaa_nm, x.technologiat_hanaa_cd, x.technologiat_hanaa_nm,
         x.automatic_ind, x.sug_tkina_cd, x.sug_tkina_nm, x.sug_mamir_cd, x.sug_mamir_nm, x.merkav,
         x.mispar_dlatot, x.mispar_moshavim, x.mishkal_kolel, x.kosher_grira_im_blamim,
         x.kosher_grira_bli_blamim, x.kamut_co2_city, x.kamut_co2_hway, x.co2_wltp, x.nox_wltp,
         x.co_wltp, x.hc_wltp, x.kvutzat_zihum, x.madad_yarok, x.nikud_betihut,
         x.ramat_eivzur_betihuty, x.mispar_kariot_avir, x.abs_ind, x.bakarat_yatzivut_ind,
         public.catalog_variant_equipment_mask(x.doc, false), public.catalog_variant_equipment_mask(x.doc, true),
         public.catalog_variant_equipment_source_texts(x.doc), coalesce(x.parse_issues, '[]'::jsonb)
    from x
    join public.catalog_raw_records r
      on r.snapshot_id = v_snapshot.id and r.upstream_record_id = x.upstream_record_id
    left join public.catalog_register_snapshot_archives a on a.snapshot_id = v_snapshot.id
    left join lateral (select cc.* from public.catalog_candidate_variants cc
                        where cc.raw_record_id = r.id order by cc.id limit 1) c on true
   order by r.upstream_record_id
  on conflict (snapshot_id, upstream_record_id, mapper_version) do nothing;
  get diagnostics v_inserted = row_count;

  update public.catalog_variant_builds b
     set built_rows = n.rows,
         completed_at = case when n.rows = b.expected_rows then coalesce(b.completed_at, now()) end,
         updated_at = now()
    from (select count(*)::integer as rows from public.catalog_variants v
           where v.snapshot_id = p_snapshot_id and v.mapper_version = p_mapper_version) n
   where b.snapshot_id = p_snapshot_id and b.mapper_version = p_mapper_version
  returning b.* into v_build;

  with touched as (
    select distinct v.variant_identity_key as k
      from jsonb_array_elements(p_rows) e
      join public.catalog_variants v
        on v.snapshot_id = p_snapshot_id and v.mapper_version = p_mapper_version
       and v.upstream_record_id = e->>'upstream_record_id'
     where v.variant_identity_key is not null
  ),
  contents as (
    select v.variant_identity_key as k, count(distinct v.content_sha256) as distinct_contents,
           min(v.content_sha256 collate "C") as only_content,
           encode(sha256(convert_to(string_agg(distinct v.content_sha256, ','
                                               order by v.content_sha256), 'UTF8')), 'hex') as collision_content
      from public.catalog_variants v
     where v.snapshot_id = p_snapshot_id and v.mapper_version = p_mapper_version
       and v.variant_identity_key in (select k from touched)
     group by v.variant_identity_key
  ),
  written as (
    insert into public.catalog_variant_coverage as cv
      (variant_identity_key, level, status, last_run_id, snapshot_key, content_sha256,
       vocabulary_version, reason_code)
    select c.k, l.level,
           case when c.distinct_contents > 1 then 'failed' else 'enriched' end,
           v_snapshot.created_by_run_id, v_snapshot.snapshot_key,
           case when c.distinct_contents > 1 then c.collision_content else c.only_content end,
           public.catalog_vocabulary_version(),
           case when c.distinct_contents > 1 then 'CATALOG_COVERAGE_KEY_COLLISION' end
      from contents c
     cross join (values ('identity'), ('government_fields')) as l(level)
     order by c.k collate "C", l.level
    on conflict (variant_identity_key, level) do update
       set status = excluded.status, last_run_id = excluded.last_run_id,
           snapshot_key = excluded.snapshot_key, content_sha256 = excluded.content_sha256,
           vocabulary_version = excluded.vocabulary_version, reason_code = excluded.reason_code,
           updated_at = now()
     where not exists (select 1 from public.catalog_source_snapshots ns
                        where ns.snapshot_key = cv.snapshot_key
                          and ns.activated_at > v_snapshot.activated_at)
       and (cv.content_sha256 is distinct from excluded.content_sha256
            or public.catalog_variant_coverage_rank(excluded.status)
                 > public.catalog_variant_coverage_rank(cv.status)
            or (public.catalog_variant_coverage_rank(excluded.status)
                  = public.catalog_variant_coverage_rank(cv.status)
                and (cv.status, cv.last_run_id, cv.snapshot_key, cv.vocabulary_version, cv.reason_code)
                    is distinct from (excluded.status, excluded.last_run_id, excluded.snapshot_key,
                                      excluded.vocabulary_version, excluded.reason_code)))
    returning 1
  )
  select count(*) into v_keys from written;

  -- A complete build re-measures the register units that captured the
  -- snapshot: raw records, candidates, variants and their ledger rows.
  if v_build.completed_at is not null then
    update public.catalog_register_capture_units
       set measured_bytes = public.catalog_register_measured_bytes(p_snapshot_id),
           measurement_method = 'pg_column_size(raw_records+candidates+variants+ledger)',
           updated_at = now()
     where snapshot_id = p_snapshot_id and status = 'captured';
  end if;

  return jsonb_build_object('snapshot_key', v_snapshot.snapshot_key, 'mapper_version', p_mapper_version,
                            'rows', jsonb_array_length(p_rows), 'inserted', v_inserted,
                            'ledger_written', v_keys, 'built_rows', v_build.built_rows,
                            'expected_rows', v_build.expected_rows,
                            'complete', v_build.completed_at is not null);
end;
$$;

-- ---------------------------------------------------------------------------
-- 4. The discovery tree: resolve the tozar's current snapshot, then read by
--    snapshot id (catalog_variants_tree_idx), never the whole table. A
--    snapshot is scoped to exactly one tozar, so this is the view's answer.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_variant_current_snapshot(p_tozar text)
returns uuid
language sql
stable
set search_path = pg_catalog
as $$
  select b.snapshot_id from public.catalog_variant_builds b
   where b.mapper_version = public.catalog_variant_mapper_version() and b.completed_at is not null
     and b.tozar = p_tozar
   order by b.activated_at desc, b.snapshot_id limit 1
$$;

create or replace function public.catalog_browser_models(
  p_tozar text, p_segment text, p_year_from integer, p_year_to integer, p_delek_cd integer,
  p_merkav text, p_limit integer, p_offset integer
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  if p_tozar is null or not public.catalog_browser_page_valid(p_limit, p_offset) then
    raise exception 'CATALOG_BROWSER_QUERY_INVALID' using errcode = '22023';
  end if;
  return (
    with g as (select v.kinuy_mishari, count(*) as n, min(v.shnat_yitzur) as year_min,
                      max(v.shnat_yitzur) as year_max
                 from public.catalog_variants v
                where v.snapshot_id = (select public.catalog_variant_current_snapshot(p_tozar))
                  and v.mapper_version = public.catalog_variant_mapper_version()
                  and (p_segment is null or v.vehicle_segment = p_segment)
                  and (p_year_from is null or v.shnat_yitzur >= p_year_from)
                  and (p_year_to is null or v.shnat_yitzur <= p_year_to)
                  and (p_delek_cd is null or v.delek_cd = p_delek_cd)
                  and (p_merkav is null or v.merkav = p_merkav)
                  and v.tozar = p_tozar
                group by v.kinuy_mishari)
    select jsonb_build_object(
      'total', (select count(*) from g), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(jsonb_build_object('kinuy_mishari', p.kinuy_mishari,
                                                             'variants', p.n, 'year_min', p.year_min,
                                                             'year_max', p.year_max)
                                          order by p.kinuy_mishari collate "C" nulls last)
                           from (select * from g order by g.kinuy_mishari collate "C" nulls last
                                  limit p_limit offset p_offset) p), '[]'::jsonb)));
end;
$$;

create or replace function public.catalog_browser_years(
  p_tozar text, p_kinuy_mishari text, p_segment text, p_year_from integer, p_year_to integer,
  p_delek_cd integer, p_merkav text, p_limit integer, p_offset integer
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  if p_tozar is null or p_kinuy_mishari is null or not public.catalog_browser_page_valid(p_limit, p_offset) then
    raise exception 'CATALOG_BROWSER_QUERY_INVALID' using errcode = '22023';
  end if;
  return (
    with g as (select v.shnat_yitzur, count(*) as n
                 from public.catalog_variants v
                where v.snapshot_id = (select public.catalog_variant_current_snapshot(p_tozar))
                  and v.mapper_version = public.catalog_variant_mapper_version()
                  and (p_segment is null or v.vehicle_segment = p_segment)
                  and (p_year_from is null or v.shnat_yitzur >= p_year_from)
                  and (p_year_to is null or v.shnat_yitzur <= p_year_to)
                  and (p_delek_cd is null or v.delek_cd = p_delek_cd)
                  and (p_merkav is null or v.merkav = p_merkav)
                  and v.tozar = p_tozar and v.kinuy_mishari = p_kinuy_mishari
                group by v.shnat_yitzur)
    select jsonb_build_object(
      'total', (select count(*) from g), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(jsonb_build_object('shnat_yitzur', p.shnat_yitzur, 'variants', p.n)
                                          order by p.shnat_yitzur desc nulls last)
                           from (select * from g order by g.shnat_yitzur desc nulls last
                                  limit p_limit offset p_offset) p), '[]'::jsonb)));
end;
$$;

create or replace function public.catalog_browser_variants(
  p_tozar text, p_kinuy_mishari text, p_shnat_yitzur integer, p_segment text, p_year_from integer,
  p_year_to integer, p_delek_cd integer, p_merkav text, p_limit integer, p_offset integer
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  if p_tozar is null or p_kinuy_mishari is null or p_shnat_yitzur is null
     or not public.catalog_browser_page_valid(p_limit, p_offset) then
    raise exception 'CATALOG_BROWSER_QUERY_INVALID' using errcode = '22023';
  end if;
  return (
    with m as (select v.*
                 from public.catalog_variants v
                where v.snapshot_id = (select public.catalog_variant_current_snapshot(p_tozar))
                  and v.mapper_version = public.catalog_variant_mapper_version()
                  and (p_segment is null or v.vehicle_segment = p_segment)
                  and (p_year_from is null or v.shnat_yitzur >= p_year_from)
                  and (p_year_to is null or v.shnat_yitzur <= p_year_to)
                  and (p_delek_cd is null or v.delek_cd = p_delek_cd)
                  and (p_merkav is null or v.merkav = p_merkav)
                  and v.tozar = p_tozar and v.kinuy_mishari = p_kinuy_mishari
                  and v.shnat_yitzur = p_shnat_yitzur),
    p as (select * from m
           order by m.degem_nm collate "C" nulls last, m.ramat_gimur collate "C" nulls last,
                    m.upstream_record_id collate "C", m.id
           limit p_limit offset p_offset)
    select jsonb_build_object(
      'total', (select count(*) from m), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(
          (to_jsonb(p) - 'id' - 'snapshot_id' - 'created_at' - 'equipment_stated' - 'equipment_on'
                       - 'equipment_sources')
          || jsonb_build_object('equipment', public.catalog_variant_equipment(
                                  p.equipment_stated, p.equipment_on, p.equipment_sources))
          || jsonb_build_object('coverage', coalesce((
               select jsonb_object_agg(cv.level, jsonb_build_object(
                        'status', cv.status, 'reason_code', cv.reason_code,
                        'current', cv.content_sha256 = p.content_sha256))
                 from public.catalog_variant_coverage cv
                where cv.variant_identity_key = p.variant_identity_key), '{}'::jsonb))
          order by p.degem_nm collate "C" nulls last, p.ramat_gimur collate "C" nulls last,
                   p.upstream_record_id collate "C", p.id)
        from p), '[]'::jsonb)));
end;
$$;

-- All tozars read the view; one tozar reads its current snapshot only (each
-- branch's gate is a one-time filter on the parameter).
create or replace function public.catalog_browser_facets(p_tozar text)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with m as (select v.vehicle_segment, v.delek_cd, v.delek_nm, v.merkav, v.shnat_yitzur
               from public.catalog_variants_current v
              where p_tozar is null
             union all
             select v.vehicle_segment, v.delek_cd, v.delek_nm, v.merkav, v.shnat_yitzur
               from public.catalog_variants v
              where p_tozar is not null
                and v.snapshot_id = (select public.catalog_variant_current_snapshot(p_tozar))
                and v.mapper_version = public.catalog_variant_mapper_version()
                and v.tozar = p_tozar)
  select jsonb_build_object(
    'segments', coalesce((select jsonb_agg(jsonb_build_object('value', s.vehicle_segment, 'variants', s.n)
                                           order by s.vehicle_segment)
                            from (select vehicle_segment, count(*) n from m group by 1) s), '[]'::jsonb),
    'fuels', coalesce((select jsonb_agg(jsonb_build_object('delek_cd', f.delek_cd, 'delek_nm', f.delek_nm,
                                                           'variants', f.n) order by f.delek_cd, f.delek_nm)
                         from (select delek_cd, min(delek_nm) as delek_nm, count(*) n from m
                                where delek_cd is not null group by 1 order by 1 limit 100) f), '[]'::jsonb),
    'bodies', coalesce((select jsonb_agg(jsonb_build_object('merkav', b.merkav, 'variants', b.n)
                                         order by b.merkav collate "C")
                          from (select merkav, count(*) n from m where merkav is not null
                                 group by 1 order by merkav collate "C" limit 100) b), '[]'::jsonb),
    'year_min', (select min(shnat_yitzur) from m),
    'year_max', (select max(shnat_yitzur) from m))
$$;

-- ---------------------------------------------------------------------------
-- 5. Retention.
-- ---------------------------------------------------------------------------

-- 20260929000100's rule, with 20260930000100's variant keep-set narrowed to
-- the CURRENT build of each tozar and the ledger's to `register` rows.
create or replace function public.catalog_register_prunable_snapshots()
returns table (snapshot_id uuid, snapshot_key text, tozar text, validation_state text,
               activated_at timestamptz, raw_rows bigint, estimated_bytes bigint)
language sql
stable
set search_path = pg_catalog
as $$
  with scoped as (
    select s.*, s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' as scoped_tozar
      from public.catalog_source_snapshots s
     where s.source_family = 'government'
       and s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' is not null
  ),
  ranked as (
    select sc.id, row_number() over (partition by sc.scoped_tozar
                                     order by sc.activated_at desc, sc.id) as activation_rank
      from scoped sc where sc.activated_at is not null
  )
  select sc.id, sc.snapshot_key, sc.scoped_tozar, sc.validation_state, sc.activated_at,
         (select count(*) from public.catalog_raw_records r where r.snapshot_id = sc.id),
         public.catalog_register_snapshot_bytes(sc.id) + pg_column_size(sc.*)
         + coalesce((select sum(pg_column_size(b.*)) from public.catalog_variant_builds b
                      where b.snapshot_id = sc.id), 0)
    from scoped sc
   where not exists (select 1 from ranked k where k.id = sc.id and k.activation_rank <= 2)
     and not exists (select 1 from public.catalog_candidate_evidence_links x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_canonical_field_provenance x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_snapshot_adoptions x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_units x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_batches x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_queue_items x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_variant_coverage x
                      where x.snapshot_key = sc.snapshot_key and x.level = 'register')
     and sc.id not in (select distinct on (b.tozar) b.snapshot_id
                         from public.catalog_variant_builds b
                        where b.mapper_version = public.catalog_variant_mapper_version()
                          and b.completed_at is not null
                        order by b.tozar, b.activated_at desc, b.snapshot_id)
     and sc.id not in (select distinct on (u.tozar) u.snapshot_id
                         from public.catalog_register_capture_units u
                        where u.status = 'captured' and u.snapshot_id is not null
                        order by u.tozar, u.updated_at desc)
     and not exists (select 1 from public.run_checkpoints x
                      where x.artifacts->'government'->>'snapshot_key' = sc.snapshot_key)
     and not exists (select 1 from public.catalog_candidate_variants c
                      where c.snapshot_id = sc.id
                        and (exists (select 1 from public.catalog_model_variants m
                                      where m.promoted_from_candidate_id = c.id)
                             or exists (select 1 from public.catalog_candidate_evidence_links x
                                         where x.candidate_id = c.id)
                             or exists (select 1 from public.catalog_canonical_field_provenance x
                                         where x.candidate_id = c.id)
                             or exists (select 1 from public.catalog_work_scope_queue_items x
                                         where x.candidate_id = c.id)
                             or exists (select 1 from public.catalog_variant_reservations x
                                         where x.candidate_id = c.id)))
     and not exists (select 1 from public.runs w
                      where w.id = sc.created_by_run_id
                        and w.status not in ('completed', 'partial_success', 'failed', 'cancelled',
                                             'timed_out', 'budget_exhausted'))
   order by sc.snapshot_key
$$;

-- The variant rows of an OLD mapper version, once the same snapshot's build
-- under the current mapper version is complete (a snapshot pruned whole is
-- not listed again here). `item` is what the digest covers.
create or replace function public.catalog_register_prunable_variant_builds()
returns table (snapshot_id uuid, snapshot_key text, tozar text, mapper_version text, item text,
               variant_rows bigint, estimated_bytes bigint)
language sql
stable
set search_path = pg_catalog
as $$
  select b.snapshot_id, b.snapshot_key, b.tozar, b.mapper_version, b.snapshot_key || ' ' || b.mapper_version,
         (select count(*) from public.catalog_variants v
           where v.snapshot_id = b.snapshot_id and v.mapper_version = b.mapper_version),
         coalesce((select sum(pg_column_size(v.*)) from public.catalog_variants v
                    where v.snapshot_id = b.snapshot_id and v.mapper_version = b.mapper_version), 0)
         + pg_column_size(b.*)
    from public.catalog_variant_builds b
   where b.mapper_version <> public.catalog_variant_mapper_version()
     and exists (select 1 from public.catalog_variant_builds n
                  where n.snapshot_id = b.snapshot_id
                    and n.mapper_version = public.catalog_variant_mapper_version()
                    and n.completed_at is not null)
     and b.snapshot_id not in (select p.snapshot_id from public.catalog_register_prunable_snapshots() p)
   order by b.snapshot_key || ' ' || b.mapper_version collate "C"
$$;

-- Both lists and the ONE digest over their items, computed in one statement.
create or replace function public.catalog_register_prunable_list()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with p as (select * from public.catalog_register_prunable_snapshots()),
       b as (select * from public.catalog_register_prunable_variant_builds())
  select jsonb_build_object(
    'snapshots', coalesce((select jsonb_agg(jsonb_build_object(
                             'snapshot_id', p.snapshot_id, 'snapshot_key', p.snapshot_key, 'tozar', p.tozar,
                             'raw_rows', p.raw_rows, 'estimated_bytes', p.estimated_bytes)
                           order by p.snapshot_key collate "C") from p), '[]'::jsonb),
    'variant_builds', coalesce((select jsonb_agg(jsonb_build_object(
                                  'snapshot_id', b.snapshot_id, 'snapshot_key', b.snapshot_key,
                                  'tozar', b.tozar, 'mapper_version', b.mapper_version, 'item', b.item,
                                  'variant_rows', b.variant_rows, 'estimated_bytes', b.estimated_bytes)
                                order by b.item collate "C") from b), '[]'::jsonb),
    'digest', public.catalog_register_prune_digest(
                coalesce((select array_agg(p.snapshot_key) from p), '{}'::text[])
                || coalesce((select array_agg(b.item) from b), '{}'::text[])))
$$;

-- The prune (20260929000100's), now also of variants and variant builds. The
-- items are snapshot keys and `<snapshot_key> <mapper_version>` variant
-- builds; the digest must be the current lists' digest.
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
  lock table public.catalog_source_snapshots, public.catalog_raw_records,
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
-- 6. Privileges (PR-D1's pattern). The view was rebuilt: its grants too.
-- ---------------------------------------------------------------------------
do $$
declare
  fn text;
  ro record;
  v_reads text[] := array[
    'public.catalog_variant_equipment_keys()',
    'public.catalog_variant_equipment_mask(jsonb,boolean)',
    'public.catalog_variant_equipment_source_texts(jsonb)',
    'public.catalog_variant_equipment_sources_valid(text[])',
    'public.catalog_variant_equipment(integer,integer,text[])',
    'public.catalog_register_measured_bytes(uuid)',
    'public.catalog_variant_current_snapshot(text)',
    'public.catalog_register_prunable_variant_builds()'];
begin
  foreach fn in array v_reads loop
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
  execute 'revoke all on table public.catalog_variants_current from public';
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table public.catalog_variants_current from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table public.catalog_variants_current from authenticated';
  end if;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    execute 'grant select on table public.catalog_variants_current to service_role';
  end if;

  -- The read-only release roles, exactly as 20260929000100 selects them.
  for ro in
    select r.rolname from pg_roles r
     where r.rolcanlogin and not r.rolsuper
       and r.rolname not in ('postgres', 'service_role', 'authenticator', 'anon', 'authenticated')
       and (r.rolname = 'supabase_read_only_user'
            or (r.rolbypassrls and r.rolname like 'milo\_release\_readonly\_%')
            or (r.rolbypassrls and pg_has_role(r.oid, 'pg_read_all_data', 'MEMBER')))
  loop
    execute format('grant select on table public.catalog_variants_current to %I', ro.rolname);
    foreach fn in array v_reads loop
      execute format('grant execute on function %s to %I', fn, ro.rolname);
    end loop;
  end loop;
end $$;
