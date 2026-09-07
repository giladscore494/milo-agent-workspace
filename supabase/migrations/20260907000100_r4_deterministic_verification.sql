-- R4: deterministic verification, durable support links, explicit conflict
-- resolution.
--
-- R3 made evidence REAL: a versioned source, a located fragment and a located
-- structured fact.  What it deliberately did not do was compare any of it.  So
-- two things were still impossible to state durably:
--
--   * WHICH evidence settled a claim, under which rules, by which mechanism.
--     A verdict lived only inside a run checkpoint as three strings, so an
--     accepted `verified` could never be re-checked or replayed.
--   * That a contradiction had been DECIDED.  public.conflicts could be
--     opened, and its `outcome` column could in principle be rewritten, but
--     nothing recorded who won, what superseded what, or under which policy --
--     and rewriting the row would have destroyed the history it exists for.
--
-- This migration adds the smallest durable objects that fix both, plus the one
-- column the deterministic comparison needs:
--
--   public.claims                  + identity_scope        (closed identity dimensions)
--   public.claim_verdicts          NEW  one verdict per claim per run
--   public.claim_verdict_supports  NEW  the exact durable fragments behind it
--   public.conflict_resolutions    NEW  one typed decision per contradicting scope
--
-- Additive, nullable and forward-only.  No existing table, column, RPC, row or
-- index is modified or rewritten and nothing is backfilled: every claim
-- written before R4 keeps a NULL identity_scope, stays readable exactly as it
-- is, and is simply not a fully R4-qualified structured fact.  Legacy verdicts
-- have no row here at all, which is precisely how they stay readable in their
-- checkpoint without being presented as R4-grounded.
--
-- Why NEW relations rather than columns on public.claims / public.conflicts:
--
--   * public.claims and public.conflicts are already part of a browser
--     contract.  The API writes a claim or a conflict and immediately appends
--     a run event carrying the WHOLE row (backend/main.py), and the browser
--     reconstructs both from public.run_events.  A support link or a verdict
--     stored there would become browser payload the moment it was written.
--     Verdict support is verifier-INTERNAL provenance and belongs in a
--     service-only relation with RLS and zero policies, exactly like
--     public.claims and public.source_evidence_fragments.
--   * A conflict decision must not overwrite the conflict.  Keeping the
--     decision in its own append-only relation is what lets a losing claim,
--     its evidence and the original contradiction all survive the resolution.
--
-- Every new relation is append-only (an update/delete trigger refuses any
-- rewrite, like public.source_evidence_fragments and public.run_usage_ledger),
-- is written ONLY through a lease-guarded RPC, repeats the backend's own
-- deterministic bounds as SQL constraints so a direct RPC call cannot bypass
-- them, and re-asserts its service-only ACL rather than assuming it.
--
-- Rerun-safe: every column, table, index, function, trigger and constraint is
-- created or replaced idempotently, and the constraints are dropped and
-- re-added by name so a database that applied an earlier draft converges on
-- the current definition.

-- ---------------------------------------------------------------------------
-- 1. The closed identity dimensions a structured fact stated.
-- ---------------------------------------------------------------------------
--
-- "Make + commercial model + an overlapping year" does not identify a variant:
-- a different generation, engine, transmission, official/model code, trim,
-- drivetrain or body style is a different thing.  R4 compares a claim to a
-- source fact on the COMPLETE identity, so the dimensions the record stated
-- have to be durable.  The vocabulary is closed and mirrors
-- backend/engines/swarm_v2/evidence_bounds.py IDENTITY_DIMENSIONS;
-- tests/test_evidence_migration_static.py pins the two definitions together.
alter table public.claims add column if not exists identity_scope jsonb;

