-- R5: CURRENT verdict authority. Append-only history stops meaning "true now".
--
-- THE DEFECT THIS CLOSES
-- ----------------------
--
-- public.claim_verdicts is append-only, and that is right: a verdict is an
-- audit record and rewriting one would destroy the history it exists for. But
-- every reader asked the table the wrong question. They asked whether a
-- verified verdict EXISTS:
--
--     t0  verdict: verified     (the register said 1798 cc)
--     t1  verdict: rejected     (a re-verification found 1600 cc)
--
--     exists (select 1 from public.claim_verdicts
--              where claim_id = c.id and verdict = 'verified')   ->  true, forever
--
-- So a newer invalidation, contradiction, supersession or unsupported state
-- could be bypassed by any older `verified` row that happened to remain in the
-- table. `public.catalog_run_pending_promotions` joined on exactly that
-- predicate, `link_catalog_candidate_evidence_guarded` checked only what the
-- CITED verdict said, and the canonical field-provenance gate re-checked the
-- same cited row -- so a stale `verified` could authorize a canonical fact.
--
-- WHAT THIS MIGRATION ADDS
-- ------------------------
--
--   public.current_verdict_contract_version()  the bounded contract id
--   public.claim_current_verdict_id()          the ordering rule, alone
--   public.claim_current_verdict_state()       ONE claim's current state
--   public.claim_current_verdict_states()      a bounded read for a run
--   public.claim_verdict_is_current_support()  "is THAT citation current?"
--
-- plus two BEFORE INSERT triggers that hold every writer -- including a direct
-- `service_role` INSERT that never came through the backend -- to the current
-- state, and a rewritten `catalog_run_pending_promotions` that resolves
-- current truth instead of scanning history.
--
-- THE RULE, STATED ONCE
-- ---------------------
--
--   1. the claim row must still be ACTIVE, else `invalidated`;
--   2. a resolved conflict naming it among the superseded claims makes it
--      `superseded`;
--   3. an UNRESOLVED conflict covering it makes it `contested`;
--   4. otherwise the CURRENT verdict is the latest row by `created_at`, then
--      -- for rows written in the same instant -- a non-`verified` verdict
--      ahead of a `verified` one, then the greatest id. That middle term is
--      the fail-closed tiebreak;
--   5. no verdict row is `unverified`;
--   6. a current `rejected` / `needs_review` verdict is exactly that;
--   7. a current `verified` verdict citing no durable support row, or stating
--      no evidence-bearing mode and contract version, is `unsupported`;
--   8. and only then, `supported`.
--
-- `supported` is the ONLY state that authorizes anything.
--
-- backend/engines/swarm_v2/current_verdict.py implements exactly this rule in
-- Python, for the same reason every other durable rule in this schema has a
-- backend twin: so a refusal is a readable static reason before it is a
-- database exception, and so the in-memory repository and PostgreSQL apply one
-- rule rather than two. tests/test_current_verdict_authority.py and
-- tests/test_migrations_postgres.py pin the two definitions together.
--
-- Additive and forward-only. No existing table, column, row or constraint is
-- modified or rewritten, nothing is backfilled, and no verdict already durable
-- is changed: what changes is which of them is read as CURRENT. Rerun-safe:
-- every function, index and trigger is created or replaced idempotently.
--
-- ORDER MATTERS, and only for a RE-RUN.
-- `20260916120000_catalog_field_level_promotion.sql` defines
-- `catalog_run_pending_promotions` with the "a verified verdict exists" join
-- this file replaces. Migrations apply strictly in filename sequence
-- (`docs/production-readiness/MIGRATIONS.md`), so the shipped end state is
-- this one -- but an operator who re-runs `20260916120000` AFTER this file
-- would silently get that join back. This migration is rerun-safe and must be
-- re-applied last; `test_reapplying_the_catalog_promotion_migration_needs_
-- this_one_again` states the hazard and its remedy.

-- ---------------------------------------------------------------------------
-- 1. The contract identifier, as a function so there is one definition.
-- ---------------------------------------------------------------------------
create or replace function public.current_verdict_contract_version()
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select 'r5.current_verdict.1'::text;
$$;

-- ---------------------------------------------------------------------------
-- 2. Indexes the resolution needs, so current truth is cheap to ask for.
-- ---------------------------------------------------------------------------
--
-- The existing `claim_verdicts_claim_idx` is on (run_id, claim_id, created_at)
-- and cannot serve a lookup that is about the CLAIM rather than the run: a
-- verdict is current or stale independently of which run is asking.
create index if not exists claim_verdicts_claim_current_idx
  on public.claim_verdicts(claim_id, created_at desc, id desc);
-- Containment lookups over the two contradiction relations.
create index if not exists conflicts_claim_ids_gin_idx
  on public.conflicts using gin (claim_ids);
create index if not exists conflict_resolutions_superseded_gin_idx
  on public.conflict_resolutions using gin (superseded_claim_ids);

