#!/usr/bin/env bash
# The operator path for the bounded Government production capture.
#
# WHAT THIS IS NOT: it is not a second implementation of the capture. The
# capture lives in backend/catalog/government/ and its operator entrypoint is
# backend/catalog/operator_capture.py. Both already ship inside the worker
# image (Dockerfile.worker copies the whole backend package). This script only
# arranges for that existing entrypoint to run somewhere that has outbound
# access to data.gov.il and the production Supabase service-role credential --
# a dedicated, bounded Cloud Run Job -- and then proves the result.
#
# Why a separate job rather than the product worker: posture. The product
# worker must never carry the catalog master switch, and the capture must
# never carry a provider credential. One job each keeps both true by
# construction instead of by remembering to unset something afterwards.
#
# SAFETY PROPERTIES, all enforced below or by the entrypoint itself:
#   * no provider credential is ever bound to the capture job, so the capture
#     cannot spend model money even if something went wrong;
#   * MILO_ENABLE_PAID_EXECUTION is pinned false, and operator_capture.py
#     refuses (CAPTURE_PAID_EXECUTION_ENABLED) if it is ever on;
#   * MILO_ENABLE_CATALOG_PROMOTION is pinned false: a capture lands raw
#     records and candidates, never canonical facts;
#   * the catalog master switch has no committed value in this repository --
#     the operator supplies it here with --enable-catalog-execution, which is
#     what makes turning it on a deliberate decision rather than a default;
#   * the job is finite and bounded (task timeout, deterministic retries) and
#     exposes no HTTP endpoint;
#   * re-running is safe: the snapshot key is derived from captured content,
#     so an identical re-capture collapses onto the same rows, and the
#     idempotency key returns the SAME prepared run rather than a second one.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# Read by operator-config.sh, sourced below. ShellCheck sees that use only when
# it follows the source (CI runs it with -x); run bare, it would not.
# shellcheck disable=SC2034
MILO_REPO_ROOT="$REPO_ROOT"
# shellcheck source=../deploy/operator-config.sh disable=SC1091
source "${REPO_ROOT}/scripts/deploy/operator-config.sh"
# shellcheck source=../deploy/deployment-contract.sh disable=SC1091
source "${REPO_ROOT}/scripts/deploy/deployment-contract.sh"

MODE="plan" MILO_OPERATOR_CONFIG_PATH="" CATALOG_EXECUTION_VALUE="" RUN_ID="" IDEMPOTENCY_KEY=""
WORK_SCOPE_ID="" WORK_SCOPE_REVISION="" WORK_SCOPE_DIGEST="" WORK_SCOPE_PREPARATION_VALUE=""
TASK_TIMEOUT="${MILO_CAPTURE_TASK_TIMEOUT:-3600s}"

usage() {
  cat << 'EOF'
Usage: government-production-capture.sh [mode] [options]

Runs the EXISTING operator capture entrypoint inside a dedicated, bounded
Cloud Run Job. Nothing here re-implements the capture.

Modes (exactly one; default --plan):
  --plan        Read-only. Print the job definition and the exact commands.
  --ensure-job  Create or update the capture Cloud Run Job (idempotent).
  --prepare     Execute the job in --prepare mode and report the run id.
  --capture     Execute the real capture. Requires --run-id.
  --prepare-work-scope
                Scoped catalog PR2: prepare ONE Mapping Plan revision -- a
                scoped capture per verified manufacturer, then the durable
                queue and batches. Requires --run-id, the three --work-scope-*
                values and --enable-work-scope-preparation. Starts no batch.
  --all         ensure-job, prepare, capture, verify — in order.

Options:
  --enable-catalog-execution
                REQUIRED for every mutating mode. Supplies the value of the
                catalog master switch for this job. This repository commits no
                enabled value for it (scripts/check_unsafe_defaults.py enforces
                that), so turning it on is an explicit operator act, recorded
                in the command you ran.
  --run-id <uuid>          The prepared run (for --capture / --prepare-work-scope).
  --work-scope-id <uuid>   The Mapping Plan to prepare.
  --work-scope-revision <n>
                           The plan revision to prepare; it must be the head.
  --work-scope-digest <hex>
                           That revision's digest; a stale plan is refused.
  --enable-work-scope-preparation
                REQUIRED for --prepare-work-scope. Turns the scoped-preparation
                switch on for THAT ONE execution only; the job definition keeps
                it pinned off.
  --idempotency-key <key>  For --prepare / --all: this attempt's key, instead of
                           CAPTURE_IDEMPOTENCY_KEY. A key already used returns
                           THAT run (which a finished capture cannot reuse), so
                           each new capture or preparation attempt names a new
                           key; production-activate.sh derives one per attempt.
  --operator-config <path> Operator identifier file.
  --task-timeout <dur>     Cloud Run task timeout (default 3600s).
  --help

The release worker image (<region>-docker.pkg.dev/.../worker:<HEAD SHA>) must
already exist in Artifact Registry -- it is built by
`DEPLOY_MODE=apply scripts/deploy/cloud-run.sh` -- and every mutating mode
refuses before touching the job when it does not, or when the job does not run
that exact image.

Exits nonzero on refusal, on failure, or on a snapshot that is not usable.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) MODE="plan"; shift ;;
    --ensure-job) MODE="ensure-job"; shift ;;
    --prepare) MODE="prepare"; shift ;;
    --capture) MODE="capture"; shift ;;
    --prepare-work-scope) MODE="prepare-work-scope"; shift ;;
    --all) MODE="all"; shift ;;
    --enable-catalog-execution) CATALOG_EXECUTION_VALUE="true"; shift ;;
    --enable-work-scope-preparation) WORK_SCOPE_PREPARATION_VALUE="true"; shift ;;
    --run-id) RUN_ID="${2:?}"; shift 2 ;;
    --idempotency-key) IDEMPOTENCY_KEY="${2:?}"; shift 2 ;;
    --work-scope-id) WORK_SCOPE_ID="${2:?}"; shift 2 ;;
    --work-scope-revision) WORK_SCOPE_REVISION="${2:?}"; shift 2 ;;
    --work-scope-digest) WORK_SCOPE_DIGEST="${2:?}"; shift 2 ;;
    --operator-config) MILO_OPERATOR_CONFIG_PATH="${2:?}"; shift 2 ;;
    --task-timeout) TASK_TIMEOUT="${2:?}"; shift 2 ;;
    --help) usage; exit 0 ;;
    *) printf 'FAIL: unknown argument %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() { printf 'FAIL: %s\n' "$1" >&2; exit "${2:-1}"; }

