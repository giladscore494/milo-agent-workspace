# Stage C step 3 — enable the minimum paid-run surface (MANUAL ONLY)

`REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. By repository policy
(`STAGED_ACTIVATION.md`, enforced by `scripts/check_unsafe_defaults.py`)
no committed script may enable an execution flag; the operator types these
commands deliberately, one at a time, from an authenticated shell. After
running them, execute `./03b-verify-stage-c-posture.sh` (read-only) before
proceeding.

Kill switch at any time: `./kill-switch.sh`.

## 3.1 Pre-check — provider secret must be worker-only

```bash
gcloud secrets get-iam-policy KIMI_API_KEY --project big-cabinet-457321-t7
# Expect exactly one binding: roles/secretmanager.secretAccessor for
# serviceAccount:milo-worker-runtime@big-cabinet-457321-t7.iam.gserviceaccount.com
```

## 3.2 Worker job — paid flag + smallest-safe caps + provider limits + provider key (worker only)

```bash
source scripts/release/stage-c/stage-c-env.sh   # exports STAGE_C_CAPS, STAGE_C_WORKER_PROVIDER_LIMITS etc.

gcloud run jobs update milo-agent-worker \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${STAGE_C_ON},${STAGE_C_CAPS},${STAGE_C_WORKER_PROVIDER_LIMITS}" \
  --update-secrets "KIMI_API_KEY=KIMI_API_KEY:latest"
```

(`STAGE_C_ON=true` is exported by `stage-c-env.sh` at operator run time —
the literal enable value never appears in a committed executable line.)

The Worker — and ONLY the Worker — receives the pinned Attempt 7 provider
operating envelope (`STAGE_C_WORKER_PROVIDER_LIMITS`): concurrency 2,
RPM 350, TPM 2,400,000, 5 rate-limit retries, 240s max backpressure wait,
2s/30s backoff. The production Kimi organization is operator-confirmed
Tier 2 (concurrency 100 / RPM 500 / TPM 3,000,000 / TPD unlimited); the
envelope is deliberately below that ceiling and concurrency stays 2 to
match the preserved V1 engine parallelism. That Tier 2 confirmation does
NOT authorize a provider call or a paid smoke.

## 3.3 API service — run creation + launcher + caps (paid flag STAYS false)

```bash
gcloud run services update milo-agent-api \
  --project "${STAGE_C_PROJECT}" --region "${STAGE_C_REGION}" \
  --update-env-vars "JOB_LAUNCHER=cloud_run,MILO_ENABLE_RUN_CREATION=${STAGE_C_ON},${STAGE_C_CAPS}"
```

Notes:

- The API receives `STAGE_C_CAPS` only. It must receive NO
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
- The Vercel gateway is untouched — `GATEWAY_ALLOW_EXECUTION_ROUTES` stays
  off, so browsers cannot reach run creation; only the probe running as
  the approved gateway service account can, and project membership then
  confines it to the dedicated `stage-c-smoke` test user.

## 3.4 Verify

```bash
scripts/release/stage-c/03b-verify-stage-c-posture.sh
```

## Rollback (any time)

```bash
scripts/release/stage-c/kill-switch.sh
```
