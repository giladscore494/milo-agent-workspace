-- PR-L1: deterministic catalog variants -- levels 1 (identity) and 1.5 (every
-- mapped Government field) as TYPED columns, the vehicle category, and the
-- discovery tree. No model, $0 (owner decisions 16, 22, 26, 27, 32).
--
-- What this adds
-- --------------
--
--   catalog_variants        ONE row per (snapshot, upstream record, mapper
--                           version) of an ACTIVE, count-verified, whole-tozar
--                           Government snapshot. Typed columns mapped by
--                           backend/catalog/register/variants.py (the mapper
--                           is code-owned and versioned; this table only
--                           stores and checks what it produced). Append-only:
--                           a new mapper version writes new rows and never
--                           mutates old ones.
--   catalog_variant_builds  one row per (snapshot, mapper version): how many
--                           rows are built and whether the build is complete.
--                           The discovery tree reads the newest COMPLETE build
--                           per tozar only.
--   catalog_variants_current (view) the variants the discovery tree serves.
--   record_catalog_variants()   one bounded batch (<= 500 rows) of a build, and
--                               the coverage ledger at levels `identity` and
--                               `government_fields` for the keys it touches
--   catalog_variant_build_state()  a build's progress (read)
--   catalog_browser_manufacturers() / _models() / _years() / _variants() /
--   catalog_browser_facets()    the discovery tree, each ONE bounded, paged
--                               jsonb document (rule 20: never the register
--                               in bulk)
--
-- What it changes
-- ---------------
--
--   * catalog_variant_coverage's level CHECK admits the two new levels. Every
--     existing row is `register` and is untouched; no other ledger rule moves.
--   * catalog_register_prunable_snapshots() keeps every snapshot that has
--     variant rows or a variant build (retention must never orphan them; the
--     foreign keys below refuse it anyway).
--
-- What it never does
-- ------------------
--
-- It changes no snapshot, raw record, candidate, content hash, existing ledger
-- row, Prepare or run path. It reads no Government source and calls no model.
-- Additive and forward-only; rerun-safe.

-- ---------------------------------------------------------------------------
-- 1. The coverage ledger's two new levels.
-- ---------------------------------------------------------------------------
alter table public.catalog_variant_coverage
  drop constraint if exists catalog_variant_coverage_level;
alter table public.catalog_variant_coverage
  add constraint catalog_variant_coverage_level
  check (level in ('register', 'identity', 'government_fields'));

-- ---------------------------------------------------------------------------
-- 2. The mapper's pinned facts (backend/catalog/register/variants.py; a test
--    holds each equal to the code).
-- ---------------------------------------------------------------------------
create or replace function public.catalog_variant_mapper_version()
returns text
language sql
immutable
set search_path = pg_catalog
as $$ select 'gov.wltp.variant-mapper.1'::text $$;