CONFIG_PATH="$(milo_operator_config_path "$REPO_ROOT" "$MILO_OPERATOR_CONFIG_PATH")"
milo_load_operator_config "$CONFIG_PATH" || exit 2
milo_require_op GCP_PROJECT_ID GCP_REGION ARTIFACT_REGISTRY_REPOSITORY \
  CLOUD_RUN_CAPTURE_JOB SUPABASE_PROJECT_REF \
  SECRET_SUPABASE_URL SECRET_SUPABASE_SERVICE_KEY || exit 2

PROJECT_ID="$(milo_op GCP_PROJECT_ID)"
REGION="$(milo_op GCP_REGION)"
CAPTURE_JOB="$(milo_op CLOUD_RUN_CAPTURE_JOB)"
PROJECT_REF="$(milo_op SUPABASE_PROJECT_REF)"
CAPTURE_SA="$(milo_op CAPTURE_SERVICE_ACCOUNT)"
[[ -n "$CAPTURE_SA" ]] || CAPTURE_SA="$(milo_op WORKER_SERVICE_ACCOUNT)"
RELEASE_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/$(milo_op ARTIFACT_REGISTRY_REPOSITORY)/${MILO_WORKER_IMAGE_REPO}:${RELEASE_SHA}"

# One whole number of seconds, minutes or hours: the wait for an execution is
# bounded by it, so a value this script cannot read is refused, never guessed.
if [[ ! "$TASK_TIMEOUT" =~ ^[0-9]{1,6}[smh]?$ ]]; then
  fail "--task-timeout must be a whole number of seconds, minutes or hours (for example 3600s, 60m, 1h)" 2
fi

