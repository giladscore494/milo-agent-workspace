#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Image paths, Stage A posture and required bindings are shared with
# scripts/release/generate-deployment-plan.sh so the two tools cannot drift.
# shellcheck source=deployment-contract.sh
source "$SCRIPT_DIR/deployment-contract.sh"

PROJECT_ID=${PROJECT_ID:-big-cabinet-457321-t7}
REGION=${REGION:-us-central1}
REPOSITORY=${REPOSITORY:-milo-agent}
API_SERVICE_ACCOUNT=${API_SERVICE_ACCOUNT:-milo-api-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}
WORKER_SERVICE_ACCOUNT=${WORKER_SERVICE_ACCOUNT:-milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com}
DEPLOY_MODE=${DEPLOY_MODE:-check}
JOB_LAUNCHER_MODE=${JOB_LAUNCHER_MODE:-disabled}
API_SERVICE=${API_SERVICE:-milo-agent-api}
WORKER_JOB=${WORKER_JOB:-milo-agent-worker}
# Immutable image identity: the FULL 40-character commit SHA. Short SHAs are
# ambiguous (they can collide and they can be extended by future objects), so
# they are never used as a production image tag.
RELEASE_SHA=$(git rev-parse HEAD)
API_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$MILO_API_IMAGE_REPO:$RELEASE_SHA"
WORKER_IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPOSITORY/$MILO_WORKER_IMAGE_REPO:$RELEASE_SHA"
REQUIRED_APIS=(run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com)
# Secret Manager resources this Stage A deployment binds. No provider key
# appears here: Stage A calls no provider, so no provider credential is
# reachable from either runtime (see MILO_PROVIDER_KEY_ENV_NAMES).
REQUIRED_SECRETS=(SUPABASE_URL SUPABASE_SECRET_KEY UPSTASH_REDIS_REST_URL UPSTASH_REDIS_REST_TOKEN)
ENV_VAR_DELIMITER="$MILO_ENV_VAR_DELIMITER"

# Stage A execution flags come from the shared deployment contract.
STAGE_A_EXECUTION_FLAGS=("${MILO_STAGE_A_EXECUTION_FLAGS[@]}")
STAGE_A_FLAG_NAMES=("${MILO_STAGE_A_FLAG_NAMES[@]}")

# Gateway identity configuration. Required in production at EVERY stage,
# including execution-disabled Stage A: without it backend/production_config.py
# refuses to start (GATEWAY_AUTH_MISSING) because the API would otherwise trust
# bare browser identity headers on its read-only routes. The deployment sets
# approved values explicitly rather than relying on whatever the service
# happens to carry already.
MILO_GATEWAY_AUDIENCE=${MILO_GATEWAY_AUDIENCE:-}
MILO_APPROVED_GATEWAY_IDENTITIES=${MILO_APPROVED_GATEWAY_IDENTITIES:-}

# Environment variables this release owns. Anything else already configured on
# the service or job is preserved: the deploy uses --update-env-vars /
# --update-secrets, never the destructive --set-* variants.
API_ENV_VARS=(
  "ENVIRONMENT=production"
  "JOB_LAUNCHER=$JOB_LAUNCHER_MODE"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "CLOUD_RUN_WORKER_JOB=$WORKER_JOB"
  "ALLOWED_CORS_ORIGINS=${ALLOWED_CORS_ORIGINS:-}"
  "MILO_GATEWAY_AUDIENCE=$MILO_GATEWAY_AUDIENCE"
  "MILO_APPROVED_GATEWAY_IDENTITIES=$MILO_APPROVED_GATEWAY_IDENTITIES"
  "${STAGE_A_EXECUTION_FLAGS[@]}"
)
WORKER_ENV_VARS=(
  "ENVIRONMENT=production"
  "GCP_PROJECT_ID=$PROJECT_ID"
  "GCP_REGION=$REGION"
  "${STAGE_A_EXECUTION_FLAGS[@]}"
)
# Stage A binds NO provider key — not to the API, not to the worker. The API
# never calls a provider; the worker does not call one either until Stage C
# introduces the key deliberately, alongside the budget caps and the rehearsed
# kill switch. Both runtimes carry the same Supabase and Upstash bindings.
API_SECRETS=(
  "SUPABASE_URL=SUPABASE_URL:$MILO_SECRET_VERSION"
  "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:$MILO_SECRET_VERSION"
  "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:$MILO_SECRET_VERSION"
  "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:$MILO_SECRET_VERSION"
)
WORKER_SECRETS=(
  "SUPABASE_URL=SUPABASE_URL:$MILO_SECRET_VERSION"
  "SUPABASE_SERVICE_ROLE_KEY=SUPABASE_SECRET_KEY:$MILO_SECRET_VERSION"
  "UPSTASH_REDIS_REST_URL=UPSTASH_REDIS_REST_URL:$MILO_SECRET_VERSION"
  "UPSTASH_REDIS_REST_TOKEN=UPSTASH_REDIS_REST_TOKEN:$MILO_SECRET_VERSION"
)

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found."
}

