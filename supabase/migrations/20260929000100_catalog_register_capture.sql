-- PR-D1: the whole Government register, captured gradually into our own
-- immutable snapshots, one exact tozar at a time (owner decisions 28 and 32).
--
-- What this adds
-- --------------
--
--   catalog_register_directory_versions / catalog_register_directory_units
--                    A versioned DIRECTORY of the register: every distinct
--                    tozar with the row count the source reported, read with
--                    bounded metadata requests only (no row payload). A
--                    version is the SHA-256 of the canonical sorted
--                    (tozar, count) list plus the resource id, so refreshing
--                    an unchanged register lands on the same version.
--   catalog_register_capture_groups / catalog_register_capture_units
--                    One request row per (register_version, tozar): the
--                    idempotency of Capture. A group is one capture-job
--                    execution covering one or more units.
--   catalog_register_snapshot_archives
--                    The immutable Cloud Storage object that holds a captured
--                    snapshot's full upstream rows (gs://.../<snapshot_key>
--                    .jsonl.gz): uri, byte size, sha256. The long-term raw
--                    source for PR-L. A pruned snapshot KEEPS its archive row.
--   catalog_register_archive_lines (view)
--                    The archive line of every raw record of an archived
--                    snapshot. The object is written in capture order, so a
--                    record's line is its `source_locator.capture_index` + 1:
--                    exact, per row, and costs no storage.
--   request_register_capture()      the claim, the group cap and the
--                                   capacity guard, atomically
--   record_register_capture_trigger() the claimer's compare-and-set
--   record_register_unit_status()   the capture job's outcome per unit
--                                   (lease-guarded; measures the snapshot)
--   record_register_snapshot_archive() the archive record (lease-guarded)
--   record_register_directory()     a new directory version, only on change
--   catalog_register_prunable_snapshots() / prune_register_snapshots()
--                                   retention (O22): dry-run list, and the
--                                   digest-bound prune of DB rows only
--   catalog_register_coverage()     the REGISTER_COVERAGE gate facts
--
-- What it never does
-- ------------------
--
-- It changes no existing table, function, trigger, snapshot, content hash or
-- the coverage ledger. It reads no Government source and calls no model.
-- Additive and forward-only; rerun-safe. Every new relation is readable by the
-- read-only release role (explicit grant below, besides default privileges).

-- ---------------------------------------------------------------------------
-- 1. The register directory.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_register_directory_versions (
  id uuid primary key default gen_random_uuid(),
  resource_id text not null check (resource_id ~ '^[0-9a-f-]{36}$'),
  register_version text not null check (register_version ~ '^[0-9a-f]{64}$'),
  unit_count integer not null check (unit_count >= 0),
  total_rows bigint not null check (total_rows >= 0),
  fetched_at timestamptz not null,
  created_at timestamptz not null default now()
);
-- NOT unique: a register that reverts (A -> B -> A) records A again as the
-- newest row, so "the current version" is always the newest row.
create index if not exists catalog_register_directory_versions_version_idx
  on public.catalog_register_directory_versions (register_version);
create index if not exists catalog_register_directory_versions_created_idx
  on public.catalog_register_directory_versions (created_at desc);

create table if not exists public.catalog_register_directory_units (
  version_id uuid not null
    references public.catalog_register_directory_versions(id) on delete restrict,
  -- EXACT register spelling: never normalized (that is D3). Different
  -- spellings are different units.
  tozar text not null check (char_length(tozar) between 1 and 200),
  expected_rows integer not null check (expected_rows >= 0),
  primary key (version_id, tozar)
);

create or replace function public.forbid_catalog_register_rewrite() returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception 'CATALOG_REGISTER_IMMUTABLE: register directory and archive records are append-only'
    using errcode = '42501';
end;
$$;

drop trigger if exists catalog_register_directory_versions_append_only
  on public.catalog_register_directory_versions;
create trigger catalog_register_directory_versions_append_only
  before update or delete on public.catalog_register_directory_versions
  for each row execute function public.forbid_catalog_register_rewrite();
drop trigger if exists catalog_register_directory_units_append_only
  on public.catalog_register_directory_units;
create trigger catalog_register_directory_units_append_only
  before update or delete on public.catalog_register_directory_units
  for each row execute function public.forbid_catalog_register_rewrite();

-- The version of a (resource, units) directory. `p_units` is a JSON array of
-- {"tozar": text, "expected_rows": int}. Canonical form, byte for byte what
-- backend/catalog/government/directory.py `register_version` computes:
--   "gov.register.directory.1\n" || resource_id || "\n"
--   || for each unit, ordered by tozar in UTF-8 byte order:
--        json(tozar) || ":" || expected_rows || "\n"
create or replace function public.catalog_register_version(p_resource_id text, p_units jsonb)
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select encode(sha256(convert_to(
    'gov.register.directory.1' || E'\n' || p_resource_id || E'\n' ||
    coalesce((select string_agg(to_jsonb(u.tozar)::text || ':' || u.expected_rows::text || E'\n',
                                '' order by u.tozar collate "C")
                from jsonb_to_recordset(p_units) as u(tozar text, expected_rows bigint)), ''),
    'UTF8')), 'hex')