create or replace function public.r4_identity_scope_valid(p_identity jsonb)
returns boolean
language sql
immutable
set search_path = pg_catalog
as $$
  -- Strictly boolean, never NULL: a CHECK constraint treats NULL as passing,
  -- so a helper that could yield NULL would admit the rows it exists to refuse.
  -- NULL input is the pre-R4 default and is allowed by the constraint itself.
  select coalesce(
    jsonb_typeof(p_identity) = 'object'
    and not exists (
      select 1 from jsonb_each(p_identity) as entry(key, value)
      where entry.key not in ('body_style', 'drivetrain', 'engine', 'generation',
                              'model_code', 'transmission', 'trim')
         or jsonb_typeof(entry.value) <> 'string'
         or char_length(entry.value #>> '{}') not between 1 and 120
    ), false)
$$;

alter table public.claims drop constraint if exists claims_identity_scope_closed;
alter table public.claims add constraint claims_identity_scope_closed check (
  identity_scope is null or public.r4_identity_scope_valid(identity_scope));

-- The claim RPC is replaced so the new column has exactly one write path.
-- EVERY existing guarantee of 20260902000100 is carried forward verbatim -- the
-- worker lease assertion, the unsafe-payload rejection, the required
-- evidence/task keys, the trusted canonical scope identity and its one
-- sanctioned pre-canonical upgrade-on-replay, the locator/version/unit rules,
-- the same-run source binding, the focused-evidence backing of a located fact,
-- the atomic source_claim_links insert and the replay invariants -- and R4 adds
-- only the optional identity scope and its own replay invariant.
create or replace function public.create_claim_with_source_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_claim jsonb
) returns setof public.claims
language plpgsql
set search_path = pg_catalog
as $$
declare v_row public.claims; v_source public.sources; v_locator text; v_identity jsonb;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_claim::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_claim::text) like '%secret sentinel%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_claim->>'evidence_key', '') is null or nullif(p_claim->>'task_key', '') is null then
    raise exception 'invalid claim: evidence_key and task_key are required' using errcode = '22023';
  end if;
  if coalesce(p_claim->>'canonical_scope_hash', '') !~ '^[0-9a-f]{64}$'
     or coalesce(p_claim->>'scope_normalization_version', '') !~ '^[1-9][0-9]{0,3}$' then
    raise exception 'invalid claim: trusted canonical scope identity is required' using errcode = '22023';
  end if;
  -- R4: the closed identity dimensions the record stated.  Optional, so a
  -- pre-R4 claim carries NULL and is never retro-invalidated; present, it must
  -- satisfy exactly the closed vocabulary and bounds the backend applies.
  v_identity := p_claim->'identity_scope';
  if v_identity is not null and jsonb_typeof(v_identity) = 'null' then
    v_identity := null;
  end if;
  if v_identity is not null and not public.r4_identity_scope_valid(v_identity) then
    raise exception 'invalid claim: identity scope is not a bounded closed dimension set' using errcode = '22023';
  end if;
  v_locator := nullif(p_claim->>'evidence_locator', '');
  if v_locator is not null then
    if char_length(v_locator) > 800 then
      raise exception 'invalid claim: evidence locator exceeds the durable bound' using errcode = '22023';
    end if;
    -- The locator must be the canonical rendering of one of the two closed
    -- locator shapes; an arbitrary string is not a location.
    if public.r3_canonical_locator(v_locator) is null then
      raise exception 'invalid claim: evidence locator is not a canonical bounded location' using errcode = '22023';
    end if;
    -- An R3-qualified fact states its unit.  "1798" is not a fact until it
    -- says cc.  Gated on the locator so no legacy unit-less claim is ever
    -- retro-invalidated; R3 carries the unit and never interprets it.
    if jsonb_typeof(p_claim->'value') = 'number' and nullif(p_claim->>'unit', '') is null then
      raise exception 'invalid claim: a numeric located fact requires an explicit unit' using errcode = '22023';
    end if;
  end if;
  select * into v_source from public.sources
    where id = (p_claim->>'source_id')::uuid and run_id = p_run_id for key share;
  if v_source.id is null then
    raise exception 'invalid claim source' using errcode = '23503';
  end if;
  if v_locator is not null and v_source.source_version_kind is null then
    -- R3 evidence may only rest on a source whose version was captured.
    raise exception 'invalid claim: a located fact requires a versioned source' using errcode = '22023';
  end if;
  -- A located fact is only a fact because focused evidence at that exact
  -- location supports it: the locator must already be a fragment locator of
  -- THIS durable source in THIS run.  A locator that exists only on another
  -- source, only in another run, or nowhere at all cannot be attached here.
  if v_locator is not null and not exists (
       select 1 from public.source_evidence_fragments f
        where f.run_id = p_run_id and f.source_id = v_source.id and f.locator_key = v_locator) then
    raise exception 'invalid claim: a located fact must be backed by focused evidence of its own source' using errcode = '22023';
  end if;
  insert into public.claims
    (run_id, entity_key, field_key, value, unit, time_scope, geography, market,
     source_id, source_strength, confidence, agent, status, evidence_key, task_key,
     canonical_scope_hash, scope_normalization_version, evidence_locator, identity_scope)
  values (p_run_id, p_claim->>'entity_key', p_claim->>'field_key', p_claim->'value',
    p_claim->>'unit', coalesce(p_claim->'time_scope', '{}'::jsonb), p_claim->>'geography',
    p_claim->>'market', v_source.id, p_claim->>'source_strength',
    (p_claim->>'confidence')::numeric, p_claim->>'agent',
    coalesce(p_claim->>'status', 'active'), p_claim->>'evidence_key', p_claim->>'task_key',
    p_claim->>'canonical_scope_hash', (p_claim->>'scope_normalization_version')::integer,
    v_locator, v_identity)
  on conflict (run_id, evidence_key) where evidence_key is not null do nothing
  returning * into v_row;
  if v_row is null then
    select * into v_row from public.claims
      where run_id = p_run_id and evidence_key = p_claim->>'evidence_key'
      for update;
    if v_row.source_id <> v_source.id then
      raise exception 'idempotency key belongs to a different source' using errcode = '22023';
    end if;
    -- The locator is part of a claim's provenance, so a replay may never
    -- move a stored fact to a different record or field.
    if v_row.evidence_locator is distinct from v_locator then
      raise exception 'claim evidence locator mismatch' using errcode = '22023';
    end if;
    -- R4: so is the identity.  A replay may never re-attribute a stored fact
    -- to a different generation, engine, transmission or official code.
    if v_row.identity_scope is distinct from v_identity then
      raise exception 'claim identity scope mismatch' using errcode = '22023';
    end if;
    if v_row.canonical_scope_hash is not null and v_row.scope_normalization_version is not null then
      if v_row.canonical_scope_hash is distinct from p_claim->>'canonical_scope_hash'
         or v_row.scope_normalization_version is distinct from (p_claim->>'scope_normalization_version')::integer then
        raise exception 'claim canonical scope identity mismatch' using errcode = '22023';
      end if;
    elsif v_row.canonical_scope_hash is null and v_row.scope_normalization_version is null then
      if v_row.entity_key is distinct from p_claim->>'entity_key'
         or v_row.field_key is distinct from p_claim->>'field_key'
         or v_row.value is distinct from p_claim->'value'
         or v_row.unit is distinct from p_claim->>'unit'
         or v_row.time_scope is distinct from coalesce(p_claim->'time_scope', '{}'::jsonb)
         or v_row.geography is distinct from p_claim->>'geography'
         or v_row.market is distinct from p_claim->>'market'
         or v_row.source_strength is distinct from p_claim->>'source_strength'
         or v_row.confidence is distinct from (p_claim->>'confidence')::numeric
         or v_row.agent is distinct from p_claim->>'agent'
         or v_row.status is distinct from coalesce(p_claim->>'status', 'active')
         or v_row.task_key is distinct from p_claim->>'task_key' then
        raise exception 'idempotent claim replay does not match the stored claim' using errcode = '22023';
      end if;
      update public.claims
        set canonical_scope_hash = p_claim->>'canonical_scope_hash',
            scope_normalization_version = (p_claim->>'scope_normalization_version')::integer
        where id = v_row.id
        returning * into v_row;
    else
      raise exception 'claim canonical scope state is invalid' using errcode = '22023';
    end if;
  end if;
  insert into public.source_claim_links(source_id, claim_id)
    values (v_source.id, v_row.id) on conflict do nothing;
  return next v_row;
