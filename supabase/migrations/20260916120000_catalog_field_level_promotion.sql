-- Catalog PR3, part 2: FIELD-LEVEL, append-only canonical promotion.
--
-- What Catalog PR1's corrective round required, verbatim
-- ------------------------------------------------------
--
-- `20260915120000_catalog_integrity_corrections.sql` §6 left the canonical
-- tables immutable outright and wrote down exactly what must exist before an
-- insert is ever enabled:
--
--   "`catalog_model_variants` carries ONE `promoted_from_candidate_id` and ONE
--    `promoted_from_verdict_id` for a row of several independent facts [...]
--    Row-level provenance cannot verify a multi-field row: a verdict that
--    confirmed the drivetrain says nothing about the model year beside it.
--    [...] PR3 MUST add FIELD-LEVEL, append-only revision provenance -- one
--    provenance row per fact, not one per canonical row -- before any insert is
--    enabled; a row-level FK is not sufficient and must not be treated as if it
--    were."
--
-- This migration is that, and it enables the insert in the only shape that is
-- safe: `service_role` gains INSERT on the two canonical relations and NOTHING
-- else -- no UPDATE, no DELETE, and the rows stay immutable by trigger -- while
-- two triggers make a canonical row without complete verified field provenance
-- IMPOSSIBLE TO COMMIT, whichever path wrote it.
--
-- Why triggers rather than "the RPC is the only way"
-- --------------------------------------------------
--
-- `service_role` holds direct DML on these relations, and the guarded RPCs are
-- SECURITY INVOKER, so "the repository goes through the RPC" is true and is not
-- an enforcement. The corrective round wrote that rule down: anything that must
-- hold for EVERY writer is a constraint or a trigger. So:
--
--   * a provenance row is validated against its whole support chain BEFORE it
--     is stored (`catalog_check_field_provenance`, BEFORE INSERT). A forged
--     row, a row citing an unverified verdict, a row citing the legacy
--     catalog, a row whose claim states a different field or a different value,
--     and a row whose claim sits in an unresolved conflict are all refused for
--     a direct INSERT exactly as they are through the RPC;
--   * a canonical variant's stated fields must ALL be covered by revision-1
--     provenance at COMMIT (`catalog_require_field_provenance`, a DEFERRED
--     constraint trigger), so the two are created in one transaction or neither
--     survives it;
--   * a canonical model must carry at least one canonical variant at COMMIT,
--     so a bare model row is not a shape this schema has.
--
-- Which relation states "the current canonical value"
-- ---------------------------------------------------
--
-- ONE answer, and it is the view. `public.catalog_canonical_variant_current` is
-- assembled from the highest revision of each promoted field, so a later, better
-- source revises a fact by APPENDING and no canonical row is ever rewritten.
--
-- `public.catalog_model_variants` is the canonical IDENTITY and is frozen at
-- REVISION 1 by construction: its columns are what the promotion that created
-- it stated, they can never be updated (PR1 corrective §6 stands unchanged),
-- and a revision-1 provenance row must equal them exactly. So the two are not
-- two definitions of the same thing -- the table says WHICH vehicle and what
-- was first established, the view says what is currently believed -- and where
-- they overlap a constraint keeps them identical.
--
-- The identity columns cannot drift from the view either: `model_year_start`,
-- `model_year_end`, `official_model_code` and `trim` are part of the canonical
-- variant KEY, so a revision of one of them is a DIFFERENT variant by
-- construction, and the provenance trigger additionally refuses a later
-- revision of an identity field whose value is not the column's. Only
-- `identity_dimensions` is revisable in place, and for it the view is the only
-- place a reader should look.
--
-- IDENTITY versus REVISABLE FACT, stated once for the whole schema
-- ----------------------------------------------------------------
--
-- A canonical variant IS: its model, its model year range, its official model
-- code and its trim. Four places say so and they say the same thing --
-- `canonical_variant_key` in `backend/catalog/keys.py` derives the key from
-- exactly those four, `catalog_canonical_identity_field()` freezes exactly
-- those four, `catalog_model_variants_natural_uidx` is unique on exactly those
-- four, and `promote_catalog_variant_guarded` refuses a promotion whose
-- identity is not its candidate's.
--
-- `identity_dimensions` is a FACT ABOUT the variant, not part of who it is. A
-- better source may revise the fuel type, and that appends a provenance
-- revision to this same variant rather than naming a second vehicle. The
-- COLUMN on `catalog_model_variants` is what revision 1 established and never
-- changes, exactly like the other columns; `catalog_canonical_variant_current`
-- is where the CURRENT dimensions are read.