join_by() {
  local sep="$1"
  shift
  local out="" item
  for item in "$@"; do
    if [[ -z "$out" ]]; then
      out="$item"
    else
      out="$out$sep$item"
    fi
  done
  printf '%s' "$out"
}

# gcloud dict flags accept an alternate delimiter via the ^DELIM^ prefix, which
# keeps comma-containing values (ALLOWED_CORS_ORIGINS) intact.
delimited_env_arg() {
  printf '^%s^%s' "$ENV_VAR_DELIMITER" "$(join_by "$ENV_VAR_DELIMITER" "$@")"
}

require_full_release_sha() {
  if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    fail "Release SHA '$RELEASE_SHA' is not a full 40-character commit SHA. Image tags must be immutable full SHAs."
  fi
}

require_allowed_cors_origins() {
  if [[ -z "${ALLOWED_CORS_ORIGINS:-}" ]]; then
    fail "ALLOWED_CORS_ORIGINS must be set to one or more explicit origins before deployment. Do not use '*'."
  fi
  IFS=',' read -ra origins <<<"$ALLOWED_CORS_ORIGINS"
  for origin in "${origins[@]}"; do
    origin="${origin//[[:space:]]/}"
    if [[ -z "$origin" ]]; then
      fail "ALLOWED_CORS_ORIGINS contains an empty origin."
    fi
    if [[ "$origin" == "*" ]]; then
      fail "ALLOWED_CORS_ORIGINS must not contain '*'. Use explicit origins only."
    fi
    if [[ "$origin" == *"$ENV_VAR_DELIMITER"* ]]; then
      fail "ALLOWED_CORS_ORIGINS must not contain the gcloud env-var delimiter '$ENV_VAR_DELIMITER'."
    fi
  done
}

# Stage A requires a verified gateway identity even though execution is off:
# the API's read-only routes must never trust a bare browser header.
require_gateway_identity_config() {
  if [[ -z "${MILO_GATEWAY_AUDIENCE:-}" ]]; then
    fail "MILO_GATEWAY_AUDIENCE must be set to the approved gateway audience before deployment. Stage A is execution-disabled but still requires verified gateway identity."
  fi
  if [[ "$MILO_GATEWAY_AUDIENCE" == "*" || "$MILO_GATEWAY_AUDIENCE" == *"$ENV_VAR_DELIMITER"* ]]; then
    fail "MILO_GATEWAY_AUDIENCE must be a single explicit audience without '*' or the gcloud env-var delimiter '$ENV_VAR_DELIMITER'."
  fi
  if [[ -z "${MILO_APPROVED_GATEWAY_IDENTITIES:-}" ]]; then
    fail "MILO_APPROVED_GATEWAY_IDENTITIES must list the approved gateway service account(s) before deployment. An empty allowlist would accept no verified caller and leave browser headers as the only identity."
  fi
  local identity identities
  IFS=',' read -ra identities <<<"$MILO_APPROVED_GATEWAY_IDENTITIES"
  for identity in "${identities[@]}"; do
    identity="${identity//[[:space:]]/}"
    [[ -n "$identity" ]] || fail "MILO_APPROVED_GATEWAY_IDENTITIES contains an empty entry."
    [[ "$identity" != "*" ]] || fail "MILO_APPROVED_GATEWAY_IDENTITIES must not contain '*'. List explicit service account identities only."
    [[ "$identity" == *"@"*"."* ]] || fail "MILO_APPROVED_GATEWAY_IDENTITIES entry '$identity' is not a service account email."
    [[ "$identity" != *"$ENV_VAR_DELIMITER"* ]] || \
      fail "MILO_APPROVED_GATEWAY_IDENTITIES must not contain the gcloud env-var delimiter '$ENV_VAR_DELIMITER'."
  done
}

