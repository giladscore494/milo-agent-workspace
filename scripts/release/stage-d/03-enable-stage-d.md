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
source scripts/release/stage-d/stage-d-env.sh   # generates STAGE_D_CAPS, STAGE_D_WORKER_PROVIDER_LIMITS,
                                                # STAGE_D_WORKER_ENGINE_LIMITS from backend/runtime_policy.py

# REQUIRED FIRST. The envelope below is generated from THIS CHECKOUT, while
# the run executes separately pinned release images. This proves the checkout
# IS the accepted release with the policy source unmodified, so the caps you
# are about to apply are the ones those images enforce. It refuses — and you
# must stop — if the checkout is any other commit, if the policy source is
# modified, or if the binding cannot be proven at all.
(cd scripts/release/stage-d && python3 ./policy_envelope.py binding)

gcloud run jobs update milo-agent-worker \
  --project "${STAGE_D_PROJECT}" --region "${STAGE_D_REGION}" \
  --update-env-vars "MILO_ENABLE_PAID_EXECUTION=${STAGE_D_ON},${STAGE_D_CAPS},${STAGE_D_WORKER_PROVIDER_LIMITS},${STAGE_D_WORKER_ENGINE_LIMITS}" \
  --update-secrets "KIMI_API_KEY=KIMI_API_KEY:latest"
```

(`STAGE_D_ON=true` is exported by `stage-d-env.sh` at operator run time —
the literal enable value never appears in a committed executable line.)

The Worker — and ONLY the Worker — receives the pinned provider operating
envelope (`STAGE_D_WORKER_PROVIDER_LIMITS`) and the pinned engine
parallelism (`STAGE_D_WORKER_ENGINE_LIMITS`). Both are GENERATED from
`backend/runtime_policy.py`, the one canonical runtime policy, so the exact
values are whatever `python3 scripts/release/stage-d/policy_envelope.py
provider-limits` and `… engine-limits` print — not a number transcribed
into this runbook, which is how they drift. Print them before you run the
command; `verify_caps.py` re-derives the same values and refuses the run on
any disagreement.

> **This command is a TIGHTENING, not a widening.** Three of the values it
> applies are strictly tighter than what production or the previous Stage D
> transcription carried:
>
> * `MILO_PROVIDER_MAX_CONCURRENCY` — production carries `8` (drift from
>   the later swarm-v2 smoke work, verified read-only 2026-09-18); the
>   policy value is `2`.
> * `MILO_PROVIDER_RPM_LIMIT` — the previous transcription pinned `350`
>   against MILO's organization ceiling of `80`.
>   `ProviderLimitsConfig.from_env` raises on exactly that, so the pinned
>   posture could not have started a Worker at all; the policy value is
>   `40`.
> * `MILO_SWARM_MAX_ACTIVE_WORKERS` — never pinned by this toolkit before,
>   and its code default of `4` is wider than the reviewed width of `2`.
>
> The verified Kimi Tier 2 limits for this organization are
> **inference concurrency 40 / RPM 100 / TPM 3,000,000 / TPD Unlimited**
> (`backend/provider_quota.py :: KIMI_TIER2_PROVIDER_LIMITS`, verified
> 2026-09-19). MILO's own organization ceiling is 80% of each finite value
> — concurrency 32 / RPM 80 / TPM 2,400,000 — and the envelope applied above
> sits at or below half of that. An earlier revision of this runbook quoted
> "concurrency 100 / RPM 500", which is not what the account was verified at
> and is what let a pinned RPM of 350 look reasonable. That Tier 2
> verification authorizes NO provider call and NO paid run.

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