-- ---------------------------------------------------------------------------
-- 1. The closed vocabulary of promotable fields, as a database function.
-- ---------------------------------------------------------------------------
--
-- Mirrors `CANONICAL_VARIANT_FIELDS` / `stated_canonical_fields` in
-- `backend/catalog/contracts.py`, pinned against it by
-- `tests/test_catalog_migration_static.py`. An IMMUTABLE pure function of its
-- arguments: it reads no table and depends on no session state.
create or replace function public.catalog_canonical_stated_fields(
  p_model_year_start integer, p_model_year_end integer,
  p_official_model_code text, p_trim text, p_identity_dimensions jsonb
) returns jsonb
language sql immutable
set search_path = pg_catalog
as $$
  select coalesce(jsonb_object_agg(entry.field_key, entry.field_value), '{}'::jsonb)
    from (
      select 'model_year_start'::text as field_key, to_jsonb(p_model_year_start) as field_value
      union all
      select 'model_year_end', to_jsonb(p_model_year_end)
      union all
      select 'official_model_code', to_jsonb(p_official_model_code)
       where p_official_model_code is not null
      union all
      select 'trim', to_jsonb(p_trim) where p_trim is not null
      union all
      select 'identity_dimensions.' || d.key, d.value
        from jsonb_each(coalesce(p_identity_dimensions, '{}'::jsonb)) as d(key, value)
    ) as entry
$$;

-- Whether one field key is promotable at all. Closed in the same two halves as
-- the Python vocabulary: the four variant columns, plus one namespaced entry
-- per dimension of the closed `CANDIDATE_IDENTITY_DIMENSIONS` tuple.
create or replace function public.catalog_promotable_field(p_field_key text)
returns boolean
language sql immutable
set search_path = pg_catalog
as $$
  select coalesce(
    p_field_key in ('model_year_start', 'model_year_end', 'official_model_code', 'trim')
    or (p_field_key like 'identity_dimensions.%'
        and substring(p_field_key from 21) in
            ('body_style', 'drivetrain', 'engine_code', 'fuel_type',
             'generation', 'market', 'propulsion_technology', 'transmission')),
    false)
$$;

-- The four fields that are part of the canonical variant KEY. A revision may
-- never restate one of them differently, because a different value is a
-- different variant.
create or replace function public.catalog_canonical_identity_field(p_field_key text)
returns boolean
language sql immutable
set search_path = pg_catalog
as $$
  select coalesce(p_field_key in ('model_year_start', 'model_year_end',
                                  'official_model_code', 'trim'), false)
$$;

-- The catalog's durable LOCATOR RECORD IDENTITY: which captured row a focused
-- evidence locator points at. A locator's record component is the SNAPSHOT KEY
-- and the upstream row id together, because the register reuses its `_id`
-- number space across captures -- so the bare upstream id would name "row
-- 36451" of no particular retrieval, and two different vehicles from two
-- snapshots would share one durable locator.
--
-- Mirrored by `government_record_id()` in
-- `backend/catalog/government/evidence.py`; the two spellings are pinned
-- together by `tests/test_catalog_migration_static.py`. This is a CATALOG
-- convention, not a Government one: any future source family that captures
-- rows into `catalog_raw_records` locates them the same way.
create or replace function public.catalog_record_locator_id(
  p_snapshot_key text, p_upstream_record_id text
) returns text
language sql immutable
set search_path = pg_catalog
as $$
  select case when p_snapshot_key is null or p_upstream_record_id is null
              then null else p_snapshot_key || ':' || p_upstream_record_id end
$$;