if [[ -n "$IDEMPOTENCY_KEY" && ! "$IDEMPOTENCY_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$ ]]; then
  fail "--idempotency-key must be 8-128 characters of letters, digits, '.', '_', ':' or '-'" 2
fi

# The release worker image must EXIST before the capture job is created or
# executed. The job runs `${WORKER_IMAGE}`, tagged with the checked-out commit,
# and that tag is pushed only by `DEPLOY_MODE=apply scripts/deploy/cloud-run.sh`.
# Creating the job first would point it at an image that is not there, and
# executing it would fail on the pull -- or, for a job ensured by an earlier
# release, silently run THAT release's code. Both are refused here instead.
require_worker_image() {
  if ! gcloud artifacts docker images describe "$WORKER_IMAGE" --project "$PROJECT_ID" \
       > /dev/null 2>&1; then
    fail "the release worker image ${WORKER_IMAGE} does not exist (or cannot be read) in Artifact Registry. Build and deploy this commit first: DEPLOY_MODE=apply scripts/deploy/cloud-run.sh (or scripts/deploy/production-activate.sh --deploy). Nothing was created or executed." 1
  fi
  printf 'Worker image present: %s\n' "$WORKER_IMAGE"
}

# The capture job must run EXACTLY the release image before it is executed.
require_job_on_release_image() {
  local current
  current="$(gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
    --format='value(spec.template.spec.template.spec.containers[0].image)' 2> /dev/null || true)"
  if [[ "$current" != "$WORKER_IMAGE" ]]; then
    fail "the capture job ${CAPTURE_JOB} runs '${current:-<missing>}', not the release image ${WORKER_IMAGE}. Re-run with --ensure-job first. Nothing was executed." 1
  fi
}

if [[ "$MODE" != "plan" && -z "$CATALOG_EXECUTION_VALUE" ]]; then
  fail "--enable-catalog-execution is required for ${MODE}. The capture cannot construct anything without the catalog master switch, and this repository stores no enabled value for it by design." 2
fi

# The capture job's environment. The master switch is assembled from the
# operator's explicit argument; every other flag is pinned off by the shared
# deployment contract. No provider key appears anywhere in this list.
build_env_args() {
  local -a pairs=(
    "ENVIRONMENT=production"
    "GCP_PROJECT_ID=${PROJECT_ID}"
    "GCP_REGION=${REGION}"
    "MILO_RELEASE_SHA=${RELEASE_SHA}"
    "${MILO_SUPABASE_PROJECT_REF_ENV_NAME}=${PROJECT_REF}"
    "${MILO_CAPTURE_MASTER_FLAG_NAME}=${CATALOG_EXECUTION_VALUE:-false}"
  )
  pairs+=("${MILO_CAPTURE_PINNED_OFF_FLAGS[@]}")
  local joined
  joined="$(IFS="$MILO_ENV_VAR_DELIMITER"; printf '%s' "${pairs[*]}")"
  printf '^%s^%s' "$MILO_ENV_VAR_DELIMITER" "$joined"
}

build_secret_args() {
  printf '%s=%s:%s,%s=%s:%s' \
    "SUPABASE_URL" "$(milo_op SECRET_SUPABASE_URL)" "$MILO_SECRET_VERSION" \
    "SUPABASE_SERVICE_ROLE_KEY" "$(milo_op SECRET_SUPABASE_SERVICE_KEY)" "$MILO_SECRET_VERSION"
}

ensure_job() {
  require_worker_image
  local verb="create"
  if gcloud run jobs describe "$CAPTURE_JOB" --region "$REGION" --project "$PROJECT_ID" \
       > /dev/null 2>&1; then
    verb="update"
  fi
  printf 'Capture job: %s (%s)\n' "$CAPTURE_JOB" "$verb"
  # --args is always written --args=VALUE: the value starts with "-m", and
  # gcloud's argument parser reads a separate "-m,..." token as a flag, so
  # `--args "-m,..."` fails with "argument --args: expected one argument"
  # before anything is created or executed (the stage-c/-d probe scripts
  # already use the = form for the same reason).
  # --max-retries 0: a capture that failed halfway must be inspected, not
  # silently re-attempted. The snapshot contract already makes a deliberate
  # re-run safe; an automatic one would hide the reason the first failed.
  gcloud run jobs "$verb" "$CAPTURE_JOB" \
    --image "$WORKER_IMAGE" \
    --region "$REGION" \
    --project "$PROJECT_ID" \
    --service-account "$CAPTURE_SA" \
    --command python \
    --args="-m,${MILO_CAPTURE_ENTRYPOINT_MODULE}" \
    --set-env-vars "$(build_env_args)" \
    --set-secrets "$(build_secret_args)" \
    --max-retries 0 \
    --task-timeout "$TASK_TIMEOUT" \
    --tasks 1
}

# A Cloud Run execution name: lowercase letters, digits and hyphens, starting
# with a letter and ending with a letter or digit. Nothing else is a name.
MILO_CAPTURE_EXECUTION_NAME_PATTERN='^[a-z]([-a-z0-9]{0,126}[a-z0-9])?$'

# Runs the job with the given entrypoint arguments and echoes the execution
# name once THAT execution has terminated.
#
# `gcloud run jobs execute --args` REPLACES the container arguments the job was
# defined with (`-m,${MILO_CAPTURE_ENTRYPOINT_MODULE}`, see ensure_job) rather
# than appending to them, so an execution that passed only the entrypoint's
# own arguments ran `python --prepare ...` -- not the entrypoint at all. Every
# execution therefore restates the module first. Any further arguments are
# per-execution overrides (the scoped mode's one --update-env-vars).
#
# The execution is started with --async, which answers with the execution the
# moment it EXISTS. It used to be started with --wait, which answers only when
# the execution SUCCEEDS: a failed execution printed no name at all, so the
# one document that says why it failed (2026-09-24, milo-catalog-capture-wwg5p)
# could not be read from here.
#
# Evidence, from the Google Cloud SDK 586.0.0 source
# (storage.googleapis.com/cloud-sdk-release/google-cloud-cli-linux-x86_64.tar.gz):
#   * lib/googlecloudsdk/command_lib/run/serverless_operations.py, RunJob():
#     after the Run API call, `if asyn: return ex` (lines 2164-2165) -- the
#     Execution resource, before any polling. With --wait it instead polls and,
#     on a failed execution, `raise serverless_exceptions.ExecutionFailedError`
#     (line 2196): nothing is returned, so nothing reaches stdout.
#   * lib/surface/run/jobs/execute.py, Run(): `e = operations.RunJob(...)`
#     (line 179) and `return e` (line 222); the command's default format is
#     'none' (line 98), which --format='value(metadata.name)' overrides, so the
#     returned resource's name is printed on STDOUT. The "started
#     asynchronously" and console-link messages go through pretty_print /
#     log.status, i.e. STDERR. The name is then waited on with
# `gcloud run jobs executions describe` until it states completion, within a
# bound (the task timeout plus a margin); an execution that cannot be seen to
# finish fails closed rather than being read half-way.
#
# The name is read from gcloud's STDOUT alone: `--format='value(metadata.name)'`
# is its machine-readable answer. Progress and advisory text (such as
# "Or visit https://console.cloud.google.com/...") goes to stderr, which
# reaches the operator's terminal and is never read as the name. Anything but
# exactly one well-formed name -- nothing, several lines, prose -- fails closed:
# Cloud Logging is only ever read for an execution named exactly.
MILO_CAPTURE_POLL_SECONDS="${MILO_CAPTURE_POLL_SECONDS:-15}"
MILO_CAPTURE_WAIT_MARGIN_SECONDS="${MILO_CAPTURE_WAIT_MARGIN_SECONDS:-600}"

# Seconds, from a Cloud Run duration such as 3600s, 60m, 1h or 3600 (the
# shape is validated where --task-timeout is read). Base 10 explicitly, so a
# leading zero is never read as octal.
duration_seconds() {
  local value="$1"
  case "$value" in
    *h) printf '%s' "$(( 10#${value%h} * 3600 ))" ;;
    *m) printf '%s' "$(( 10#${value%m} * 60 ))" ;;
    *s) printf '%s' "$(( 10#${value%s} ))" ;;
    *) printf '%s' "$(( 10#${value} ))" ;;
  esac
}

