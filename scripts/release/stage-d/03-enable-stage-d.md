# Stage D step 3 — enable the minimum paid-run surface (MANUAL ONLY)

`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. By repository policy
(`STAGED_ACTIVATION.md`, enforced by `scripts/check_unsafe_defaults.py`)
no committed script may enable an execution flag; the operator types these
commands deliberately, one at a time, from an authenticated shell. After
running them, execute `./03b-verify-stage-d-posture.sh` (read-only) before
proceeding.

**Nothing below has been executed.** This file is part of a PROPOSED Stage D
authorization; running it requires a fresh, explicit operator decision.

Kill switch at any time: `./kill-switch.sh`.

## 3.1 Pre-check — provider secret must be worker-only

```bash
gcloud secrets get-iam-policy KIMI_API_KEY --project big-cabinet-457321-t7
# Expect exactly one binding: roles/secretmanager.secretAccessor for
# serviceAccount:milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com
# (verified read-only 2026-09-18 — this is the current live state)
```

## 3.2 Worker job — paid flag + strict caps + provider envelope + provider key (worker only)

```bash
source scripts/release/stage-d/stage-d-env.sh   # exports STAGE_D_CAPS, STAGE_D_WORKER_PROVIDER_LIMITS etc.

gcloud run jobs update milo-agent-worker \
  --project "${STAGE_D_PROJECT}" --region "${STAGE_D_REGION}" \
  --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${STAGE_D_ON},${STAGE_D_CAPS},${STAGE_D_WORKER_PROVIDER_LIMITS}" \
  --update-secrets "KIMI_API_KEY=KIMI_API_KEY:latest"
```

(`STAGE_D_ON=true` is exported by `stage-d-env.sh` at operator run time —
the literal enable value never appears in a committed executable line.)

The Worker — and ONLY the Worker — receives the pinned provider operating
envelope (`STAGE_D_WORKER_PROVIDER_LIMITS`): concurrency 2, RPM 350,
TPM 2,400,000, 5 rate-limit retries, 240s max backpressure wait, 2s/30s
backoff. That is byte-for-byte the envelope Stage C Attempt 7 succeeded
under (0 retries, 0 backpressure events). The production Kimi organization
is operator-confirmed Tier 2 (concurrency 100 / RPM 500 / TPM 3,000,000 /
TPD unlimited); the envelope is deliberately far below that ceiling. That
Tier 2 confirmation authorizes NO provider call and NO paid run.

> **This command is a TIGHTENING, not a widening.** Production currently
> carries `MILO_PROVIDER_MAX_CONCURRENCY=8` on the Worker (drift from the
> later swarm-v2 smoke work, verified read-only 2026-09-18). The command
> above restores the Attempt 7 value of `2`, and `verify_caps.py` refuses
> the run while the live value is anything else.

## 3.3 API service — run creation + launcher + caps (paid flag STAYS false)

```bash
gcloud run services update milo-agent-api \
  --project "${STAGE_D_PROJECT}" --region "${STAGE_D_REGION}" \
  --update-env-vars "JOB_LAUNCHER=cloud_run,MILO_ENABLE_RUN_CREATION=${STAGE_D_ON},${STAGE_D_CAPS}"
```

Notes:

- The API receives `STAGE_D_CAPS` only. It must receive NO
  `MILO_PROVIDER_*` variable — provider scheduling configuration is
  Worker-only, and `verify_caps.py` fails on any `MILO_PROVIDER_*`
  variable found on the API service.
- The API must keep `MILO_ENABLE_PAID_EXECUTION=false`: it never holds the
  provider key (neither `KIMI_API_KEY` nor `MOONSHOT_API_KEY`, as a secret
  binding or a literal value), and `backend/production_config.py` refuses
  a production start with the paid flag set and no key. The worker is the
  sole paid enforcement point.
- `MILO_ENABLE_PROPOSAL_MUTATIONS` / `_PROPOSAL_READS` /
  `_RUN_CANCELLATION` / `_EXECUTION_CONTROL` all stay `false`.
- **`MILO_ENABLE_CATALOG_EXECUTION` stays `false` on BOTH surfaces
  throughout Stage D.** It is never enabled here, and Stage D is not an
  authorization to enable it. With it off the Government tool is not
  registered and the canonical promotion pipeline is not constructed in
  any run — which is also part of why the prepared Government capture run
  cannot be executed as a side effect of Stage D.
- The Vercel/browser execution surface is untouched —
  `GATEWAY_ALLOW_EXECUTION_ROUTES` stays off, so no browser can reach run
  creation while the backend flag is on. The only caller that passes
  gateway auth is `stage-d-gw-probe`, running as the operator-controlled
  approved gateway service account, and project membership then confines
  run creation to the dedicated `stage-d-smoke` test user/project.

## 3.4 Verify

```bash
scripts/release/stage-d/03b-verify-stage-d-posture.sh
```

## Rollback (any time)

```bash
scripts/release/stage-d/kill-switch.sh
```