-- The ENTITY a claim about one canonical vehicle is filed under: the canonical
-- model's own key, at one model year. Mirrored by `government_entity_key()` in
-- `backend/catalog/government/evidence.py`.
--
-- Using the MODEL KEY rather than the source's row id is what lets a
-- Government claim and a future Web claim about the same car meet, conflict
-- and be resolved -- and it is what makes "this evidence is about this
-- canonical row" checkable here without re-deriving a digest, because the
-- model key is a column this schema already stores.
create or replace function public.catalog_claim_entity_key(
  p_model_canonical_key text, p_model_year integer
) returns text
language sql immutable
set search_path = pg_catalog
as $$
  select case when p_model_canonical_key is null or p_model_year is null
              then null else p_model_canonical_key || ':' || p_model_year::text end
$$;

-- The R4 scope normalization, mirrored for the ONE comparison below that needs
-- it: `claims.identity_scope` is stored NORMALIZED (see `normalize_identity` in
-- `backend/engines/swarm_v2/comparison.py`), so comparing it to a candidate's
-- raw text would reject every value that merely differs in case or separator.
--
-- Unicode NFKC, case folding, `_` and `-` unified to a space, whitespace runs
-- collapsed and trimmed -- the same four steps, in the same order, as
-- `_normalize_text` in `backend/engines/swarm_v2/normalization.py`, pinned
-- against it over a real vocabulary by `tests/test_migrations_postgres.py`.
--
-- `lower()` is PostgreSQL's nearest equivalent of Python's `casefold()`. They
-- differ only for characters the closed identity vocabulary does not contain
-- (eszett, the final sigma, a handful of ligatures NFKC has already expanded),
-- and where they could differ the consequence is a REFUSAL, never an
-- acceptance: a promoted fact is rejected for a scope mismatch that is really
-- a spelling difference, which is the safe direction to fail in.
create or replace function public.r4_normalized_scope_text(p_value text)
returns text
language sql immutable
set search_path = pg_catalog
as $$
  select case when p_value is null then null else
    btrim(regexp_replace(translate(lower(normalize(p_value, NFKC)), '_-', '  '),
                         '\s+', ' ', 'g')) end
$$;

-- ---------------------------------------------------------------------------
-- 2. The append-only field provenance relation.
-- ---------------------------------------------------------------------------
--
-- ONE row per promoted canonical FACT. Every column below is something a
-- reviewer needs in order to check the fact without trusting this code: which
-- canonical thing, which field, which value, which revision, which candidate,
-- which evidence link, which snapshot, which source, which claim, which
-- verified verdict, which run and worker lease, which source version, which
-- exact locator, and under which idempotency key.
create table if not exists public.catalog_canonical_field_provenance (
  id uuid primary key default gen_random_uuid(),
  model_id uuid not null references public.catalog_models(id) on delete restrict,
  variant_id uuid not null references public.catalog_model_variants(id) on delete restrict,
  field_key text not null,
  field_value jsonb not null,
  revision integer not null default 1,
  candidate_id uuid not null
    references public.catalog_candidate_variants(id) on delete restrict,
  evidence_link_id uuid not null
    references public.catalog_candidate_evidence_links(id) on delete restrict,
  snapshot_id uuid not null
    references public.catalog_source_snapshots(id) on delete restrict,
  source_id uuid not null references public.sources(id) on delete restrict,
  claim_id uuid not null references public.claims(id) on delete restrict,
  verdict_id uuid not null references public.claim_verdicts(id) on delete restrict,
  -- Provenance, not ownership: RESTRICT, so a run that durable canonical state
  -- depends on cannot be deleted and take the provenance with it.
  run_id uuid not null references public.runs(id) on delete restrict,
  worker_id text not null,
  attempt integer not null,
  source_version text not null,
  source_version_kind text not null,
  record_locator text not null,
  -- The SCOPE the cited claim stated, derived from it and stored here so a
  -- reviewer can read WHICH vehicle, WHICH model year, WHICH market and WHICH
  -- identity a canonical fact was established under without joining four
  -- relations -- and so the trigger below can hold every later fact about this
  -- variant to the same scope with a plain comparison.
  entity_key text not null,
  market text not null,
  geography text not null,
  time_scope jsonb not null,
  identity_scope jsonb not null,
  promotion_key text not null,
  created_at timestamptz not null default now(),
  constraint catalog_canonical_field_provenance_field_allowlisted
    check (public.catalog_promotable_field(field_key)),
  constraint catalog_canonical_field_provenance_revision_positive
    check (revision >= 1),
  constraint catalog_canonical_field_provenance_attempt_positive
    check (attempt >= 1),
  constraint catalog_canonical_field_provenance_worker_bounded
    check (char_length(worker_id) between 1 and 200),
  constraint catalog_canonical_field_provenance_value_bounded
    check (char_length(field_value::text) between 1 and 2048),
  constraint catalog_canonical_field_provenance_locator_canonical
    check (public.r3_canonical_locator(record_locator) is not null),
  constraint catalog_canonical_field_provenance_version_valid
    check (public.r3_source_version_valid(source_version_kind, source_version)),
  constraint catalog_canonical_field_provenance_key_derived
    check (promotion_key ~ '^cp1\.[0-9a-f]{32}$'),
  constraint catalog_canonical_field_provenance_entity_bounded
    check (char_length(entity_key) between 1 and 200),
  constraint catalog_canonical_field_provenance_market_bounded
    check (char_length(market) between 1 and 120
           and char_length(geography) between 1 and 120),
  constraint catalog_canonical_field_provenance_scope_bounded
    check (jsonb_typeof(time_scope) = 'object'
           and jsonb_typeof(identity_scope) = 'object'
           and char_length(time_scope::text) <= 512
           and char_length(identity_scope::text) <= 1024),
  constraint catalog_canonical_field_provenance_identity_scope_closed
    check (public.r4_identity_scope_valid(identity_scope))
);