-- ---------------------------------------------------------------------------
-- 3. The ordering rule, alone and readable.
-- ---------------------------------------------------------------------------
--
-- `(verdict = 'verified') asc` puts a non-verified verdict FIRST among rows
-- that share a `created_at`: when two verdicts are indistinguishable in time,
-- the one that does not assert verification wins. `id desc` makes the answer
-- independent of the order rows are read in.
create or replace function public.claim_current_verdict_id(p_claim_id uuid)
returns uuid
language sql
stable
set search_path = pg_catalog
as $$
  select v.id
    from public.claim_verdicts v
   where v.claim_id = p_claim_id
   order by v.created_at desc, (v.verdict = 'verified') asc, v.id desc
   limit 1;
$$;

-- ---------------------------------------------------------------------------
-- 4. ONE claim's current state, whole.
-- ---------------------------------------------------------------------------
--
-- Returns NO ROW for a claim that does not exist. That is deliberate: an
-- unknown claim must not be readable as "not verified" (which is a statement
-- about a claim) and must not be readable as a claim either.
create or replace function public.claim_current_verdict_state(p_claim_id uuid)
returns table (
  claim_id uuid, state text, verdict_id uuid, verdict text, reason text,
  verification_mode text, support_count integer, contract_version text
)
language plpgsql
stable
set search_path = pg_catalog
as $$
declare
  v_claim public.claims;
  v_row public.claim_verdicts;
  v_support integer := 0;
  v_state text;
begin
  if p_claim_id is null then
    raise exception 'a claim is required' using errcode = '22023';
  end if;
  select * into v_claim from public.claims where id = p_claim_id;
  if v_claim.id is null then
    return;
  end if;

  if v_claim.status is distinct from 'active' then
    v_state := 'invalidated';
  elsif exists (select 1 from public.conflict_resolutions r
                 where r.state = 'resolved'
                   and r.superseded_claim_ids @> array[p_claim_id]) then
    -- The losing claim keeps its row, its evidence and its history. What it
    -- does not keep is its old verdict's standing as current truth.
    v_state := 'superseded';
  elsif exists (select 1 from public.conflicts c
                 where c.outcome = 'unresolved_needs_review'
                   and c.claim_ids @> array[p_claim_id]) then
    -- Two sources disagree and nobody has decided. Nothing is current.
    v_state := 'contested';
  else
    select * into v_row from public.claim_verdicts v
      where v.claim_id = p_claim_id
      order by v.created_at desc, (v.verdict = 'verified') asc, v.id desc
      limit 1;
    if v_row.id is null then
      v_state := 'unverified';
    else
      select count(*) into v_support
        from public.claim_verdict_supports s where s.verdict_id = v_row.id;
      if v_row.verdict = 'verified' then
        if v_row.verification_mode in ('deterministic_structured', 'grounded_model')
           and nullif(btrim(coalesce(v_row.verifier_contract_version, '')), '') is not null
           and v_support > 0 then
          v_state := 'supported';
        else
          -- A `verified` that cites no durable evidence row, or that cannot
          -- say HOW it was reached, is an assertion rather than evidence.
          v_state := 'unsupported';
        end if;
      elsif v_row.verdict in ('needs_review', 'rejected') then
        v_state := v_row.verdict;
      else
        -- An unknown verdict is not a weaker known one.
        v_state := 'unsupported';
      end if;
    end if;
  end if;

  return query
    select p_claim_id, v_state, v_row.id, v_row.verdict, v_row.reason,
           v_row.verification_mode, coalesce(v_support, 0)::integer,
           public.current_verdict_contract_version();
end;
$$;

-- ---------------------------------------------------------------------------
-- 5. A bounded read: every claim of one run, resolved by the same rule.
-- ---------------------------------------------------------------------------
--
-- A READ: no lease, `SECURITY INVOKER`, fixed `search_path`, every relation
-- named as a literal, and bounded so it can never become an unbounded scan.
create or replace function public.claim_current_verdict_states(
  p_run_id uuid, p_claim_ids uuid[] default null, p_limit integer default 200
) returns table (
  claim_id uuid, state text, verdict_id uuid, verdict text, reason text,
  verification_mode text, support_count integer, contract_version text
)
language plpgsql
stable
set search_path = pg_catalog
as $$
declare v_limit integer;
begin
  if p_run_id is null then
    raise exception 'a run is required' using errcode = '22023';
  end if;
  v_limit := greatest(0, least(coalesce(p_limit, 200), 500));
  return query
    select s.claim_id, s.state, s.verdict_id, s.verdict, s.reason,
           s.verification_mode, s.support_count, s.contract_version
      from public.claims c
      join lateral public.claim_current_verdict_state(c.id) s on true
     where c.run_id = p_run_id
       and (p_claim_ids is null or c.id = any (p_claim_ids))
     order by c.id
     limit v_limit;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. "Is THAT citation the current, supported verdict?"