end;
$$;


-- ---------------------------------------------------------------------------
-- 2. Durable verdicts and the exact evidence behind them.
-- ---------------------------------------------------------------------------

create table if not exists public.claim_verdicts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  claim_id uuid not null references public.claims(id) on delete restrict,
  evidence_key text not null,
  verdict text not null,
  -- A bounded, backend-owned reason CODE. Never model prose: the backend picks
  -- it from a static allowlist, so an unbounded explanation, a quoted source
  -- fragment or a chain of thought cannot be stored here.
  reason text not null,
  -- HOW the verdict was reached, from the closed server-owned vocabulary.
  verification_mode text not null,
  -- The bounded identifier of the verification contract it was decided under.
  verifier_contract_version text not null,
  created_at timestamptz not null default now(),
  constraint claim_verdicts_verdict_allowlisted
    check (verdict in ('verified', 'needs_review', 'rejected')),
  constraint claim_verdicts_mode_allowlisted
    check (verification_mode in ('deterministic_local', 'deterministic_structured',
                                 'grounded_model')),
  constraint claim_verdicts_reason_bounded
    check (char_length(reason) between 1 and 500),
  constraint claim_verdicts_contract_bounded
    check (char_length(verifier_contract_version) between 1 and 120)
);

-- Retry/resume identity: one logical verdict per run. The backend derives
-- evidence_key from the verdict's own content, so an exact replay -- a resumed
-- run, a re-verification after the one bounded correction round, a retried
-- batch -- collapses onto this row instead of appending a second one.
create unique index if not exists claim_verdicts_run_evidence_uidx
  on public.claim_verdicts(run_id, evidence_key);