# The state of one execution from its `describe --format=json` document:
# succeeded, failed or running. Terminal only when Cloud Run says so.
execution_state() {
  python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    print("running"); sys.exit(0)
status = doc.get("status") or {}
conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []
              if isinstance(c, dict)}
if not status.get("completionTime") and conditions.get("Completed") not in ("True", "False"):
    print("running")
elif conditions.get("Completed") == "True" or (status.get("succeededCount") or 0) >= 1:
    print("succeeded")
else:
    print("failed")
'
}

execute_job() {
  local args_csv="$1" execution="" gcloud_status=0
  shift
  require_job_on_release_image
  execution="$(gcloud run jobs execute "$CAPTURE_JOB" \
    --region "$REGION" --project "$PROJECT_ID" \
    --args="-m,${MILO_CAPTURE_ENTRYPOINT_MODULE},${args_csv}" "$@" \
    --async --format='value(metadata.name)')" || gcloud_status=$?
  if [[ ! "$execution" =~ $MILO_CAPTURE_EXECUTION_NAME_PATTERN ]]; then
    fail "gcloud run jobs execute (exit ${gcloud_status}) printed no single well-formed execution name on stdout; no Cloud Logging read is attempted for an execution that cannot be named exactly. An execution MAY nevertheless have been created and be RUNNING UNATTENDED -- nothing here waits for it or reads its outcome. Check before doing anything else: gcloud run jobs executions list --job ${CAPTURE_JOB} --region ${REGION} --project ${PROJECT_ID}"
  fi
  if (( gcloud_status != 0 )); then
    printf 'WARN: gcloud run jobs execute exited %s for execution %s; its own document states the outcome.\n' \
      "$gcloud_status" "$execution" >&2
  fi
  local deadline waited=0 state="running"
  deadline=$(( $(duration_seconds "$TASK_TIMEOUT") + MILO_CAPTURE_WAIT_MARGIN_SECONDS ))
  while :; do
    state="$(gcloud run jobs executions describe "$execution" \
      --region "$REGION" --project "$PROJECT_ID" --format=json 2> /dev/null \
      | execution_state)" || state="running"
    [[ "$state" == "running" ]] || break
    if (( waited >= deadline )); then
      fail "execution ${execution} did not verifiably finish within ${deadline}s; its document is not read half-way. Inspect it with: gcloud run jobs executions describe ${execution} --region ${REGION} --project ${PROJECT_ID}"
    fi
    sleep "$MILO_CAPTURE_POLL_SECONDS"
    waited=$(( waited + MILO_CAPTURE_POLL_SECONDS ))
  done
  if [[ "$state" != "succeeded" ]]; then
    printf 'WARN: execution %s finished %s; its own document states the outcome.\n' \
      "$execution" "$state" >&2
  fi
  printf '%s' "$execution"
}