-- ---------------------------------------------------------------------------
--
-- Two questions in one, deliberately. A citation of a verdict that is no
-- longer current is refused even when the current verdict also says
-- `verified`: the fact being written down rests on the row that was cited and
-- on nothing else. A NULL verdict id answers false, so the gates below fail
-- closed on a link that cites nothing.
create or replace function public.claim_verdict_is_current_support(
  p_claim_id uuid, p_verdict_id uuid
) returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select exists (
    select 1 from public.claim_current_verdict_state(p_claim_id) s
     where s.state = 'supported' and s.verdict_id = p_verdict_id);
$$;

-- ---------------------------------------------------------------------------
-- 7. The two durable gates, for EVERY writer.
-- ---------------------------------------------------------------------------
--
-- Both are separate, additive triggers rather than edits of the existing gate
-- functions: the rule they add is one property, it is readable on its own, and
-- re-stating a 300-line trigger body to append it would be the more dangerous
-- change.

-- 7a. A link may not be created citing a verdict that is not current.
create or replace function public.catalog_check_link_current_verdict() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  -- A link with no verdict asserts no verification; `link_catalog_candidate_
  -- evidence_guarded` and the promotion gate both refuse to promote from one.
  if new.verdict_id is null then
    return new;
  end if;
  if not public.claim_verdict_is_current_support(new.claim_id, new.verdict_id) then
    raise exception 'catalog evidence link verdict is not the claim''s current supported verdict'
      using errcode = '22023';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_candidate_evidence_links_current_verdict
  on public.catalog_candidate_evidence_links;
create trigger catalog_candidate_evidence_links_current_verdict
  before insert on public.catalog_candidate_evidence_links
  for each row execute function public.catalog_check_link_current_verdict();

-- 7b. THE ONE THAT MATTERS. A canonical fact may only be written from the
--     claim's CURRENT supported verdict. A link created while its verdict was
--     current, followed by a newer invalidation, is exactly the stale
--     authorization this migration exists to refuse -- and the link-time check
--     above cannot see it, because the link was already legitimate when it was
--     written.
--
--     The link is read here rather than the row's own derived columns, so this
--     holds whatever order the two BEFORE INSERT triggers fire in.
create or replace function public.catalog_check_provenance_current_verdict() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare v_link public.catalog_candidate_evidence_links;
begin
  select * into v_link from public.catalog_candidate_evidence_links
    where id = new.evidence_link_id;
  if v_link.id is null then
    raise exception 'canonical field provenance cites no catalog evidence link'
      using errcode = '23503';
  end if;
  if v_link.verdict_id is null then
    raise exception 'canonical field provenance cites unverified evidence'
      using errcode = '22023';
  end if;
  if not public.claim_verdict_is_current_support(v_link.claim_id, v_link.verdict_id) then
    raise exception 'canonical field provenance cites a verdict that is no longer current'
      using errcode = '22023';
  end if;
  return new;
end;
$$;

drop trigger if exists catalog_canonical_field_provenance_current_verdict
  on public.catalog_canonical_field_provenance;
create trigger catalog_canonical_field_provenance_current_verdict
  before insert on public.catalog_canonical_field_provenance
  for each row execute function public.catalog_check_provenance_current_verdict();

-- ---------------------------------------------------------------------------
-- 8. The pending-promotion read, resolving CURRENT truth.
-- ---------------------------------------------------------------------------
--
-- Identical to `20260916120000_catalog_field_level_promotion.sql` in every
-- respect but ONE: the `evidence` CTE no longer joins `public.claim_verdicts`
-- on "a verified verdict exists for this claim". It joins the resolved current
-- state and keeps only `supported`, and the `verdict_id` it returns is the
-- CURRENT verdict rather than whichever historical row matched. The signature,
-- the column list, the bound and the ordering are unchanged, so every caller
-- and every downstream contract stays exactly as it was.
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
      join public.catalog_candidate_variants cand
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

-- ---------------------------------------------------------------------------
-- 9. Privileges: service-only, re-asserted rather than assumed.
-- ---------------------------------------------------------------------------
do $$
declare fn text;
begin
  foreach fn in array array[
    'public.current_verdict_contract_version()',
    'public.claim_current_verdict_id(uuid)',
    'public.claim_current_verdict_state(uuid)',
    'public.claim_current_verdict_states(uuid,uuid[],integer)',
    'public.claim_verdict_is_current_support(uuid,uuid)',
    'public.catalog_check_link_current_verdict()',
    'public.catalog_check_provenance_current_verdict()',
    'public.catalog_run_pending_promotions(uuid,text,integer)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke execute on function %s from anon', fn);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke execute on function %s from authenticated', fn);
    end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant execute on function %s to service_role', fn);
    end if;
  end loop;
end;
$$;
