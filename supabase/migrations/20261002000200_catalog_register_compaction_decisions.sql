-- PR-L2 follow-up: the Mapping Plan's coverage decisions read a compacted
-- snapshot's rows from their variant row joined ONCE.
--
-- 20261002000100 left catalog_work_scope_coverage_decisions reading every row
-- through the resolved view and then four correlated helper calls per row
-- (catalog_raw_record_code x3, catalog_raw_record_content_sha256): on a
-- compacted snapshot each one re-joins the compaction and the variant row --
-- five joins to the same variant row per candidate. (Measured with every
-- decision evaluated, 6,000 rows: 3,452 ms before compaction, 3,515 ms after;
-- with this read, 3,524 ms before and 2,616 ms after -- most of the rest is
-- the per-row catalog_variant_identity_key the ledger join computes, which
-- predates PR-L2.)
--
-- catalog_candidate_register_reading(snapshot) is that read, once: every
-- candidate of the snapshot with its identity, its register codes
-- (tozeret_cd, degem_cd, sug_degem) and its content hash --
--   (a) a candidate still stating its identity: as stored, the codes and the
--       hash from its raw payload (the two helpers, whose payload branch is a
--       plain field read);
--   (b) a compacted candidate: its identity, codes and hash from its variant
--       row (the compaction's mapper version), joined once, exactly as the
--       resolved view's branch (b) reads its identity; a candidate whose
--       variant row is gone (the view's branch (c)) keeps a null identity and
--       null codes, as before.
-- The split rests on an INVARIANT: a candidate that was never compacted never
-- has `manufacturer IS NULL`. A new candidate must carry its identity (the
-- BEFORE INSERT trigger catalog_candidate_variants_identity_required,
-- CATALOG_CANDIDATE_IDENTITY_REQUIRED, 20261002000100), and its identity is
-- immutable afterwards (catalog_candidate_variants_identity_immutable,
-- 20260914200000), a trigger suspended only inside compact_register_snapshot
-- -- which nulls the identity together with the row's payload -- and inside
-- the prune, which deletes rows. So branch (a) is exactly the uncompacted rows
-- and branch (b) exactly the compacted (or skeleton) ones.
-- It is inlinable SQL (stable, no SET, one SELECT; every name qualified), and
-- the decisions read it with nothing else changed: the placeholder rule, the
-- ledger join, the archived-snapshot guard and the year filter. Same result,
-- row for row (tests/test_register_compaction_postgres.py holds the two equal).
--
-- Forward-only and rerun-safe.

create or replace function public.catalog_candidate_register_reading(p_snapshot_id uuid)
returns table (candidate_id uuid, status text, upstream_record_id text, manufacturer text,
               commercial_model text, model_year_start integer, model_year_end integer,
               official_model_code text, "trim" text, identity_dimensions jsonb,
               tozeret_cd text, degem_cd text, sug_degem text, content_sha256 text)
language sql
stable
as $$
  select c.id, c.status, r.upstream_record_id, c.manufacturer, c.commercial_model, c.model_year_start,
         c.model_year_end, c.official_model_code, c.trim, c.identity_dimensions,
         public.catalog_raw_record_code(r, 'tozeret_cd'), public.catalog_raw_record_code(r, 'degem_cd'),
         public.catalog_raw_record_code(r, 'sug_degem'), public.catalog_raw_record_content_sha256(r)
    from public.catalog_candidate_variants c
    join public.catalog_raw_records r on r.id = c.raw_record_id
   where c.snapshot_id = p_snapshot_id and c.manufacturer is not null
  union all
  select c.id, c.status, r.upstream_record_id,
         nullif(btrim(v.tozar), ''), nullif(btrim(v.kinuy_mishari), ''), v.shnat_yitzur, v.shnat_yitzur,
         nullif(btrim(v.degem_nm), ''), nullif(btrim(v.ramat_gimur), ''),
         case when v.id is null then c.identity_dimensions
              else jsonb_strip_nulls(jsonb_build_object(
                     'body_style', v.norm_body_style, 'drivetrain', v.norm_drivetrain,
                     'fuel_type', v.norm_fuel_type, 'propulsion_technology', v.norm_propulsion_technology)) end,
         v.tozeret_cd::text, v.degem_cd::text, v.sug_degem, v.content_sha256
    from public.catalog_candidate_variants c
    join public.catalog_raw_records r on r.id = c.raw_record_id and r.snapshot_id = c.snapshot_id
    left join public.catalog_register_snapshot_compactions k
      on k.snapshot_id = c.snapshot_id and k.readers = 'variants'
    left join public.catalog_variants v
      on v.snapshot_id = c.snapshot_id and v.mapper_version = k.mapper_version
     and v.upstream_record_id = r.upstream_record_id
   where c.snapshot_id = p_snapshot_id and c.manufacturer is null
$$;

-- catalog_work_scope_coverage_decisions: restated from 20261002000100; it
-- reads catalog_candidate_register_reading instead of the resolved view and
-- the four per-row helper calls. Nothing else changed.
create or replace function public.catalog_work_scope_coverage_decisions(
  p_snapshot_id uuid, p_from integer, p_to integer, p_include_unresolved boolean
) returns table (candidate_id uuid, upstream_record_id text, decision text)
language sql
stable
as $$
  select x.candidate_id, x.upstream_record_id,
         -- P27: a placeholder source record is never queued, whatever the ledger says.
         case when public.catalog_is_placeholder_identity(x.commercial_model, x.official_model_code)
              then 'excluded_placeholder_source_record'
              else public.catalog_variant_coverage_decision(cv.status, cv.content_sha256,
                                                            cv.vocabulary_version, x.content_sha256,
                                                            p_include_unresolved) end
    from public.catalog_candidate_register_reading(p_snapshot_id) x
    left join public.catalog_variant_coverage cv
      on cv.level = 'register'
     and cv.variant_identity_key = public.catalog_variant_identity_key(
           x.manufacturer, x.commercial_model, x.model_year_start, x.model_year_end,
           x.official_model_code, x.trim, x.tozeret_cd, x.degem_cd, x.sug_degem, x.identity_dimensions)
   where x.status = 'candidate'
     -- Evaluated once (it reads the parameter only).
     and public.catalog_snapshot_not_archived(p_snapshot_id)
     and (p_from is null or x.model_year_start >= p_from)
     and (p_to is null or x.model_year_end <= p_to);
$$;

-- Service-path only, as the decisions themselves; the read-only release roles
-- read it exactly as 20261002000100 lets them read the decisions.
do $$
declare
  fn text := 'public.catalog_candidate_register_reading(uuid)';
  ro record;
begin
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
  for ro in
    select r.rolname from pg_roles r
     where r.rolcanlogin and not r.rolsuper
       and r.rolname not in ('postgres', 'service_role', 'authenticator', 'anon', 'authenticated')
       and (r.rolname = 'supabase_read_only_user'
            or (r.rolbypassrls and r.rolname like 'milo\_release\_readonly\_%')
            or (r.rolbypassrls and pg_has_role(r.oid, 'pg_read_all_data', 'MEMBER')))
  loop
    if has_function_privilege(ro.rolname,
         'public.catalog_work_scope_coverage_decisions(uuid,integer,integer,boolean)', 'EXECUTE') then
      execute format('grant execute on function %s to %I', fn, ro.rolname);
    end if;
  end loop;
end $$;