# Stage A binds no provider key anywhere. This guards the binding arrays
# themselves, so a future edit that reintroduces a provider key fails before
# anything is built rather than after it is deployed.
require_no_provider_key_bindings() {
  local binding name
  for binding in "${API_SECRETS[@]}" "${WORKER_SECRETS[@]}" "${API_ENV_VARS[@]}" "${WORKER_ENV_VARS[@]}"; do
    for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
      if [[ "${binding%%=*}" == "$name" ]]; then
        fail "Provider key '$name' must not be bound during Stage A. Provider keys are introduced only by an explicit Stage C operator action (docs/production-readiness/STAGED_ACTIVATION.md)."
      fi
    done
  done
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    if milo_contains "$name" "${REQUIRED_SECRETS[@]}"; then
      fail "Provider key secret '$name' must not be a Stage A prerequisite; Stage A performs no provider call."
    fi
  done
}

preflight() {
  require_command git
  require_command gcloud
  require_full_release_sha
  require_allowed_cors_origins
  require_gateway_identity_config
  require_no_provider_key_bindings

  local account
  account=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n 1 || true)
  [[ -n "$account" ]] || fail "No active gcloud account found. Run 'gcloud auth login' with the intended operator identity."

  gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null || \
    fail "Project '$PROJECT_ID' is not accessible to the active gcloud account."

  for api in "${REQUIRED_APIS[@]}"; do
    local state
    state=$(gcloud services list --enabled --project "$PROJECT_ID" --filter="config.name:$api" --format='value(config.name)' 2>/dev/null || true)
    [[ "$state" == "$api" ]] || fail "Required API '$api' is not enabled for project '$PROJECT_ID'."
  done

  if [[ "$API_SERVICE_ACCOUNT" == "$WORKER_SERVICE_ACCOUNT" ]]; then
    fail "API_SERVICE_ACCOUNT and WORKER_SERVICE_ACCOUNT must be distinct identities."
  fi

  gcloud iam service-accounts describe "$API_SERVICE_ACCOUNT" --project "$PROJECT_ID" --format='value(email)' >/dev/null || \
    fail "API runtime service account '$API_SERVICE_ACCOUNT' does not exist or is not accessible."

  gcloud iam service-accounts describe "$WORKER_SERVICE_ACCOUNT" --project "$PROJECT_ID" --format='value(email)' >/dev/null || \
    fail "Worker runtime service account '$WORKER_SERVICE_ACCOUNT' does not exist or is not accessible."

  gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" --project "$PROJECT_ID" >/dev/null || \
    fail "Artifact Registry repository '$REPOSITORY' does not exist in region '$REGION'. Create it before deployment."

  for secret in "${REQUIRED_SECRETS[@]}"; do
    gcloud secrets describe "$secret" --project "$PROJECT_ID" --format='value(name)' >/dev/null || \
      fail "Required Secret Manager secret '$secret' does not exist or is not accessible."
  done

  # Live configuration is inspected (read-only) with python3, in both modes.
  require_command python3

  # Last preflight gate: the LIVE resources must not already carry a provider
  # key. This runs after authentication and project access are proven, so an
  # inspection failure here is a real failure and not a missing credential.
  require_no_live_provider_key_bindings
}

print_targets() {
  cat <<TARGETS
Deployment mode: $DEPLOY_MODE
Job launcher mode: $JOB_LAUNCHER_MODE
Release SHA (full): $RELEASE_SHA
Project: $PROJECT_ID
Region: $REGION
Artifact Registry repository: $REPOSITORY
API service: $API_SERVICE
Worker job: $WORKER_JOB
API runtime service account: $API_SERVICE_ACCOUNT
Worker runtime service account: $WORKER_SERVICE_ACCOUNT
API image: $API_IMAGE
Worker image: $WORKER_IMAGE
Stage A execution flags: ${STAGE_A_FLAG_NAMES[*]} (all false)
Stage A provider keys: ${MILO_PROVIDER_KEY_ENV_NAMES[*]} bound to NOTHING (Stage C introduces them)
Gateway audience: $MILO_GATEWAY_AUDIENCE
Approved gateway identities: $MILO_APPROVED_GATEWAY_IDENTITIES
Env/secret update mode: --update-env-vars / --update-secrets (non-destructive)
Cloud Build configs:
  API: scripts/deploy/cloudbuild-api.yaml
  Worker: scripts/deploy/cloudbuild-worker.yaml
TARGETS
}