-- The CLOSED driver-assistance key list (`variants.EQUIPMENT_FIELDS`): the
-- *_ind indicators are 0 or 1, the *_makor_hatkana sources short text.
create or replace function public.catalog_variant_equipment_valid(p_equipment jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select jsonb_typeof(p_equipment) = 'object'
     and not exists (
       select 1 from jsonb_each(p_equipment) e
        where e.key <> all (array[
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
                'zihuy_tamrurey_tnua_makor_hatkana']::text[])
           or (e.key like '%\_makor\_hatkana'
               and (jsonb_typeof(e.value) <> 'string' or char_length(e.value #>> '{}') not between 1 and 80))
           or (e.key not like '%\_makor\_hatkana'
               and (jsonb_typeof(e.value) <> 'number' or (e.value #>> '{}') not in ('0', '1'))))
$$;

-- The per-row parse issues: [{"field": <payload field>, "reason": <code>}].
create or replace function public.catalog_variant_parse_issues_valid(p_issues jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  select jsonb_typeof(p_issues) = 'array' and jsonb_array_length(p_issues) <= 100
     and not exists (
       select 1 from jsonb_array_elements(p_issues) i
        where jsonb_typeof(i) <> 'object'
           or (select array_agg(k order by k) from jsonb_object_keys(i) k) is distinct from array['field', 'reason']
           or (i->>'field') !~ '^[A-Za-z0-9_]{1,80}$'
           or (i->>'reason') not in ('not_text', 'too_long', 'not_a_number', 'not_a_whole_number',
                                     'out_of_range', 'not_an_indicator'))
$$;

-- ---------------------------------------------------------------------------
-- 3. The relations.
-- ---------------------------------------------------------------------------
create table if not exists public.catalog_variant_builds (
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  mapper_version text not null check (char_length(mapper_version) between 1 and 80),
  snapshot_key text not null check (snapshot_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  -- The snapshot's declared scope (exactly one tozar) and its activation.
  tozar text not null check (char_length(tozar) between 1 and 200),
  activated_at timestamptz not null,
  expected_rows integer not null check (expected_rows >= 0),
  built_rows integer not null default 0 check (built_rows between 0 and expected_rows),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (snapshot_id, mapper_version),
  constraint catalog_variant_builds_complete check ((completed_at is null) = (built_rows < expected_rows))
);
create index if not exists catalog_variant_builds_current_idx
  on public.catalog_variant_builds (mapper_version, tozar, activated_at desc)
  where completed_at is not null;
create index if not exists catalog_variant_builds_key_idx
  on public.catalog_variant_builds (snapshot_key);

create table if not exists public.catalog_variants (
  id uuid primary key default gen_random_uuid(),
  -- Provenance. (snapshot_id, upstream_record_id) IS the raw record (unique
  -- there), so it is the foreign key: RESTRICT, a prune can never orphan a
  -- variant. snapshot_key is carried for citation without a join.
  snapshot_id uuid not null references public.catalog_source_snapshots(id) on delete restrict,
  snapshot_key text not null check (snapshot_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  upstream_record_id text not null check (char_length(upstream_record_id) between 1 and 200),
  -- The record's content hash (catalog_variant_content_sha256: payload - _id).
  content_sha256 text not null check (content_sha256 ~ '^[0-9a-f]{64}$'),
  -- The record's line in the snapshot's immutable archive, when one exists.
  archive_line integer check (archive_line is null or archive_line >= 1),
  mapper_version text not null check (char_length(mapper_version) between 1 and 80),
  -- milo-variant-identity/2 over the record's stored candidate; null when the
  -- reviewed normalization produced no candidate for the row.
  variant_identity_key text check (variant_identity_key is null or variant_identity_key ~ '^[0-9a-f]{64}$'),
  -- D4: code-owned category (variants.SEGMENT_BY_SUG_DEGEM).
  vehicle_segment text not null check (vehicle_segment in ('private', 'commercial', 'unknown')),

  -- Level 1: identity, as the register states it.
  tozar text check (char_length(tozar) between 1 and 200),
  tozeret_cd integer,
  tozeret_nm text check (char_length(tozeret_nm) between 1 and 200),
  tozeret_eretz_nm text check (char_length(tozeret_eretz_nm) between 1 and 200),
  degem_cd integer,
  degem_nm text check (char_length(degem_nm) between 1 and 200),
  sug_degem text check (char_length(sug_degem) between 1 and 200),
  kinuy_mishari text check (char_length(kinuy_mishari) between 1 and 200),
  shnat_yitzur integer,
  ramat_gimur text check (char_length(ramat_gimur) between 1 and 200),
  delek_cd integer,
  delek_nm text check (char_length(delek_nm) between 1 and 200),
  -- Level 1 normalised values, ONLY where PR-V's vocabulary defines them
  -- (normalize.read_wltp_record); the source columns are never changed.
  norm_fuel_type text check (char_length(norm_fuel_type) between 1 and 40),
  norm_propulsion_technology text check (char_length(norm_propulsion_technology) between 1 and 40),
  norm_drivetrain text check (char_length(norm_drivetrain) between 1 and 40),
  norm_body_style text check (char_length(norm_body_style) between 1 and 40),

  -- Level 1.5: every mapped Government field, typed. Column names are the
  -- register's field names (lower-cased by PostgreSQL: CO2_WLTP -> co2_wltp).
  nefah_manoa integer,
  koah_sus integer,
  hanaa_cd integer,
  hanaa_nm text check (char_length(hanaa_nm) between 1 and 200),
  technologiat_hanaa_cd integer,
  technologiat_hanaa_nm text check (char_length(technologiat_hanaa_nm) between 1 and 200),
  -- The ONLY transmission statement: 1 automatic, 0 as the register states it.
  automatic_ind smallint check (automatic_ind in (0, 1)),
  -- The HOMOLOGATION STANDARD (never the transmission).
  sug_tkina_cd integer,
  sug_tkina_nm text check (char_length(sug_tkina_nm) between 1 and 200),
  -- Null when the register says `לא ידוע קוד 0` (code 0).
  sug_mamir_cd integer check (sug_mamir_cd is null or sug_mamir_cd <> 0),
  sug_mamir_nm text check (char_length(sug_mamir_nm) between 1 and 200),
  merkav text check (char_length(merkav) between 1 and 200),
  mispar_dlatot integer,
  mispar_moshavim integer,
  mishkal_kolel integer,
  kosher_grira_im_blamim integer,
  kosher_grira_bli_blamim integer,
  kamut_co2_city numeric,
  kamut_co2_hway numeric,
  co2_wltp numeric,
  nox_wltp numeric,
  co_wltp numeric,
  hc_wltp numeric,
  kvutzat_zihum integer,
  madad_yarok numeric,
  nikud_betihut numeric,
  ramat_eivzur_betihuty integer,
  mispar_kariot_avir integer,
  abs_ind smallint check (abs_ind in (0, 1)),
  -- ESC: the register's `bakarat_yatzivut_ind`.
  bakarat_yatzivut_ind smallint check (bakarat_yatzivut_ind in (0, 1)),
  -- The driver-assistance indicators and their sources, CLOSED key list.
  equipment jsonb not null default '{}'::jsonb
    check (public.catalog_variant_equipment_valid(equipment)),
  -- Values the mapper could not parse (then null above), never a failure.
  parse_issues jsonb not null default '[]'::jsonb
    check (public.catalog_variant_parse_issues_valid(parse_issues)),
  created_at timestamptz not null default now(),
  constraint catalog_variants_record_fk foreign key (snapshot_id, upstream_record_id)
    references public.catalog_raw_records (snapshot_id, upstream_record_id) on delete restrict,
  constraint catalog_variants_build_fk foreign key (snapshot_id, mapper_version)
    references public.catalog_variant_builds (snapshot_id, mapper_version) on delete restrict
);
-- One row per record per mapper version; also the FK's supporting index.
create unique index if not exists catalog_variants_record_uidx
  on public.catalog_variants (snapshot_id, upstream_record_id, mapper_version);
-- The discovery tree: one snapshot is one tozar; then model, then year.
create index if not exists catalog_variants_tree_idx
  on public.catalog_variants (snapshot_id, mapper_version, kinuy_mishari, shnat_yitzur);
-- The ledger refresh reads a key's rows within a snapshot.
create index if not exists catalog_variants_identity_idx
  on public.catalog_variants (snapshot_id, variant_identity_key)
  where variant_identity_key is not null;

create or replace function public.forbid_catalog_variant_rewrite() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'CATALOG_VARIANT_IMMUTABLE: catalog variants are append-only' using errcode = '42501';
end;
$$;
drop trigger if exists catalog_variants_append_only on public.catalog_variants;
create trigger catalog_variants_append_only
  before update or delete on public.catalog_variants
  for each row execute function public.forbid_catalog_variant_rewrite();

-- The variants the discovery tree serves: per tozar, the newest-activated
-- snapshot whose build under the CURRENT mapper version is complete.
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
-- 4. The build: one bounded batch at a time, idempotent.
-- ---------------------------------------------------------------------------
--
-- A snapshot is buildable when it is an ACTIVE, complete Government WLTP
-- snapshot whose declared scope is exactly one tozar, whose stored rows equal
-- its declared rows, and which no register capture recorded as count-
-- unverified. `p_rows` are the mapper's rows, keyed by upstream_record_id;
-- the provenance (content hash, archive line, identity key) is derived HERE
-- from the stored raw record and candidate -- never taken from the caller.
-- A row already built under this mapper version is left exactly as it is.
--
-- The ledger, for every identity key the batch touches, at BOTH new levels:
-- `enriched` when every row of the snapshot stating that key states the same
-- content; else a key collision -- `failed`, CATALOG_COVERAGE_KEY_COLLISION,
-- over every content involved (catalog_variant_coverage_apply's rule). The
-- update rule is the ledger's own (a changed content replaces; a stronger
-- status replaces; the same rank refreshes its facts), except that a row
-- recorded from a NEWER-activated snapshot is never taken back by an older
-- one. `last_run_id` is the run that wrote the snapshot: the run whose
-- durable data the row derives from.
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
  select * into v_snapshot from public.catalog_source_snapshots where id = p_snapshot_id;
  v_filters := v_snapshot.retrieval_metadata->'capture_scope'->'filters';
  if v_snapshot.id is null or v_snapshot.source_family <> 'government' or v_snapshot.activated_at is null
     or v_snapshot.validation_state <> 'complete'
     or v_snapshot.stored_record_count <> v_snapshot.declared_record_count
     or jsonb_typeof(v_filters) is distinct from 'object'
     or (case when jsonb_typeof(v_filters) = 'object'
              then (select array_agg(k) from jsonb_object_keys(v_filters) k) end) is distinct from array['tozar']
     or exists (select 1 from public.catalog_register_capture_units u
                 where u.snapshot_id = p_snapshot_id and u.count_verified is false) then
    raise exception 'CATALOG_VARIANT_SNAPSHOT_INELIGIBLE: only an active, count-verified, whole-tozar Government snapshot is built'
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
    select * from jsonb_to_recordset(p_rows) as t(
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
    mispar_kariot_avir, abs_ind, bakarat_yatzivut_ind, equipment, parse_issues)
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
         coalesce(x.equipment, '{}'::jsonb), coalesce(x.parse_issues, '[]'::jsonb)
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
     where not exists (select 1 from public.catalog_variant_builds nb
                        where nb.snapshot_key = cv.snapshot_key
                          and nb.activated_at > v_snapshot.activated_at)
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

  return jsonb_build_object('snapshot_key', v_snapshot.snapshot_key, 'mapper_version', p_mapper_version,
                            'rows', jsonb_array_length(p_rows), 'inserted', v_inserted,
                            'ledger_written', v_keys, 'built_rows', v_build.built_rows,
                            'expected_rows', v_build.expected_rows,
                            'complete', v_build.completed_at is not null);
end;
$$;

-- A build's progress; null when nothing was built yet.
create or replace function public.catalog_variant_build_state(p_snapshot_id uuid, p_mapper_version text)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select to_jsonb(b) from public.catalog_variant_builds b
   where b.snapshot_id = p_snapshot_id and b.mapper_version = p_mapper_version
$$;

-- ---------------------------------------------------------------------------
-- 5. The discovery tree (D2). Every read is ONE jsonb document (PostgREST's
--    row cap never truncates it), filtered server-side and paged: limit
--    1..100, offset 0..100000. Filters (all optional): vehicle segment, model
--    year range, fuel code (delek_cd), body (merkav, exact).
-- ---------------------------------------------------------------------------
create or replace function public.catalog_browser_rows(
  p_segment text, p_year_from integer, p_year_to integer, p_delek_cd integer, p_merkav text
) returns setof public.catalog_variants_current
language sql
stable
set search_path = pg_catalog
as $$
  select v.* from public.catalog_variants_current v
   where (p_segment is null or v.vehicle_segment = p_segment)
     and (p_year_from is null or v.shnat_yitzur >= p_year_from)
     and (p_year_to is null or v.shnat_yitzur <= p_year_to)
     and (p_delek_cd is null or v.delek_cd = p_delek_cd)
     and (p_merkav is null or v.merkav = p_merkav)
$$;

create or replace function public.catalog_browser_page_valid(p_limit integer, p_offset integer)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$ select p_limit between 1 and 100 and p_offset between 0 and 100000 $$;

create or replace function public.catalog_browser_manufacturers(
  p_segment text, p_year_from integer, p_year_to integer, p_delek_cd integer, p_merkav text,
  p_limit integer, p_offset integer
) returns jsonb
language plpgsql
stable
set search_path = pg_catalog
as $$
begin
  if not public.catalog_browser_page_valid(p_limit, p_offset) then
    raise exception 'CATALOG_BROWSER_QUERY_INVALID' using errcode = '22023';
  end if;
  return (
    with g as (select v.tozar, count(*) as n
                 from public.catalog_browser_rows(p_segment, p_year_from, p_year_to, p_delek_cd, p_merkav) v
                group by v.tozar)
    select jsonb_build_object(
      'total', (select count(*) from g), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(jsonb_build_object('tozar', p.tozar, 'variants', p.n)
                                          order by p.tozar collate "C")
                           from (select * from g order by g.tozar collate "C"
                                  limit p_limit offset p_offset) p), '[]'::jsonb)));
end;
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
                 from public.catalog_browser_rows(p_segment, p_year_from, p_year_to, p_delek_cd, p_merkav) v
                where v.tozar = p_tozar
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
                 from public.catalog_browser_rows(p_segment, p_year_from, p_year_to, p_delek_cd, p_merkav) v
                where v.tozar = p_tozar and v.kinuy_mishari = p_kinuy_mishari
                group by v.shnat_yitzur)
    select jsonb_build_object(
      'total', (select count(*) from g), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(jsonb_build_object('shnat_yitzur', p.shnat_yitzur, 'variants', p.n)
                                          order by p.shnat_yitzur desc nulls last)
                           from (select * from g order by g.shnat_yitzur desc nulls last
                                  limit p_limit offset p_offset) p), '[]'::jsonb)));
end;
$$;

-- One page of variants of one model year, in a stable order, each with its
-- level-1.5 fields and the ledger's status at every level (`current` is
-- whether that status was recorded for this variant's content).
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
                 from public.catalog_browser_rows(p_segment, p_year_from, p_year_to, p_delek_cd, p_merkav) v
                where v.tozar = p_tozar and v.kinuy_mishari = p_kinuy_mishari
                  and v.shnat_yitzur = p_shnat_yitzur),
    p as (select * from m
           order by m.degem_nm collate "C" nulls last, m.ramat_gimur collate "C" nulls last,
                    m.upstream_record_id collate "C", m.id
           limit p_limit offset p_offset)
    select jsonb_build_object(
      'total', (select count(*) from m), 'limit', p_limit, 'offset', p_offset,
      'items', coalesce((select jsonb_agg(
          (to_jsonb(p) - 'id' - 'snapshot_id' - 'created_at')
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

-- The filter options the tree offers (bounded: each is a small closed set).
create or replace function public.catalog_browser_facets(p_tozar text)
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with m as (select v.vehicle_segment, v.delek_cd, v.delek_nm, v.merkav, v.shnat_yitzur
               from public.catalog_variants_current v
              where p_tozar is null or v.tozar = p_tozar)
  select jsonb_build_object(
    'segments', coalesce((select jsonb_agg(jsonb_build_object('value', s.vehicle_segment, 'variants', s.n)
                                           order by s.vehicle_segment)
                            from (select vehicle_segment, count(*) n from m group by 1) s), '[]'::jsonb),
    'fuels', coalesce((select jsonb_agg(jsonb_build_object('delek_cd', f.delek_cd, 'delek_nm', f.delek_nm,
                                                           'variants', f.n) order by f.delek_cd, f.delek_nm)
                         from (select delek_cd, min(delek_nm) as delek_nm, count(*) n from m
                                where delek_cd is not null group by 1 limit 100) f), '[]'::jsonb),
    'bodies', coalesce((select jsonb_agg(jsonb_build_object('merkav', b.merkav, 'variants', b.n)
                                         order by b.merkav collate "C")
                          from (select merkav, count(*) n from m where merkav is not null
                                 group by 1 limit 100) b), '[]'::jsonb),
    'year_min', (select min(shnat_yitzur) from m),
    'year_max', (select max(shnat_yitzur) from m))
$$;

-- ---------------------------------------------------------------------------
-- 6. Retention (PR-D1): a snapshot with variants or a variant build is KEPT.
--    Otherwise exactly 20260929000100's rule.
-- ---------------------------------------------------------------------------
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
    from scoped sc
   where not exists (select 1 from ranked k where k.id = sc.id and k.activation_rank <= 2)
     and not exists (select 1 from public.catalog_candidate_evidence_links x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_canonical_field_provenance x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_snapshot_adoptions x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_units x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_batches x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_work_scope_queue_items x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_variant_coverage x where x.snapshot_key = sc.snapshot_key)
     and not exists (select 1 from public.catalog_variant_builds x where x.snapshot_id = sc.id)
     and not exists (select 1 from public.catalog_variants x where x.snapshot_id = sc.id)
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

-- ---------------------------------------------------------------------------
-- 7. RLS and privileges (PR-D1's pattern).
-- ---------------------------------------------------------------------------
alter table public.catalog_variant_builds enable row level security;
alter table public.catalog_variants enable row level security;

do $$
declare
  fn text;
  tbl text;
  ro record;
  v_reads text[] := array[
    'public.catalog_variant_mapper_version()',
    'public.catalog_variant_equipment_valid(jsonb)',
    'public.catalog_variant_parse_issues_valid(jsonb)',
    'public.catalog_variant_build_state(uuid,text)',
    'public.catalog_browser_rows(text,integer,integer,integer,text)',
    'public.catalog_browser_page_valid(integer,integer)',
    'public.catalog_browser_manufacturers(text,integer,integer,integer,text,integer,integer)',
    'public.catalog_browser_models(text,text,integer,integer,integer,text,integer,integer)',
    'public.catalog_browser_years(text,text,text,integer,integer,integer,text,integer,integer)',
    'public.catalog_browser_variants(text,text,integer,text,integer,integer,integer,text,integer,integer)',
    'public.catalog_browser_facets(text)'];
  v_relations text[] := array['public.catalog_variant_builds', 'public.catalog_variants',
                              'public.catalog_variants_current'];
begin
  -- Writes: service role only.
  fn := 'public.record_catalog_variants(uuid,text,jsonb)';
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
  foreach tbl in array v_relations loop
    execute format('revoke all on table %s from public', tbl);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table %s from anon', tbl);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table %s from authenticated', tbl);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    -- Written only through record_catalog_variants; variants never updated
    -- or deleted (the trigger refuses it too).
    execute 'grant select, insert on table public.catalog_variants to service_role';
    execute 'grant select, insert, update on table public.catalog_variant_builds to service_role';
    execute 'grant select on table public.catalog_variants_current to service_role';
    execute 'revoke delete, truncate on table public.catalog_variants from service_role';
    execute 'revoke delete, truncate on table public.catalog_variant_builds from service_role';
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
    foreach tbl in array v_relations loop
      execute format('grant select on table %s to %I', tbl, ro.rolname);
    end loop;
    foreach fn in array v_reads loop
      execute format('grant execute on function %s to %I', fn, ro.rolname);
    end loop;
    -- catalog_browser_variants reads the ledger's status per level.
    execute format('grant select on table public.catalog_variant_coverage to %I', ro.rolname);
  end loop;
end $$;
