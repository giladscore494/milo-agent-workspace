#!/usr/bin/env bash
# Stage C emergency kill switch — run at ANY sign of trouble (invariant
# failure, unexpected cost, anomalous behavior). Applies ROLLBACK.md
# emergency order: paid off → run creation off → launcher off → provider
# key bindings removed (EVERY accepted alias) → cancel any genuinely
# active worker execution → unconditional final zero-active verification →
# verify the fail-closed postconditions.
#
# Safe and idempotent to rerun (including when already fail-closed and when
# zero executions exist). Prints the final success line ONLY after every
# critical postcondition verified; otherwise exits non-zero.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

# Every provider-key alias the Worker accepts (see worker_provider_api_key
# in backend/engines/vehicle_catalog_v1/adapter.py). The kill switch must
# remove and verify ALL of them — leaving any alias bound violates the
# fail-closed claim.
PROVIDER_SECRET_ALIASES=("KIMI_API_KEY" "MOONSHOT_API_KEY")

# Bounded cancel re-verification — never an indefinite poll.
KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS="${KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS:-3}"
KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS="${KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS:-5}"

critical_failures=0
note_failure() {
  critical_failures=$((critical_failures + 1))
  echo "KILL SWITCH CRITICAL: $1" >&2
}

# -- 1. Paid execution OFF (worker). A failure here is critical on its own.
if ! gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="MILO_ENABLE_PAID_EXECUTION=false"; then
  note_failure "worker paid-execution flag update failed"
fi

# -- 2. Remove EVERY provider-key alias from the worker, one invocation per
# alias and per binding kind: gcloud refuses to remove an absent binding,
# and a combined removal is not atomic across partly absent aliases — a
# combined call could hide one alias's failure behind another's success.
# Removal failures are tolerated here (the usual cause is "already absent");
# the worker postcondition below independently proves each alias absent, in
# both valueFrom and literal-value form. Secret VALUES are never read.
for alias in "${PROVIDER_SECRET_ALIASES[@]}"; do
  if ! gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
      --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
      --remove-secrets="${alias}"; then
    echo "note: removing secret binding ${alias} failed (may already be absent); the postcondition verifies."
  fi
  if ! gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
      --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
      --remove-env-vars="${alias}"; then
    echo "note: removing env var ${alias} failed (may already be absent); the postcondition verifies."
  fi
done

# -- 3+4. Explicitly reset the COMPLETE API execution surface.  Do not
# depend on absent-variable defaults or on a previous revision's template.
if ! gcloud run services update "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="MILO_ENABLE_RUN_CREATION=false,MILO_ENABLE_PROPOSAL_MUTATIONS=false,MILO_ENABLE_PROPOSAL_READS=false,MILO_ENABLE_RUN_CANCELLATION=false,MILO_ENABLE_EXECUTION_CONTROL=false,MILO_ENABLE_PAID_EXECUTION=false,JOB_LAUNCHER=disabled"; then
  note_failure "API fail-closed flag update failed"
fi

# Provider aliases are forbidden on both surfaces.  As above, removal errors
# may mean "already absent"; the postcondition is authoritative.
for alias in "${PROVIDER_SECRET_ALIASES[@]}"; do
  gcloud run services update "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --remove-secrets="${alias}" || echo "note: API secret ${alias} already absent or removal failed; verifying."
  gcloud run services update "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --remove-env-vars="${alias}" || echo "note: API variable ${alias} already absent or removal failed; verifying."
done

# -- 5. Find genuinely nonterminal worker executions via structured JSON.
# An execution is terminal only when the serialized status carries a
# non-empty completionTime or a terminal Completed condition; absent, null
# and malformed fields are all nonterminal-safe (they get cancelled).
list_active_executions() {
  gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --format=json | python3 -c '
import json, sys

def is_terminal(execution):
    if not isinstance(execution, dict):
        return False
    status = execution.get("status")
    if not isinstance(status, dict):
        return False
    if status.get("completionTime"):
        return True
    for condition in status.get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == "Completed" and condition.get("status") in ("True", "False"):
            return True
    return False

try:
    executions = json.load(sys.stdin)
except json.JSONDecodeError:
    print("execution list was not valid JSON", file=sys.stderr)
    raise SystemExit(3)
if not isinstance(executions, list):
    print("execution list had an unexpected shape", file=sys.stderr)
    raise SystemExit(3)
for execution in executions:
    if is_terminal(execution):
        continue
    metadata = execution.get("metadata") if isinstance(execution, dict) else None
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not name:
        print("active execution without a name cannot be cancelled", file=sys.stderr)
        raise SystemExit(3)
    print(name)
'
}

# -- 6. Cancel ONLY genuinely active executions. Each execution is
# cancelled at most once per run; re-listing an already-cancelled execution
# just waits for it to settle instead of re-cancelling.
cancel_failed=0
already_cancelled=" "
cancel_listed() {
  local names="$1" execution
  while IFS= read -r execution; do
    [ -n "${execution}" ] || continue
    case "${already_cancelled}" in
      *" ${execution} "*) continue ;;
    esac
    already_cancelled="${already_cancelled}${execution} "
    echo "Cancelling active execution ${execution}"
    if ! gcloud run jobs executions cancel "${execution}" \
        --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --quiet; then
      note_failure "failed to cancel active execution ${execution}"
      cancel_failed=1
    fi
  done <<< "${names}"
}