-- One value per field per revision: a revision is a POSITION in a field's
-- history, so two rows claiming the same position are two different histories.
create unique index if not exists catalog_canonical_field_provenance_revision_uidx
  on public.catalog_canonical_field_provenance(variant_id, field_key, revision);
-- One promotion writes each field at most once. A replayed key presenting a
-- second value for a field it already promoted is a conflict, not a retry.
create unique index if not exists catalog_canonical_field_provenance_promotion_uidx
  on public.catalog_canonical_field_provenance(promotion_key, field_key);

-- Every referencing column gets an index: a foreign key without one makes the
-- referenced side's delete-check a sequential scan.
create index if not exists catalog_canonical_field_provenance_model_idx
  on public.catalog_canonical_field_provenance(model_id);
create index if not exists catalog_canonical_field_provenance_variant_idx
  on public.catalog_canonical_field_provenance(variant_id, field_key, revision desc);
create index if not exists catalog_canonical_field_provenance_candidate_idx
  on public.catalog_canonical_field_provenance(candidate_id);
create index if not exists catalog_canonical_field_provenance_link_idx
  on public.catalog_canonical_field_provenance(evidence_link_id);
create index if not exists catalog_canonical_field_provenance_snapshot_idx
  on public.catalog_canonical_field_provenance(snapshot_id);
create index if not exists catalog_canonical_field_provenance_source_idx
  on public.catalog_canonical_field_provenance(source_id);
create index if not exists catalog_canonical_field_provenance_claim_idx
  on public.catalog_canonical_field_provenance(claim_id);
create index if not exists catalog_canonical_field_provenance_verdict_idx
  on public.catalog_canonical_field_provenance(verdict_id);
create index if not exists catalog_canonical_field_provenance_run_idx
  on public.catalog_canonical_field_provenance(run_id);

alter table public.catalog_canonical_field_provenance enable row level security;
-- No policies: browser roles (PUBLIC / anon / authenticated) have no access at
-- all, exactly like every other relation in this namespace.

