-- Catalog PR3, part 1: bounded DATABASE-SIDE aggregation over candidate rows.
--
-- Why this exists
-- ---------------
--
-- Catalog PR2's `backend/catalog/government/projection.py` answers by reading a
-- whole snapshot into Python and grouping it there. That is correct and cheap
-- for the pinned `q=RAV4` capture (233 rows) and it REFUSES -- deliberately,
-- with `GOV_PROJECTION_BOUND_EXCEEDED` -- any snapshot beyond
-- `MAX_PROJECTION_CANDIDATES` (5 000). A complete WLTP resource is around
-- 101 000 rows, so the Tool that Catalog PR3 registers could not be answered by
-- that path at all.
--
-- These five functions are that missing support, and nothing more. They are
-- READS: no lease, no write, no canonical row, no ingestion. Each one takes an
-- ALREADY ACTIVE snapshot id, applies fixed filters in a fixed order, and
-- returns one explicitly bounded page together with the EXACT total so a caller
-- can state `has_more` rather than infer it.
--
-- They are GENERIC over `public.catalog_candidate_variants`, not Government
-- specific: nothing here names `data.gov.il`, CKAN, a Government field or a
-- vehicle register. Which family and which resource a snapshot belongs to is
-- decided by the caller that resolved it (`find_active_catalog_snapshot` pins
-- family, resource, key and active state), so this migration adds no
-- Government-specific relation and no Government-specific vocabulary.
--
-- What is deliberately NOT here
-- -----------------------------
--
-- *   No dynamic table name, no caller-supplied SQL, no caller-chosen ordering
--     and no unbounded materialization. Every relation is a literal, every
--     ordering is fixed in the function body, and every page is capped by a
--     server-owned constant this file declares.
-- *   No whole-resource dump. The largest page any of these will return is
--     `catalog_page_limit()` rows, and a caller asking for more is given that
--     many rather than an error -- the bound is the answer, not a suggestion.
-- *   No weakening of `MAX_PROJECTION_CANDIDATES`. The Python projection keeps
--     its own bound and keeps refusing beyond it; this is a SECOND reader with
--     a different cost model, not a wider version of the first.
--
-- Ordering and collation
-- ----------------------
--
-- The identity text in this namespace is Hebrew. PostgreSQL's default text
-- collation is a property of the cluster, so ordering by it would make "the
-- same snapshot produces the same ordered page" false on a differently
-- configured server -- and the Python projection sorts by CODEPOINT. Every
-- ordering below is therefore `collate "C"`, which for UTF-8 is byte order and
-- therefore codepoint order, and the indexes are created with the same
-- collation so the ordering is both deterministic AND indexed.

-- ---------------------------------------------------------------------------
-- 1. The server-owned page bound, as a function rather than a literal.
-- ---------------------------------------------------------------------------
--
-- One definition, referenced by every function below, mirroring
-- `MAX_RESULT_ITEMS` in `backend/catalog/government/projection.py` and pinned
-- against it by `tests/test_catalog_migration_static.py`.
create or replace function public.catalog_page_limit()
returns integer language sql immutable
set search_path = pg_catalog
as $$ select 200 $$;

-- ---------------------------------------------------------------------------
-- 2. The snapshot gate: active, complete, and USABLE.
-- ---------------------------------------------------------------------------
--
-- `activated_at is not null` is what makes a snapshot readable at all, and
-- `validation_state = 'complete'` is already implied by the activation CHECK --
-- both are asserted here anyway, because a reader that depends on an invariant
-- should state it rather than inherit it.
--
-- USABILITY is the third condition and the one PR2 introduced: a snapshot whose
-- own durable metadata records rows its reviewed vocabulary could not read is a
-- real capture with a stated, counted gap, and it may answer only when a caller
-- has acknowledged that gap. A `raw_only` snapshot states no identities at all
-- and is never answerable, acknowledged or not.
--
-- The FULL parse of that metadata lives in `parse_normalization_state`
-- (Python): it checks every count, every reason and every id, and refuses a
-- summary that is internally inconsistent. This function deliberately checks
-- only the two things SQL can check cheaply and without reproducing that parser
-- -- so it is defence in depth for a direct caller, not a second definition of
-- the rule.
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