# Bounded settle loop: succeeds ONLY when a fresh listing proves zero
# nonterminal executions; cancels anything newly visible (once) along the
# way. Never polls indefinitely.
settle_executions() {
  local attempt=1 remaining
  while :; do
    if ! remaining="$(list_active_executions)"; then
      note_failure "could not determine active worker executions"
      return 1
    fi
    if [ -z "${remaining}" ]; then
      return 0
    fi
    cancel_listed "${remaining}"
    if [ "${cancel_failed}" -ne 0 ]; then
      return 1
    fi
    if [ "${attempt}" -ge "${KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS}" ]; then
      note_failure "executions still active after bounded cancellation: ${remaining//$'\n'/ }"
      return 1
    fi
    attempt=$((attempt + 1))
    sleep "${KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS}"
  done
}

# Phase A: initial detection + cancellation pass.
if ! initial_active="$(list_active_executions)"; then
  note_failure "could not determine active worker executions"
  initial_active=""
fi
cancel_listed "${initial_active}"

# Phase B (-- 7): UNCONDITIONAL final verification — runs even when the
# initial listing was empty. The success claim rests solely on this proof:
# a fresh listing must show zero nonterminal executions; anything that
# appeared meanwhile is cancelled (bounded) or fails the script.
executions_clear=0
if [ "${cancel_failed}" -eq 0 ] && settle_executions; then
  executions_clear=1
fi
if [ "${executions_clear}" -eq 1 ]; then
  echo "Final verification: zero active worker executions."
fi

# -- 8. Verify final fail-closed postconditions (names/flags only — no
# secret values are ever read or printed). BOTH provider-key aliases must
# be absent from the worker AND from the API, whether bound via valueFrom
# or present as a literal env value.
verify_api_posture() {
  gcloud run services describe "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
    | python3 -c '
import json, sys
aliases = sys.argv[1:]
service = json.load(sys.stdin)
container = service["spec"]["template"]["spec"]["containers"][0]
env = container.get("env") or []
values = {e["name"]: e.get("value") for e in env if "value" in e}
secret_refs = {e["name"] for e in env if "valueFrom" in e}
assert values.get("MILO_ENABLE_RUN_CREATION") == "false", "MILO_ENABLE_RUN_CREATION is not false"
for flag in ("MILO_ENABLE_PROPOSAL_MUTATIONS", "MILO_ENABLE_PROPOSAL_READS", "MILO_ENABLE_RUN_CANCELLATION", "MILO_ENABLE_EXECUTION_CONTROL", "MILO_ENABLE_PAID_EXECUTION"):
    assert values.get(flag) == "false", f"{flag} is not false"
assert values.get("JOB_LAUNCHER") == "disabled", "JOB_LAUNCHER is not disabled"
for alias in aliases:
    assert alias not in secret_refs, f"provider secret {alias} bound to the API"
    assert alias not in values, f"provider variable {alias} present on the API"
status = service.get("status") or {}
ready = status.get("latestReadyRevisionName")
traffic = status.get("traffic") or []
assert ready, "API has no latest ready revision"
assert any(t.get("revisionName") == ready and int(t.get("percent", 0)) == 100 for t in traffic), "latest ready revision is not serving 100% of traffic"
print("OK: API fail-closed, no provider alias present")
' "${PROVIDER_SECRET_ALIASES[@]}"
}

verify_worker_posture() {
  gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
    | python3 -c '
import json, sys
aliases = sys.argv[1:]
container = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = container.get("env") or []
values = {e["name"]: e.get("value") for e in env if "value" in e}
secret_refs = {e["name"] for e in env if "valueFrom" in e}
assert values.get("MILO_ENABLE_PAID_EXECUTION") == "false", "worker MILO_ENABLE_PAID_EXECUTION is not false"
for alias in aliases:
    assert alias not in secret_refs, f"provider secret {alias} still bound to the worker"
    assert alias not in values, f"provider variable {alias} still present on the worker"
print("OK: worker fail-closed, no provider alias present")
' "${PROVIDER_SECRET_ALIASES[@]}"
}

if ! verify_api_posture; then
  note_failure "API postcondition failed (run creation / launcher / paid flags / provider aliases)"
fi
if ! verify_worker_posture; then
  note_failure "worker postcondition failed (paid flag / provider alias bindings)"
fi

# -- 9. Truthful exit: success is printed only when EVERYTHING verified,
# including the unconditional final zero-active-execution proof.
if [ "${critical_failures}" -gt 0 ] || [ "${executions_clear}" -ne 1 ]; then
  echo "KILL SWITCH INCOMPLETE: ${critical_failures} critical step(s) failed — production may NOT be fully fail-closed. Investigate and re-run immediately." >&2
  exit 1
fi
echo "KILL SWITCH APPLIED: paid off, all provider key aliases unbound, run creation off, launcher disabled, no active worker executions."
