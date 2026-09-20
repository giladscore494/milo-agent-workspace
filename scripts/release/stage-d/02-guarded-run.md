# Stage D step 2 — the ONE guarded operator block (MANUAL ONLY)

`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. **Nothing below has been
executed.** This is part of a PROPOSED Stage D authorization; running it
requires a fresh, explicit operator decision.

## Why this is one block and not a checklist

Steps 3–7 mutate production. Run as separate commands, any unexpected
exit — a failed `gcloud`, a dropped SSH session, Ctrl-C during the poll —
can leave the paid-execution flag **on**, the provider key **bound**, run
creation **enabled**, or a half-created credentialed probe job standing.
A checklist cannot fix that, because the thing that failed is the thing
that was supposed to clean up.

So the whole mutating sequence is **one subshell** under
`set -Eeuo pipefail` whose cleanup trap is armed **before the first
mutation** and fires on `EXIT`, `ERR`, `INT` and `TERM`. Every exit path —
success, failure, interrupt, kill — ends with the kill switch applied and
both probe jobs deleted **and proven absent**.

Enabling execution remains a deliberate manual operator action: this block
lives in a Markdown runbook that a human pastes, and no committed script
enables an execution flag (`scripts/check_unsafe_defaults.py`). The enable
value reaches the command through `${STAGE_D_ON}`, exported by
`stage-d-env.sh` at operator run time, so no committed line pairs a flag
name with an enabled literal.

## Before you paste it

1. `./01-verify-release-images.sh` must have passed. The block re-runs it
   as its first action anyway, read-only, before anything is armed.
2. Step 0 (`resolve-government-capture.sh`) decision made and, if
   retiring, applied.
3. You have verified the current `$web_search` per-invocation fee **and a
   hard provider-account spending ceiling is in place** — see the cost
   section of `STAGE_D_AUTHORIZATION.md`. MILO's caps bound tracked token
   cost only; they do not bound provider-side tool fees.

Kill switch at any time, from another shell: `./kill-switch.sh`.

## The block

Paste it whole. `STAGE_D_WORKDIR` and the run id are persisted to
`state.json` as they come into existence, so the evidence gate and the
cleanup never depend on you copying an id out of the terminal.

```bash
(
  set -Eeuo pipefail
  cd "$(git rev-parse --show-toplevel)/scripts/release/stage-d"
  # shellcheck source=stage-d-env.sh
  source ./stage-d-env.sh

  hard_stop() { echo "STAGE D HARD STOP: $1" >&2; exit 1; }

  # -- Machine-readable state, created before anything else. Everything
  # downstream reads ids from here rather than from the terminal.
  STAGE_D_WORKDIR="${STAGE_D_WORKDIR:-${HOME}/.milo-stage-d/$(date -u +%Y%m%dT%H%M%SZ)}"
  mkdir -p "${STAGE_D_WORKDIR}"
  export STAGE_D_WORKDIR
  STAGE_D_STATE="${STAGE_D_WORKDIR}/state.json"
  python3 ./state_file.py "${STAGE_D_STATE}" write stage_d_workdir "${STAGE_D_WORKDIR}"
  python3 ./state_file.py "${STAGE_D_STATE}" write idempotency_key "${STAGE_D_IDEMPOTENCY_KEY}"
  echo "STAGE_D_WORKDIR=${STAGE_D_WORKDIR}"

  # -- Phase 0: READ-ONLY. Nothing is armed because nothing can be dirty.
  echo "== 0. Accepted release digests (read-only; never rebuilds, never re-tags)"
  ./01-verify-release-images.sh || hard_stop "accepted release digests do not match — a separate reviewed release is required"

  # -- The cleanup. Runs the one authoritative lockdown, which applies the
  # kill switch, proves the Government capture was never claimed, deletes
  # both probe jobs and proves they are absent. Deleting a probe that was
  # never created is a tolerated no-op; the proof is the listing, not the
  # delete. Exit code 2 from the lockdown means "fail-closed and probes
  # gone, but the capture posture was not provable" — still a failure here.
  stage_d_completed=0
  cleanup_body() {
    echo "== CLEANUP: applying the fail-closed lockdown"
    ./07-post-run-lockdown.sh
  }
  on_exit() {
    rc=$?
    trap - EXIT ERR INT TERM
    if [ "${stage_d_completed}" -eq 1 ]; then exit "${rc}"; fi
    echo "== Stage D did not complete cleanly (exit ${rc}) — running automatic fail-closed cleanup" >&2
    if cleanup_body; then
      echo "CLEANUP COMPLETE: production is fail-closed and both probes are proven absent." >&2
    else
      echo "CLEANUP INCOMPLETE: production may NOT be fully fail-closed. Investigate immediately, then rerun ./07-post-run-lockdown.sh." >&2
    fi
    if [ "${rc}" -eq 0 ]; then rc=1; fi
    exit "${rc}"
  }

  # ======================================================================
  # ARM THE CLEANUP. Every line below this one may mutate production, and
  # every exit path from here on runs the lockdown.
  # ======================================================================
  trap on_exit EXIT ERR INT TERM

  echo "== 3.1b The envelope below is generated from THIS CHECKOUT — prove it is the release's"
  # Applying the caps MUTATES the job. 03b and 05 would refuse the run
  # afterwards, but by then a drifted envelope would already be on it.
  python3 ./policy_envelope.py binding

  echo "== 3.2 Worker: paid flag + strict caps + provider envelope + provider key (worker only)"
  gcloud run jobs update "${STAGE_D_WORKER_JOB}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${STAGE_D_ON},${STAGE_D_CAPS},${STAGE_D_WORKER_PROVIDER_LIMITS},${STAGE_D_WORKER_ENGINE_LIMITS}" \
    --update-secrets "KIMI_API_KEY=KIMI_API_KEY:latest"

  echo "== 3.3 API: run creation + launcher + caps (paid flag STAYS false)"
  gcloud run services update "${STAGE_D_API_SERVICE}" \
    --project="${STAGE_D_PROJECT}" --region="${STAGE_D_REGION}" \
    --update-env-vars "JOB_LAUNCHER=cloud_run,MILO_ENABLE_RUN_CREATION=${STAGE_D_ON},${STAGE_D_CAPS}"

  echo "== 3.4 Verify the enabled posture (read-only)"
  ./03b-verify-stage-d-posture.sh

  echo "== 4. Create the two disposable probe jobs"
  ./04-create-probes.sh

  echo "== 5. Execute the ONE authorized run"
  ./05-execute-run.sh

  echo "== 6. Acceptance gate (run id read from state.json)"
  ./06-collect-evidence.sh

  echo "== 7. Immediate post-run lockdown"
  ./07-post-run-lockdown.sh
  stage_d_completed=1

  echo
  echo "STAGE D EXPANSION STEP 1 COMPLETE."
  echo "Evidence: ${STAGE_D_WORKDIR}"
  echo "Remaining: verify the actual billed total (tokens AND web-search tool fees) in the"
  echo "Moonshot console, then record the outcome in STAGE_D_AUTHORIZATION.md."
)
```

## What the cleanup guarantees, failure by failure

| Failure point | What may be dirty | What the trap does |
| --- | --- | --- |
| Worker enable command | paid flag on, provider key bound | kill switch unbinds every provider alias, sets paid + catalog false |
| API enable command | run creation on, launcher `cloud_run` | kill switch resets the complete API execution surface |
| Posture verification | both enables applied | full lockdown |
| Probe creation, after only the db probe exists | one credentialed job standing | deletes **both** by name and proves absence from a fresh listing |
| Preflight / setup | probes exist | full lockdown |
| Run creation | a run may exist | full lockdown; the run id is already in `state.json` |
| Polling (including Ctrl-C) | a run is executing, and its DB row is `running` with reservations held | kill switch cancels the execution; the lockdown then **terminalizes the database run** and proves it terminal with zero active runs and zero dangling reservations |
| Evidence gate / replay | run finished, probes exist | full lockdown |
| Lockdown itself | — | block exits non-zero and tells you to rerun `./07-post-run-lockdown.sh` |

In every row the end state is: paid execution off, catalog execution off,
run creation off, launcher disabled, **both** provider aliases unbound on
both surfaces, zero active Worker executions, **both** probe jobs proven
absent from a fresh `gcloud run jobs list`, and — whenever a run was
created — the database run terminal with zero active runs for its user
and project and zero reservations left in `reserved`.

`tests/test_stage_d_toolkit.py` injects a failure at **every** mutation
boundary in the table above and asserts exactly that end state each time.

## If the cleanup itself fails

The block prints `CLEANUP INCOMPLETE` and exits non-zero. Production may
not be fail-closed. Immediately:

```sh
scripts/release/stage-d/kill-switch.sh
scripts/release/stage-d/07-post-run-lockdown.sh
```

and do not start anything else until both print their success lines.
