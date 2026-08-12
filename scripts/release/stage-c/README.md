# Stage C operator toolkit — one controlled paid smoke run

Implements the Stage C procedure of
[`docs/production-readiness/STAGED_ACTIVATION.md`](../../../docs/production-readiness/STAGED_ACTIVATION.md)
as an exact, auditable command sequence. Every script is manual-only,
prints no secret values, and does exactly one step. Run them **in order**
from an operator-authenticated `gcloud` shell (the account that owns
`big-cabinet-457321-t7`). The remote automation identity is read-only in
production by design; these steps are why.

| Step | Script | Mutates | Purpose |
| --- | --- | --- | --- |
| 0 | (pre-flight already verified — see `STAGE_C_ACCEPTANCE.md`) | no | IAM/secret/flag/exec-count invariants |
| 1 | `01-build-images.sh` | registry only | build `api`+`worker` images at the release SHA via Cloud Build (public repo cloned at the exact commit, SHA verified in-build) |
| 2 | `02-deploy-images.sh` | worker job, API service | deploy release images, **flags unchanged/off** |
| 3 | `03-enable-stage-c.md` (**manual commands** — by policy no committed script enables execution flags) then `03b-verify-stage-c-posture.sh` (read-only) | worker job, API service | smallest-safe caps; worker: paid flag on + `KIMI_API_KEY` binding (worker only); API: launcher + run creation on |
| 4 | `04-create-probes.sh` | creates 2 disposable jobs | `stagec-db-probe` (as `milo-api-runtime@`; DB checks/setup/evidence) and `stagec-gw-probe` (as `milo-vercel-gateway@`; drives the API) |
| 5 | `05-execute-smoke.sh` | one run | re-verifies launch invariants, creates test data, executes exactly ONE run through API → launcher → worker → provider → Supabase, monitors to terminal state, immediate idempotent-replay check |
| 6 | `06-collect-evidence.sh` | no | full post-run DB/lifecycle/budget evidence + post-completion replay check |
| 7 | `07-post-smoke-posture.sh` | worker job, API service, deletes probes | fail-closed posture: run creation off, launcher disabled, paid off, provider-key binding removed |
| any | `kill-switch.sh` | worker job, API service | immediate fail-closed (use at ANY sign of trouble) |

Notes:

- The Vercel gateway is deliberately untouched: `GATEWAY_ALLOW_EXECUTION_ROUTES`
  stays off, so no browser can reach run creation while the backend flag is
  on; the only caller that passes gateway auth is `stagec-gw-probe`
  (running as the approved gateway service account), and membership
  authorization further restricts run creation to the dedicated
  `stage-c-smoke` test user/project/conversation.
- The API keeps `MILO_ENABLE_PAID_EXECUTION=false`: it never holds the
  provider key (production config validation forbids the paid flag without
  it); the worker is the only paid-execution enforcement point and the only
  identity able to read `KIMI_API_KEY`.
- Do **not** run a second paid run. `05-execute-smoke.sh` refuses to run if
  any worker execution already exists.
- Caps are defined once in `stage-c-env.sh` (`STAGE_C_CAPS`); see
  `docs/production-readiness/STAGE_C_ACCEPTANCE.md` for the sizing
  rationale against the preserved pipeline shape.