-- ---------------------------------------------------------------------------
-- 3. A promoted fact is checked against its WHOLE support chain, for every
--    writer, before it is stored.
-- ---------------------------------------------------------------------------
--
-- Everything the fact asserts about its own provenance is DERIVED from the
-- evidence link it cites. A caller MAY state a derived column and is then held
-- to it: stating a different source, claim, verdict, run, version or locator is
-- a factual disagreement with the evidence, not a formatting preference, so it
-- fails closed instead of being overwritten.
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
  v_expected_keys text[];
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

  select * into v_candidate from public.catalog_candidate_variants where id = new.candidate_id;
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
  select coalesce(array_agg(expected.name order by expected.name), '{}'::text[])
    into v_expected_keys
    from (
      select d.key as name
        from jsonb_object_keys(v_candidate.identity_dimensions) as d(key)
       where d.key in ('body_style', 'drivetrain', 'generation', 'transmission')
      union all
      select 'model_code' where v_candidate.official_model_code is not null
      union all
      select 'trim' where v_candidate.trim is not null
    ) as expected;
  if coalesce((select array_agg(e.name order by e.name)
                 from jsonb_object_keys(v_identity) as e(name)), '{}'::text[])
       is distinct from v_expected_keys then
    raise exception 'canonical field provenance claim is scoped to another vehicle identity'
      using errcode = '22023';
  end if;
  -- ... and every value, compared under the SAME normalization R4 stored it
  -- with, must be the candidate's own.
  if exists (
      select 1 from jsonb_each_text(v_identity) as e(name, value)
       where e.value is distinct from public.r4_normalized_scope_text(
               case e.name
                 when 'model_code' then v_candidate.official_model_code
                 when 'trim' then v_candidate.trim
                 else v_candidate.identity_dimensions->>e.name
               end)) then
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

drop trigger if exists catalog_canonical_field_provenance_checked
  on public.catalog_canonical_field_provenance;
create trigger catalog_canonical_field_provenance_checked
  before insert on public.catalog_canonical_field_provenance
  for each row execute function public.catalog_check_field_provenance();

-- Append-only outright, like every other audit record in this schema.
drop trigger if exists catalog_canonical_field_provenance_append_only
  on public.catalog_canonical_field_provenance;
create trigger catalog_canonical_field_provenance_append_only
  before update or delete on public.catalog_canonical_field_provenance
  for each row execute function public.forbid_catalog_source_mutation();

-- ---------------------------------------------------------------------------
-- 4. A canonical row cannot COMMIT without complete field provenance.
-- ---------------------------------------------------------------------------
--
-- DEFERRED, because the provenance references the variant by id and therefore
-- cannot exist before it. Deferring to COMMIT is what makes "the canonical
-- identity and every field's provenance are created together or not at all" a
-- property of the transaction rather than of the order a caller happened to
-- write in.
create or replace function public.catalog_require_field_provenance() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
declare v_stated jsonb; v_promoted jsonb;
begin
  v_stated := public.catalog_canonical_stated_fields(
    new.model_year_start, new.model_year_end, new.official_model_code,
    new.trim, new.identity_dimensions);
  select coalesce(jsonb_object_agg(p.field_key, p.field_value), '{}'::jsonb)
    into v_promoted
    from public.catalog_canonical_field_provenance p
   where p.variant_id = new.id and p.revision = 1;
  -- Both directions at once: a stated field with no provenance and a
  -- provenance row for a field the row does not state are the same defect --
  -- the row and its evidence describing different vehicles.
  if v_stated is distinct from v_promoted then
    raise exception 'a canonical catalog variant requires verified provenance for every field it states'
      using errcode = '22023';
  end if;
  -- The PR1 row-level back-pointer stays, and is now CHECKED: it must name a
  -- verdict that is actually in this row's own field provenance. It is a
  -- back-pointer into the provenance, never a substitute for it.
  if not exists (select 1 from public.catalog_canonical_field_provenance p
                  where p.variant_id = new.id
                    and p.verdict_id = new.promoted_from_verdict_id) then
    raise exception 'a canonical catalog variant must name a verdict from its own field provenance'
      using errcode = '22023';
  end if;
  if not exists (select 1 from public.catalog_canonical_field_provenance p
                  where p.variant_id = new.id
                    and p.candidate_id = new.promoted_from_candidate_id) then
    raise exception 'a canonical catalog variant must name a candidate from its own field provenance'
      using errcode = '22023';
  end if;
  return null;
end;
$$;