# The entrypoint prints exactly one JSON document to stdout, which Cloud Run
# sends to Cloud Logging. Reading it back is how the operator learns the run
# id without the capture having to write anywhere else.
#
# Cloud Logging ingests with a delay, so a document not yet visible is read
# again, a bounded number of times, for the same exact execution name.
MILO_CAPTURE_LOG_RETRIES="${MILO_CAPTURE_LOG_RETRIES:-6}"
MILO_CAPTURE_LOG_RETRY_SECONDS="${MILO_CAPTURE_LOG_RETRY_SECONDS:-10}"
execution_document() {
  local execution="$1" document="" attempt=0
  while :; do
    document="$(gcloud logging read \
      "resource.type=cloud_run_job AND labels.\"run.googleapis.com/execution_name\"=${execution}" \
      --project "$PROJECT_ID" --format='value(textPayload)' --limit 400 2> /dev/null \
      | tac)" || document=""
    if printf '%s' "$document" | json_field entrypoint > /dev/null 2>&1; then
      break
    fi
    attempt=$(( attempt + 1 ))
    (( attempt < MILO_CAPTURE_LOG_RETRIES )) || break
    sleep "$MILO_CAPTURE_LOG_RETRY_SECONDS"
  done
  printf '%s\n' "$document"
}

json_field() {
  python3 -c '
import json,sys
raw = sys.stdin.read()
start = raw.find("{")
if start < 0:
    sys.exit(1)
try:
    doc = json.loads(raw[start:])
except Exception:
    sys.exit(1)
node = doc
for key in sys.argv[1].split("."):
    if not isinstance(node, dict) or key not in node:
        sys.exit(1)
    node = node[key]
print(node)
' "$1"
}

# The ingestion measurements of a capture document, one line per phase, so the
# operator can check the cost directly: database calls, wall time, rows
# inserted vs already present, and the adoption (if any). Counts, seconds and
# run ids only -- the document carries nothing else in these fields.
print_ingestion_metrics() {
  python3 -c '
import json, sys
raw = sys.stdin.read()
start = raw.find("{")
try:
    doc = json.loads(raw[start:]) if start >= 0 else {}
except Exception:
    sys.exit(0)
entries = []
snapshot = ((doc.get("capture") or {}).get("snapshot") or {})
if isinstance(snapshot.get("ingestion"), dict):
    entries.append(("register", snapshot["ingestion"]))
for unit in (doc.get("work_scope") or {}).get("units") or []:
    if isinstance(unit, dict) and isinstance(unit.get("ingestion"), dict) \
            and unit["ingestion"].get("total_calls"):
        entries.append((str(unit.get("unit_key")), unit["ingestion"]))
calls = seconds = sent = 0
slowest = 0.0
for name, metrics in entries:
    for phase in ("snapshot", "raw", "candidates", "activate"):
        entry = metrics.get(phase) or {}
        rows = ""
        if "inserted" in entry:
            rows = " inserted=%s already_present=%s" % (entry.get("inserted"), entry.get("already_present"))
        print("INGESTION unit=%s phase=%s calls=%s seconds=%s request_bytes=%s max_call_seconds=%s%s"
              % (name, phase, entry.get("calls"), entry.get("seconds"),
                 entry.get("request_bytes"), entry.get("max_call_seconds"), rows))
    print("INGESTION unit=%s adoption_seq=%s previous_writer_run_id=%s"
          % (name, metrics.get("adoption_seq"), metrics.get("previous_writer_run_id") or "none"))
    calls += int(metrics.get("total_calls") or 0)
    seconds += float(metrics.get("total_seconds") or 0)
    sent += int(metrics.get("total_request_bytes") or 0)
    slowest = max(slowest, float(metrics.get("max_call_seconds") or 0))
if entries:
    print("INGESTION_TOTAL_DB_CALLS=%d" % calls)
    print("INGESTION_TOTAL_SECONDS=%.3f" % seconds)
    print("INGESTION_TOTAL_REQUEST_BYTES=%d" % sent)
    # The 8 s statement / lock timeout PostgREST runs under applies to ONE call.
    print("INGESTION_MAX_CALL_SECONDS=%.3f (statement timeout 8 s)" % slowest)
'
}

# A snapshot key, exactly the database's own shape (the
# `catalog_source_snapshots_key_shape` constraint, 20260915120000).
MILO_CAPTURE_SNAPSHOT_KEY_PATTERN='^cs1\.[0-9a-f]{32}$'

# The snapshot a successful whole capture names as the register's active one,
# read by do_capture from THAT execution's own document. It is the only
# snapshot verify_snapshot checks: since scoped catalog PR2 the newest
# Government snapshot may be a scoped manufacturer capture, so "the latest
# snapshot" says nothing about what this capture landed.
CAPTURED_SNAPSHOT_KEY=""