-- ---------------------------------------------------------------------------
-- 3. Indexes for the exact filters and orderings below.
-- ---------------------------------------------------------------------------
--
-- Created with the SAME `collate "C"` the functions order by, so the ordering
-- is served by the index rather than by a sort of the whole snapshot.
create index if not exists catalog_candidate_variants_snapshot_identity_idx
  on public.catalog_candidate_variants
  (snapshot_id, manufacturer collate "C", commercial_model collate "C",
   model_year_start, model_year_end, candidate_key collate "C");

create index if not exists catalog_candidate_variants_snapshot_code_idx
  on public.catalog_candidate_variants
  (snapshot_id, official_model_code collate "C", candidate_key collate "C")
  where official_model_code is not null;

create index if not exists catalog_candidate_variants_snapshot_status_idx
  on public.catalog_candidate_variants (snapshot_id, status, candidate_key collate "C");

-- ---------------------------------------------------------------------------
-- 4. The bounded aggregations.
-- ---------------------------------------------------------------------------
--
-- Each returns `total_count`: the EXACT number of rows the filter matched,
-- computed over the same filtered set the page comes from, so `has_more` is a
-- fact rather than "the page came back full".
--
-- THE EMPTY PAGE, and why it still carries the count
-- --------------------------------------------------
--
-- A page whose offset is past the last matching row has no rows to hang the
-- count on, and a reader that inferred "no rows, therefore nothing matched"
-- would report a total of 0 for a filter that matched thousands -- and would
-- then say `has_more` is false while every row is still unread.
--
-- So every function below joins its page to a one-row anchor. When the page is
-- empty the anchor still produces exactly ONE row: every item column NULL, and
-- `total_count` the real count. `_read` in
-- `backend/catalog/government/query.py` recognises that row by its shape --
-- every column except `total_count` is null -- drops it from the items, and
-- keeps the count. It is a COUNT ROW, never a result, and it is the same shape
-- in all four functions so no caller needs per-function knowledge.

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
      from public.catalog_candidate_variants c
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
      from public.catalog_candidate_variants c
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

-- One row per model YEAR, expanded from each candidate's stated range. A
-- candidate covering 2022-2024 is three model years here, because that is what
-- the range says; `generate_series` is bounded by the range CHECK on the
-- column itself (1900..2100), so no row can expand without limit.
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
      from public.catalog_candidate_variants c
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