# ---------------------------------------------------------------------------
# read-only inspection helpers (names and references only; never values of
# secrets, and only the explicitly allowlisted non-secret flag values)
# ---------------------------------------------------------------------------
CONTAINER_REPORT_PY='
import json
import sys

allow_values = set(sys.argv[1:])
try:
    doc = json.load(sys.stdin)
except Exception:
    doc = {}


def find_containers(node):
    if isinstance(node, dict):
        value = node.get("containers")
        if isinstance(value, list) and value:
            return value
        for child in node.values():
            found = find_containers(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_containers(child)
            if found:
                return found
    return []


def find_service_account(node):
    if isinstance(node, dict):
        for key in ("serviceAccountName", "serviceAccount"):
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        for child in node.values():
            found = find_service_account(child)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = find_service_account(child)
            if found:
                return found
    return ""


spec = doc.get("spec", doc) if isinstance(doc, dict) else {}
containers = find_containers(spec)
container = containers[0] if containers else {}
print("image\t%s" % (container.get("image") or ""))
print("service-account\t%s" % find_service_account(spec))
for entry in container.get("env") or []:
    if not isinstance(entry, dict):
        continue
    name = entry.get("name") or ""
    ref = ((entry.get("valueFrom") or {}).get("secretKeyRef") or {})
    if ref:
        secret_name = ref.get("secret") or ref.get("name") or ""
        secret_version = ref.get("version") or ref.get("key") or ""
        print("secret\t%s\t%s:%s" % (name, secret_name, secret_version))
    else:
        print("env\t%s" % name)
        if name in allow_values:
            print("flag\t%s\t%s" % (name, entry.get("value") or ""))
'

describe_json() {
  local kind="$1" name="$2" out=""
  case "$kind" in
    service) out=$(gcloud run services describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>/dev/null || true) ;;
    job) out=$(gcloud run jobs describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>/dev/null || true) ;;
    *) fail "describe_json: unknown resource kind '$kind'." ;;
  esac
  [[ -n "$out" ]] || out='{}'
  printf '%s' "$out"
}

# binding_report KIND NAME — one record per line, tab separated:
#   image\t<image>          service-account\t<email>
#   env\t<NAME>             secret\t<ENV_NAME>\t<SECRET>:<VERSION>
#   flag\t<NAME>\t<VALUE>   (only for JOB_LAUNCHER / MILO_ENABLE_* flags)
GATEWAY_IDENTITY_VAR_NAMES=(MILO_GATEWAY_AUDIENCE MILO_APPROVED_GATEWAY_IDENTITIES)

report_from_json() {
  printf '%s' "$1" | python3 -c "$CONTAINER_REPORT_PY" \
    JOB_LAUNCHER "${STAGE_A_FLAG_NAMES[@]}" "${GATEWAY_IDENTITY_VAR_NAMES[@]}"
}

binding_report() {
  report_from_json "$(describe_json "$1" "$2")"
}

# ---------------------------------------------------------------------------
# live provider-key preflight
#
# --update-secrets is non-destructive by design, which means it never removes
# a binding it does not mention. A provider key bound by an earlier release
# therefore SURVIVES this deployment untouched — the previous version of this
# script bound KIMI_API_KEY to the worker, so that is a realistic legacy
# state, not a hypothetical one. Detecting it only in post-deployment
# verification would mean discovering it after both images were built, the
# worker job was deployed, IAM was written and the API was deployed.
#
# So it is checked here, before any mutation, and the deployment stops. The
# binding is NOT removed automatically: this script is deliberately
# non-destructive, and deleting a live secret binding is an operator decision
# with its own review. The removal commands live in
# docs/production-readiness/DEPLOYMENT.md so that no destructive flag appears
# anywhere in this script.
# ---------------------------------------------------------------------------
# gcloud's stderr is captured to a file rather than a variable: this function
# is called from a command substitution, so anything it assigned would be lost
# with the subshell.
LIVE_DESCRIBE_ERROR_FILE=""

