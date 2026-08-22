# Stage C step 8 — Attempt 7 evidence recovery (MANUAL ONLY, one RUN_ID)

`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. This runbook re-runs the
CORRECTED acceptance gate over the ALREADY-COMPLETED Attempt 7 run after
the evidence-gate corrective PR is merged. It authorizes **no new paid
run, no new Worker execution and no provider call**. The Worker paid flag
stays `false` and BOTH provider aliases (`KIMI_API_KEY`,
`MOONSHOT_API_KEY`) stay absent throughout every step. By repository
policy (`STAGED_ACTIVATION.md`, enforced by
`scripts/check_unsafe_defaults.py`) no committed script may enable an
execution flag, so the single temporary flag change in R.4 is typed
manually — exactly like step 3.

Hard preconditions — STOP unless ALL hold:

- The evidence-gate corrective PR is merged and your working tree is at
  (or after) its merge commit — the probes must be recreated from the
  CORRECTED source.
- This recovery applies ONLY to the existing completed run
  `8b4a4277-fdf0-41b2-8515-d7e1d50e441b` (Worker execution
  `milo-agent-worker-gggdc`, succeeded). Any other RUN_ID: STOP — a new
  paid run is NOT authorized.
- The Attempt 7 kill switch has completed: paid execution off, provider
  aliases unbound, API run creation off, launcher disabled, zero active
  executions (verified read-only in R.1 before anything else runs).

Kill switch at any time: `./kill-switch.sh`. If ANY step below shows an
unexpected run, execution or posture value, run the kill switch, then
R.6, and record the failure in `STAGE_C_ACCEPTANCE.md`.

## R.1 Verify the fail-closed posture and post-run baselines (read-only)

```bash
cd scripts/release/stage-c
source ./stage-c-env.sh
RUN_ID="8b4a4277-fdf0-41b2-8515-d7e1d50e441b"   # the ONLY authorized recovery target

# Worker: paid flag false, NO provider alias in any binding form.
gcloud run jobs describe "${STAGE_C_WORKER_JOB}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
c = json.load(sys.stdin)["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e.get("value") for e in c.get("env", []) if "value" in e}
bound = {e["name"] for e in c.get("env", []) if "valueFrom" in e}
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", env.get("MILO_ENABLE_PAID_EXECUTION")
for alias in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
    assert alias not in bound and alias not in env, f"provider alias {alias} still present"
print("OK: worker paid flag false, provider aliases absent")
'

# API: run creation off, launcher disabled, paid flag false.
gcloud run services describe "${STAGE_C_API_SERVICE}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
  | python3 -c '
import json, sys
env = {e["name"]: e.get("value") for e in json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}
assert env.get("MILO_ENABLE_RUN_CREATION") == "false", env.get("MILO_ENABLE_RUN_CREATION")
assert env.get("JOB_LAUNCHER") == "disabled", env.get("JOB_LAUNCHER")
assert env.get("MILO_ENABLE_PAID_EXECUTION") == "false", env.get("MILO_ENABLE_PAID_EXECUTION")
print("OK: API fail-closed")
'

# Exactly 2 visible Worker executions (pinned baseline 1 + Attempt 7's
# milo-agent-worker-gggdc), every one terminal, zero active.
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total 2
```

## R.2 Recreate the disposable probes from the corrected source

```bash
./04-create-probes.sh
```

Creates `stagec-db-probe` and `stagec-gw-probe` only (no paid flags, no
extra mutations — regression-tested); the transported `probe_db.py` now
contains the corrected fail-closed evidence gate.

## R.3 Idempotent test-data lookup (NO run creation)

The DB probe `setup` mode idempotently re-reads the existing operator
test user / `stage-c-smoke` project / conversation — it creates no run
and calls no provider. The execution name is captured with `--async`
BEFORE completion (the same name-preserving pattern as the corrected
`06-collect-evidence.sh`) so its log can always be attributed.

```bash
setup_exec="$(gcloud run jobs execute "${STAGE_C_DB_PROBE_JOB}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "STAGE_C_MODE=setup" \
  --async --format='value(metadata.name)')"
test -n "${setup_exec}" || echo "STOP: no execution name established"

# Repeat until it prints `succeeded`; on `failed`, STOP and investigate.
gcloud run jobs executions describe "${setup_exec}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --format=json \
  | python3 ./execution_state.py

# Retrieve the structured setup record (repeat if log ingestion lags).
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
' | tee /tmp/stagec-setup.log
grep -q '"stage_c_probe": "setup"' /tmp/stagec-setup.log || echo "STOP: setup record missing"
```

## R.4 Enable the replay surface ONLY (manual, minimal, temporary)

The corrected gate's step 2 replays the idempotency key through the real
gateway path; that POST route is gated by `MILO_ENABLE_RUN_CREATION`.
Enable ONLY that flag. The launcher STAYS `disabled` and the worker /
provider posture is untouched, so even a defect cannot start an
execution: a replay of a run already in a terminal state returns the same
run id before any launcher call (`backend/main.py`), and a hypothetical
accidental new run could never execute, would break the exact-count
gates in R.5, and would be terminated by the kill switch.

```bash
gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "MILO_ENABLE_RUN_CREATION=${STAGE_C_ON}"
```

(`STAGE_C_ON=true` is exported by `stage-c-env.sh` at operator run time —
the literal enable value never appears in a committed executable line.
`JOB_LAUNCHER` remains `disabled`; `MILO_ENABLE_PAID_EXECUTION` remains
`false` on BOTH surfaces; no provider alias is bound anywhere.)

## R.5 Rerun the corrected acceptance gate

```bash
./06-collect-evidence.sh "${RUN_ID}"
```

This is the corrected executable gate: DB evidence (exactly 2 total runs
= pinned baseline 1 + this run; exactly 1 run under the Attempt 7 key;
exactly 1 launch invocation via the real
`launcher`/`execution_name`/`created_at` columns; all 84 reservations
settled with zero dangling; Decimal call_seq-aware one-to-one cost
reconciliation; strict caps), the post-completion idempotent replay
(must return `8b4a4277-…` itself), exactly 2 terminal Worker executions
(proof that NO new execution was created), and the worker-log
secret-marker scan. Any failure: kill switch, R.6, record — do NOT
create another run.

## R.6 Restore the full fail-closed posture and delete the probes

```bash
gcloud run services update "${STAGE_C_API_SERVICE}" \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "MILO_ENABLE_RUN_CREATION=false"

./kill-switch.sh   # idempotent: re-proves paid off, aliases absent, zero active

gcloud run jobs delete "${STAGE_C_DB_PROBE_JOB}" --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --quiet
gcloud run jobs delete "${STAGE_C_GW_PROBE_JOB}" --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" --quiet
```

## R.7 Prove no new run or Worker execution was created

```bash
gcloud run jobs executions list --job="${STAGE_C_WORKER_JOB}" \
  --project="${STAGE_C_PROJECT}" --region="${STAGE_C_REGION}" --format=json \
  | python3 ./verify_executions.py --expected-total 2
```

The DB totals (2 runs, 1 Attempt-7-key run, 1 invocation) were proven
inside R.5 by the evidence gate itself. Record every command and outcome
in `STAGE_C_ACCEPTANCE.md`; only after R.5 passes may Stage C be
recorded as PASSED. A second paid run remains unauthorized regardless of
the outcome.
