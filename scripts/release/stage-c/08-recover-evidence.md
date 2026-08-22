# Stage C step 8 — Attempt 7 evidence recovery (MANUAL ONLY, one RUN_ID)

`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. This runbook re-runs the
CORRECTED acceptance gate over the ALREADY-COMPLETED Attempt 7 run after
the evidence-gate corrective PR is merged. It authorizes **no new paid
run, no new Worker execution and no provider call**. The Worker paid flag
stays `false` and BOTH provider aliases (`KIMI_API_KEY`,
`MOONSHOT_API_KEY`) stay absent throughout every step. By repository
policy (`STAGED_ACTIVATION.md`, enforced by
`scripts/check_unsafe_defaults.py`) no committed script may enable an
execution flag, so this recovery is ONE copy-paste operator block (below)
whose single temporary flag change goes through the operator-run-time
`STAGE_C_ON` indirection — exactly like step 3.

Hard preconditions — the block itself also verifies them and hard-stops:

- The evidence-gate corrective PR is merged and your working tree is at
  (or after) its merge commit — the probes must be recreated from the
  CORRECTED source.
- This recovery applies ONLY to the existing completed run
  `8b4a4277-fdf0-41b2-8515-d7e1d50e441b` (Worker execution
  `milo-agent-worker-gggdc`, succeeded). Any other RUN_ID: STOP — a new
  paid run is NOT authorized.
- `kill-switch.sh` completed after the false-failed gate: paid execution
  off, provider aliases unbound, API run creation off, launcher
  disabled, zero active executions. **`07-post-smoke-posture.sh` was NOT
  run**, so the disposable `stagec-db-probe` / `stagec-gw-probe` jobs
  are still present in production and still carry the DEFECTIVE
  pre-corrective probe source; step R.2 verifies each stale job's exact
  name and posture, refuses to touch anything that is not a known
  disposable probe, deletes them, and recreates both from the corrected
  source.

Fail-closed design of the block:

- One subshell under `set -Eeuo pipefail`: the first failing command
  aborts the whole recovery — there are no "continue anyway" paths, and
  every stop is a real nonzero exit.
- Cleanup traps (`EXIT`/`ERR`/`INT`/`TERM`) are installed BEFORE the
  first production mutation and therefore before API run creation is
  enabled. On EVERY exit path — success, failure, interrupt — the traps
  restore `MILO_ENABLE_RUN_CREATION=false`, run `kill-switch.sh`
  (idempotent), delete both probes and verify their absence.
- All waiting is bounded: execution polling stops after
  `RECOVERY_POLL_TIMEOUT_SECONDS`, log-ingestion fetches retry at most
  `RECOVERY_LOG_RETRIES` times. Nothing polls forever.
- The launcher stays `disabled` and the worker/provider posture is
  untouched, so even a defect cannot start an execution: a replay of a
  run already in a terminal state returns the same run id before any
  launcher call (`backend/main.py`), and a hypothetical accidental new
  run could never execute, would break the exact-count gates, and would
  be terminated by the kill switch the traps run.

Run the block from the repository root in an operator-authenticated
`gcloud` shell. If it exits nonzero, production has already been
restored fail-closed by the traps (the output says if that cleanup
itself was incomplete); investigate before any further action and record
the outcome in `STAGE_C_ACCEPTANCE.md`.

```bash
(
set -Eeuo pipefail
cd scripts/release/stage-c
# shellcheck source=stage-c-env.sh
source ./stage-c-env.sh
RUN_ID="8b4a4277-fdf0-41b2-8515-d7e1d50e441b"   # the ONLY authorized recovery target

# Bounded waits — nothing in this block polls forever.
RECOVERY_POLL_INTERVAL_SECONDS="${RECOVERY_POLL_INTERVAL_SECONDS:-15}"
RECOVERY_POLL_TIMEOUT_SECONDS="${RECOVERY_POLL_TIMEOUT_SECONDS:-900}"
RECOVERY_LOG_RETRIES="${RECOVERY_LOG_RETRIES:-10}"
RECOVERY_LOG_RETRY_DELAY_SECONDS="${RECOVERY_LOG_RETRY_DELAY_SECONDS:-10}"

hard_stop() { echo "RECOVERY HARD STOP: $1" >&2; exit 1; }

wait_for_recovery_execution() { # exec_name — bounded; succeeded=0 failed=1 timeout=2
  local exec_name="$1" waited=0 state
  while :; do
    state="$(gcloud run jobs executions describe "${exec_name}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
      | python3 ./execution_state.py)" || state="running"
    case "${state}" in
      succeeded) return 0 ;;
      failed) return 1 ;;
    esac
    if [ "${waited}" -ge "${RECOVERY_POLL_TIMEOUT_SECONDS}" ]; then
      echo "RECOVERY: execution '${exec_name}' not verifiably terminal after ${RECOVERY_POLL_TIMEOUT_SECONDS}s" >&2
      return 2
    fi
    sleep "${RECOVERY_POLL_INTERVAL_SECONDS}"
    waited=$((waited + RECOVERY_POLL_INTERVAL_SECONDS))
  done
}

