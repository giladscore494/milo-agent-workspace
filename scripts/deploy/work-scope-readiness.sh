#!/usr/bin/env bash
# Read-only readiness of the EXACT execution path a website batch run takes:
#
#   Mapping Plan revision + digest -> its preparation -> each prepared unit's
#   scoped Government snapshot -> the batches cut from it -> the NEXT batch.
#
# It answers "is THIS plan revision ready to start its next batch", never "is
# there some Government snapshot somewhere". A whole-register snapshot, a
# scoped snapshot of another plan, a preparation of an older revision or a
# digest that is not the head's all answer NO here, because the worker would
# refuse every one of them (GOVERNMENT_BATCH_REQUIRED / GOVERNMENT_BATCH_INVALID
# / GOVERNMENT_SNAPSHOT_UNAVAILABLE / WORK_SCOPE_STALE).
#
# THREE ANSWERS, NEVER TWO. Every fact is VERIFIED, NO or UNVERIFIED. A check
# that could not be performed -- no read-only URL, no psql, a query that failed,
# or a database role that row-level security may be hiding rows from -- is
# UNVERIFIED, and UNVERIFIED is never READY.
#
# It performs SELECTs only (psql -X, ON_ERROR_STOP, every value bound as a psql
# variable), calls no RPC (they are service_role-only by design), creates no
# run, writes nothing and prints no credential.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck disable=SC2034
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=operator-config.sh
source "${SCRIPT_DIR}/operator-config.sh"
# shellcheck source=deployment-contract.sh
source "${SCRIPT_DIR}/deployment-contract.sh"

MODE="scoped" MILO_OPERATOR_CONFIG_PATH="" DB_URL_ENV=""
WS_ID="" WS_REV="" WS_DIGEST="" SNAPSHOT_KEY=""

usage() {
  cat << 'EOF'
Usage: work-scope-readiness.sh [mode] [options]

Read-only. SELECT statements only; no RPC, no write, no run.

Modes:
  (default)       Readiness of ONE plan revision. Requires --work-scope-id,
                  --work-scope-revision and --work-scope-digest.
  --schema-only   The database surface the path needs: tables, row-level
                  security, the RPCs and their EXECUTE grants.
  --list          The open Mapping Plans, with their head revision, full
                  digest and whether that head is prepared. Use it to read the
                  three values the other modes need.
  --year-coverage --snapshot-key <cs1.…>
                  Per model year of ONE scoped snapshot: readable vs ambiguous
                  candidates, and for every "from year Y onward" range whether
                  the preparation's vocabulary gate (a unit is
                  vocabulary_insufficient when ambiguous > readable in the
                  plan's range) would pass. Use it to choose a plan range the
                  reviewed vocabulary can read -- nothing about normalization
                  changes.

Options:
  --work-scope-id <uuid>
  --work-scope-revision <n>
  --work-scope-digest <64 hex>
  --database-url-env <NAME>  Environment variable holding a READ-ONLY
                             connection string (default: the operator
                             config's READONLY_DATABASE_URL_ENV). The URL is
                             never accepted on the command line or printed.
  --operator-config <path>
  --help

Exit status: 0 every required fact VERIFIED; 1 a fact is NO; 3 nothing is NO
but a fact is UNVERIFIED; 2 usage or configuration error.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --schema-only) MODE="schema"; shift ;;
    --list) MODE="list"; shift ;;
    --year-coverage) MODE="years"; shift ;;
    --snapshot-key) SNAPSHOT_KEY="${2:?}"; shift 2 ;;
    --work-scope-id) WS_ID="${2:?}"; shift 2 ;;
    --work-scope-revision) WS_REV="${2:?}"; shift 2 ;;
    --work-scope-digest) WS_DIGEST="${2:?}"; shift 2 ;;
    --database-url-env) DB_URL_ENV="${2:?}"; shift 2 ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DB_URL_ENV" ]]; then
  CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
  milo_load_operator_config "$CONFIG_PATH" || exit 2
  DB_URL_ENV="$(milo_op READONLY_DATABASE_URL_ENV)"