verify_snapshot() {
  local key="${1:-}"
  if [[ ! "$key" =~ $MILO_CAPTURE_SNAPSHOT_KEY_PATTERN ]]; then
    printf 'FAIL: there is no valid captured snapshot key to verify; no other snapshot is verified in its place.\n' >&2
    return 1
  fi
  local db_env db_url
  db_env="$(milo_op READONLY_DATABASE_URL_ENV)"
  db_url="${!db_env:-}"
  if [[ -z "$db_url" ]] || ! command -v psql > /dev/null 2>&1; then
    printf 'MANUAL: set $%s and install psql to verify snapshot %s here.\n' "${db_env:-READONLY_DATABASE_URL_ENV}" "$key"
    printf '        Otherwise verify THAT snapshot by its key: whole register (no capture_scope), complete,\n'
    printf '        active, stored = declared, with candidates. The newest snapshot proves nothing.\n'
    return 0
  fi
  # ONE snapshot, named exactly. The key travels as a psql variable and is
  # quoted by psql (`:'snapshot_key'`); it has been shape-checked above too.
  local row
  if ! row="$(psql "$db_url" -X -At -F'|' -v ON_ERROR_STOP=1 -v snapshot_key="$key" 2> /dev/null <<'SQL'
select s.id, s.snapshot_key, s.resource_id, s.validation_state,
       (s.activated_at is not null) as active,
       (s.retrieval_metadata ? 'capture_scope') as scoped,
       s.declared_record_count, s.stored_record_count,
       (select count(*) from public.catalog_raw_records r where r.snapshot_id = s.id),
       (select count(*) from public.catalog_candidate_variants v where v.snapshot_id = s.id)
  from public.catalog_source_snapshots s
 where s.source_family = 'government'
   and s.snapshot_key = :'snapshot_key';
SQL
  )"; then
    printf 'FAIL: snapshot %s could not be read from the database.\n' "$key" >&2
    return 1
  fi
  if [[ -z "$row" ]]; then
    printf 'FAIL: the captured snapshot %s does not exist.\n' "$key" >&2
    return 1
  fi
  if [[ "$row" == *$'\n'* ]]; then
    printf 'FAIL: more than one row answered for snapshot %s.\n' "$key" >&2
    return 1
  fi
  local sid skey resource vstate active scoped declared stored raws cands
  IFS='|' read -r sid skey resource vstate active scoped declared stored raws cands <<< "$row"
  printf '\nGOVERNMENT_SNAPSHOT_ID=%s\n' "$sid"
  printf 'GOVERNMENT_SNAPSHOT_KEY=%s\n' "$skey"
  printf 'GOVERNMENT_SNAPSHOT_ACTIVE=%s\n' "$active"
  printf 'GOVERNMENT_VALIDATION_STATE=%s\n' "$vstate"
  printf 'GOVERNMENT_DECLARED_RECORDS=%s\n' "$declared"
  printf 'GOVERNMENT_STORED_RECORDS=%s\n' "$stored"
  printf 'GOVERNMENT_RAW_RECORD_COUNT=%s\n' "$raws"
  printf 'GOVERNMENT_CANDIDATE_COUNT=%s\n' "$cands"
  if [[ "$skey" != "$key" ]]; then
    printf 'FAIL: the database answered for %s, not for the captured snapshot %s.\n' "$skey" "$key" >&2
    return 1
  fi
  if [[ "$scoped" != "f" ]]; then
    printf 'FAIL: snapshot %s is a scoped manufacturer capture, not the whole register.\n' "$key" >&2
    return 1
  fi
  if [[ "$resource" != "$MILO_CAPTURE_RESOURCE_ID" ]]; then
    printf 'FAIL: snapshot %s belongs to resource %s, not to the pinned %s.\n' "$key" "$resource" "$MILO_CAPTURE_RESOURCE_ID" >&2
    return 1
  fi
  if [[ "$vstate" != "complete" ]]; then
    printf 'FAIL: snapshot %s is not complete (validation_state=%s).\n' "$key" "$vstate" >&2
    return 1
  fi
  if [[ "$active" != "t" ]]; then
    printf 'FAIL: snapshot exists but is not active; the activation gate did not pass.\n' >&2
    return 1
  fi
  if [[ "$declared" != "$stored" ]]; then
    printf 'FAIL: stored (%s) != declared (%s); a prefix is never activated.\n' "$stored" "$declared" >&2
    return 1
  fi
  if [[ "${cands:-0}" -le 0 ]]; then
    printf 'FAIL: snapshot is active but has no candidate variants, so no deterministic queue can form.\n' >&2
    return 1
  fi
  printf 'USABLE_GOVERNMENT_SNAPSHOT=YES\nDETERMINISTIC_QUEUE_READY=YES\n'
  return 0
}