# Cleanup body — runs on EVERY exit path once the traps are armed, and
# is idempotent (safe if a partial earlier pass already restored things).
RECOVERY_CLEANED=0
recover_cleanup_body() {
  echo "== R.6 restore the full fail-closed posture + delete probes (every exit path)"
  local failed=0 job
  gcloud run services update "${STAGE_C_API_SERVICE}" \
    --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
    --update-env-vars "MILO_ENABLE_RUN_CREATION=false" || failed=1
  ./kill-switch.sh || failed=1
  for job in "${STAGE_C_DB_PROBE_JOB}" "${STAGE_C_GW_PROBE_JOB}"; do
    gcloud run jobs delete "${job}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --quiet || true
    if gcloud run jobs describe "${job}" \
        --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
        --format='value(metadata.name)' >/dev/null 2>&1; then
      echo "RECOVERY CRITICAL: probe job '${job}' still exists after cleanup deletion" >&2
      failed=1
    fi
  done
  RECOVERY_CLEANED=1
  if [ "${failed}" -ne 0 ]; then
    echo "RECOVERY CLEANUP INCOMPLETE — restore the fail-closed posture manually NOW (kill-switch.sh, probe deletion) and record it in STAGE_C_ACCEPTANCE.md" >&2
    return 1
  fi
  echo "OK: fail-closed posture restored, probes deleted"
}

on_exit() {
  local rc=$?
  trap - EXIT ERR INT TERM
  if [ "${RECOVERY_CLEANED}" != "1" ]; then
    recover_cleanup_body || rc=1
  fi
  if [ "${rc}" -ne 0 ]; then
    echo "RECOVERY DID NOT COMPLETE (exit ${rc}) — Stage C evidence is NOT recovered; investigate before any further action" >&2
  fi
  exit "${rc}"
}

echo "== R.1 verify the fail-closed posture and post-run baselines (read-only)"
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c.get("env") or [] if "value" in e}
bound = {e["name"] for e in c.get("env") or [] if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", "worker paid flag is not false"
for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
    assert alias not in bound and alias not in env, f"provider alias {alias} still present on the worker"
print("OK: worker paid flag false, provider aliases absent")
'
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
env = {e["name"]: e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
assert env.get("MILO_ENABLE_RUN_CREATION") == "false", "API run creation is not false"
assert env.get("JOB_LAUNCHER") == "disabled", "API launcher is not disabled"
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", "API paid flag is not false"
print("OK: API fail-closed")
'
# Exactly 2 visible Worker executions (pinned baseline 1 + Attempt 7's
# milo-agent-worker-gggdc), every one terminal, zero active.
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total 2

# Arm the cleanup traps BEFORE the first production mutation (and thus
# before API run creation is enabled): from here on, every exit path
# restores MILO_ENABLE_RUN_CREATION=false, runs kill-switch.sh and
# deletes both probes.
trap on_exit EXIT ERR INT TERM

echo "== R.2 reconcile the STALE disposable probes (07 cleanup never ran)"
# The pre-corrective probes still exist and still carry the DEFECTIVE
# probe source. Each is verified by exact name AND posture before it is
# touched: the pinned disposable-probe service account, a transported
# PROBE_SOURCE, no paid flag and no provider alias — anything else is NOT
# a known disposable probe and hard-stops the recovery. Verified stale
# probes (with zero active executions) are deleted and recreated from
# the corrected source.
for pair in "${STAGE_C_DB_PROBE_JOB}=${STAGE_C_API_SA}" "${STAGE_C_GW_PROBE_JOB}=${STAGE_C_GATEWAY_SA}"; do
  job="${pair%%=*}"
  expected_sa="${pair#*=}"
  if gcloud run jobs describe "${job}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
      > "/tmp/stagec-recover-${job}.json" 2>/dev/null; then
    python3 - "${job}" "${expected_sa}" "/tmp/stagec-recover-${job}.json" <<'PY'
import json, sys
job, expected_sa, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, encoding="utf-8") as fh:
    described = json.load(fh)
name = (described.get("metadata") or {}).get("name")
assert name == job, f"describe returned {name!r}, expected {job!r}"
tpl = described["spec"]["template"]["spec"]["template"]["spec"]
sa = tpl.get("serviceAccountName")
assert sa == expected_sa, f"stale job {job} runs as {sa!r}, expected {expected_sa!r} - NOT a known disposable probe; refusing to delete"
env_entries = tpl["containers"][0].get("env") or []
values = {e["name"]: e.get("value") for e in env_entries if "value" in e}
names = {e["name"] for e in env_entries}
assert "PROBE_SOURCE" in names, f"stale job {job} has no transported PROBE_SOURCE - not a disposable probe; refusing to delete"
assert values.get("MILO_ENABLE_PAID_EXECUTION") in (None, "false"), f"stale job {job} carries an enabled paid flag"
for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
    assert alias not in names, f"stale job {job} carries provider alias {alias}"
print(f"OK: stale disposable probe {job} verified (sa={sa}); safe to delete and recreate")
PY
    # Never delete a job with an active execution.
    gcloud run jobs executions list --job="${job}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
      | python3 -c '
import json, sys
execs = json.load(sys.stdin)
assert isinstance(execs, list), "unparseable probe execution listing"
def terminal(e):
    s = (e.get("status") or {}) if isinstance(e, dict) else {}
    if s.get("completionTime"):
        return True
    return any(isinstance(c, dict) and c.get("type") == "Completed" and c.get("status") in ("True", "False") for c in s.get("conditions") or [])
active = [e for e in execs if not terminal(e)]
assert not active, f"{len(active)} active/unverifiable probe execution(s) - refusing to delete the job"
print("OK: stale probe has zero active executions")
'
    gcloud run jobs delete "${job}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --quiet
  else
    echo "note: probe job '${job}' not present (nothing stale to reconcile)"
  fi
  if gcloud run jobs describe "${job}" \
      --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
      --format='value(metadata.name)' >/dev/null 2>&1; then
    hard_stop "probe job '${job}' still exists after deletion"
  fi
done
./04-create-probes.sh   # recreate BOTH probes from the corrected source

echo "== R.3 idempotent test-data lookup (setup mode; creates NO run)"
setup_exec="$(gcloud run jobs execute "${STAGE_C_DB_PROBE_JOB}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "STAGE_C_MODE=setup" \
  --async --format='value(metadata.name)')"