create index if not exists claim_verdicts_claim_idx
  on public.claim_verdicts(run_id, claim_id, created_at);

create table if not exists public.claim_verdict_supports (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  verdict_id uuid not null references public.claim_verdicts(id) on delete cascade,
  -- The durable evidence row itself. A real foreign key, so a forged support
  -- link cannot name a fragment that does not exist.
  fragment_id uuid not null references public.source_evidence_fragments(id) on delete restrict,
  content_hash text not null,
  locator_key text,
  created_at timestamptz not null default now(),
  constraint claim_verdict_supports_hash_shape
    check (content_hash ~ '^[0-9a-f]{64}$')
);

-- One logical support link per verdict: a replayed verdict re-links the same
-- evidence instead of accumulating duplicates.
create unique index if not exists claim_verdict_supports_unique_uidx
  on public.claim_verdict_supports(verdict_id, fragment_id);
create index if not exists claim_verdict_supports_run_idx
  on public.claim_verdict_supports(run_id, verdict_id);

-- ---------------------------------------------------------------------------
-- 3. Typed, append-only conflict resolutions.
-- ---------------------------------------------------------------------------

create table if not exists public.conflict_resolutions (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  evidence_key text not null,
  -- The bounded identity of the contradicting scope: the canonical scope plus
  -- the closed identity dimensions, hashed by the backend.
  scope_hash text not null,
  entity_key text not null,
  field_key text not null,
  state text not null,
  reason text not null,
  policy_version text not null,
  claim_ids uuid[] not null,
  -- NULL exactly when the conflict stayed open. A resolved conflict names one
  -- winner and supersedes every losing claim; nothing is ever deleted.
  winning_claim_id uuid references public.claims(id) on delete restrict,
  superseded_claim_ids uuid[] not null default '{}'::uuid[],
  created_at timestamptz not null default now(),
  constraint conflict_resolutions_state_allowlisted
    check (state in ('unresolved', 'resolved')),
  constraint conflict_resolutions_reason_allowlisted
    check (reason in ('R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE',
                      'R4_CONFLICT_UNRESOLVED_NO_DECISIVE_SOURCE',
                      'R4_CONFLICT_UNRESOLVED_AMBIGUOUS')),
  constraint conflict_resolutions_scope_shape
    check (scope_hash ~ '^[0-9a-f]{64}$'),
  constraint conflict_resolutions_policy_bounded
    check (char_length(policy_version) between 1 and 64),
  constraint conflict_resolutions_claims_bounded
    check (cardinality(claim_ids) between 2 and 100),
  -- A resolved conflict has exactly one winner and supersedes at least one
  -- losing claim (it exists because two different values were stated); an
  -- unresolved one has no winner and supersedes nothing. Neither ever removes
  -- a claim: a claim stating the winning value corroborates the decision and
  -- is neither the winner nor superseded.
  constraint conflict_resolutions_winner_pairing
    check ((state = 'resolved') = (winning_claim_id is not null)),
  constraint conflict_resolutions_superseded_pairing
    check ((state = 'resolved') = (cardinality(superseded_claim_ids) > 0))
);