fi
if [[ "$MODE" == "years" ]]; then
  [[ "$SNAPSHOT_KEY" =~ ^cs1\.[0-9a-f]{32}$ ]] \
    || { printf 'FAIL: --year-coverage needs --snapshot-key cs1.<32 hex> (a UNIT line or NEXT_BATCH_SNAPSHOT_KEY names it)\n' >&2; exit 2; }
fi
if [[ "$MODE" == "scoped" ]]; then
  [[ "$WS_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
    || { printf 'FAIL: --work-scope-id must be the plan'"'"'s lowercase UUID\n' >&2; exit 2; }
  [[ "$WS_REV" =~ ^[1-9][0-9]{0,8}$ ]] \
    || { printf 'FAIL: --work-scope-revision must be a whole revision number\n' >&2; exit 2; }
  [[ "$WS_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
    || { printf 'FAIL: --work-scope-digest must be the revision'"'"'s 64-character digest\n' >&2; exit 2; }
fi

DB_URL=""
[[ -n "$DB_URL_ENV" && "$DB_URL_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && DB_URL="${!DB_URL_ENV:-}"

# --- the three-valued record ---------------------------------------------
NO_COUNT=0 UNVERIFIED_COUNT=0
fact() {
  # fact NAME VERIFIED|NO|UNVERIFIED [detail]
  local name="$1" value="$2" detail="${3:-}"
  printf '%s=%s%s\n' "$name" "$value" "${detail:+ (${detail})}"
  case "$value" in
    VERIFIED) ;;
    NO) NO_COUNT=$((NO_COUNT + 1)) ;;
    *) UNVERIFIED_COUNT=$((UNVERIFIED_COUNT + 1)) ;;
  esac
}
finish() {
  if [[ "$NO_COUNT" -gt 0 ]]; then
    printf 'WORK_SCOPE_READINESS=NO (%d fact(s) NO, %d UNVERIFIED)\n' "$NO_COUNT" "$UNVERIFIED_COUNT"
    exit 1
  fi
  if [[ "$UNVERIFIED_COUNT" -gt 0 ]]; then
    printf 'WORK_SCOPE_READINESS=UNVERIFIED (%d fact(s) could not be verified)\n' "$UNVERIFIED_COUNT"
    exit 3
  fi
  printf 'WORK_SCOPE_READINESS=VERIFIED\n'
  exit 0
}

if [[ -z "$DB_URL" ]]; then
  fact DATABASE_READ UNVERIFIED "set \$${DB_URL_ENV:-READONLY_DATABASE_URL_ENV} to a read-only connection string"
  finish
fi
if ! command -v psql > /dev/null 2>&1; then
  fact DATABASE_READ UNVERIFIED "psql is not installed"
  finish
fi

# q SQL -> rows on stdout ('|'-separated). Returns psql's status; its stderr is
# discarded so no connection detail can reach the terminal. Every value the
# SQL uses arrives as a psql variable and is quoted by psql (:'name').
q() {
  psql "$DB_URL" -X -A -t -F'|' -v ON_ERROR_STOP=1 \
    -v ws_id="$WS_ID" -v ws_rev="$WS_REV" -v ws_digest="$WS_DIGEST" \
    -v prep_id="${PREP_ID:-}" -v resource_id="$MILO_CAPTURE_RESOURCE_ID" \
    -v snapshot_key="$SNAPSHOT_KEY" \
    -v rpcs="$(IFS=,; printf '%s' "${MILO_WORK_SCOPE_RPCS[*]}")" \
    -v tables="$(IFS=,; printf '%s' "${MILO_WORK_SCOPE_TABLES[*]}")" \
    2> /dev/null <<< "$1"
}

# --- 0. can this role see service-only rows at all? ------------------------
# Every relation on this path has row-level security ON and NO policy, so a
# role without BYPASSRLS reads ZERO rows from each -- which would look exactly
# like "nothing prepared". An empty answer from such a role is not evidence.
if ! visibility="$(q "select (r.rolsuper or r.rolbypassrls)::text from pg_roles r where r.rolname = current_user;")"; then
  fact DATABASE_READ UNVERIFIED "the read-only connection failed or was refused"
  finish
fi
if [[ "$visibility" != "true" ]]; then
  fact DATABASE_READ UNVERIFIED "this role is subject to row-level security on the service-only catalog tables; an empty answer would prove nothing. Use a read-only role with BYPASSRLS (Supabase: supabase_read_only_user)"
  finish
fi
fact DATABASE_READ VERIFIED "read-only role sees service-only rows"

# --- 1. the schema this path needs ----------------------------------------
check_schema() {
  local rows missing_tables="" rls_off="" rpc_missing="" rpc_ungranted="" rpc_exposed="" name exists rls svc pub
  if ! rows="$(q "
    select t.name,
           (to_regclass('public.' || t.name) is not null)::text,
           coalesce((select c.relrowsecurity::text from pg_class c
                      where c.oid = to_regclass('public.' || t.name)), 'false')
      from unnest(string_to_array(:'tables', ',')) as t(name);")"; then
    fact WORK_SCOPE_SCHEMA UNVERIFIED "the table inventory query failed"
    return
  fi
  while IFS='|' read -r name exists rls; do
    [[ -n "$name" ]] || continue
    [[ "$exists" == "true" ]] || missing_tables+="${name} "
    [[ "$exists" != "true" || "$rls" == "true" ]] || rls_off+="${name} "
  done <<< "$rows"
  if ! rows="$(q "
    select f.name,
           count(p.oid)::text,
           coalesce(bool_and(case when exists (select 1 from pg_roles where rolname = 'service_role')
                                  then has_function_privilege('service_role', p.oid, 'EXECUTE')
                                  else false end), false)::text,
           coalesce(bool_or((exists (select 1 from pg_roles where rolname = 'anon')
                               and has_function_privilege('anon', p.oid, 'EXECUTE'))
                            or (exists (select 1 from pg_roles where rolname = 'authenticated')
                               and has_function_privilege('authenticated', p.oid, 'EXECUTE'))), false)::text
      from unnest(string_to_array(:'rpcs', ',')) as f(name)
      left join pg_proc p
        on p.proname = f.name
       and p.pronamespace = 'public'::regnamespace
     group by f.name;")"; then
    fact WORK_SCOPE_SCHEMA UNVERIFIED "the RPC inventory query failed"
    return
  fi
  local count
  while IFS='|' read -r name count svc pub; do
    [[ -n "$name" ]] || continue
    if [[ "$count" == "0" ]]; then rpc_missing+="${name} "; continue; fi
    [[ "$svc" == "true" ]] || rpc_ungranted+="${name} "
    [[ "$pub" != "true" ]] || rpc_exposed+="${name} "
  done <<< "$rows"
  local problems=""
  [[ -z "$missing_tables" ]] || problems+="missing tables: ${missing_tables}; "
  [[ -z "$rls_off" ]] || problems+="row-level security OFF: ${rls_off}; "
  [[ -z "$rpc_missing" ]] || problems+="missing RPCs: ${rpc_missing}; "
  [[ -z "$rpc_ungranted" ]] || problems+="not EXECUTE-able by service_role: ${rpc_ungranted}; "
  [[ -z "$rpc_exposed" ]] || problems+="EXECUTE-able by anon/authenticated: ${rpc_exposed}; "
  if [[ -n "$problems" ]]; then
    fact WORK_SCOPE_SCHEMA NO "${problems%; }. Apply the pending migrations (${MILO_WORK_SCOPE_MIGRATIONS}) through the Deploy Supabase Migrations workflow"
  else
    fact WORK_SCOPE_SCHEMA VERIFIED "${#MILO_WORK_SCOPE_TABLES[@]} tables with RLS, ${#MILO_WORK_SCOPE_RPCS[@]} RPCs service_role-only"
  fi
}

# An ORPHANED scoped snapshot: a Government WLTP capture declaring a scope that
# was opened and never finished -- pending, not activated. Because a snapshot
# key is derived from content, the next capture of the same register content
# lands on that very row, so an unprepared revision's preparation either
# ADOPTS it (its CURRENT writer -- the latest adopter, else the run that opened
# it -- ended failed / cancelled / timed_out and holds no live lease:
# 20260924000200) or fails late with GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN. Stated
# BEFORE a capture is attempted, never discovered after one. The writer is
# resolved inline (a read-only role may not execute the service functions).
check_orphaned_snapshots() {
  local rows sid skey scope_key writer writer_status stored declared adoptable n=0 blocked=""
  if ! rows="$(q "
    select o.id, o.snapshot_key, o.scope_key, o.writer, coalesce(r.status, 'missing'),
           o.stored, o.declared,
           (coalesce(r.status in ('failed', 'cancelled', 'timed_out'), false)
            and (r.lease_expires_at is null or r.lease_expires_at <= now()))::text
      from (select s.id, s.snapshot_key, s.created_at,
                   left(coalesce(s.retrieval_metadata->'capture_scope'->>'scope_key', ''), 16) as scope_key,
                   coalesce((select a.adopted_by_run_id from public.catalog_snapshot_adoptions a
                              where a.snapshot_id = s.id order by a.adoption_seq desc limit 1),
                            s.created_by_run_id) as writer,
                   s.stored_record_count::text as stored, s.declared_record_count::text as declared
              from public.catalog_source_snapshots s
             where s.source_family = 'government'
               and s.resource_id = :'resource_id'
               and s.retrieval_metadata ? 'capture_scope'
               and s.activated_at is null
               and s.validation_state = 'pending') o
      left join public.runs r on r.id = o.writer
     order by o.created_at
     limit 20;")"; then
    fact ORPHANED_SNAPSHOTS UNVERIFIED "the pending scoped snapshot query failed"
    return
  fi
  while IFS='|' read -r sid skey scope_key writer writer_status stored declared adoptable; do
    [[ -n "$sid" ]] || continue
    n=$((n + 1))
    printf 'ORPHANED_SCOPED_SNAPSHOT id=%s key=%s scope_key=%s writer_run=%s writer_status=%s stored=%s declared=%s adoptable=%s\n' \
      "$sid" "$skey" "$scope_key" "$writer" "$writer_status" "$stored" "$declared" \
      "$([[ "$adoptable" == "true" ]] && echo yes || echo no)"
    [[ "$adoptable" == "true" ]] || blocked+="${skey} (writer ${writer} is ${writer_status}); "
  done <<< "$rows"
  if [[ -n "$blocked" ]]; then
    fact ORPHANED_SNAPSHOTS NO "a pending scoped snapshot is written by a run that is still live or did not fail, so a capture of the same content cannot land or adopt it (GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN): ${blocked%; }. Let that run finish, or resolve it through the run lifecycle tools, before preparing"
  elif [[ "$n" -gt 0 ]]; then
    fact ORPHANED_SNAPSHOTS VERIFIED "${n} orphaned pending scoped snapshot(s), each written by a run that ended failed/cancelled/timed_out with no live lease: the next preparation ADOPTS one if its capture reproduces that content, and completes it through the same idempotent writes and completeness gate"
  else
    fact ORPHANED_SNAPSHOTS VERIFIED "no pending scoped Government snapshot is waiting"
  fi
}

list_plans() {
  local rows
  if ! rows="$(q "
    select s.id, s.head_revision, s.head_digest, s.conversation_id,
           to_char(s.created_at at time zone 'utc', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'),
           (select count(*) from public.catalog_work_scope_preparations p
             where p.work_scope_id = s.id and p.revision = s.head_revision)::text,
           coalesce((select c.action = 'pause' from public.catalog_work_scope_controls c
                      where c.work_scope_id = s.id order by c.sequence desc limit 1), false)::text
      from public.catalog_work_scopes s
     where s.closed_at is null
     order by s.created_at desc
     limit 20;")"; then
    fact WORK_SCOPE_LIST UNVERIFIED "the plan listing query failed"
    return
  fi
  printf 'OPEN MAPPING PLANS (newest first, at most 20):\n'
  local id rev digest conv created prepared paused n=0
  while IFS='|' read -r id rev digest conv created prepared paused; do
    [[ -n "$id" ]] || continue
    n=$((n + 1))
    printf '  work_scope_id=%s revision=%s digest=%s conversation=%s created=%s head_prepared=%s paused=%s\n' \
      "$id" "$rev" "$digest" "$conv" "$created" "$([[ "$prepared" == "1" ]] && echo yes || echo no)" "$paused"
  done <<< "$rows"
  [[ "$n" -gt 0 ]] || printf '  (none)\n'
  fact WORK_SCOPE_LIST VERIFIED "${n} open plan(s)"
}

# The vocabulary gate, year by year, for one snapshot. The same counts
# `prepare_work_scope_queue` takes over a plan's range (readable = status other
# than ambiguous, eligible = status candidate), per model_year_start, plus the
# running totals for every "from year Y onward" range (no upper bound).
year_coverage() {
  local head rows sid active vstate total y readable ambiguous eligible cr ca ce verdict
  if ! head="$(q "
    select s.id, (s.activated_at is not null)::text, s.validation_state,
           (select count(*) from public.catalog_candidate_variants c where c.snapshot_id = s.id)::text
      from public.catalog_source_snapshots s
     where s.source_family = 'government' and s.snapshot_key = :'snapshot_key';")"; then
    fact YEAR_COVERAGE UNVERIFIED "the snapshot query failed"
    return
  fi
  if [[ -z "$head" ]]; then
    fact YEAR_COVERAGE NO "no Government snapshot ${SNAPSHOT_KEY} exists"
    return
  fi
  IFS='|' read -r sid active vstate total <<< "$head"
  printf 'SNAPSHOT id=%s key=%s active=%s validation_state=%s candidates=%s\n' \
    "$sid" "$SNAPSHOT_KEY" "$active" "$vstate" "$total"
  if ! rows="$(q "
    with per as (
      select c.model_year_start as y,
             count(*) filter (where c.status <> 'ambiguous') as readable,
             count(*) filter (where c.status = 'ambiguous') as ambiguous,
             count(*) filter (where c.status = 'candidate') as eligible
        from public.catalog_candidate_variants c
        join public.catalog_source_snapshots s on s.id = c.snapshot_id
       where s.snapshot_key = :'snapshot_key' and c.model_year_start is not null
       group by c.model_year_start)
    select y::text, readable::text, ambiguous::text, eligible::text,
           (sum(readable) over w)::text, (sum(ambiguous) over w)::text, (sum(eligible) over w)::text
      from per
    window w as (order by y desc rows between unbounded preceding and current row)
     order by y;")"; then
    fact YEAR_COVERAGE UNVERIFIED "the per-year query failed"
    return
  fi
  while IFS='|' read -r y readable ambiguous eligible cr ca ce; do
    [[ -n "$y" ]] || continue
    if (( ca > cr )); then verdict="vocabulary_insufficient"
    elif (( ce == 0 )); then verdict="passes_but_queues_nothing"
    else verdict="passes"; fi
    printf 'YEAR %s readable=%s ambiguous=%s eligible=%s | FROM_%s_ONWARD readable=%s ambiguous=%s eligible=%s gate=%s\n' \
      "$y" "$readable" "$ambiguous" "$eligible" "$y" "$cr" "$ca" "$ce" "$verdict"
  done <<< "$rows"
  if [[ "$active" != "true" ]]; then
    fact YEAR_COVERAGE VERIFIED "counted from a snapshot that is NOT active; a preparation reads only an active one, so these counts are what it will see once the snapshot is activated"
  else
    fact YEAR_COVERAGE VERIFIED "per-year counts of the active snapshot; a plan range passes the vocabulary gate when its ambiguous count is not above its readable count"
  fi
}

case "$MODE" in
  schema) check_schema; finish ;;
  list) list_plans; finish ;;
  years) year_coverage; finish ;;
esac

check_schema
if [[ "$NO_COUNT" -gt 0 || "$UNVERIFIED_COUNT" -gt 0 ]]; then
  fact EVIDENCE_READY UNVERIFIED "the schema this path needs is not in place"
  fact BATCH_READY UNVERIFIED "the schema this path needs is not in place"
  finish
fi

# --- 2. the plan: the requested revision must be the open head -------------
if ! plan="$(q "
  select (s.closed_at is null)::text, s.head_revision::text, s.head_digest, p.workflow_key,
         coalesce((select r.digest from public.catalog_work_scope_revisions r
                    where r.work_scope_id = s.id and r.revision = :'ws_rev'::int), ''),
         coalesce((select c.action = 'pause' from public.catalog_work_scope_controls c
                    where c.work_scope_id = s.id order by c.sequence desc limit 1), false)::text
    from public.catalog_work_scopes s
    join public.projects p on p.id = s.project_id
   where s.id = :'ws_id'::uuid;")"; then
  fact WORK_SCOPE_PLAN UNVERIFIED "the plan query failed"
  fact EVIDENCE_READY UNVERIFIED "the plan could not be read"
  fact BATCH_READY UNVERIFIED "the plan could not be read"
  finish
fi
if [[ -z "$plan" ]]; then
  fact WORK_SCOPE_PLAN NO "no Mapping Plan ${WS_ID} exists; list the open plans with --list"
  finish
fi
IFS='|' read -r plan_open head_rev head_digest workflow rev_digest paused <<< "$plan"
printf 'WORK_SCOPE_ID=%s\nWORK_SCOPE_HEAD_REVISION=%s\nWORK_SCOPE_HEAD_DIGEST=%s\n' "$WS_ID" "$head_rev" "$head_digest"
plan_problem=""
[[ "$workflow" == "swarm_v2" ]] || plan_problem+="its project runs '${workflow}', not swarm_v2; "
[[ "$plan_open" == "true" ]] || plan_problem+="the plan is closed; "
[[ -n "$rev_digest" ]] || plan_problem+="revision ${WS_REV} does not exist; "
[[ -z "$rev_digest" || "$rev_digest" == "$WS_DIGEST" ]] \
  || plan_problem+="revision ${WS_REV} has digest ${rev_digest}, not the one given; "
[[ "$head_rev" == "$WS_REV" ]] \
  || plan_problem+="revision ${WS_REV} is not the head (head is ${head_rev}): a stale revision never starts; "
[[ "$head_digest" == "$WS_DIGEST" ]] || plan_problem+="the head digest is not the one given; "
if [[ -n "$plan_problem" ]]; then
  fact WORK_SCOPE_PLAN NO "${plan_problem%; }"
  finish
fi
fact WORK_SCOPE_PLAN VERIFIED "revision ${WS_REV} is the open head with that exact digest"

# --- 3. its preparation ---------------------------------------------------
if ! prep="$(q "
  select p.id, p.scope_digest, p.unit_count::text, p.prepared_unit_count::text,
         p.queued_item_count::text, p.batch_count::text,
         coalesce(r.run_identity->>'workflow_key', ''), coalesce(r.status, '')
    from public.catalog_work_scope_preparations p
    left join public.runs r on r.id = p.prepared_by_run_id
   where p.work_scope_id = :'ws_id'::uuid and p.revision = :'ws_rev'::int;")"; then
  fact WORK_SCOPE_PREPARED UNVERIFIED "the preparation query failed"
  finish
fi
if [[ -z "$prep" ]]; then
  fact WORK_SCOPE_PREPARED NO "revision ${WS_REV} has not been prepared. Run the scoped preparation (government-production-capture.sh --prepare-work-scope) for exactly this revision and digest"
  check_orphaned_snapshots
  fact EVIDENCE_READY NO "no scoped Government snapshot is linked to this revision"
  fact BATCH_READY NO "no batch exists for this revision"
  finish
fi
if [[ "$prep" == *$'\n'* ]]; then
  fact WORK_SCOPE_PREPARED NO "more than one preparation answered for one revision"
  finish
fi
IFS='|' read -r PREP_ID prep_digest unit_count prepared_units queued batch_count prep_workflow prep_status <<< "$prep"
printf 'WORK_SCOPE_PREPARATION_ID=%s\nWORK_SCOPE_UNITS=%s\nWORK_SCOPE_PREPARED_UNITS=%s\nWORK_SCOPE_QUEUED_ITEMS=%s\nWORK_SCOPE_BATCHES=%s\nWORK_SCOPE_PREPARED_BY=%s(%s)\n' \
  "$PREP_ID" "$unit_count" "$prepared_units" "$queued" "$batch_count" "${prep_workflow:-unknown}" "${prep_status:-unknown}"
prep_problem=""
[[ "$prep_digest" == "$WS_DIGEST" ]] || prep_problem+="the preparation names another digest; "
[[ "$prep_workflow" == "operator_capture" ]] || prep_problem+="it was not written by an operator capture run; "
if [[ -n "$prep_problem" ]]; then
  fact WORK_SCOPE_PREPARED NO "${prep_problem%; }"
  finish
fi
fact WORK_SCOPE_PREPARED VERIFIED "prepared once, for this revision and digest"

# --- 4. the units, and each prepared unit's scoped snapshot ---------------
if ! units="$(q "
  select u.priority::text, u.unit_key, u.state, coalesce(u.reason_code, ''), u.queued_count::text,
         coalesce(u.snapshot_key, ''),
         coalesce((s.snapshot_key = u.snapshot_key)::text, 'false'),
         coalesce((s.source_family = 'government')::text, 'false'),
         coalesce((s.resource_id = :'resource_id')::text, 'false'),
         coalesce(s.validation_state, ''),
         coalesce((s.activated_at is not null)::text, 'false'),
         coalesce((s.declared_record_count = s.stored_record_count)::text, 'false'),
         coalesce((s.retrieval_metadata->'capture_scope'->>'scope_key' = u.capture_scope_key)::text, 'false'),
         coalesce((s.retrieval_metadata->'capture_scope'->'filters'->>'tozar' = u.register_marque)::text, 'false'),
         coalesce((s.retrieval_metadata->>'normalization_contract' = 'gov.wltp.normalize.1')::text, 'false'),
         coalesce(s.retrieval_metadata->>'normalization_issue_count', '')
    from public.catalog_work_scope_units u
    left join public.catalog_source_snapshots s on s.id = u.snapshot_id
   where u.preparation_id = :'prep_id'::uuid
   order by u.priority;")"; then
  fact EVIDENCE_READY UNVERIFIED "the unit/snapshot query failed"
  finish
fi
evidence_problem="" prepared_seen=0
while IFS='|' read -r priority unit state reason ucount skey same_key gov resource vstate active complete scope_ok marque_ok contract_ok issues; do
  [[ -n "$unit" ]] || continue
  printf 'UNIT %s. %s state=%s%s queued=%s%s\n' "$priority" "$unit" "$state" \
    "${reason:+ reason=${reason}}" "$ucount" "${skey:+ snapshot=${skey}}"
  [[ "$state" == "prepared" ]] || continue
  prepared_seen=$((prepared_seen + 1))
  local_problem=""
  [[ "$same_key" == "true" && "$gov" == "true" ]] || local_problem+="its snapshot is not the recorded Government snapshot; "
  [[ "$resource" == "true" ]] || local_problem+="its snapshot is not of the pinned WLTP resource; "
  [[ "$vstate" == "complete" ]] || local_problem+="its snapshot is not complete (${vstate:-missing}); "
  [[ "$active" == "true" ]] || local_problem+="its snapshot is not active; "
  [[ "$complete" == "true" ]] || local_problem+="stored != declared records; "
  [[ "$scope_ok" == "true" && "$marque_ok" == "true" ]] \
    || local_problem+="its snapshot does not declare exactly this unit's register scope; "
  [[ "$contract_ok" == "true" && "$issues" == "0" ]] \
    || local_problem+="its snapshot is not fully normalized (issues=${issues:-unknown}); "
  [[ -z "$local_problem" ]] || evidence_problem+="${unit}: ${local_problem}"
done <<< "$units"
if [[ "$prepared_seen" -eq 0 ]]; then
  fact EVIDENCE_READY NO "no unit of this revision was prepared (see the UNIT lines: register_unverified / vocabulary_insufficient / snapshot_unusable queue nothing). Revise the plan to a manufacturer with a verified register spelling"
  fact BATCH_READY NO "nothing was queued"
  finish
fi
if [[ "$prepared_seen" != "$prepared_units" ]]; then
  evidence_problem+="the preparation counts ${prepared_units} prepared unit(s) but ${prepared_seen} were found; "
fi
if [[ -n "$evidence_problem" ]]; then
  fact EVIDENCE_READY NO "${evidence_problem%; }"
  finish
fi
fact EVIDENCE_READY VERIFIED "${prepared_seen} prepared unit(s), each pinned to an active, complete, fully normalized scoped snapshot of its own register spelling"

# --- 5. the batches: exact linkage, and which one is next -----------------
if ! batches="$(q "
  select b.batch_number::text, b.id, b.unit_key, b.item_count::text, b.snapshot_key,
         (b.revision = :'ws_rev'::int and b.scope_digest = :'ws_digest')::text,
         (u.state = 'prepared' and u.snapshot_id = b.snapshot_id and u.snapshot_key = b.snapshot_key)::text,
         (select count(*) from public.catalog_work_scope_queue_items i where i.batch_id = b.id)::text,
         (select count(*) from public.catalog_work_scope_queue_items i
           where i.batch_id = b.id and i.snapshot_id <> b.snapshot_id)::text,
         coalesce((select r.status from public.catalog_work_scope_batch_runs br
                     join public.runs r on r.id = br.run_id
                    where br.batch_id = b.id order by br.attempt desc limit 1), '')
    from public.catalog_work_scope_batches b
    join public.catalog_work_scope_units u on u.id = b.unit_id
   where b.preparation_id = :'prep_id'::uuid
   order by b.batch_number;")"; then
  fact BATCH_READY UNVERIFIED "the batch query failed"
  finish
fi
if ! live="$(q "
  select coalesce(string_agg(r.id::text || ':' || r.status, ','), '')
    from public.catalog_work_scope_batch_runs br
    join public.runs r on r.id = br.run_id
   where br.work_scope_id = :'ws_id'::uuid
     and r.status in ('queued', 'launching', 'starting', 'running', 'waiting', 'cancellation_requested');")"; then
  fact BATCH_READY UNVERIFIED "the live-batch query failed"
  finish
fi
batch_problem="" expected=1 seen=0 next="" settled=0
while IFS='|' read -r number bid bunit items bkey rev_ok unit_ok qcount foreign last_status; do
  [[ -n "$bid" ]] || continue
  seen=$((seen + 1))
  [[ "$number" == "$expected" ]] || batch_problem+="batch numbers are not 1..n (found ${number} where ${expected} was due); "
  expected=$((number + 1))
  [[ "$rev_ok" == "true" ]] || batch_problem+="batch ${number} names another revision or digest; "
  [[ "$unit_ok" == "true" ]] || batch_problem+="batch ${number} is not pinned to its prepared unit's snapshot; "
  [[ "$qcount" == "$items" ]] || batch_problem+="batch ${number} holds ${qcount} queue item(s), not ${items}; "
  [[ "$foreign" == "0" ]] || batch_problem+="batch ${number} holds items of another snapshot; "
  case "$last_status" in
    completed | partial_success) settled=$((settled + 1)) ;;
    *) [[ -n "$next" ]] || next="${number}|${bid}|${bunit}|${items}|${bkey}|${last_status:-never_started}" ;;
  esac