[[ -n "$IDEMPOTENCY_KEY" ]] || IDEMPOTENCY_KEY="$(milo_op CAPTURE_IDEMPOTENCY_KEY)"
PREPARE_ARGS="--prepare,--acknowledge-schema-report-reviewed,${MILO_CAPTURE_SCHEMA_ACK},--project-ref,${PROJECT_REF},--conversation-id,$(milo_op CAPTURE_CONVERSATION_ID),--requested-by,$(milo_op CAPTURE_REQUESTED_BY),--idempotency-key,${IDEMPOTENCY_KEY}"
capture_args() {
  printf -- '--execute,--acknowledge-live-government-egress,%s,--acknowledge-schema-report-reviewed,%s,--project-ref,%s,--run-id,%s,--package-id,%s,--resource-id,%s,--page-limit,%s,--max-pages,%s,--max-records,%s' \
    "$MILO_CAPTURE_EGRESS_ACK" "$MILO_CAPTURE_SCHEMA_ACK" "$PROJECT_REF" "$1" \
    "$MILO_CAPTURE_PACKAGE_ID" "$MILO_CAPTURE_RESOURCE_ID" \
    "$MILO_CAPTURE_PAGE_LIMIT" "$MILO_CAPTURE_MAX_PAGES" "$MILO_CAPTURE_MAX_RECORDS"
}

# The scoped mode's arguments: the capture's own, plus the plan. Validated to
# the entrypoint's exact shapes first -- which also keeps them comma-free,
# because gcloud splits --args on commas.
work_scope_args() {
  printf -- '%s,--work-scope-id,%s,--work-scope-revision,%s,--work-scope-digest,%s' \
    "$(capture_args "$1")" "$WORK_SCOPE_ID" "$WORK_SCOPE_REVISION" "$WORK_SCOPE_DIGEST"
}

do_prepare_work_scope() {
  [[ -n "$RUN_ID" ]] || fail "--prepare-work-scope requires --run-id (a run made with --prepare)" 2
  [[ "$WORK_SCOPE_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] \
    || fail "--work-scope-id must be the plan's lowercase UUID" 2
  [[ "$WORK_SCOPE_REVISION" =~ ^[1-9][0-9]{0,8}$ ]] \
    || fail "--work-scope-revision must be a whole revision number" 2
  [[ "$WORK_SCOPE_DIGEST" =~ ^[0-9a-f]{64}$ ]] \
    || fail "--work-scope-digest must be the revision's 64-character digest" 2
  [[ -n "$WORK_SCOPE_PREPARATION_VALUE" ]] \
    || fail "--enable-work-scope-preparation is required for --prepare-work-scope. The job keeps the scoped-preparation switch pinned off; this execution alone turns it on." 2
  printf '\n== prepare work scope ==\n'
  local execution document status
  execution="$(execute_job "$(work_scope_args "$RUN_ID")" \
    --update-env-vars "${MILO_WORK_SCOPE_PREPARATION_FLAG_NAME}=${WORK_SCOPE_PREPARATION_VALUE}")"
  printf 'Execution: %s\n' "$execution"
  document="$(execution_document "$execution")"
  status="$(printf '%s' "$document" | json_field status || true)"
  printf 'WORK_SCOPE_PREPARATION_STATUS=%s\n' "${status:-unknown}"
  printf '%s' "$document" | print_ingestion_metrics
  if [[ "$status" != "succeeded" ]]; then
    printf '%s\n' "$document" >&2
    report_egress_stop "$document"
    fail "work-scope preparation did not succeed; the document above states the outcome"
  fi
  printf 'WORK_SCOPE_QUEUED_ITEMS=%s\n' \
    "$(printf '%s' "$document" | json_field work_scope.queued_item_count || true)"
  printf 'WORK_SCOPE_BATCHES=%s\n' \
    "$(printf '%s' "$document" | json_field work_scope.batch_count || true)"
  verify_work_scope
}

# The prepared revision, verified by the one scoped readiness check -- the same
# one production-verify.sh and the Stage 2 gate run -- never by "a snapshot
# exists". With no read-only database URL it says so and stops short of READY.
verify_work_scope() {
  local status=0
  printf '\n== verify the prepared revision (read-only) ==\n'
  bash "${REPO_ROOT}/scripts/deploy/work-scope-readiness.sh" --operator-config "$CONFIG_PATH" \
    --work-scope-id "$WORK_SCOPE_ID" --work-scope-revision "$WORK_SCOPE_REVISION" \
    --work-scope-digest "$WORK_SCOPE_DIGEST" || status=$?
  case "$status" in
    0) printf 'WORK_SCOPE_PREPARED_AND_VERIFIED=YES\n' ;;
    3) printf 'MANUAL: the preparation succeeded but readiness is UNVERIFIED from here (above).\n'
       printf '        Re-run scripts/deploy/work-scope-readiness.sh with a read-only database URL.\n' ;;
    *) fail "the preparation reported success but the revision is not ready (above)" ;;
  esac
}