create unique index if not exists conflict_resolutions_run_evidence_uidx
  on public.conflict_resolutions(run_id, evidence_key);
create index if not exists conflict_resolutions_scope_idx
  on public.conflict_resolutions(run_id, scope_hash, created_at);

-- ---------------------------------------------------------------------------
-- 4. Append-only, service-only posture for all three relations.
-- ---------------------------------------------------------------------------

alter table public.claim_verdicts enable row level security;
alter table public.claim_verdict_supports enable row level security;
alter table public.conflict_resolutions enable row level security;
-- No policies: browser roles (PUBLIC / anon / authenticated) have no access at
-- all. Only the trusted service path reads and appends.

-- A recorded verdict, its support and a conflict decision are audit records:
-- once durable they can never be rewritten or silently removed, by any role,
-- exactly like public.source_evidence_fragments (20260828000200) and
-- public.run_usage_ledger (013).
create or replace function public.forbid_verification_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'verification records are append-only';
end;
$$;

drop trigger if exists claim_verdicts_append_only on public.claim_verdicts;
create trigger claim_verdicts_append_only
  before update or delete on public.claim_verdicts
  for each row execute function public.forbid_verification_mutation();

drop trigger if exists claim_verdict_supports_append_only on public.claim_verdict_supports;
create trigger claim_verdict_supports_append_only
  before update or delete on public.claim_verdict_supports
  for each row execute function public.forbid_verification_mutation();

drop trigger if exists conflict_resolutions_append_only on public.conflict_resolutions;
create trigger conflict_resolutions_append_only
  before update or delete on public.conflict_resolutions
  for each row execute function public.forbid_verification_mutation();

-- ---------------------------------------------------------------------------
-- 5. The only write path: lease-guarded, fail-closed, idempotent.
-- ---------------------------------------------------------------------------

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
  v_count integer;
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

  insert into public.claim_verdicts
    (run_id, claim_id, evidence_key, verdict, reason, verification_mode,
     verifier_contract_version)
  values (p_run_id, v_claim.id, v_key, v_verdict, v_reason, v_mode, v_contract)
  on conflict (run_id, evidence_key) do nothing
  returning * into v_row;

  if v_row.id is null then
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
  end if;

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
    insert into public.claim_verdict_supports
      (run_id, verdict_id, fragment_id, content_hash, locator_key)
    values (p_run_id, v_row.id, v_fragment.id, v_fragment.content_hash,
            v_fragment.locator_key)
    on conflict (verdict_id, fragment_id) do nothing;
  end loop;

  -- The stored link set must be EXACTLY what was cited: a replay that dropped
  -- or added evidence is a contract failure, not a partial success.
  select count(*) into v_count from public.claim_verdict_supports
    where verdict_id = v_row.id;
  if v_count <> jsonb_array_length(v_support) then
    raise exception 'verdict support links do not match the cited evidence' using errcode = '22023';
  end if;
  return next v_row;
end;
$$;