done <<< "$batches"
[[ "$seen" == "$batch_count" ]] || batch_problem+="the preparation counts ${batch_count} batch(es) but ${seen} exist; "
printf 'WORK_SCOPE_BATCHES_SETTLED=%s of %s\n' "$settled" "$seen"
if [[ -n "$batch_problem" ]]; then
  fact BATCH_READY NO "${batch_problem%; }"
  finish
fi
if [[ -n "$live" ]]; then
  printf 'LIVE_BATCH_RUNS=%s\n' "$live"
  fact BATCH_READY NO "a batch run of this plan is still live; nothing else may start until it settles (and no second paid run is started for it)"
  finish
fi
if [[ "$paused" == "true" ]]; then
  fact BATCH_READY NO "the plan is paused; resume it in the Mapping Plan first"
  finish
fi
if [[ -z "$next" ]]; then
  fact BATCH_READY NO "every batch of this revision has settled; there is nothing left to start"
  finish
fi
IFS='|' read -r n_number n_id n_unit n_items n_key n_last <<< "$next"
printf 'NEXT_BATCH_NUMBER=%s\nNEXT_BATCH_ID=%s\nNEXT_BATCH_UNIT=%s\nNEXT_BATCH_ITEMS=%s\nNEXT_BATCH_SNAPSHOT_KEY=%s\nNEXT_BATCH_LAST_RUN=%s\n' \
  "$n_number" "$n_id" "$n_unit" "$n_items" "$n_key" "$n_last"
fact BATCH_READY VERIFIED "batch ${n_number} of ${seen} (${n_unit}, ${n_items} candidates) is next; no batch is live and the plan is not paused"
finish