$$;

create or replace function public.record_register_directory(
  p_resource_id text, p_fetched_at timestamptz, p_units jsonb
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_version text;
  v_row public.catalog_register_directory_versions%rowtype;
  v_count integer;
  v_total bigint;
begin
  if p_resource_id is null or p_resource_id !~ '^[0-9a-f-]{36}$' or p_fetched_at is null
     or p_units is null or jsonb_typeof(p_units) <> 'array'
     or jsonb_array_length(p_units) > 5000 then
    raise exception 'CATALOG_REGISTER_DIRECTORY_INVALID: invalid register directory'
      using errcode = '22023';
  end if;
  if exists (select 1 from jsonb_array_elements(p_units) e
              where jsonb_typeof(e) <> 'object'
                 or jsonb_typeof(e->'tozar') <> 'string'
                 or jsonb_typeof(e->'expected_rows') <> 'number'
                 or char_length(e->>'tozar') not between 1 and 200
                 or (e->>'expected_rows') !~ '^[0-9]{1,9}$')
     or (select count(*) <> count(distinct e->>'tozar') from jsonb_array_elements(p_units) e) then
    raise exception 'CATALOG_REGISTER_DIRECTORY_INVALID: invalid register directory unit'
      using errcode = '22023';
  end if;
  v_version := public.catalog_register_version(p_resource_id, p_units);
  select count(*), coalesce(sum((e->>'expected_rows')::bigint), 0)
    into v_count, v_total from jsonb_array_elements(p_units) e;

  -- One writer at a time; a new row only when the content differs from the
  -- CURRENT (newest) version.
  perform pg_advisory_xact_lock(hashtext('public.catalog_register_directory'));
  select * into v_row from public.catalog_register_directory_versions
   order by created_at desc, id desc limit 1;
  if found and v_row.register_version = v_version then
    return jsonb_build_object('decision', 'unchanged', 'version', to_jsonb(v_row));
  end if;
  insert into public.catalog_register_directory_versions
    (resource_id, register_version, unit_count, total_rows, fetched_at)
  values (p_resource_id, v_version, v_count, v_total, p_fetched_at)
  returning * into v_row;
  insert into public.catalog_register_directory_units (version_id, tozar, expected_rows)
  select v_row.id, e->>'tozar', (e->>'expected_rows')::integer
    from jsonb_array_elements(p_units) e;
  return jsonb_build_object('decision', 'created', 'version', to_jsonb(v_row));
end;
$$;

-- ---------------------------------------------------------------------------
-- 2. Capture requests: one row per (register_version, tozar).
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_register_capture_groups (
  id uuid primary key default gen_random_uuid(),
  -- 'capture': one group of tozars of one directory version; 'directory': one
  -- directory refresh (no units, no version). One execution of the capture
  -- job each; the same trigger record and liveness rule for both.
  kind text not null default 'capture' check (kind in ('capture', 'directory')),
  register_version text check ((kind = 'capture' and register_version is not null
                                and register_version ~ '^[0-9a-f]{64}$')
                               or (kind = 'directory' and register_version is null)),
  -- An audit fact, not a foreign key (as elsewhere for requesters).
  requested_by uuid not null,
  expected_rows bigint not null check (expected_rows >= 0),
  -- The operator capture run this group captures under. Null until made.
  run_id uuid references public.runs(id) on delete restrict,
  trigger_state text not null default 'claimed'
    check (trigger_state in ('claimed', 'triggered', 'trigger_failed', 'trigger_unknown')),
  execution_name text check (execution_name is null or char_length(execution_name) <= 400),
  claimed_at timestamptz not null default now(),
  triggered_at timestamptz,
  updated_at timestamptz not null default now()
);
create index if not exists catalog_register_capture_groups_run_idx
  on public.catalog_register_capture_groups (run_id);
create index if not exists catalog_register_capture_groups_kind_idx
  on public.catalog_register_capture_groups (kind, claimed_at desc);

-- THE liveness rule of a group (a capture group or a directory refresh): it
-- is STALE -- its work will not happen, so it may be requested again and it
-- holds no capacity -- when its trigger failed, it was claimed but never got
-- a run within the grace, its run ended, its run was never claimed by a
-- worker within the grace, its run's lease expired longer ago than the
-- grace (a killed job), or its run sits in any other live status (launching,
-- waiting, cancellation_requested, a claimed queued run) with nothing --
-- lease, trigger, claim -- newer than the grace (a job killed mid-cancel).
-- Used by request_register_capture (retries and the
-- in-flight capacity), request_register_directory_refresh (one at a time)
-- and mirrored by the page (backend/catalog/register/service.py).
create or replace function public.catalog_register_group_stale(p_group_id uuid, p_grace interval)
returns boolean
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce((
    select g.trigger_state = 'trigger_failed'
        or (g.trigger_state = 'claimed' and g.run_id is null and g.claimed_at < now() - p_grace)
        or (r.id is not null and r.status in ('completed', 'partial_success', 'failed',
                                              'cancelled', 'timed_out', 'budget_exhausted'))
        or (r.id is not null and r.status = 'queued' and r.worker_id is null
            and coalesce(g.triggered_at, g.claimed_at) < now() - p_grace)
        or (r.id is not null and r.status in ('starting', 'running') and r.lease_expires_at is not null
            and r.lease_expires_at < now() - p_grace)
        or (r.id is not null
            and (r.status in ('launching', 'waiting', 'cancellation_requested')
                 or (r.status = 'queued' and r.worker_id is not null))
            and coalesce(r.lease_expires_at, g.triggered_at, g.claimed_at) < now() - p_grace)
      from public.catalog_register_capture_groups g
      left join public.runs r on r.id = g.run_id
     where g.id = p_group_id), true)
$$;

create table if not exists public.catalog_register_capture_units (
  id uuid primary key default gen_random_uuid(),
  group_id uuid not null references public.catalog_register_capture_groups(id) on delete restrict,
  register_version text not null check (register_version ~ '^[0-9a-f]{64}$'),
  tozar text not null check (char_length(tozar) between 1 and 200),
  expected_rows integer not null check (expected_rows >= 0),
  attempt integer not null default 1 check (attempt between 1 and 1000),
  status text not null default 'requested'
    check (status in ('requested', 'capturing', 'captured', 'failed')),
  failure_code text check (failure_code is null or failure_code ~ '^[A-Z][A-Z0-9_]{2,79}$'),
  -- The captured snapshot. Deliberately NOT a foreign key: retention may
  -- prune an old snapshot, and this request row is history.
  snapshot_id uuid,
  snapshot_key text check (snapshot_key is null or snapshot_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  -- The source's total for this tozar AT CAPTURE TIME, and what was stored.
  api_total integer check (api_total is null or api_total >= 0),
  captured_rows integer check (captured_rows is null or captured_rows >= 0),
  count_verified boolean,
  -- Measured storage of the snapshot's rows (see record_register_unit_status).
  measured_bytes bigint check (measured_bytes is null or measured_bytes >= 0),
  measurement_method text check (measurement_method is null or char_length(measurement_method) <= 80),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint catalog_register_capture_units_captured_has_snapshot
    check (status <> 'captured' or (snapshot_id is not null and count_verified is true))
);
create unique index if not exists catalog_register_capture_units_version_tozar_uidx
  on public.catalog_register_capture_units (register_version, tozar);
create index if not exists catalog_register_capture_units_tozar_idx
  on public.catalog_register_capture_units (tozar, updated_at desc);
create index if not exists catalog_register_capture_units_group_idx
  on public.catalog_register_capture_units (group_id);

-- ---------------------------------------------------------------------------
-- 3. Archives (D1-8): the immutable object per captured snapshot.
-- ---------------------------------------------------------------------------

create table if not exists public.catalog_register_snapshot_archives (
  id uuid primary key default gen_random_uuid(),
  -- Not a foreign key, on purpose: the archive OUTLIVES a pruned snapshot
  -- (prune deletes database rows only, never the object) and stays citable.
  snapshot_id uuid not null,
  snapshot_key text not null check (snapshot_key ~ '^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$'),
  gcs_uri text not null check (gcs_uri ~ '^gs://[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]/register/[0-9a-f-]{36}/[0-9a-f]{16}/[A-Za-z0-9._:@+-]{1,200}\.jsonl\.gz$'),
  byte_size bigint not null check (byte_size > 0),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  line_count integer not null check (line_count >= 0),
  -- How a raw record's line in the object is derived (see the view below).
  line_basis text not null default 'source_locator.capture_index+1'
    check (line_basis = 'source_locator.capture_index+1'),
  recorded_by_run_id uuid not null references public.runs(id) on delete restrict,
  created_at timestamptz not null default now()
);
create unique index if not exists catalog_register_snapshot_archives_snapshot_uidx
  on public.catalog_register_snapshot_archives (snapshot_id);
create unique index if not exists catalog_register_snapshot_archives_key_uidx
  on public.catalog_register_snapshot_archives (snapshot_key);
create index if not exists catalog_register_snapshot_archives_run_idx
  on public.catalog_register_snapshot_archives (recorded_by_run_id);

drop trigger if exists catalog_register_snapshot_archives_append_only
  on public.catalog_register_snapshot_archives;
create trigger catalog_register_snapshot_archives_append_only
  before update or delete on public.catalog_register_snapshot_archives
  for each row execute function public.forbid_catalog_register_rewrite();

-- Per raw record: the object and the line that holds it. PR-L cites a single
-- record from the archive with (gcs_uri, archive_line).
create or replace view public.catalog_register_archive_lines
with (security_invoker = true) as
  select a.snapshot_key, a.gcs_uri, r.id as raw_record_id, r.upstream_record_id,
         ((r.source_locator->>'capture_index')::integer + 1) as archive_line
    from public.catalog_register_snapshot_archives a
    join public.catalog_raw_records r on r.snapshot_id = a.snapshot_id
   where r.source_locator ? 'capture_index';

-- ---------------------------------------------------------------------------
-- 4. Capacity (D1-4).
-- ---------------------------------------------------------------------------

create or replace function public.catalog_register_database_bytes() returns bigint
language sql
stable
set search_path = pg_catalog
as $$ select pg_database_size(current_database()) $$;

-- The measured storage of one snapshot's rows: the per-row `pg_column_size`
-- sum of its raw records and its candidates (the heap size of each row
-- value, compressed where it is stored compressed). Indexes are not included
-- -- the measured bytes/row is therefore a floor, and the capacity estimate
-- (MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE) stays the planning number.
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
$$;

-- ---------------------------------------------------------------------------
-- 5. The claim: idempotent per (register_version, tozar), with the group cap
--    and the capacity guard -- all or nothing.
-- ---------------------------------------------------------------------------
--
-- Answers {decision: 'claimed', group, units[]} when at least one unit is new
-- (or retryable) and was claimed now, else {decision: 'existing', units[]}.
-- A unit is retryable when it failed, when its group's trigger failed, or
-- when its group's run ended without capturing it / never started within the
-- grace. A unit captured or in flight is returned, never started again.
create or replace function public.request_register_capture(
  p_register_version text, p_tozars text[], p_requested_by uuid,
  p_group_max_rows integer, p_capacity_limit_bytes bigint, p_bytes_per_row integer,
  p_grace_seconds integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_latest public.catalog_register_directory_versions%rowtype;
  v_tozar text;
  v_expected integer;
  v_unit public.catalog_register_capture_units%rowtype;
  v_group public.catalog_register_capture_groups%rowtype;
  v_grace interval;
  v_new text[] := '{}';
  v_new_rows bigint := 0;
  v_current bigint;
  v_projected bigint;
  v_inflight bigint;
  v_retry boolean;
begin
  if p_register_version is null or p_register_version !~ '^[0-9a-f]{64}$'
     or p_tozars is null or cardinality(p_tozars) not between 1 and 1000
     or p_requested_by is null or p_group_max_rows is null or p_group_max_rows < 1
     or p_capacity_limit_bytes is null or p_capacity_limit_bytes < 1
     or p_bytes_per_row is null or p_bytes_per_row < 1
     or p_grace_seconds is null or p_grace_seconds not between 300 and 86400
     or (select count(*) <> count(distinct t) from unnest(p_tozars) t) then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: invalid register capture request'
      using errcode = '22023';
  end if;
  v_grace := make_interval(secs => p_grace_seconds);

  -- Serialize every claim (and the directory writer) on one advisory lock --
  -- never a row lock on the append-only directory.
  perform pg_advisory_xact_lock(hashtext('public.catalog_register_directory'));
  select * into v_latest from public.catalog_register_directory_versions
   order by created_at desc, id desc limit 1;
  if not found or v_latest.register_version <> p_register_version then
    raise exception 'CATALOG_REGISTER_VERSION_STALE: that is not the current register directory version'
      using errcode = '40001';
  end if;

  foreach v_tozar in array p_tozars loop
    select u.expected_rows into v_expected from public.catalog_register_directory_units u
     where u.version_id = v_latest.id and u.tozar = v_tozar;
    if not found then
      raise exception 'CATALOG_REGISTER_UNIT_UNKNOWN: a requested tozar is not in the register directory'
        using errcode = '22023';
    end if;
    select * into v_unit from public.catalog_register_capture_units
     where register_version = p_register_version and tozar = v_tozar for update;
    if not found then
      v_new := v_new || v_tozar;
      v_new_rows := v_new_rows + v_expected;
      continue;
    end if;
    if v_unit.status = 'captured' then
      continue;
    end if;
    v_retry := v_unit.status = 'failed' or public.catalog_register_group_stale(v_unit.group_id, v_grace);
    if v_retry then
      v_new := v_new || v_tozar;
      v_new_rows := v_new_rows + v_expected;
    end if;
  end loop;

  if cardinality(v_new) = 0 then
    return jsonb_build_object('decision', 'existing', 'group', null, 'units',
      coalesce((select jsonb_agg(to_jsonb(u) order by u.tozar)
                  from public.catalog_register_capture_units u
                 where u.register_version = p_register_version and u.tozar = any(p_tozars)), '[]'));
  end if;
  -- Group cap: a group covers at most N expected rows; ONE tozar larger than N
  -- is captured alone (never split across snapshots).
  if cardinality(v_new) > 1 and v_new_rows > p_group_max_rows then
    raise exception 'CATALOG_REGISTER_GROUP_TOO_LARGE: expected_rows=% cap=%', v_new_rows, p_group_max_rows
      using errcode = '22023';
  end if;
  -- Capacity guard, before anything is written: no partial start.
  -- Rows claimed by requests still in flight are not in the database yet:
  -- they count against the threshold too.
  -- Only LIVE work counts: a unit whose group is stale (a killed job, a
  -- failed trigger, ...) will write nothing and reserves nothing.
  select coalesce(sum(u.expected_rows), 0) into v_inflight
    from public.catalog_register_capture_units u
   where u.status in ('requested', 'capturing')
     and not (u.register_version = p_register_version and u.tozar = any(v_new))
     and not public.catalog_register_group_stale(u.group_id, v_grace);
  v_current := pg_database_size(current_database());
  v_projected := v_current + (v_new_rows + v_inflight) * p_bytes_per_row;
  if v_projected > p_capacity_limit_bytes then
    raise exception 'CATALOG_CAPACITY_THRESHOLD_EXCEEDED: current=% projected=% limit=%',
      v_current, v_projected, p_capacity_limit_bytes using errcode = 'P0001';
  end if;

  insert into public.catalog_register_capture_groups (register_version, requested_by, expected_rows)
  values (p_register_version, p_requested_by, v_new_rows)
  returning * into v_group;
  foreach v_tozar in array v_new loop
    select u.expected_rows into v_expected from public.catalog_register_directory_units u
     where u.version_id = v_latest.id and u.tozar = v_tozar;
    insert into public.catalog_register_capture_units
      (group_id, register_version, tozar, expected_rows)
    values (v_group.id, p_register_version, v_tozar, v_expected)
    on conflict (register_version, tozar) do update
      set group_id = excluded.group_id, attempt = catalog_register_capture_units.attempt + 1,
          status = 'requested', failure_code = null, snapshot_id = null, snapshot_key = null,
          api_total = null, captured_rows = null, count_verified = null, measured_bytes = null,
          measurement_method = null, updated_at = now();
  end loop;
  return jsonb_build_object('decision', 'claimed', 'group', to_jsonb(v_group), 'units',
    (select jsonb_agg(to_jsonb(u) order by u.tozar) from public.catalog_register_capture_units u
      where u.group_id = v_group.id));
end;
$$;

-- One directory refresh at a time: answered with the live one while it is
-- live (catalog_register_group_stale), else a new 'directory' group is claimed.
create or replace function public.request_register_directory_refresh(
  p_requested_by uuid, p_grace_seconds integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_group public.catalog_register_capture_groups%rowtype;
begin
  if p_requested_by is null or p_grace_seconds is null or p_grace_seconds not between 300 and 86400 then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: invalid directory refresh request' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtext('public.catalog_register_directory'));
  select * into v_group from public.catalog_register_capture_groups
   where kind = 'directory' order by claimed_at desc, id desc limit 1;
  if found and not public.catalog_register_group_stale(v_group.id, make_interval(secs => p_grace_seconds)) then
    return jsonb_build_object('decision', 'existing', 'group', to_jsonb(v_group));
  end if;
  insert into public.catalog_register_capture_groups (kind, register_version, requested_by, expected_rows)
  values ('directory', null, p_requested_by, 0)
  returning * into v_group;
  return jsonb_build_object('decision', 'claimed', 'group', to_jsonb(v_group));
end;
$$;

-- The claimer's compare-and-set for ITS group: `claimed` binds the run it
-- made; `triggered` / `trigger_unknown` / `trigger_failed` record the outcome
-- once. A failed trigger fails every still-requested unit of the group with
-- a static code, so the page shows it and a retry may claim them again.
create or replace function public.record_register_capture_trigger(
  p_group_id uuid, p_run_id uuid, p_trigger_state text, p_execution_name text
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_group public.catalog_register_capture_groups%rowtype;
begin
  if p_group_id is null or p_trigger_state is null
     or p_trigger_state not in ('claimed', 'triggered', 'trigger_failed', 'trigger_unknown')
     or (p_execution_name is not null and char_length(p_execution_name) > 400) then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: invalid trigger record' using errcode = '22023';
  end if;
  select * into v_group from public.catalog_register_capture_groups where id = p_group_id for update;
  if not found or v_group.trigger_state <> 'claimed'
     or (v_group.run_id is not null and p_run_id is distinct from v_group.run_id) then
    raise exception 'CATALOG_REGISTER_TRIGGER_CONFLICT: that group is not claimed by this attempt'
      using errcode = '40001';
  end if;
  update public.catalog_register_capture_groups
     set run_id = coalesce(p_run_id, run_id),
         trigger_state = p_trigger_state,
         execution_name = case when p_trigger_state = 'claimed' then execution_name else p_execution_name end,
         triggered_at = case when p_trigger_state = 'claimed' then triggered_at else now() end,
         updated_at = now()
   where id = p_group_id
  returning * into v_group;
  if p_trigger_state = 'trigger_failed' then
    update public.catalog_register_capture_units
       set status = 'failed', failure_code = 'CATALOG_REGISTER_TRIGGER_FAILED', updated_at = now()
     where group_id = p_group_id and status = 'requested';
  end if;
  return to_jsonb(v_group);
end;
$$;

-- The capture job's outcome for one unit, under its run's lease. The unit's
-- group must be captured by THAT run. `captured` requires the snapshot, a
-- verified count and a recorded archive; its storage is measured here, in the
-- database (catalog_register_snapshot_bytes).
create or replace function public.record_register_unit_status(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_unit_id uuid, p_status text, p_failure_code text, p_snapshot_id uuid,
  p_api_total integer, p_captured_rows integer, p_count_verified boolean
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_unit public.catalog_register_capture_units%rowtype;
  v_group public.catalog_register_capture_groups%rowtype;
  v_snapshot public.catalog_source_snapshots%rowtype;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  if p_unit_id is null or p_status is null or p_status not in ('capturing', 'captured', 'failed')
     or (p_status = 'failed' and (p_failure_code is null or p_failure_code !~ '^[A-Z][A-Z0-9_]{2,79}$'))
     or (p_status <> 'failed' and p_failure_code is not null) then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: invalid unit status' using errcode = '22023';
  end if;
  select * into v_unit from public.catalog_register_capture_units where id = p_unit_id for update;
  if not found then
    raise exception 'CATALOG_REGISTER_UNIT_UNKNOWN: unknown register capture unit' using errcode = 'P0002';
  end if;
  select * into v_group from public.catalog_register_capture_groups where id = v_unit.group_id;
  if v_group.run_id is distinct from p_run_id then
    raise exception 'CATALOG_REGISTER_UNIT_NOT_THIS_RUN: that unit is not captured by this run'
      using errcode = '42501';
  end if;
  if v_unit.status = 'captured' then
    return to_jsonb(v_unit);
  end if;
  if p_snapshot_id is not null then
    select * into v_snapshot from public.catalog_source_snapshots where id = p_snapshot_id;
    if not found then
      raise exception 'CATALOG_REGISTER_REQUEST_INVALID: unknown snapshot' using errcode = '22023';
    end if;
  end if;
  if p_status = 'captured' then
    if v_snapshot.id is null or v_snapshot.activated_at is null or p_count_verified is not true
       or p_api_total is null or p_captured_rows is null or p_api_total <> p_captured_rows
       or v_snapshot.stored_record_count <> p_captured_rows
       or not exists (select 1 from public.catalog_register_snapshot_archives a
                       where a.snapshot_id = v_snapshot.id) then
      raise exception 'CATALOG_REGISTER_CAPTURE_UNVERIFIED: a captured unit needs an active, count-verified, archived snapshot'
        using errcode = '22023';
    end if;
  end if;
  update public.catalog_register_capture_units
     set status = p_status,
         failure_code = p_failure_code,
         snapshot_id = coalesce(v_snapshot.id, snapshot_id),
         snapshot_key = coalesce(v_snapshot.snapshot_key, snapshot_key),
         api_total = coalesce(p_api_total, api_total),
         captured_rows = coalesce(p_captured_rows, captured_rows),
         count_verified = coalesce(p_count_verified, count_verified),
         measured_bytes = case when v_snapshot.id is not null and p_status <> 'capturing'
                               then public.catalog_register_snapshot_bytes(v_snapshot.id)
                               else measured_bytes end,
         measurement_method = case when v_snapshot.id is not null and p_status <> 'capturing'
                                   then 'pg_column_size(raw_records+candidates)'
                                   else measurement_method end,
         updated_at = now()
   where id = p_unit_id
  returning * into v_unit;
  return to_jsonb(v_unit);
end;
$$;

-- The archive of one snapshot, under an operator capture run's lease.
-- Content-addressed and idempotent: recording the SAME object again answers
-- the existing row; a different object for the same snapshot is a conflict.
create or replace function public.record_register_snapshot_archive(
  p_run_id uuid, p_worker_id text, p_attempt integer, p_lease_token text,
  p_snapshot_id uuid, p_gcs_uri text, p_byte_size bigint, p_sha256 text, p_line_count integer
) returns jsonb
language plpgsql
set search_path = pg_catalog
as $$
declare
  v_snapshot public.catalog_source_snapshots%rowtype;
  v_row public.catalog_register_snapshot_archives%rowtype;
  v_lines bigint;
begin
  perform public.assert_worker_lease(p_run_id, p_worker_id, p_attempt, p_lease_token);
  select * into v_snapshot from public.catalog_source_snapshots where id = p_snapshot_id;
  if not found then
    raise exception 'CATALOG_REGISTER_REQUEST_INVALID: unknown snapshot' using errcode = '22023';
  end if;
  -- One line per stored raw record, each at its capture position.
  select count(*) into v_lines from public.catalog_raw_records r
   where r.snapshot_id = p_snapshot_id and r.source_locator ? 'capture_index';
  if p_line_count is null or v_lines <> p_line_count or v_snapshot.declared_record_count <> p_line_count then
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
  values (v_snapshot.id, v_snapshot.snapshot_key, p_gcs_uri, p_byte_size, p_sha256, p_line_count, p_run_id)
  returning * into v_row;
  return to_jsonb(v_row);
end;
$$;

-- ---------------------------------------------------------------------------
-- 6. Retention (O22): what may be pruned, and the digest-bound prune.
-- ---------------------------------------------------------------------------
--
-- Candidates are scoped Government snapshots only (they have a tozar). Always
-- kept: per tozar, the active snapshot and the one before it (the two latest
-- activations); any snapshot referenced by evidence, claims (canonical field
-- provenance), runs (adoptions, run checkpoints), work-scope units / batches /
-- queue items, the coverage ledger, or any candidate referenced elsewhere;
-- and any snapshot whose writer run is still live. Everything else is
-- prunable. Unscoped (whole-register) snapshots are never candidates.
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

-- The digest a dry-run prints and an apply must present: SHA-256 of the
-- sorted snapshot keys, one per line, each followed by "\n".
create or replace function public.catalog_register_prune_digest(p_snapshot_keys text[])
returns text
language sql
immutable
set search_path = pg_catalog
as $$
  select encode(sha256(convert_to(
    coalesce((select string_agg(k || E'\n', '' order by k collate "C")
                from unnest(p_snapshot_keys) k), ''), 'UTF8')), 'hex')
$$;

-- The prune: DATABASE ROWS ONLY (never an archive object). Refuses unless
-- the caller presents the exact current prunable list and its digest. The
-- append-only triggers of the three source tables are suspended for THIS
-- transaction only (an error rolls the suspension back with everything else).
create or replace function public.prune_register_snapshots(p_snapshot_keys text[], p_digest text)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog
as $$
declare
  v_keys text[];
  v_ids uuid[];
  v_digest text;
  v_candidates bigint;
  v_records bigint;
  v_snapshots bigint;
begin
  if p_snapshot_keys is null or p_digest is null or p_digest !~ '^[0-9a-f]{64}$' then
    raise exception 'CATALOG_PRUNE_REQUEST_INVALID: a key list and a digest are required'
      using errcode = '22023';
  end if;
  -- Lock the source tables first, so the list cannot move under the check.
  lock table public.catalog_source_snapshots, public.catalog_raw_records,
             public.catalog_candidate_variants in share row exclusive mode;
  select coalesce(array_agg(p.snapshot_key order by p.snapshot_key collate "C"), '{}'),
         coalesce(array_agg(p.snapshot_id), '{}')
    into v_keys, v_ids
    from public.catalog_register_prunable_snapshots() p;
  v_digest := public.catalog_register_prune_digest(v_keys);
  if v_digest <> p_digest
     or public.catalog_register_prune_digest(p_snapshot_keys) <> p_digest then
    raise exception 'CATALOG_PRUNE_DIGEST_MISMATCH: the prunable list changed or the digest is not its digest'
      using errcode = '40001';
  end if;
  if cardinality(v_ids) = 0 then
    return jsonb_build_object('snapshots', 0, 'raw_records', 0, 'candidates', 0, 'digest', v_digest);
  end if;
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
                            'candidates', v_candidates, 'digest', v_digest);
end;
$$;

-- ---------------------------------------------------------------------------
-- 7. REGISTER_COVERAGE (D1-6): read-only facts for the gates.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_register_coverage()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with latest as (
    select * from public.catalog_register_directory_versions
     order by created_at desc, id desc limit 1
  ),
  captured as (
    select distinct on (u.tozar) u.tozar, u.captured_rows
      from public.catalog_register_capture_units u
     where u.status = 'captured'
     order by u.tozar, u.updated_at desc
  )
  select jsonb_build_object(
    'register_version', (select register_version from latest),
    'units_total', coalesce((select unit_count from latest), 0),
    'rows_total', coalesce((select total_rows from latest), 0),
    'units_captured', (select count(*) from captured c
                        where exists (select 1 from public.catalog_register_directory_units d
                                       join latest l on l.id = d.version_id where d.tozar = c.tozar)),
    'rows_captured', coalesce((select sum(c.captured_rows) from captured c
                                where exists (select 1 from public.catalog_register_directory_units d
                                               join latest l on l.id = d.version_id where d.tozar = c.tozar)), 0),
    'unverified_snapshots', (select count(*) from public.catalog_register_capture_units u
                              where u.snapshot_id is not null and u.count_verified is distinct from true),
    'database_bytes', pg_database_size(current_database()))
$$;

-- ---------------------------------------------------------------------------
-- 7b. The page's reads, each ONE jsonb document: a set-returning read through
-- PostgREST is truncated at its row cap (1000 on hosted Supabase), and the
-- register has thousands of tozars.
-- ---------------------------------------------------------------------------
create or replace function public.catalog_register_latest_directory()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with latest as (
    select * from public.catalog_register_directory_versions
     order by created_at desc, id desc limit 1
  )
  select case when not exists (select 1 from latest) then null else jsonb_build_object(
    'version', (select to_jsonb(l) from latest l),
    'units', coalesce((select jsonb_agg(jsonb_build_object('tozar', d.tozar, 'expected_rows', d.expected_rows)
                                        order by d.tozar collate "C")
                         from public.catalog_register_directory_units d
                         join latest l on l.id = d.version_id), '[]'::jsonb)) end
$$;

-- The newest request row per tozar (any directory version).
create or replace function public.catalog_register_unit_states()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  select coalesce(jsonb_agg(to_jsonb(u) order by u.tozar collate "C"), '[]'::jsonb)
    from (select distinct on (x.tozar) x.*
            from public.catalog_register_capture_units x
           order by x.tozar, x.updated_at desc, x.id desc) u
$$;

-- The prunable list and its digest, as one document computed in one statement.
create or replace function public.catalog_register_prunable_list()
returns jsonb
language sql
stable
set search_path = pg_catalog
as $$
  with p as (select * from public.catalog_register_prunable_snapshots())
  select jsonb_build_object(
    'snapshots', coalesce((select jsonb_agg(jsonb_build_object(
                             'snapshot_id', p.snapshot_id, 'snapshot_key', p.snapshot_key, 'tozar', p.tozar,
                             'raw_rows', p.raw_rows, 'estimated_bytes', p.estimated_bytes)
                           order by p.snapshot_key collate "C") from p), '[]'::jsonb),
    'digest', public.catalog_register_prune_digest(
                coalesce((select array_agg(p.snapshot_key) from p), '{}'::text[])))
$$;

-- ---------------------------------------------------------------------------
-- 8. RLS and privileges.
-- ---------------------------------------------------------------------------
alter table public.catalog_register_directory_versions enable row level security;
alter table public.catalog_register_directory_units enable row level security;
alter table public.catalog_register_capture_groups enable row level security;
alter table public.catalog_register_capture_units enable row level security;
alter table public.catalog_register_snapshot_archives enable row level security;

do $$
declare
  fn text;
  tbl text;
  ro record;
begin
  -- Writes: service role only.
  foreach fn in array array[
    'public.record_register_directory(text,timestamptz,jsonb)',
    'public.request_register_capture(text,text[],uuid,integer,bigint,integer,integer)',
    'public.request_register_directory_refresh(uuid,integer)',
    'public.record_register_capture_trigger(uuid,uuid,text,text)',
    'public.record_register_unit_status(uuid,text,integer,text,uuid,text,text,uuid,integer,integer,boolean)',
    'public.record_register_snapshot_archive(uuid,text,integer,text,uuid,text,bigint,text,integer)',
    'public.prune_register_snapshots(text[],text)'
  ] loop
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
  -- Reads: service role and the read-only role (below).
  foreach fn in array array[
    'public.catalog_register_version(text,jsonb)',
    'public.catalog_register_database_bytes()',
    'public.catalog_register_snapshot_bytes(uuid)',
    'public.catalog_register_prunable_snapshots()',
    'public.catalog_register_prune_digest(text[])',
    'public.catalog_register_coverage()',
    'public.catalog_register_latest_directory()',
    'public.catalog_register_unit_states()',
    'public.catalog_register_prunable_list()',
    'public.catalog_register_group_stale(uuid,interval)'
  ] loop
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

  foreach tbl in array array[
    'public.catalog_register_directory_versions', 'public.catalog_register_directory_units',
    'public.catalog_register_capture_groups', 'public.catalog_register_capture_units',
    'public.catalog_register_snapshot_archives', 'public.catalog_register_archive_lines'
  ] loop
    execute format('revoke all on table %s from public', tbl);
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table %s from anon', tbl);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table %s from authenticated', tbl);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    -- Written only through the RPCs above; never deleted.
    execute 'grant select, insert on table public.catalog_register_directory_versions to service_role';
    execute 'grant select, insert on table public.catalog_register_directory_units to service_role';
    execute 'grant select, insert, update on table public.catalog_register_capture_groups to service_role';
    execute 'grant select, insert, update on table public.catalog_register_capture_units to service_role';
    execute 'grant select, insert on table public.catalog_register_snapshot_archives to service_role';
    execute 'grant select on table public.catalog_register_archive_lines to service_role';
    foreach tbl in array array[
      'public.catalog_register_directory_versions', 'public.catalog_register_directory_units',
      'public.catalog_register_capture_groups', 'public.catalog_register_capture_units',
      'public.catalog_register_snapshot_archives'
    ] loop
      execute format('revoke delete, truncate on table %s from service_role', tbl);
    end loop;
  end if;

  -- The read-only roles the release tools, the gates and retention connect
  -- as: the release read-only role (milo_release_readonly_<suffix>: LOGIN,
  -- BYPASSRLS, SELECT through postgres's default privileges, NOT a member of
  -- pg_read_all_data), Supabase's supabase_read_only_user, and any other
  -- read-only login role that is BYPASSRLS and a pg_read_all_data member.
  -- Never a superuser or a platform role. SELECT on the tables and view,
  -- EXECUTE on the read functions -- nothing that writes. Explicit: SELECT
  -- may arrive by default privileges, EXECUTE never does (the read functions
  -- are revoked from PUBLIC above).
  for ro in
    select r.rolname from pg_roles r
     where r.rolcanlogin and not r.rolsuper
       and r.rolname not in ('postgres', 'service_role', 'authenticator', 'anon', 'authenticated')
       and (r.rolname = 'supabase_read_only_user'
            or (r.rolbypassrls and r.rolname like 'milo\_release\_readonly\_%')
            or (r.rolbypassrls and pg_has_role(r.oid, 'pg_read_all_data', 'MEMBER')))
  loop
    foreach tbl in array array[
      'public.catalog_register_directory_versions', 'public.catalog_register_directory_units',
      'public.catalog_register_capture_groups', 'public.catalog_register_capture_units',
      'public.catalog_register_snapshot_archives', 'public.catalog_register_archive_lines'
    ] loop
      execute format('grant select on table %s to %I', tbl, ro.rolname);
    end loop;
    foreach fn in array array[
      'public.catalog_register_version(text,jsonb)',
      'public.catalog_register_database_bytes()',
      'public.catalog_register_snapshot_bytes(uuid)',
      'public.catalog_register_prunable_snapshots()',
      'public.catalog_register_prune_digest(text[])',
      'public.catalog_register_coverage()',
      'public.catalog_register_latest_directory()',
      'public.catalog_register_unit_states()',
      'public.catalog_register_prunable_list()',
      'public.catalog_register_group_stale(uuid,interval)'
    ] loop
      execute format('grant execute on function %s to %I', fn, ro.rolname);
    end loop;
  end loop;
end $$;