create or replace function public.record_conflict_resolution_guarded(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_resolution jsonb
) returns setof public.conflict_resolutions
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_row public.conflict_resolutions;
  v_key text; v_state text; v_winner uuid;
  v_claims uuid[]; v_superseded uuid[]; v_present integer;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_resolution::text ~* '"(chain_of_thought|provider_detail|raw_error|api_key|secret|password|authorization|credentials|exception|lease_token|token)"[[:space:]]*:'
     or lower(p_resolution::text) like '%secret sentinel%'
     or lower(p_resolution::text) like '%chain of thought%' then
    raise exception 'unsafe evidence payload rejected' using errcode = '22023';
  end if;
  if nullif(p_resolution->>'evidence_key', '') is null then
    raise exception 'invalid conflict resolution: evidence_key is required' using errcode = '22023';
  end if;
  v_key := p_resolution->>'evidence_key';
  v_state := p_resolution->>'state';
  v_winner := nullif(p_resolution->>'winning_claim_id', '')::uuid;
  select array_agg(value::uuid order by value::text) into v_claims
    from jsonb_array_elements_text(p_resolution->'claim_ids') value;
  select coalesce(array_agg(value::uuid order by value::text), '{}'::uuid[])
    into v_superseded
    from jsonb_array_elements_text(coalesce(p_resolution->'superseded_claim_ids',
                                            '[]'::jsonb)) value;
  if coalesce(cardinality(v_claims), 0) < 2 then
    raise exception 'a conflict resolution decides at least two claims' using errcode = '22023';
  end if;
  -- Every claim it decides must exist, in THIS run. A resolution can never
  -- name a claim from another run, and it never removes one from this one.
  select count(*) into v_present from public.claims
    where run_id = p_run_id and id = any(v_claims);
  if v_present <> cardinality(v_claims) then
    raise exception 'a conflict resolution must decide claims of this run' using errcode = '23503';
  end if;
  if v_winner is not null and not (v_winner = any(v_claims)) then
    raise exception 'the winning claim must belong to the conflict' using errcode = '22023';
  end if;
  if v_state = 'resolved' then
    -- A closed conflict must actually close something: it supersedes at least
    -- one losing claim, every superseded claim belongs to it, and the winner
    -- is never among them. A claim that already stated the winning value
    -- corroborates the decision and is deliberately not superseded.
    if not (v_superseded <@ v_claims) or v_winner = any(v_superseded)
       or cardinality(v_superseded) = 0 then
      raise exception 'a resolved conflict supersedes at least one losing claim' using errcode = '22023';
    end if;
  end if;

  insert into public.conflict_resolutions
    (run_id, evidence_key, scope_hash, entity_key, field_key, state, reason,
     policy_version, claim_ids, winning_claim_id, superseded_claim_ids)
  values (p_run_id, v_key, p_resolution->>'scope_hash', p_resolution->>'entity',
          p_resolution->>'field', v_state, p_resolution->>'reason',
          p_resolution->>'policy_version', v_claims, v_winner, v_superseded)
  on conflict (run_id, evidence_key) do nothing
  returning * into v_row;

  if v_row.id is null then
    select * into v_row from public.conflict_resolutions
      where run_id = p_run_id and evidence_key = v_key;
    -- ONE replay invariant: the same decision, or a caller bug.
    if v_row.scope_hash is distinct from (p_resolution->>'scope_hash')
       or v_row.state is distinct from v_state
       or v_row.reason is distinct from (p_resolution->>'reason')
       or v_row.winning_claim_id is distinct from v_winner
       or v_row.claim_ids is distinct from v_claims then
      raise exception 'conflict resolution idempotency conflict' using errcode = '22023';
    end if;
  end if;
  return next v_row;
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Service-path-only ACL, asserted rather than assumed.
-- ---------------------------------------------------------------------------
--
-- The table grants are explicit rather than relying on a point-in-time
-- `grant ... on all tables` or on default privileges (see migrations
-- 20260818000100/20260818000200 for why that class of gap must be closed in
-- the migration that introduces the object).
do $$
declare fn text; tbl text;
begin
  foreach fn in array array[
    'public.record_claim_verdict_guarded(uuid,text,integer,text,jsonb)',
    'public.record_conflict_resolution_guarded(uuid,text,integer,text,jsonb)',
    'public.create_claim_with_source_guarded(uuid,text,integer,text,jsonb)',
    'public.r4_identity_scope_valid(jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;
  foreach tbl in array array[
    'public.claim_verdicts', 'public.claim_verdict_supports',
    'public.conflict_resolutions'
  ] loop
    execute format('revoke all on table %s from public', tbl);
    if exists (select 1 from pg_roles where rolname='anon') then
      execute format('revoke all on table %s from anon', tbl);
    end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then
      execute format('revoke all on table %s from authenticated', tbl);
    end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      -- Read and append only: the append-only trigger already blocks rewrites,
      -- and the service path has no reason to hold UPDATE/DELETE either.
      execute format('grant select, insert on table %s to service_role', tbl);
      execute format('revoke update, delete on table %s from service_role', tbl);
    end if;
  end loop;
end $$;