-- The one row-level page: every candidate matching the stated filters, with the
-- raw record it was read from. `p_model_year` selects candidates whose stated
-- range CONTAINS that year. Every filter is optional and an omitted one is not
-- applied -- but the snapshot is never optional, so an unfiltered call is still
-- one snapshot's rows, one bounded page at a time.
--
-- `p_identity_dimensions` is a jsonb object of dimension -> exact value and is
-- matched with `@>` (containment), never interpreted: the key set is already
-- closed by `catalog_identity_dimensions_valid` on the column.
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
  p_allow_incomplete boolean default false
) returns table (
  id uuid, snapshot_id uuid, raw_record_id uuid, manufacturer text,
  commercial_model text, model_year_start integer, model_year_end integer,
  official_model_code text, "trim" text, identity_dimensions jsonb, status text,
  candidate_key text, upstream_record_id text, resource_id text,
  source_locator jsonb, payload_sha256 text, total_count bigint
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
           r.upstream_record_id, r.resource_id, r.source_locator, r.payload_sha256
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
         p.resource_id, p.source_locator, p.payload_sha256,
         (select count(*) from matched)
    from (select 1) as anchor
    left join page p on true
   order by p.manufacturer collate "C" nulls last, p.commercial_model collate "C",
            p.model_year_start, p.model_year_end,
            coalesce(p.official_model_code, '') collate "C",
            coalesce(p.trim, '') collate "C", p.candidate_key collate "C";
end;
$$;

-- ---------------------------------------------------------------------------
-- 4b. What changed between two snapshots of one resource -- computed HERE.
-- ---------------------------------------------------------------------------
--
-- The one comparison in this namespace that spans a WHOLE snapshot. It runs
-- inside PostgreSQL precisely so it never becomes an unbounded read: the real
-- resource is ~101 000 candidate rows per side, and comparing them by reading
-- both into a process would materialize 200 000 rows to answer one question.
--
-- What it returns, and what is bounded
-- ------------------------------------
--
-- The three COUNTS are exact, always, over every matching row of both
-- snapshots -- never over a page, and never refused for size. What is bounded
-- is the ITEM LIST: at most `p_limit` delta rows, and dropped WHOLE rather
-- than truncated when more than that changed, because a truncated list is a
-- diff claiming a completeness it does not have.
--
-- An answer with no items is the same COUNT ROW shape the aggregations above
-- return: one row, every delta column null, the three counts real. So "nothing
-- changed" and "too much changed to list" are both counted exactly, and are
-- told apart by the counts rather than by the absence of rows.
--
-- The IDENTITY is the candidate's complete stated identity including its
-- dimensions, mirroring `DIFF_IDENTITY` in `backend/catalog/diff.py`. A
-- snapshot may state one identity twice -- the register does -- and the FIRST
-- row in the page order wins on both sides, which is what makes the comparison
-- independent of which duplicate happened to be read last.
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
      from public.catalog_candidate_variants c
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

-- One register row by its OWN upstream id, with the preserved payload. Bounded
-- to a single row by the snapshot's own unique index on
-- `(snapshot_id, upstream_record_id)`; there is no "list every record"
-- counterpart, deliberately.
create or replace function public.catalog_raw_record_by_upstream_id(
  p_snapshot_id uuid, p_upstream_record_id text, p_allow_incomplete boolean default false
) returns setof public.catalog_raw_records
language plpgsql stable
set search_path = pg_catalog
as $$
begin
  perform public.catalog_readable_snapshot(p_snapshot_id, p_allow_incomplete);
  if p_upstream_record_id is null or btrim(p_upstream_record_id) = '' then
    raise exception 'an upstream record id is required' using errcode = '22023';
  end if;
  return query
  select * from public.catalog_raw_records
   where snapshot_id = p_snapshot_id and upstream_record_id = p_upstream_record_id
   limit 1;
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. Privileges: service-path only, least privilege, EXECUTE revoked first.
-- ---------------------------------------------------------------------------
--
-- Same posture as every other function in this namespace. These are reads, so
-- they need no new table privilege at all: `service_role` already holds SELECT
-- on both relations, and being SECURITY INVOKER they can never read more than
-- the caller could. No grant is added to PUBLIC, `anon` or `authenticated`,
-- and the EXECUTE Supabase default privileges would otherwise hand those roles
-- is revoked before the narrow grant.
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.catalog_page_limit()',
    'public.catalog_readable_snapshot(uuid,boolean)',
    'public.catalog_candidate_manufacturers(uuid,integer,integer,boolean)',
    'public.catalog_candidate_models(uuid,text,integer,integer,boolean)',
    'public.catalog_candidate_model_years(uuid,text,text,integer,integer,boolean)',
    'public.catalog_candidate_variant_page(uuid,text,text,integer,text,text,jsonb,text,integer,integer,boolean)',
    'public.catalog_snapshot_candidate_diff(uuid,uuid,integer,boolean)',
    'public.catalog_raw_record_by_upstream_id(uuid,text,boolean)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
end $$;