# describe_live_json KIND NAME -> JSON on stdout.
#   0 = described   2 = resource does not exist yet   1 = inspection failed
# Unlike describe_json, a failure is never flattened into an empty document:
# "I could not look" must not be indistinguishable from "there is nothing
# there", or an inaccessible resource would silently pass this gate.
describe_live_json() {
  local kind="$1" name="$2" out="" status=0
  : >"$LIVE_DESCRIBE_ERROR_FILE"
  case "$kind" in
    service) out=$(gcloud run services describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>"$LIVE_DESCRIBE_ERROR_FILE") || status=$? ;;
    job) out=$(gcloud run jobs describe "$name" --project "$PROJECT_ID" --region "$REGION" --format=json 2>"$LIVE_DESCRIBE_ERROR_FILE") || status=$? ;;
    *) fail "describe_live_json: unknown resource kind '$kind'." ;;
  esac
  if [[ "$status" -eq 0 ]]; then
    printf '%s' "$out"
    return 0
  fi
  # A resource that has never been created carries no provider key.
  if grep -qiE 'NOT_FOUND|not found|Cannot find|does not exist' "$LIVE_DESCRIBE_ERROR_FILE"; then
    return 2
  fi
  return 1
}

require_no_live_provider_key_bindings() {
  local target kind name label json status record_kind record_name found="" reported
  LIVE_DESCRIBE_ERROR_FILE=$(mktemp)
  for target in "service:$API_SERVICE" "job:$WORKER_JOB"; do
    kind="${target%%:*}"
    name="${target#*:}"
    case "$kind" in
      service) label="API service '$name'" ;;
      job) label="Worker job '$name'" ;;
    esac

    status=0
    json=$(describe_live_json "$kind" "$name") || status=$?
    case "$status" in
      2)
        echo "Live provider-key check — $label: not created yet, nothing bound."
        continue
        ;;
      1)
        reported=$(head -n 1 "$LIVE_DESCRIBE_ERROR_FILE")
        rm -f "$LIVE_DESCRIBE_ERROR_FILE"
        fail "Could not inspect the live configuration of $label, so the absence of a provider key cannot be proven. Failing closed rather than deploying blind. gcloud reported: ${reported:-<no output>}"
        ;;
    esac

    # Only binding NAMES are read here. No value of any kind is printed.
    while IFS=$'\t' read -r record_kind record_name _; do
      case "$record_kind" in env | secret) ;; *) continue ;; esac
      if milo_contains "$record_name" "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; then
        if [[ "$record_kind" == "secret" ]]; then
          found+="  $label carries $record_name (Secret Manager-backed binding)"$'\n'
        else
          found+="  $label carries $record_name (plain environment variable)"$'\n'
        fi
      fi
    done <<<"$(report_from_json "$json")"

    echo "Live provider-key check — $label: inspected."
  done
  rm -f "$LIVE_DESCRIBE_ERROR_FILE"

  if [[ -n "$found" ]]; then
    local message
    message="A provider API key is already bound to live Cloud Run configuration:"$'\n'
    message+="$found"
    message+="Stage A requires provider keys (${MILO_PROVIDER_KEY_ENV_NAMES[*]}) to be absent from BOTH the API service and the worker job."$'\n'
    message+="This deployment updates configuration non-destructively, so it would leave the existing binding in place rather than remove it."$'\n'
    message+="Removing a legacy provider binding is a separate, explicit operator action: see 'Legacy provider bindings' in docs/production-readiness/DEPLOYMENT.md for the removal command matching the binding type shown above, then re-run this preflight."$'\n'
    message+="Nothing was built, deployed, executed or changed."
    fail "$message"
  fi
}

report_field() {
  # report_field REPORT KIND -> the value column of the single-valued record
  printf '%s\n' "$1" | awk -F'\t' -v kind="$2" '$1 == kind { print $2; exit }'
}

binding_identities() {
  # FULL identity of every binding, not just its name:
  #   env    <TAB> NAME
  #   secret <TAB> ENV_NAME <TAB> SECRET:VERSION
  # Comparing the secret reference as well as the environment name is what
  # makes a REMAP visible. A binding silently repointed at a different secret
  # (or a different version) keeps its name while changing what the runtime
  # actually reads — a name-only comparison would call that "preserved".
  printf '%s\n' "$1" | awk -F'\t' '
    $1 == "env"    { print "env\t" $2 }
    $1 == "secret" { print "secret\t" $2 "\t" $3 }' | sort -u
}

has_binding() {
  printf '%s\n' "$1" | grep -Fxq "$2"
}

secret_reference_for() {
  # secret_reference_for IDENTITIES ENV_NAME -> "SECRET:VERSION" (or empty)
  printf '%s\n' "$1" | awk -F'\t' -v name="$2" '$1 == "secret" && $2 == name { print $3; exit }'
}