if [ -z "${setup_exec}" ]; then
  hard_stop "no execution name established for the setup probe"
fi
wait_for_recovery_execution "${setup_exec}" \
  || hard_stop "setup probe execution '${setup_exec}' did not verifiably succeed"
log_attempt=0
while :; do
  gcloud logging read \
    "resource.type=cloud_run_job AND resource.labels.job_name=${STAGE_C_DB_PROBE_JOB} AND labels.\"run.googleapis.com/execution_name\"=${setup_exec}" \
    --project "${STAGE_C_PROJECT}" --format='json(textPayload,jsonPayload)' --order=asc \
    | python3 -c '
import json, sys
for record in json.load(sys.stdin):
    if not isinstance(record, dict):
        continue
    payload = record.get("jsonPayload")
    if not isinstance(payload, dict):
        text = record.get("textPayload")
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
    if isinstance(payload, dict) and payload.get("stage_c_probe") == "setup":
        print(json.dumps(payload, sort_keys=True))
' > /tmp/stagec-setup.log || true
  if grep -q '"stage_c_probe": "setup"' /tmp/stagec-setup.log; then
    break
  fi
  log_attempt=$((log_attempt + 1))
  if [ "${log_attempt}" -ge "${RECOVERY_LOG_RETRIES}" ]; then
    hard_stop "setup record not ingested in Cloud Logging after ${RECOVERY_LOG_RETRIES} bounded retries"
  fi
  sleep "${RECOVERY_LOG_RETRY_DELAY_SECONDS}"
done

echo "== R.4 enable ONLY the replay surface (launcher stays disabled; paid stays false)"
gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "MILO_ENABLE_RUN_CREATION=${STAGE_C_ON}"

echo "== R.5 rerun the corrected acceptance gate (replay + evidence + executions + log scan)"
./06-collect-evidence.sh "${RUN_ID}"

# R.6 runs here explicitly on the success path; the armed traps run the
# SAME cleanup on every failure/interrupt path.
recover_cleanup_body

echo "== R.7 final proof: still exactly 2 terminal executions, zero active"
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total 2

echo "RECOVERY COMPLETE for run ${RUN_ID} — record every command and outcome in STAGE_C_ACCEPTANCE.md."
echo "Only after R.5 passed may Stage C be recorded as PASSED. A second paid run remains unauthorized."
)
```

Notes:

- (`STAGE_C_ON=true` is exported by `stage-c-env.sh` at operator run
  time — the literal enable value never appears in a committed
  executable line.)
- The DB totals (2 runs, 1 Attempt-7-key run, 1 invocation) are proven
  inside R.5 by the corrected evidence gate itself; R.5's execution gate
  and R.7 prove no new Worker execution was created; the traps and R.6
  prove the restored fail-closed posture via `kill-switch.sh`'s own
  postcondition verification.