drop trigger if exists catalog_model_variants_require_field_provenance
  on public.catalog_model_variants;
create constraint trigger catalog_model_variants_require_field_provenance
  after insert on public.catalog_model_variants
  deferrable initially deferred
  for each row execute function public.catalog_require_field_provenance();

-- A canonical model exists because a canonical variant of it was promoted.
-- A bare model row -- a manufacturer and a name with nothing established
-- underneath -- is not a shape this schema has.
create or replace function public.catalog_require_model_variant() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  if not exists (select 1 from public.catalog_model_variants v where v.model_id = new.id) then
    raise exception 'a canonical catalog model requires at least one promoted variant'
      using errcode = '22023';
  end if;
  return null;
end;
$$;

drop trigger if exists catalog_models_require_variant on public.catalog_models;
create constraint trigger catalog_models_require_variant
  after insert on public.catalog_models
  deferrable initially deferred
  for each row execute function public.catalog_require_model_variant();

-- ---------------------------------------------------------------------------
-- 5. Derived canonical keys, and the identity-rewrite trigger, unchanged.
-- ---------------------------------------------------------------------------
--
-- The canonical keys become DERIVED identities exactly like the staging keys
-- did in the corrective round: `cm1.`/`cv1.` plus 128 bits of the domain-
-- separated digest that `backend/catalog/keys.py` builds. The tables are still
-- empty, so this is a no-op against existing data by construction.
alter table public.catalog_models
  drop constraint if exists catalog_models_key_shape,
  drop constraint if exists catalog_models_key_derived,
  add constraint catalog_models_key_derived
    check (canonical_key ~ '^cm1\.[0-9a-f]{32}$');

alter table public.catalog_model_variants
  drop constraint if exists catalog_model_variants_key_shape,
  drop constraint if exists catalog_model_variants_key_derived,
  add constraint catalog_model_variants_key_derived
    check (canonical_key ~ '^cv1\.[0-9a-f]{32}$');

-- Natural identity, so even a wrong key cannot duplicate a logical row.
alter table public.catalog_models
  drop constraint if exists catalog_models_natural_uniq,
  add constraint catalog_models_natural_uniq unique (manufacturer, commercial_model);

create unique index if not exists catalog_model_variants_natural_uidx
  on public.catalog_model_variants
  (model_id, model_year_start, model_year_end,
   coalesce(official_model_code, ''), coalesce(trim, ''));

-- ---------------------------------------------------------------------------
-- 6. The canonical READ MODEL.
-- ---------------------------------------------------------------------------
--
-- `security_invoker` so a view never reads more than its caller could: the
-- underlying relations keep RLS and their own grants, and this adds a shape,
-- not a privilege.
create or replace view public.catalog_canonical_field_current
with (security_invoker = true) as
select distinct on (p.variant_id, p.field_key)
       p.variant_id, p.model_id, p.field_key, p.field_value, p.revision,
       p.candidate_id, p.evidence_link_id, p.snapshot_id, p.source_id, p.claim_id,
       p.verdict_id, p.run_id, p.source_version, p.source_version_kind,
       p.record_locator, p.promotion_key, p.created_at
  from public.catalog_canonical_field_provenance p
 order by p.variant_id, p.field_key, p.revision desc;

create or replace view public.catalog_canonical_variant_current
with (security_invoker = true) as
with current_fields as (
  select c.variant_id,
         jsonb_object_agg(c.field_key, c.field_value) as fields,
         jsonb_object_agg(c.field_key, c.revision) as revisions,
         max(c.created_at) as revised_at
    from public.catalog_canonical_field_current c
   group by c.variant_id
)
select v.id as variant_id, v.model_id, v.canonical_key,
       m.canonical_key as model_canonical_key, m.manufacturer, m.commercial_model,
       v.promoted_from_candidate_id, v.promoted_from_verdict_id,
       (f.fields->>'model_year_start')::integer as model_year_start,
       (f.fields->>'model_year_end')::integer as model_year_end,
       f.fields->>'official_model_code' as official_model_code,
       f.fields->>'trim' as trim,
       coalesce((select jsonb_object_agg(substring(e.key from 21), e.value)
                   from jsonb_each(f.fields) as e(key, value)
                  where e.key like 'identity_dimensions.%'), '{}'::jsonb)
         as identity_dimensions,
       f.revisions as field_revisions,
       v.created_at as promoted_at,
       f.revised_at
  from public.catalog_model_variants v
  join public.catalog_models m on m.id = v.model_id
  join current_fields f on f.variant_id = v.id;