assert_bindings_preserved() {
  local label="$1" before="$2" after="$3" kind name ref current lost=""
  while IFS=$'\t' read -r kind name ref; do
    [[ -n "$kind" && -n "$name" ]] || continue
    if [[ "$kind" == "secret" ]]; then
      has_binding "$after" "$(printf 'secret\t%s\t%s' "$name" "$ref")" && continue
      current=$(secret_reference_for "$after" "$name")
      if [[ -n "$current" ]]; then
        lost+="  secret $name was REMAPPED from $ref to $current"$'\n'
      else
        lost+="  secret $name ($ref) was removed"$'\n'
      fi
    else
      has_binding "$after" "$(printf 'env\t%s' "$name")" && continue
      lost+="  env $name was removed"$'\n'
    fi
  done <<<"$before"
  if [[ -n "$lost" ]]; then
    fail "$label lost pre-existing environment/secret bindings; the update was destructive:"$'\n'"$lost"
  fi
  echo "  preserved bindings: OK (no pre-existing env var dropped, no secret reference removed or remapped)"
}

verify_image_digest() {
  local label="$1" report="$2" expected_image="$3" deployed digest
  deployed=$(report_field "$report" image)
  digest=$(gcloud artifacts docker images describe "$expected_image" --project "$PROJECT_ID" \
    --format='value(image_summary.digest)' 2>/dev/null || true)
  if [[ "$deployed" == *"@"* ]]; then
    [[ -n "$digest" && "$deployed" == "${expected_image%:*}@$digest" ]] || \
      fail "$label runs image '$deployed', which does not resolve to the release digest of '$expected_image'."
  else
    [[ "$deployed" == "$expected_image" ]] || \
      fail "$label runs image '$deployed' instead of the release image '$expected_image'."
  fi
  [[ "$deployed" == *"$RELEASE_SHA"* || -n "$digest" ]] || \
    fail "$label image could not be tied to release SHA $RELEASE_SHA."
  echo "  image: $deployed"
  echo "  release SHA: $RELEASE_SHA"
  echo "  registry digest: ${digest:-<unresolved>}"
}

verify_service_account() {
  local label="$1" report="$2" expected="$3" actual
  actual=$(report_field "$report" service-account)
  [[ "$actual" == "$expected" ]] || \
    fail "$label runs as '$actual' instead of the expected runtime identity '$expected'."
  echo "  service account: $actual"
}

verify_env_names() {
  local label="$1" report="$2"
  shift 2
  local names name
  names=$(binding_identities "$report")
  for name in "$@"; do
    has_binding "$names" "$(printf 'env\t%s' "$name")" || \
      fail "$label is missing required environment variable '$name'."
  done
  echo "  environment variable names: $(printf '%s\n' "$report" | awk -F'\t' '$1 == "env" { printf "%s ", $2 }')"
}

verify_secret_refs() {
  local label="$1" report="$2"
  shift 2
  local expected
  for expected in "$@"; do
    printf '%s\n' "$report" | awk -F'\t' '$1 == "secret" { print $2 "=" $3 }' | grep -Fxq "$expected" || \
      fail "$label is missing the expected secret reference '$expected'."
  done
  echo "  secret references: $(printf '%s\n' "$report" | awk -F'\t' '$1 == "secret" { printf "%s->%s ", $2, $3 }')"
}

# Stage A: no provider key on EITHER runtime. Verified as a binding (secret
# reference) and as a plain environment variable, since a key pasted in as a
# literal value would be just as reachable.
verify_no_provider_key() {
  local label="$1" report="$2" name
  for name in "${MILO_PROVIDER_KEY_ENV_NAMES[@]}"; do
    if printf '%s\n' "$report" | awk -F'\t' '$1 == "env" || $1 == "secret" { print $2 }' | grep -Fxq "$name"; then
      fail "$label carries provider key '$name'. Stage A binds no provider key to any runtime; it is introduced only by an explicit Stage C operator action."
    fi
  done
  echo "  provider keys absent (${MILO_PROVIDER_KEY_ENV_NAMES[*]}): OK"
}

