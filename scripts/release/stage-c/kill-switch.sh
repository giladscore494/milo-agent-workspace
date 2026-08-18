#!/usr/bin/env bash
# Stage C emergency kill switch — run at ANY sign of trouble (invariant
# failure, unexpected cost, anomalous behavior). Applies ROLLBACK.md
# emergency order: paid off → run creation off → launcher off → provider
# key binding removed → cancel any genuinely active worker execution →
# verify the fail-closed postconditions.
#
# Safe and idempotent to rerun (including when already fail-closed and when
# zero executions exist). Prints the final success line ONLY after every
# critical postcondition verified; otherwise exits non-zero.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh

# Bounded cancel re-verification — never an indefinite polling loop.
KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS="${KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS:-3}"
KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS="${KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS:-5}"

critical_failures=0
note_failure() {
  critical_failures=$((critical_failures + 1))
  echo "KILL SWITCH CRITICAL: $1" >&2
}

# -- 1. Paid execution OFF + provider secret binding removed (worker).
# --remove-secrets fails when the binding is already gone, so a rerun falls
# back to the flag-only update; the worker postcondition below still proves
# the binding is absent either way. Secret VALUES are never read or printed.
if ! gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="MILO_ENABLE_PAID_EXECUTION=false" \
    --remove-secrets="KIMI_API_KEY"; then
  echo "Worker update with --remove-secrets failed (binding may already be removed); applying flag-only update."
  if ! gcloud run jobs update "${STAGE_C_WORKER_JOB}" \
      --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
      --update-env-vars="MILO_ENABLE_PAID_EXECUTION=false"; then
    note_failure "worker paid-execution flag update failed"
  fi
fi

# -- 2+3. Run creation OFF + launcher disabled (API).
if ! gcloud run services update "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" \
    --update-env-vars="MILO_ENABLE_RUN_CREATION=false,JOB_LAUNCHER=disabled"; then
  note_failure "API fail-closed flag update failed"
fi

# -- 4+5. Find genuinely nonterminal worker executions via structured JSON.
# An execution is terminal only when its serialized status carries a
# non-empty completionTime OR a terminal Completed condition; absent, null
# and malformed fields are all treated as nonterminal (fail-safe: cancel).
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

cancel_failed=0
active_executions=""
if ! active_executions="$(list_active_executions)"; then
  note_failure "could not determine active worker executions"
  active_executions=""
fi

# -- 6. Cancel ONLY genuinely active executions (zero active is a no-op).
while IFS= read -r execution; do
  [ -n "${execution}" ] || continue
  echo "Cancelling active execution ${execution}"
  if ! gcloud run jobs executions cancel "${execution}" \
      --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --quiet; then
    note_failure "failed to cancel active execution ${execution}"
    cancel_failed=1
  fi
done <<< "${active_executions}"

# -- 7. Bounded re-verification that cancellations actually landed.
if [ -n "${active_executions}" ] && [ "${cancel_failed}" -eq 0 ]; then
  attempt=1
  while :; do
    if ! remaining="$(list_active_executions)"; then
      note_failure "could not re-verify worker executions after cancellation"
      break
    fi
    if [ -z "${remaining}" ]; then
      echo "All cancelled executions reached a terminal state."
      break
    fi
    if [ "${attempt}" -ge "${KILL_SWITCH_CANCEL_VERIFY_ATTEMPTS}" ]; then
      note_failure "executions still active after cancellation: ${remaining//$'\n'/ }"
      break
    fi
    attempt=$((attempt + 1))
    sleep "${KILL_SWITCH_CANCEL_VERIFY_DELAY_SECONDS}"
  done
fi

# -- 8. Verify final fail-closed postconditions (names/flags only — no
# secret values are ever read or printed).
verify_api_posture() {
  gcloud run services describe "${STAGE_C_API_SERVICE}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
    | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in spec.get("env") or [] if "value" in e}
assert env.get("MILO_ENABLE_RUN_CREATION") == "false", "MILO_ENABLE_RUN_CREATION is not false"
assert env.get("JOB_LAUNCHER") == "disabled", "JOB_LAUNCHER is not disabled"
assert env.get("MILO_ENABLE_PAID_EXECUTION", "false") == "false", "API MILO_ENABLE_PAID_EXECUTION is not false"
print("OK: API fail-closed")
'
}

verify_worker_posture() {
  gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
    --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
    | python3 -c '
import json, sys
container = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in container.get("env") or [] if "value" in e}
secret_refs = {e["name"] for e in container.get("env") or [] if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", "worker MILO_ENABLE_PAID_EXECUTION is not false"
assert "KIMI_API_KEY" not in secret_refs, "provider key still bound to the worker"
print("OK: worker fail-closed, provider key unbound")
'
}

if ! verify_api_posture; then
  note_failure "API postcondition failed (run creation / launcher / paid flags)"
fi
if ! verify_worker_posture; then
  note_failure "worker postcondition failed (paid flag / provider key binding)"
fi

# -- 9. Truthful exit: success is printed only when EVERYTHING verified.
if [ "${critical_failures}" -gt 0 ]; then
  echo "KILL SWITCH INCOMPLETE: ${critical_failures} critical step(s) failed — production may NOT be fully fail-closed. Investigate and re-run immediately." >&2
  exit 1
fi
echo "KILL SWITCH APPLIED: paid off, key unbound, run creation off, launcher disabled, no active worker executions."
