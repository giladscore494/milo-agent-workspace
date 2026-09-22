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