verify_stage_a_flags() {
  local label="$1" report="$2" kind name value
  while IFS=$'\t' read -r kind name value; do
    [[ "$kind" == "flag" ]] || continue
    case "$name" in
      JOB_LAUNCHER)
        [[ "$value" == "$JOB_LAUNCHER_MODE" ]] || \
          fail "$label has JOB_LAUNCHER='$value' instead of '$JOB_LAUNCHER_MODE'."
        ;;
      MILO_ENABLE_*)
        [[ "$value" == "false" ]] || \
          fail "$label has execution flag $name='$value'; Stage A requires false."
        ;;
    esac
  done <<<"$report"
  echo "  Stage A flags: JOB_LAUNCHER=$JOB_LAUNCHER_MODE, ${STAGE_A_FLAG_NAMES[*]} all false"
}

# The gateway audience and allowlist are not secret, so the deployed VALUES
# are compared — presence alone would pass on a stale value the deployment
# was supposed to replace.
verify_gateway_identity() {
  local label="$1" report="$2" kind name value
  local seen_audience="" seen_identities=""
  while IFS=$'\t' read -r kind name value; do
    [[ "$kind" == "flag" ]] || continue
    case "$name" in
      MILO_GATEWAY_AUDIENCE)
        [[ "$value" == "$MILO_GATEWAY_AUDIENCE" ]] || \
          fail "$label has MILO_GATEWAY_AUDIENCE='$value' instead of the approved '$MILO_GATEWAY_AUDIENCE'."
        seen_audience=1
        ;;
      MILO_APPROVED_GATEWAY_IDENTITIES)
        [[ -n "$value" ]] || \
          fail "$label has an empty MILO_APPROVED_GATEWAY_IDENTITIES; no caller would be verifiable."
        [[ "$value" == "$MILO_APPROVED_GATEWAY_IDENTITIES" ]] || \
          fail "$label has MILO_APPROVED_GATEWAY_IDENTITIES='$value' instead of the approved '$MILO_APPROVED_GATEWAY_IDENTITIES'."
        seen_identities=1
        ;;
    esac
  done <<<"$report"
  [[ -n "$seen_audience" && -n "$seen_identities" ]] || \
    fail "$label is missing a gateway identity variable; production requires both MILO_GATEWAY_AUDIENCE and MILO_APPROVED_GATEWAY_IDENTITIES."
  echo "  gateway identity: audience=$MILO_GATEWAY_AUDIENCE, approved=$MILO_APPROVED_GATEWAY_IDENTITIES"
}

verify_no_public_access() {
  local kind="$1" name="$2" policy
  case "$kind" in
    service) policy=$(gcloud run services get-iam-policy "$name" --project "$PROJECT_ID" --region "$REGION" --format=json) ;;
    job) policy=$(gcloud run jobs get-iam-policy "$name" --project "$PROJECT_ID" --region "$REGION" --format=json) ;;
  esac
  if printf '%s' "$policy" | grep -qE '"(allUsers|allAuthenticatedUsers)"'; then
    fail "$kind '$name' grants access to allUsers/allAuthenticatedUsers. Production Cloud Run resources must stay private."
  fi
  echo "  IAM: private (no allUsers / allAuthenticatedUsers)"
}

worker_execution_count() {
  gcloud run jobs executions list --job "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
    --format='value(metadata.name)' 2>/dev/null | grep -c '.' || true
}

verify_no_worker_execution() {
  local before="$1" after="$2"
  [[ "$before" == "$after" ]] || \
    fail "Worker job executions changed during deployment ($before -> $after). Deployment must never execute the worker."
  echo "  worker executions: $after (unchanged; deployment executed nothing)"
}

case "$DEPLOY_MODE" in
  check|apply) ;;
  *) fail "DEPLOY_MODE must be 'check' or 'apply'. Default is 'check'." ;;
esac

case "$JOB_LAUNCHER_MODE" in
  disabled|cloud_run) ;;
  *) fail "JOB_LAUNCHER_MODE must be 'disabled' or 'cloud_run'. Default is 'disabled'." ;;
esac

preflight
print_targets

if [[ "$DEPLOY_MODE" == "apply" && "$JOB_LAUNCHER_MODE" == "cloud_run" ]]; then
  echo "WARNING: JOB_LAUNCHER_MODE=cloud_run — the API will be deployed with the Cloud Run job launcher ENABLED. This is an explicit operator override of the safe default (disabled)." >&2
fi

if [[ "$DEPLOY_MODE" == "check" ]]; then
  echo "Check mode complete: prerequisites validated. No build, deploy, IAM change, worker execution, or paid API call was performed."
  exit 0
fi

# DEPLOY_MODE=apply is the only mode that builds, deploys, and grants the narrow Cloud Run jobs executor-with-overrides binding.