# A capture that could not REACH the Government source is a stop, not a retry
# with other data. This repository has no alternate production import route:
# scripts/r5_capture_fixtures.py import-capture writes TEST fixtures only and
# must never be used to land production evidence.
report_egress_stop() {
  local reason
  reason="$(printf '%s' "$1" | json_field reason_code || true)"
  case "$reason" in
    GOV_TRANSPORT_FAILED | GOV_HTTP_STATUS_UNEXPECTED | GOV_REDIRECTED_OFF_HOST | GOV_RESPONSE_NOT_JSON)
      printf '\nSTOP: GOVERNMENT_SOURCE_UNREACHABLE (%s).\n' "$reason" >&2
      printf '      The capture job could not read data.gov.il from Cloud Run. Record this\n' >&2
      printf '      execution name and reason. Do NOT substitute fixture, cached or hand-built\n' >&2
      printf '      evidence: no supported alternate import route exists. Resolve the egress\n' >&2
      printf '      path (an approved network change, reviewed separately) and re-run.\n' >&2
      ;;
  esac
}

do_prepare() {
  milo_require_op CAPTURE_CONVERSATION_ID CAPTURE_REQUESTED_BY || exit 2
  [[ -n "$IDEMPOTENCY_KEY" ]] || fail "no idempotency key: set CAPTURE_IDEMPOTENCY_KEY or pass --idempotency-key" 2
  printf '\n== prepare ==\n'
  local execution document prepared
  execution="$(execute_job "$PREPARE_ARGS")"
  printf 'Execution: %s\n' "$execution"
  document="$(execution_document "$execution")"
  prepared="$(printf '%s' "$document" | json_field preparation.run_id || true)"
  if [[ -z "$prepared" ]]; then
    printf '%s\n' "$document" >&2
    fail "preparation did not report a run id; the document above states the refusal"
  fi
  printf 'PREPARED_RUN_ID=%s\n' "$prepared"
  RUN_ID="$prepared"
}

do_capture() {
  [[ -n "$RUN_ID" ]] || fail "--capture requires --run-id (or use --all)" 2
  printf '\n== capture ==\n'
  local execution document status
  execution="$(execute_job "$(capture_args "$RUN_ID")")"
  printf 'Execution: %s\n' "$execution"
  document="$(execution_document "$execution")"
  status="$(printf '%s' "$document" | json_field status || true)"
  printf 'CAPTURE_STATUS=%s\n' "${status:-unknown}"
  printf '%s' "$document" | print_ingestion_metrics
  # The entrypoint's own contract: a capture that did its work reports
  # `succeeded` (operator_capture's envelope). Nothing else is success.
  if [[ "$status" != "succeeded" ]]; then
    printf '%s\n' "$document" >&2
    report_egress_stop "$document"
    fail "capture did not succeed; the document above states the outcome"
  fi
  # The snapshot THIS capture names as the register's active one. It is the
  # one verify_snapshot checks, so a document that names none fails here.
  CAPTURED_SNAPSHOT_KEY="$(printf '%s' "$document" | json_field capture.active_snapshot_key || true)"
  if [[ ! "$CAPTURED_SNAPSHOT_KEY" =~ $MILO_CAPTURE_SNAPSHOT_KEY_PATTERN ]]; then
    printf '%s\n' "$document" >&2
    fail "the capture document names no valid capture.active_snapshot_key; no other snapshot is verified in its place"
  fi
  printf 'CAPTURED_SNAPSHOT_KEY=%s\n' "$CAPTURED_SNAPSHOT_KEY"
}

case "$MODE" in
  plan)
    printf 'PLAN — read-only. Nothing below has been executed.\n\n'
    printf 'Project:      %s\nRegion:       %s\nCapture job:  %s\n' "$PROJECT_ID" "$REGION" "$CAPTURE_JOB"
    printf 'Image:        %s\nIdentity:     %s\n' "$WORKER_IMAGE" "$CAPTURE_SA"
    printf 'Entrypoint:   python -m %s\n' "$MILO_CAPTURE_ENTRYPOINT_MODULE"
    printf 'Secrets:      %s\n' "$(build_secret_args)"
    printf 'Env:          %s\n\n' "$(build_env_args)"
    printf 'Upstream (pinned by backend/catalog/government/source.py):\n'
    printf '  package  %s\n  resource %s\n  bounds   page-limit=%s max-pages=%s max-records=%s\n\n' \
      "$MILO_CAPTURE_PACKAGE_ID" "$MILO_CAPTURE_RESOURCE_ID" \
      "$MILO_CAPTURE_PAGE_LIMIT" "$MILO_CAPTURE_MAX_PAGES" "$MILO_CAPTURE_MAX_RECORDS"
    printf 'To execute:\n  %s --all --enable-catalog-execution\n' "$0"
    ;;
  ensure-job) ensure_job ;;
  prepare) do_prepare ;;
  capture) do_capture; verify_snapshot "$CAPTURED_SNAPSHOT_KEY" ;;
  prepare-work-scope) do_prepare_work_scope ;;
  all)
    ensure_job
    do_prepare
    do_capture
    verify_snapshot "$CAPTURED_SNAPSHOT_KEY"
    ;;
  *) fail "unknown mode ${MODE}" 2 ;;
esac