-- ---------------------------------------------------------------------------
-- 7. The ONE reviewed promotion path.
-- ---------------------------------------------------------------------------
--
-- Same posture as every other durable catalog write: the caller presents its
-- run id, worker id, attempt and lease token, and `assert_worker_lease`
-- validates all four atomically before a byte is written. Everything the
-- promotion asserts about provenance is derived from the cited evidence links
-- by the trigger above, so this function decides IDENTITY and IDEMPOTENCY and
-- delegates truth to the constraint that holds for every writer.
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

  select * into v_candidate from public.catalog_candidate_variants
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

-- ---------------------------------------------------------------------------
-- 8. Privileges: the canonical catalog becomes INSERT-able, and nothing more.
-- ---------------------------------------------------------------------------
do $$
declare fn text; canonical text;
begin
  foreach fn in array array[
    'public.catalog_canonical_stated_fields(integer,integer,text,text,jsonb)',
    'public.catalog_promotable_field(text)',
    'public.catalog_canonical_identity_field(text)',
    'public.catalog_record_locator_id(text,text)',
    'public.catalog_claim_entity_key(text,integer)',
    'public.r4_normalized_scope_text(text)',
    'public.promote_catalog_variant_guarded(uuid,text,integer,text,jsonb)'
  ] loop
    execute format('revoke execute on function %s from public', fn);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke execute on function %s from anon', fn); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke execute on function %s from authenticated', fn); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then execute format('grant execute on function %s to service_role', fn); end if;
  end loop;

  execute 'revoke all on table public.catalog_canonical_field_provenance from public';
  if exists (select 1 from pg_roles where rolname='anon') then execute 'revoke all on table public.catalog_canonical_field_provenance from anon'; end if;
  if exists (select 1 from pg_roles where rolname='authenticated') then execute 'revoke all on table public.catalog_canonical_field_provenance from authenticated'; end if;
  if exists (select 1 from pg_roles where rolname='service_role') then
    execute 'grant select, insert on table public.catalog_canonical_field_provenance to service_role';
    execute 'revoke update, delete on table public.catalog_canonical_field_provenance from service_role';
  end if;

  -- The canonical pair gains INSERT and NOTHING else. UPDATE and DELETE stay
  -- revoked and the rows stay immutable by trigger, so a promoted row can
  -- never be rewritten -- a later, better source APPENDS a revision instead.
  foreach canonical in array array[
    'public.catalog_models', 'public.catalog_model_variants'
  ] loop
    execute format('revoke all on table %s from public', canonical);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke all on table %s from anon', canonical); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke all on table %s from authenticated', canonical); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      execute format('grant select, insert on table %s to service_role', canonical);
      execute format('revoke update, delete on table %s from service_role', canonical);
    end if;
  end loop;

  foreach canonical in array array[
    'public.catalog_canonical_field_current', 'public.catalog_canonical_variant_current'
  ] loop
    execute format('revoke all on %s from public', canonical);
    if exists (select 1 from pg_roles where rolname='anon') then execute format('revoke all on %s from anon', canonical); end if;
    if exists (select 1 from pg_roles where rolname='authenticated') then execute format('revoke all on %s from authenticated', canonical); end if;
    if exists (select 1 from pg_roles where rolname='service_role') then
      -- REVOKE ALL first, then grant the one privilege. A view is a NEW object,
      -- so `alter default privileges ... grant all on tables to service_role`
      -- (migration 20260706192500) hands it every privilege the moment it is
      -- created; granting SELECT on top of that would leave INSERT, UPDATE and
      -- DELETE standing on the read model.
      execute format('revoke all on %s from service_role', canonical);
      execute format('grant select on %s to service_role', canonical);
    end if;
  end loop;
end $$;