# Snapshot the live configuration first so the post-deploy verification can
# prove that no pre-existing environment variable or secret binding was lost.
API_BINDINGS_BEFORE=$(binding_identities "$(binding_report service "$API_SERVICE")")
WORKER_BINDINGS_BEFORE=$(binding_identities "$(binding_report job "$WORKER_JOB")")
WORKER_EXECUTIONS_BEFORE=$(worker_execution_count)

gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-worker.yaml --substitutions "_WORKER_IMAGE=$WORKER_IMAGE" .
gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config scripts/deploy/cloudbuild-api.yaml --substitutions "_API_IMAGE=$API_IMAGE" .

# The worker job is deployed BEFORE the API and is never executed here.
gcloud run jobs deploy "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" --image "$WORKER_IMAGE" \
  --service-account "$WORKER_SERVICE_ACCOUNT" --cpu 2 --memory 2Gi --task-timeout 3600 --max-retries 1 --parallelism 1 --tasks 1 \
  --update-env-vars "$(delimited_env_arg "${WORKER_ENV_VARS[@]}")" \
  --update-secrets "$(join_by ',' "${WORKER_SECRETS[@]}")"

# The API identity launches worker executions with overrides, so grant the executor binding to the API account, not the worker account.
gcloud run jobs add-iam-policy-binding "$WORKER_JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$API_SERVICE_ACCOUNT" --role roles/run.jobsExecutorWithOverrides >/dev/null

gcloud run deploy "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$API_IMAGE" \
  --service-account "$API_SERVICE_ACCOUNT" --no-allow-unauthenticated --port 8080 --cpu 1 --memory 1Gi --timeout 300 --max-instances 10 \
  --update-env-vars "$(delimited_env_arg "${API_ENV_VARS[@]}")" \
  --update-secrets "$(join_by ',' "${API_SECRETS[@]}")"

# ---------------------------------------------------------------------------
# post-deployment verification (read-only; no execution, no IAM change)
# ---------------------------------------------------------------------------
API_REPORT=$(binding_report service "$API_SERVICE")
WORKER_REPORT=$(binding_report job "$WORKER_JOB")

echo "Verifying worker job '$WORKER_JOB':"
verify_image_digest "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "$WORKER_IMAGE"
verify_service_account "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "$WORKER_SERVICE_ACCOUNT"
verify_env_names "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "${MILO_WORKER_REQUIRED_ENV_NAMES[@]}" "${STAGE_A_FLAG_NAMES[@]}"
verify_secret_refs "Worker job '$WORKER_JOB'" "$WORKER_REPORT" "${WORKER_SECRETS[@]}"
verify_no_provider_key "Worker job '$WORKER_JOB'" "$WORKER_REPORT"
verify_stage_a_flags "Worker job '$WORKER_JOB'" "$WORKER_REPORT"
assert_bindings_preserved "Worker job '$WORKER_JOB'" "$WORKER_BINDINGS_BEFORE" "$(binding_identities "$WORKER_REPORT")"
verify_no_public_access job "$WORKER_JOB"

echo "Verifying API service '$API_SERVICE':"
verify_image_digest "API service '$API_SERVICE'" "$API_REPORT" "$API_IMAGE"
verify_service_account "API service '$API_SERVICE'" "$API_REPORT" "$API_SERVICE_ACCOUNT"
verify_env_names "API service '$API_SERVICE'" "$API_REPORT" "${MILO_API_REQUIRED_ENV_NAMES[@]}" "${STAGE_A_FLAG_NAMES[@]}"
verify_secret_refs "API service '$API_SERVICE'" "$API_REPORT" "${API_SECRETS[@]}"
verify_no_provider_key "API service '$API_SERVICE'" "$API_REPORT"
verify_gateway_identity "API service '$API_SERVICE'" "$API_REPORT"
verify_stage_a_flags "API service '$API_SERVICE'" "$API_REPORT"
assert_bindings_preserved "API service '$API_SERVICE'" "$API_BINDINGS_BEFORE" "$(binding_identities "$API_REPORT")"
verify_no_public_access service "$API_SERVICE"

echo "Verifying that the deployment executed nothing:"
verify_no_worker_execution "$WORKER_EXECUTIONS_BEFORE" "$(worker_execution_count)"

service_url=$(gcloud run services describe "$API_SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
echo "API service URL: $service_url"
echo "Deployment complete. Worker job was deployed but not executed."
