> **HISTORICAL — superseded by [`SCOPED_BATCH_PRODUCTION_RUNBOOK.md`](SCOPED_BATCH_PRODUCTION_RUNBOOK.md).** A dated record kept as evidence; do not follow it as current procedure.

# OPERATOR-4 — Production activation audit at Console 6 / #111 (2026-09-22)

Status: **BLOCKED.** The requested sequence was: final read-only audit →
deploy current `main` → canonical hard gate → activate the website execution
stage for a first bounded paid run. The audit stopped the sequence at its own
hard decision point. **Phases B–E were not performed.**

Production was **not** mutated. No deployment, no flag change, no migration, no
Government capture, no promotion, no run creation and no paid provider call
occurred. This document contains no credential and no secret.

---

## 1. Source

| Item | Value |
| --- | --- |
| Audited source SHA | `65bef570e2f5ca1da620e1c7202bdc3c958514e5` |
| Commit | Merge of PR #111 — website integration on the Console 6 architecture |
| Local migration files | 38 |
| Previous operator record | `OPERATOR_3_PRODUCTION_ALIGNMENT_2026-09-22.md` (base `7676b1e…`, PR #108) |

`git diff 7676b1e..65bef57 -- supabase/migrations/` is **empty**: PRs #109,
#110 and #111 introduced no migration requirement.

## 2. Audit result by prerequisite

| # | Prerequisite | Result |
| --- | --- | --- |
| 1 | Deployed revision identity (API / worker / frontend) | **UNVERIFIABLE from the audit session** — see §4 |
| 2 | Production migration alignment | **PASS** — 38/38, head `20260921000200` |
| 3 | Usable immutable Government snapshot | **FAIL — the blocker** — see §3 |
| 4 | RuntimePolicy / provider prerequisites | **CONTRACT ONLY** — live binding unverifiable, see §4 |
| 5 | Canonical execution path, no fallbacks | **PASS** — see §5 |

`AUDIT_GATE: BLOCKED`

## 3. Blocker 1 — Production holds no usable Government snapshot

Read directly through the read-only Production connector:

| Relation | Rows |
| --- | --- |
| `catalog_source_snapshots` | **0** (and 0 with `activated_at` set) |
| `catalog_raw_records` | 0 |
| `catalog_candidate_variants` | 0 |
| `catalog_models` / `catalog_model_variants` | 0 / 0 |
| `catalog_canonical_field_provenance` | 0 |

No snapshot row exists at all, so there is no resource identity, no
snapshot/version identity, no complete/usable state, a deterministic
`candidate` count of **0**, and **no bounded queue can be formed**.

### Why this is not latent

The intended first-run posture sets `MILO_ENABLE_CATALOG_EXECUTION=true` and
`MILO_ENABLE_GOVERNMENT_CATALOG_READ=true`. Per
`backend/catalog/execution.py`, `government_read_enabled()` requires the master
switch *and* the read flag, so `catalog_posture()` would return
`government_read=True`.

`backend/worker/main.py` then runs Government preparation **after the lease and
before the provider**:

```
prepare_government_work()                       backend/catalog/government/preparation.py
  └─ resolve_active_snapshot(snapshot_key=None) backend/catalog/government/projection.py
       └─ list_active_catalog_snapshots() → []  (0 rows in Production)
            └─ GovernmentProjectionError("GOV_PROJECTION_NO_ACTIVE_SNAPSHOT")
  └─ GovernmentPreparationError("GOVERNMENT_SNAPSHOT_UNAVAILABLE")
       └─ finalizer.finalize(TerminalClaim.refusal(...)); return 0
```

The refusal is taken through the canonical finalizer and returns **before the
provider adapter is constructed**. A first paid website run under the intended
posture would therefore terminalize as a refusal, spending nothing. The
fail-closed behaviour is correct; the point is that the run would not be a MILO
run.

`backend/catalog/execution.py` states the same fact about the deployed
environment in its own module docstring: what has kept the catalog path
harmless in practice is "a property of the deployed environment — a catalog
schema with no usable snapshot in it — and that is an accident, not a control."

### Why it was not fixed in place

Landing a usable snapshot requires one live Government capture. Per
`STAGED_ACTIVATION.md` that is a separate step with its own prerequisites
(AUTH-1 authorization, the OPERATOR-0 schema report acknowledged,
`MILO_ENABLE_PAID_EXECUTION` off, and a prepared operator capture run). It was
not authorized for this work, and substituting live `data.gov.il` transport was
explicitly excluded. It is therefore **not** a bounded deployment/config fix.

No code defect is involved — every component behaved as designed — so no
corrective PR was opened.

## 4. Blocker 2 — no deployment or Cloud Run read capability in the audit session

| Path | State |
| --- | --- |
| Google Cloud MCP server | `403 mcp_request_blocked` on every call (Cloud Run read; bare `projects list`) |
| `gcloud` CLI | not installed |
| `vercel` CLI | not installed |
| Cloud credentials in environment | none |
| Cloud Run / Vercel deploy workflow in `.github/workflows/` | **none** (only `ci`, `repo-scan`, `backup-supabase-production`, `deploy-supabase-migrations`) |

The canonical deployment mechanism, `scripts/deploy/cloud-run.sh`, requires an
authenticated operator `gcloud` session. Phase B was therefore not performable
by any canonical mechanism available, independently of the Phase A verdict, and
the deployed revision SHAs, the deployed RuntimePolicy values, the worker-side
provider credential binding, the shared quota/coordinator state and the
gateway identities could not be read.

Last recorded read-only measurement (OPERATOR-3): both Cloud Run surfaces serve
release `84cd8696…`, which predates Console 2–6, calls the now-absent
`create_message_and_run_v2`, and states no `MILO_RELEASE_SHA`. That release is
definitively not `65bef57` and is itself fail-closed.

## 5. What the audit did prove

| Check | Result |
| --- | --- |
| Migration history | 38 rows, head `20260921000200`, version-for-version identical to the repository set |
| `create_message_and_run_v2` / `bind_run_identity` in `pg_proc` | **0 / 0** — absent from Production |
| `create_message_and_run_v3` in `pg_proc` | present |
| Same two symbols in shipping code (`backend/`, `frontend/`) | absent |
| `backend/main.py` `hasattr(repo, "create_message_and_run")` | fail-closed 503 guard on the repository *method*, which calls the v3 RPC (`backend/repository/supabase.py`) — not a V2 fallback |
| Builtin `$web_search` | offered by no production engine; `standalone_search.py` deliberately uses MILO's own function tool |
| `public.runs` | 8 rows, **0 non-terminal** — no unexpected active run |
| `runs.run_identity` | 0 of 8 populated — all legacy, readable, never executable |
| `run_usage_ledger` | 465 rows, unchanged historical spend |
| Catalog promotion | 0 canonical rows — never run |

### RuntimePolicy contract (declared; live binding unverified)

18 dimensions are mandatory for paid execution; 13 carry **no** runtime default
and must be bound in the deployed environment or the runtime fails closed.
Declared values include provider max concurrency 2, provider RPM 3, V1
technical parallelism 1, V2 max active workers 4, Search Basic/Pro QPS 1/1,
hard monetary cap $3.00, first-paid-run execution cap 1.

## 6. Flag-contract note for the activation decision

The requested flag set is internally valid: promotion requires read, and read
does not imply promotion, so `MILO_ENABLE_CATALOG_PROMOTION=false` alongside
`MILO_ENABLE_GOVERNMENT_CATALOG_READ=true` is a legal posture. Asking for
promotion *without* read is refused loudly by `catalog_posture()`.

Setting `MILO_ENABLE_GOVERNMENT_CATALOG_READ=false` would skip preparation
entirely and let a `swarm_v2` run reach the provider — but that ships the
product without its Government capability. That is an operator product
decision, not a substitution this audit made.

## 7. What must happen before this sequence can be resumed

1. **One authorized bounded Government capture** landing a usable, active,
   fully normalized snapshot (issue count 0), so a deterministic candidate
   count and a bounded queue exist. This is the AUTH-1 step in
   `STAGED_ACTIVATION.md`, with its own acceptance record.
2. **Operator deployment of `65bef57`** (or the then-current canonical `main`)
   to API, worker and frontend through `scripts/deploy/cloud-run.sh` and the
   Vercel project, with `MILO_RELEASE_SHA` bound to the exact deployed code SHA
   on both backend surfaces and the API/worker release identities agreeing.
3. **The canonical hard gate re-run against that actually deployed release.**
   The pre-Console-6 Stage D authorization is obsolete and must not be reused.

Only then does the activation posture in Phase D become a decision rather than
a guess.

## 8. Execution-safety confirmation

This audit performed read-only queries against the Production database and
read-only inspection of the repository. It changed no flag, deployed nothing,
created no run, captured nothing, promoted nothing and made no paid provider
call. Catalog promotion remains off. The website execution stage remains
inactive.

---

# Addendum — authorized capture + deployment attempt (2026-09-22, same day)

The operator subsequently issued an **explicit authorization** to proceed past
both blockers in §3 and §4: to perform the canonical bounded Government
capture, activate the resulting snapshot, deploy the reviewed `main`, resolve
the live RuntimePolicy, run the zero-cost hard gate and enable the website
execution stage (stopping short of launching the first paid run).

**Result: still BLOCKED.** The authorization removed the *permission* barrier.
It did not remove the *capability* barrier, which is infrastructural. Nothing
was mutated in Production. No fabricated or fixture data was written, no
alternate deployment path was created, and no policy-denied endpoint was
retried after its first confirmation.

`origin/main` was re-verified as still exactly
`65bef570e2f5ca1da620e1c7202bdc3c958514e5` — no delta to audit.

## A. Capture specification (the pre-execution report, as requested)

Determined from the pinned canonical source module
(`backend/catalog/government/source.py`) and the ingestion contract
(`backend/catalog/government/ingest.py`):

| Property | Value |
| --- | --- |
| Source family | `government` |
| Publisher the metadata must still name | `ministry_of_transport` |
| Host (exact match, never suffix) | `data.gov.il` |
| CKAN package (allowlisted, checked BEFORE egress) | `degem-rechev-wltp` |
| WLTP resource id | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` |
| Allowed actions | `package_show`, `datastore_search` |
| Market (source property, never a per-row field) | `IL` |
| Page size sent | 100 (`DEFAULT_PAGE_LIMIT`); a server answering with more rows than asked is refused |
| Max page size ever accepted | 1000 |
| Boundedness | `MAX_PAGES_PER_CAPTURE` 200, `MAX_RECORDS_PER_CAPTURE` 120 000 — reaching either fails closed **before anything is persisted** |
| Max single response body | 8 MiB, applied while reading |
| Connect / read timeout | 10.0 s / 30.0 s |
| Attempts per request | 3, retryable statuses only; a deterministic refusal is never retried |
| Credential | none held, sent or accepted — the dataset is public |

**Activation criteria.** A snapshot is born `pending` and *cannot* be born
active: the payload preparer refuses `activated_at` and `stored_record_count`
as inputs, and the guarded RPC refuses them again. Activation is the **last**
step and the database's own gate refuses it unless the snapshot holds exactly
as many records as the upstream declared — so a prefix cannot be activated even
by a caller that wanted to. Any crash, cancellation or lost lease leaves a
non-active snapshot, which no reader will answer from.

**Integrity / digest.** Snapshot identity is a function of captured content:
`content_sha256` determines `snapshot_key`, record keys and candidate keys.
Exact replay is a deterministic no-op that collapses onto existing rows;
changed content is a *new* snapshot and never mutates the previous one.

**Raw-record immutability.** Every durable write carries `run_id`, `worker_id`,
`attempt` and `lease_token`, validated atomically by `assert_worker_lease`
before a byte is written. A later run may reuse an already-active identical
snapshot but may **not** adopt another run's unfinished capture. A failed
refresh never replaces the last valid active snapshot.

**Reading gap is durable.** `retrieval_metadata` carries the normalization
contract, rows read, rows refused, count per reason and a bounded list of
refused ids; a replay is held to it (`GOV_SNAPSHOT_NORMALIZATION_DRIFT`).

The smallest canonical capture that yields a genuinely usable snapshot is
therefore the **complete WLTP resource** — the activation gate requires the
full declared record count, so no reduced-sample mode exists to invent.

## B. Why the authorized capture could not be executed

`backend/catalog/operator_capture.py` requires, before any egress: `--execute`,
both exact acknowledgements, a `--project-ref` matching the project derived
from `SUPABASE_URL`, `MILO_ENABLE_CATALOG_EXECUTION` on, and a prepared
lease-owned run. It then writes snapshot and raw records **through the
service-role repository**. Measured facts from this session:

| Requirement | Measured state |
| --- | --- |
| Production DB identity available to this session | `supabase_read_only_user` |
| `pg_has_role(..., 'service_role', 'member')` | **false** |
| `INSERT` on `catalog_source_snapshots` | **false** |
| `INSERT` on `catalog_raw_records` | **false** |
| `SUPABASE_URL` / service-role key in session environment | **absent** (0 of 145 env vars carry any Supabase/GCP/Vercel/provider name) |
| Egress to `data.gov.il` | **denied at the egress proxy** — `CONNECT tunnel failed, response 403` |
| A GitHub Actions workflow that performs a capture | **none** |

Both legs of the capture are unavailable: the upstream read is denied by egress
policy, and the durable write is denied by database privilege. The only
remaining routes would be fabricating rows directly (explicitly forbidden, and
denied by privilege regardless) or building a new capture pipeline around
secrets this session cannot read (explicitly forbidden). Neither was attempted.

## C. Why the authorized deployment could not be executed

Authoritative enumeration through the GitHub API — the repository has exactly
**four** workflows: `ci`, `Repo Scan`, `Backup Supabase Production`,
`Deploy Supabase Migrations`. **None deploys Cloud Run or Vercel.**

| Mechanism | State |
| --- | --- |
| Google Cloud MCP | `403 mcp_request_blocked` (confirmed once this session, then not retried) |
| `gcloud` CLI | not installed, no credentials |
| `vercel` CLI | not installed, no credentials |
| Deployment workflow in CI | none exists |
| Authoring a new deployment workflow | explicitly forbidden by the operator instruction ("do not create an alternate deployment path"), and would require GCP/Vercel secrets this session cannot read or set |

The canonical mechanism, `scripts/deploy/cloud-run.sh`, requires an
authenticated operator `gcloud` session. It was not available.

Note for the operator: `list_environments` shows a distinct environment named
**"MILO Stage B"** alongside the two "Default — trusted network access"
environments. If one of those is the environment provisioned with Cloud Run /
Vercel credentials and `data.gov.il` egress, re-running this task there is the
natural next step. This session did not spawn a session elsewhere, because
doing so to get around an egress-policy denial would be circumventing an
organizational control rather than using an authorized mechanism.

## D. Phase E canonical path — re-verified, clean

| Check | Result |
| --- | --- |
| `create_message_and_run_v3` active; `_v2` and `bind_run_identity` absent | confirmed in Production `pg_proc` and in shipping code |
| Government preparation ordered after lease, before provider construction | confirmed (`backend/worker/main.py`) |
| Canonical finalizer / ProductOutcome authority | `backend/finalization.py`, `backend/product_outcome.py` |
| Provider Authority owns admission, retries, deadline, token accounting, search | `backend/provider_authority.py` |
| Lease token carried on every durable write | confirmed (`assert_worker_lease`) |
| `MILO_ALLOW_INSECURE_DEV_IDENTITY` | `INSECURE_DEV_IDENTITY_IN_PRODUCTION` — forbidden in production |
| `CLOUD_RUN_AUTH_MODE=e2e-test`, `MILO_E2E_INPROCESS_WORKER`, non-production `MILO_WORKER_ENGINE` | `TEST_ADAPTER_IN_PRODUCTION` — forbidden in production |
| `MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS` | `TIMED_LEASE_RECLAIM_IN_PRODUCTION` — forbidden in production |
| Builtin `$web_search` | offered by no production engine |

**No defect was found, so no corrective PR was opened.** Every component
behaved exactly as designed; the obstruction is environmental, not in the code.

## E. Standing blockers

1. **No Government snapshot, and no reachable way to create one** — needs a
   session with `data.gov.il` egress **and** Production service-role write
   access, running `backend/catalog/operator_capture.py` with both
   acknowledgements against a prepared run.
2. **No deployment capability** — needs an authenticated operator `gcloud`
   session (and Vercel) running `scripts/deploy/cloud-run.sh` at
   `65bef570e2f5ca1da620e1c7202bdc3c958514e5`, binding `MILO_RELEASE_SHA` to
   that exact SHA on both backend surfaces.

Until both are resolved, the website execution stage must stay off: arming it
against a release that cannot create a run, with no snapshot for preparation to
pin, would present a Task Composer that refuses every submission.
