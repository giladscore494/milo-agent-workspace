# MILO — Repo-wide Cleanup Inventory (PR-0)

> **Status: INVENTORY ONLY.** This PR changes no code, deletes nothing, and adds
> nothing but this file. It is Phase A of the cleanup audit. Phase B asks Gilad
> for the decisions D1–D10 (section 4). No set in section 3 may be opened as a
> PR until this document is approved. Every merged cleanup PR is
> **merged-not-deployed (pending Gate 0)**.

> **Deployment status (updated 2026-09-25).** Production (API, worker and
> capture job) was deployed by the owner at `24ea412` on 2026-09-25 13:50 UTC.
> PR-0 to PR-3 (#125 `36a7b6d`, #126 `d097b1d`, #127 `d3a1743`, #128
> `24ea412`) are therefore **live**. The Gate 0 baseline is measured against
> `24ea412`. Cleanup PRs merged after that commit are merged-not-deployed until
> the owner's next deploy decision.

Governing plan: `MILO_REAL_RUN_ENABLEMENT_TECHNICAL_PLAN` Revision 2.2, §10
(PR-CLEAN) and Appendix A. Allowed classes: `ACTIVE | COMPATIBILITY_REQUIRED |
OPERATOR_ONLY | TEST_ONLY | SUPERSEDED | UNREACHABLE | DUPLICATE |
SAFE_TO_DELETE | UNKNOWN`. Only `SUPERSEDED`, `UNREACHABLE`, `DUPLICATE` and
`SAFE_TO_DELETE` rows with complete evidence may be deleted.

---

## 1. Baseline

| Item | Value |
|---|---|
| Audited SHA | `a8fec45c436e074eca0d745fe7f806b34da17b0d` (`main`, merge of PR #124 "PR-R" reasoning-aware budget, 2026-09-25) |
| Pre-scan SHA | `f641658030233290aba148798f0405a9a2683f4f` (PR #123). **`main` moved by 13 commits / 63 files.** Every pre-scan row below was re-verified at `a8fec45`, and line numbers are the ones at `a8fec45` |
| Date | 2026-09-25 |
| `reviewed_policy_fingerprint()` | `8f4ef66c58878468feeb9309eaa540dbaee7fb01f2425d2f65dca66df8476de1`. Differs from the pre-scan value `7ffc0f6d…` **because PR #124 changed the reviewed policy** (commit `28d1e8e`). **New baseline** |
| `event_registry.fingerprint()` | `c96c49ed53e1166a9298b94d223cbbb38b1987a9e194806fd702667917f18971`. Differs from `501a1b10…` because PR #124 added events (`backend/event_registry.py` +4). **New baseline** |
| `PINNED_POLICY_FINGERPRINT` | `scripts/release/stage-d/policy_envelope.py:114` = `8f4ef66c…`, equal to `reviewed_policy_fingerprint()` |
| FastAPI routes (`backend.main:app`) | 44 routes (method+path list reproduced by the §8.2 command, with `SUPABASE_URL`/`SUPABASE_SECRET_KEY` placeholders so `Settings()` validates) |
| `frontend/lib/server/gatewayPolicy.ts` | sha256 `9294e9ec1486f627de284ce13f83110c586ef25e6d629910816b4b9ecfc6842c` |
| `scripts/deploy/deployment-contract.sh` | sha256 `72d1d6be0ce7e2e94826f31325bbf0570f408ba5451d891c91663af117decc59` |
| `git ls-files backend` | 159 files |
| Python modules under `backend/` | 151 (`.py`) |

### 1.1 Commands used (all read-only)

```bash
# Fingerprints / routes (venv with backend/requirements.txt)
python -c "from backend.run_identity import reviewed_policy_fingerprint as f; print(f())"
python -c "from backend.event_registry import fingerprint as f; print(f())"
grep -n "PINNED_POLICY_FINGERPRINT" -r scripts backend
SUPABASE_URL=http://127.0.0.1:9 SUPABASE_SECRET_KEY=dummy python -c "from backend.main import app; print('\n'.join(sorted(f'{sorted(r.methods)} {r.path}' for r in app.routes if hasattr(r,'methods'))))"
# Static import graphs: Appendix A (Python) and Appendix B (TS/TSX), run from outside the repo
# Dynamic references: git grep over the whole tree for module (dotted and path), basename,
#   exported symbols, getattr/importlib/__import__/hasattr(repo, …), RPC names, event names,
#   shell paths and env vars (legacy/ and MILO-main-original/ reported separately)
git log --format='%h %ad %s' --date=short -- scripts/release/stage-c scripts/release/stage-d scripts/release/swarm-v2-smoke
diff -rq MILO-main-original/MILO-main legacy/milo-streamlit-v1 && sha256sum -c archive/SHA256SUMS.txt
# F4 experiment (outside the repo: `git archive HEAD frontend e2e` into a scratch dir)
npm ci && npm run build            # baseline
npm uninstall eslint eslint-config-next && rm -rf .next && npm run build && npx tsc --noEmit && npm test -- --run && npm run test:static
```

The clone was unshallowed (`git fetch --unshallow`) before the `git log`
and Stage D release-binding checks, so history is complete.

---

## 2. Per-area tables

Columns: path · lines · class · static callers · dynamic refs · test pins · CI refs · docs refs · evidence.
"Prod roots" means the AST graph from `backend.main`, `backend.worker.main` and
`backend.catalog.operator_capture` (Appendix A). It found **118 of 151** backend
modules reachable. The 33 unreachable ones are the B-rows below plus
`backend/testing/**` (test fixtures, loaded by Playwright and tests; not a
candidate).

### 2.1 Backend

| # | Path | Lines | Class | Static callers | Dynamic refs | Test pins | CI | Docs | Evidence / notes |
|---|---|---|---|---|---|---|---|---|---|
| B1 | `backend/engines/vehicle_catalog_v1/{client,constants,discovery,final_builder,normalization,orchestration,prompts,summary,technical,validators,verification}.py` | 11 × 1 | **SAFE_TO_DELETE** | none (AST graph; `__init__.py` imports only adapter, engine, workflow) | none. The regex `vehicle_catalog_v1[./](client\|…\|verification)` matches nothing anywhere, legacy included. Monkeypatches target `core`/`engine`/`adapter` only. `evidence_authority.py:156` `"vehicle_catalog_v1.verification"` is a **task key**, not an import | none. Only `core.py` is read by tests (`test_provider_backpressure.py:164`, `test_vehicle_catalog_engine.py:129`, `test_stage_d_toolkit.py:3005`) | none | none | Each file is one line of `from .core import …` (`orchestration.py`: `from .engine import …`). **Caveat:** `docs/roadmap/MILO_UNIFIED_ENGINE_3_STAGE_ROADMAP.md:144` marks `vehicle_catalog_v1/**` read-only for feature work, so an explicit OK is needed (it is not the D1 engine removal) |
| B2 | `backend/api/__init__.py` | 10 | **SAFE_TO_DELETE** (with its test) | `tests/test_api.py:418` only | no `backend.api` / `backend.api:app` in Dockerfiles, scripts, docs, CI | `test_compat_api_entrypoint_uses_protected_app` (`test_api.py:417-425`) | none | none | Its assertions are already covered on `backend.main:app`: `test_tool_grant_disabled_by_default_does_not_grant` (`test_api.py:279-283`, 403 `EXECUTION_SURFACE_DISABLED`), `test_disabled_surfaces_return_403_before_body_validation` (`:346-355`, includes `/runs/{run_id}/tool-grants` with `b""`), `…_even_for_invalid_path_ids` (`:358-361`), 401 by `test_unauthenticated_project_access_returns_401` (`:399-401`). Only `compat_app is app` is unique |
| B3 | `backend/internet_governance.py` + `tests/test_internet_governance.py` | 225 + 71 | **UNREACHABLE** | `tests/test_internet_governance.py:6` only | none (`InternetPolicy` also exists as a separate enum in `vehicle_catalog_v1/workflow.py:21` and a TS type in `frontend/lib/types.ts:6`; same name, not a use) | own test only | none | `MILO_UNIFIED_ENGINE_3_STAGE_ROADMAP.md:380,1310` (historical); `MIGRATIONS.md:40` names migration `005_internet_governance.sql` (different object: the SQL stays) | Production enforcement of the tool surfaces is `main.py:973-1013` + guard middleware, not this engine |
| B4 | `backend/tier2_profile.py` | 399 | **TEST_ONLY → keep this round** | tests only: `test_runtime_policy_authority.py:41`, `test_run_safety_contracts.py:328-401` (8 tests), `test_provider_concurrency_ownership.py:1440,1465` | docstring mentions: `runtime_policy.py:9,85,982`, `swarm_v2/engine.py:188`, `vehicle_catalog_v1/core.py:1276` | see evidence | none | `RUNTIME_POLICY.md:14,85`, `KIMI_TIER2_LIMITS.md:495` | **Pre-scan correction:** `reviewed_policy_fingerprint` lives in `run_identity.py:173-175` → `runtime_policy.py:723`. tier2 **imports from** runtime_policy (one-way) and only republishes the fingerprint (`:285`), so it cannot move the fingerprint. It is a derived document, not a value oracle. **However**, some literal pins (`hard_monetary_cap_usd == 3.00`, `max_run_duration_seconds == 1800`, ceiling `(32, 80, 2_400_000)`, provider limits) are asserted **only** through tier2 in `test_run_safety_contracts.py`. Deleting it loses those regression pins unless they are first ported to `POLICY` assertions. Deferred to D10 |
| B5 | `backend/catalog/digest.py` | 77 | **TEST_ONLY → keep** | `backend/testing/memory_repository.py:30,1536` | docstrings `catalog/scope/contract.py:40`, `government/snapshot.py:31` | `test_migrations_postgres.py:40,4881-4929`, `test_catalog_persistence.py:29,321,492,904-939` | none | none | **Pre-scan correction:** not a Python oracle of the SQL digest. It is the memory backend's own digest, and `test_the_raw_record_digest_is_storage_local_and_the_two_backends_differ` asserts it **differs** from PostgreSQL's. It is needed by the memory repository, so not deletable. It could only move under `backend/testing/` (an image-content change, no value) |
| B5b (new) | `backend/model_pricing.py` | 44 | **SUPERSEDED** | tests only: `test_model_profiles.py:8,37-45`, `test_corrective_blockers.py:10,111` | `model_profiles.py:8` ("It replaces `backend.model_pricing`") | the two tests above | none | `STAGE_C_ACCEPTANCE.md:444` (historical prices) | Its own docstring says it is a "compatibility surface over backend.model_profiles". `test_corrective_blockers.py:111` uses `calculate_model_cost` as the expected-cost oracle, replaceable by `get_profile(...).usage_cost(...)` (value-identical by `test_model_profiles.py`) |
| B6 | `backend/catalog/government/reconcile.py` + re-export `government/__init__.py:38-39,44-52` | 386 | **UNREACHABLE** (built, never connected) → **DECISION D9** | none in production. Loaded only because the package `__init__` re-exports it | no production reader of `reconcile_catalog`, `AliasRule`, `CatalogGap`, `CatalogMatch`, `ReconciliationReport`, `variants_from_candidate_rows`, `REVIEWED_ALIAS_RULES`; state strings `under_enriched`/`legacy_only`/`government_only` in no migration | `tests/test_catalog_pr3_swarm_promotion.py:45-48` (imports the **submodule**), 4 tests `~:1106-1198` | none | `MILO_GAP_AUDIT_2026-09-16.md:346` names it as the successor of S2-05; `catalog-pr3-swarm-and-promotion.md:255,508` | The tests are pure-function tests and still true of the module. Dropping only the `__init__` re-export breaks no test, but `government/__init__.py` is DO-NOT-TOUCH, so it goes with D9. Deleting the module needs a product answer ("is reconciliation still planned?") because the gap audit counts it as delivered capability |
| B7 | `backend/tools/mock.py` + re-export `tools/__init__.py:4-6` | 244 | **TEST_ONLY** | none in production; `mock_engine.py` and `testing/e2e_app.py` do **not** use it | `swarm_v2/fragments.py:38` (comment) | `from backend.tools import Mock…` in `test_swarm_v2_tool_contract.py:45`, `test_swarm_v2_runtime.py:31`, `test_swarm_v2_worker_output_repair.py:38`, `test_swarm_v2_r3_evidence_contract.py:53` | none | `MILO_GAP_AUDIT…:323`, roadmap `:322,330` | **Pre-scan correction:** 4 test files (not 3) use the package re-export. Because production imports `backend.tools` for `ToolRegistry`, the re-export loads mock tools into production processes. Safe refactor: remove the re-export and point the 4 imports at `backend.tools.mock`. The file stays |
| B8 | repository methods (per-method table in §2.1.1) | — | mixed | — | — | — | — | — | Only `list_unread_agent_messages` is free to delete. The others are locked by `test_release_inventory.py:93-103` (the `probe_db.py` `REQUIRED_RPC_ARGS` literal is derived by AST from `supabase.py`), `test_worker_rpc_acl_postgres.py:190` (`EXCLUDED_RPCS <= inventory`) and direct tests |
| B9 | `/workflow-proposals*` (`main.py:876-933`) + `backend/workflow_proposals.py` (158) + `MILO_ENABLE_PROPOSAL_*` | — | **SUPERSEDED (dormant) → D3** | routes live in `backend.main` | flags `false` in `config/production.example.yaml:83-84`, `deployment-contract.sh:35-36` | many (`test_api.py`, frontend `workspace`, `stateOwnership`, gateway tests, e2e `enabled.lifecycle`) | e2e | several | Prod: `workflow_proposals` has 0 rows (§2.2 of the prompt). Wired end to end in the UI (see F3) |
| B10 | worker HTTP routes `main.py:973-1096` (`/runs/{id}/tool-access-requests`, `tool-grants`, `tool-usage`, `sources`, `claims`, `conflicts`, `/internal/runs/{id}/events\|complete\|fail`) | — | **UNREACHABLE → D4** | no first-party caller. The gateway blocks them | event registry entries (a removal moves `event_registry.fingerprint()`) | `test_api.py:279-361` (the 403 contract) | — | — | `tool_usage`/`sources`/`claims`/`conflicts` **tables** are ACTIVE (the Swarm V2 EvidenceBoard writes them); only the HTTP routes are dead |
| B11 | supervisor shadow (`worker/main.py:272-295`, call sites `:299,350,463,559`), `backend/supervisor.py` (422) | — | **ACTIVE (shadow) → D2**; controlled-autonomy block `supervisor.py:211-422` **TEST_ONLY** | worker imports only the shadow names (`worker/main.py:17`) | `hasattr(repo, "create_supervisor_decision")` (`worker/main.py:283-284`) | `tests/test_controlled_supervisor_autonomy.py` (controlled block) | — | — | The pre-scan range 272-296 is essentially correct at HEAD (272-295). `initial_blackboard` → `build_milo_blueprint()` (`supervisor.py:128`), so V2 depends on V1's `workflow.py` |
| B12 | V1 engine + `worker/mock_engine.py` (V1 slot, `workflow_key = "vehicle_catalog_v1"` at `:39`) | — | **ACTIVE → D1** | `POST /conversations/{id}/runs` in a V1 project | `worker/main.py:393,395,909` `worker_provider_api_key`; `product_outcome.py:634` fallback | 13+ test files | — | — | 3 V1 projects in prod |
| B13 | truthy-flag parsers | — | **DUPLICATE** (optional consolidation) | `execution_guard.py:64`, `budget.py:61`, `gateway_auth.py:89`, `catalog/execution.py:86-87`, `runtime_policy.py:487-488` (`TRUE_VALUES` `:113`), `production_config.py:113-114`, **plus** `catalog/operator_capture.py:572,693` (missed by the pre-scan) | — | parity test only for the budget copy (`test_catalog_operator_capture.py:513-525`, inputs `" on "`, `"ON"`) | — | — | All are semantically identical: `{1,true,yes,on}`, `.strip().lower()`, unset → `""`. Three copies hard-code the set instead of importing `TRUE_VALUES`. `production_config.TRUE_VALUES` is a **mutable** `set` copy (`:25,30`) |
| B14 | `Settings.rate_limit_per_minute` (`config.py:19`) / `RATE_LIMIT_PER_MINUTE`; `Settings.environment` (`config.py:13`) | 2 | **UNREACHABLE** | no `settings.rate_limit_per_minute` / `settings.environment` read anywhere (only `api_title`, `cors_origin_list`, `gcp_*`, `cloud_run_worker_job`, `job_launcher`, `supabase_*` are read) | `ENVIRONMENT` is read **directly** from env by `production_config.py:118,390` and `gateway_auth.py:86` (those stay). The rate limiter uses `MILO_RATE_LIMIT_*` (`rate_limit.py:37-40`) | none | none | `ENVIRONMENT_MATRIX.md:32`; `check-production-config.sh:80` inventory row | `SettingsConfigDict(extra="ignore")`, so a leftover `RATE_LIMIT_PER_MINUTE` / `ENVIRONMENT` env var cannot break `Settings()` after the fields go |
| B15 | `MILO_SWARM_MAX_ACTIVE_WORKERS` | — | **DUPLICATE, document only** | `runtime_policy.py:398-401` (reviewed 2, default 4) and `swarm_v2/executor.py:42-48` (default `"4"`, range 1-32) | worker takes `min(…)` at `worker/main.py:998-1001` | `test_output_cap_concurrency.py:387-389` and others | — | `website-integration.md:286` | No behavior change this round |
| B16 | `pytest==9.1.1` in `backend/requirements.txt:7` | 1 | **TEST_ONLY → D7** | CI installs only this file before pytest (`ci.yml:37,133,165`) | — | — | ci.yml | — | Ships in both images (`Dockerfile.api:3-4`, `Dockerfile.worker:3-4`) |
| B17 | memory parity | — | coverage gap (§5) | — | — | — | — | — | not cleanup |
| B18 | worker has no `validate_production_config` | — | risk (§5) | — | — | — | — | — | not cleanup |

#### 2.1.1 B8, per repository method

| Method | `supabase.py` | `memory_repository.py` | Calls | Callers outside `backend/repository` + `backend/testing` | Class |
|---|---|---|---|---|---|
| `list_unread_agent_messages` | Protocol `:168`, impl `:1159-1160` | absent | direct `agent_messages` select | **none anywhere, tests included** | **SAFE_TO_DELETE** |
| `create_user_message` | Protocol `:32`, impl `:411-414` | `:254` (used internally by `create_message_and_run`, `:310`) | direct `messages` insert | `test_repository_supabase.py:167` | COMPATIBILITY_REQUIRED (Protocol + memory use) |
| `create_queued_run` | Protocol `:33`, impl `:416-430` (always raises `RUN_IDENTITY_ATOMIC_CREATION_REQUIRED`) | `:259` (refuses) | — | refusal pinned by `test_repository_supabase.py:~178-190`, `test_memory_repository_identity_parity.py:33-35` | COMPATIBILITY_REQUIRED (a tombstone whose refusal is the contract) |
| `mark_run_failed` / `mark_run_complete` | Protocol `:71-72`, impl `:1070-1082` (always raise `CANONICAL_FINALIZER_REQUIRED`) | `:626-630` (**succeeds**) | — | refusal pinned by `test_repository_supabase.py:193-202`; `test_canonical_finalization.py:207` forbids worker calls; the memory versions are test helpers (`test_worker.py:198,257-267`, `test_website_product_path.py:169,188,207`, `test_catalog_pr3_swarm_promotion.py:1909,1966`) | COMPATIBILITY_REQUIRED. The backends **disagree** (finding §5.6) |
| `reserve_daily_user_budget` / `reserve_daily_project_budget` | `:613-631` (not in Protocol) | absent | RPCs of the same name, revoked from `service_role` (migration 014) | `scripts/release/migration_state.py:64`, `stage-d/probe_db.py:407-408` (`REQUIRED_RPC_ARGS`), `scripts/check_migrations.py:69-70`; `test_worker_rpc_acl_postgres.py:59-60,190`; `test_migrations_postgres.py:1522,1535` | COMPATIBILITY_REQUIRED (the RPC-inventory derivation needs the call site) |
| `record_catalog_raw_record` (singular) | Protocol `:102`, impl `:1377-1381` | `:1522` (called by the memory **batch** path, `:1720`) | RPC `record_catalog_raw_record_guarded` (`probe_db.py:386`, `check_migrations.py:155`) | `test_catalog_persistence.py:44,228-233` requires it on both classes; `test_catalog_government_ingestion.py:596`; `test_catalog_operator_capture.py:792-794` | COMPATIBILITY_REQUIRED |
| `bind_work_scope_batch_run` | Protocol `:159`, impl `:1961-1970` | `:3081` | RPC `bind_work_scope_batch_run`, **ACTIVE in SQL** (called by `create_work_scope_batch_run`, migration `20260924000100…:487`) | `probe_db.py:314`, `deployment-contract.sh:334`, `check_migrations.py:120`; `test_work_scope_preparation.py:599-732`, `test_work_scope_batches.py:234` | COMPATIBILITY_REQUIRED (the wrapper keeps the RPC in the release inventory) |

#### 2.1.2 New backend candidates (not in the pre-scan)

Each has no references anywhere in the repo, tests included, except where noted.

| Symbol | Class | Touchable? |
|---|---|---|
| `backend/gateway_auth.py:80` `gateway_auth_configured()` | UNREACHABLE | yes (not on DO-NOT-TOUCH) → set 3f |
| `backend/schemas.py:328` `class RunCheckpoint` | UNREACHABLE | yes → set 3f |
| `backend/model_profiles.py:262` `profile_summary()` (only in `__all__` `:283`) | UNREACHABLE | yes → set 3f |
| `backend/engines/swarm_v2/adapters.py:14` `StateStore` | UNREACHABLE | **no**: `backend/engines/swarm_v2/*` is DO-NOT-TOUCH. Listed only |
| `backend/execution_usage.py:279` `ledger_dimensions()` | UNREACHABLE | **no**: DO-NOT-TOUCH. Listed only |
| `backend/provider_authority.py:1042` `build_provider_adapter()` (only in `__all__`) | UNREACHABLE | **no**: `backend/provider_*.py` is DO-NOT-TOUCH. Listed only |
| `backend/tier2_profile.py:389` `reviewed_environment()` | UNREACHABLE | goes with B4 / D10 |
| `provider_scheduler.estimate_request_tokens` (`:143`), `rate_limit.reset_for_tests` (`:137`) | TEST_ONLY | no: deliberate test hooks / DO-NOT-TOUCH |
| `vehicle_catalog_v1/core.py` Streamlit leftovers (`main`, `render_sidebar`, `run_pipeline`, …) | COMPATIBILITY_REQUIRED | no: frozen V1 code, parity-tested against `legacy/` by `test_vehicle_catalog_engine.py` |

### 2.2 Frontend

TS/TSX graph (Appendix B), roots `app/page.tsx`, `app/layout.tsx`,
`app/api/deployment-status/route.ts`, `app/api/gateway/[...path]/route.ts`,
`next.config.mjs` (no `middleware.ts` exists): **62 reachable files**. The only
unreachable file under `app/`, `components/`, `lib/` is
`components/run/RunOutputPanel.tsx`.

| # | Path / symbol | Lines | Class | Static callers | Dynamic refs | Test pins | CI | Docs | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| F1 | `frontend/components/run/RunOutputPanel.tsx` | 33 | **UNREACHABLE (SUPERSEDED)** | `tests/finalResultPanel.test.tsx:14` only | `'Final artifacts'` marker in `scripts/static-ui-check.mjs:44` (comment `:45-49`) is satisfied **only** by `RunOutputPanel.tsx:25` | `staticUiCheck.test.ts:25,35-40` (deletes this file and expects `Missing UI marker: Final artifacts`); `finalResultPanel.test.tsx:14,269-280,528-535` | `npm run test:static` | `docs/swarm-v2-final-result.md:5,22`; `MILO_GAP_AUDIT…:401,570` | Page render removed by `2622a5e` (2026-09-22), which gave V1 a typed result panel instead (`VehicleCatalogResultPanel`, rendered at `page.tsx:1382`; comment `:1370-1372`). e2e `enabled.final-result.spec.ts:54,222` and `finalResultRouting.test.tsx:82,104,142` assert the heading count is **0**, so they stay true after deletion. Replacement marker: `'Pipeline quality'` (`components/result/VehicleCatalogResultPanel.tsx:147`, the V1 typed-result surface). It is unique repo-wide and absent from `app/`, so the "V1 surface distinct from Swarm `Final result`" intent of `:45-49` is preserved |
| F2 | `lib/api.ts:9` `clientConfig` | — | **UNREACHABLE** | none | `tests/test_execution_gate_chain.py:41-43` reads `api.ts` but checks only `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (in `executionUiEnabled`, which stays) | none | — | — | |
| F2 | `lib/api.ts:96` `api.proposal` | — | UNREACHABLE → **goes with D3** | none | — | stubbed as `proposal: vi.fn()` in 10 test files (harmless) | — | — | Gateway GET rule `gatewayPolicy.ts:117` must stay (e2e `enabled.lifecycle.spec.ts:19,37` call the route) |
| F2 | `lib/runStatus.ts:39` `isActiveRunStatus` | — | **UNREACHABLE** | none | — | none | — | — | After removal, `ACTIVE_SET` (`:33`) is unused. `ACTIVE_RUN_STATUSES` (`:22`) / `ActiveRunStatus` (`:31`) have no other reader either. Set 1e removes only the function and `ACTIVE_SET`, and keeps the exported list as documentation |
| F2 | `lib/sanitize.ts:2` `safeUrl` | — | **UNREACHABLE** | none | — | none | — | — | The one rendered external link is already http/https-checked in `vehicleCatalogResult.ts:128-129`, so no guard is lost |
| F2 | `lib/server/rateLimit.ts:173` `normalizeRateLimitKey` | — | **UNREACHABLE**, but **not touchable this round** | none | — | none | — | — | Its comment ("used by the legacy gateway path/tests") is false. `frontend/lib/server/*` is on the DO-NOT-TOUCH list, so it is listed only and excluded from set 1e |
| F3 | `components/proposals/WorkflowProposalPanel.tsx` (105) + `page.tsx:65,947-982,1284-1298` + `api.ts:90-108` | — | **ACTIVE UI over a dormant backend → D3** | `page.tsx:65` | gateway rules `gatewayPolicy.ts:116-123,141,197` | `workspace.test.tsx:177-184,282-304`, `stateOwnership.test.tsx:195-227`, gateway tests, e2e `enabled.lifecycle`, `disabled.default-posture`; `'Workflow proposal'` static marker | e2e | — | **Pre-scan correction:** not a placeholder. It is imported, rendered and wired to `createProposal` / `decideProposal` / `reviseProposal`. It is non-functional in production only because `MILO_ENABLE_PROPOSAL_*` are `false`. Removal is a product decision |
| F4 | `eslint` `^8.57.0`, `eslint-config-next` `^15.5.25` (`package.json:30-31`) | 2 | **SAFE_TO_DELETE** | none | no config file anywhere, no `eslintConfig`, no `lint` script, no CI step; `next.config.mjs` has no `eslint` key | none (one inline `// eslint-disable-next-line` comment at `frontend/tests/finalResult.test.ts:885`) | none | — | **Verified by experiment** (§1.1): with both packages uninstalled, `next build` (Next 15.5.25) succeeds with byte-identical output apart from timing and first-run telemetry text. The "Linting and checking validity of types" line appears in both. `tsc --noEmit` exits 0, `npm test -- --run` gives 46 files / 844 tests passed, `test:static` exits 0. Next 15 finds no ESLint config and silently skips lint. Removal requires regenerating `package-lock.json` with `npm` |
| F5 | `lib/useRunRealtime.ts` | — | **ACTIVE** (polling) | — | no `.channel(`, `postgres_changes`, `removeChannel`; the only `subscribe` is auth (`supabaseClient.ts:71-74`) | `realtimeDisabled.test.ts:12-16` asserts only mode `'polling'` | — | `layout.tsx:2` says "Realtime" | Nothing to delete. Rename is out of scope |
| F6 (new) | test-only exports: `reconstructRun` (`runReducer.ts:74`, also required by name in `static-ui-check.mjs:137`), `reduceSwarmEvents` (`swarmReducer.ts:427`), `selectRunningSwarmTaskIds` (`swarmViewModel.ts:203`), `isUnsuccessfulTerminalRunStatus` (`runStatus.ts:59`), `hasRunIdentity` (`runIdentity.ts:71`), `isEventId` (`eventId.ts:57`), `CLASSIFIED_ERROR_CODES` (`errorText.ts:207`) | — | **TEST_ONLY → keep** | tests only | — | yes | — | `website-integration.md:136,193` wrongly say `reconstructRun` rebuilds state on refresh (live code uses `reduceRunEvent`) | Tested pure functions or pinned by static checks. Not cleanup. The doc line is a PR-4 fix |

`e2e/mock-supabase.mjs` (root) is **ACTIVE**: `frontend/playwright.config.ts:81-82` runs `node e2e/mock-supabase.mjs` with `cwd: REPO_ROOT` (`:16`).

### 2.3 Scripts / CI

| # | Path | Files / lines | Class | Inbound refs | Test pins | CI refs | Evidence |
|---|---|---|---|---|---|---|---|
| S1 | `scripts/runbooks/mock-e2e.md` | 1 / 11 | **SAFE_TO_DELETE** | none (`mock-e2e` and `runbooks/` both return nothing) | none | none | Only file in `scripts/runbooks/` |
| S2 | `scripts/release/swarm-v2-smoke/` (`run-swarm-smoke.sh` 329, `parse_env_contract.py` 217, `parse_run_state.py` 154, `parse_iam.py` 124, `parse_serving_state.py` 92, `parse_executions.py` 66) | 6 / 982 | **SUPERSEDED** | `stage-c/07-post-smoke-posture.sh:12` (comment) | `tests/test_release_tooling_swarm_smoke.py` (whole file); `test_catalog_execution_flag.py:496-502` (`test_the_smoke_env_contract_keeps_the_catalog_flag_off_in_every_posture`); `test_swarm_v2_smoke_offline.py:117` (docstring) | `ci.yml:59` shellcheck glob `scripts/release/swarm-v2-smoke/*.sh` (**not** locked by `test_ci_workflow_static.py:267-273`) | `RUNTIME_POLICY.md:18`, `ENVIRONMENT_MATRIX.md:44`, `STAGED_ACTIVATION.md:80`, `docs/deployment/swarm-v2-smoke.md:3,15,49`, `FINAL_ACCEPTANCE.md:68`, `MILO_GAP_AUDIT…:89,420,421,645,807`. **Pre-scan correction:** the `kill` mode is in `run-swarm-smoke.sh:306-323` (→ `canonical_shutdown()` `:294-303` → `stage-c/kill-switch.sh` via `KILL_SWITCH` `:58`), not in `parse_env_contract.py`. **PR #124 edited `parse_env_contract.py:58-65`** (Commander `kimi-k2.6` → `kimi-k3`, allowlist `kimi-k3,kimi-k2.6`). Its new comment "a worker naming any other is now refused at boot" (`:58-62`) contradicts review fix `f29c0e6`. Nothing in `scripts/deploy`, `scripts/release/lib`, other release scripts or `backend/` consumes it. Last touched `28d1e8e` (2026-09-25); 9 commits total |
| S3 | `scripts/release/stage-c/` | 17 / 3,055 | **SUPERSEDED**, but `kill-switch.sh` (319) + `stage-c-env.sh` (106) **COMPATIBILITY_REQUIRED → D5** | `swarm-v2-smoke/run-swarm-smoke.sh:23,42,58,307`; `release_inventory.py:85` (`TOOLING_SOURCES`; a missing file is skipped `:251-254`, and **no RPC is contributed only by stage-c**); comments in `catalog/government-production-capture.sh:216`, `stage-d/README.md:429`, `stage-d-env.sh:20` | `test_stage_c_toolkit.py`; `test_kill_switch.py:17`; `test_catalog_execution_flag.py:521-535`; `test_release_tooling_swarm_smoke.py:742-751`; `test_stage_d_toolkit.py:49,360-382 (S5),425-443,2564` | **not** in the shellcheck globs (stage-c scripts are never linted) | `ROLLBACK.md:41`, `docs/deployment/swarm-v2-smoke.md:31,49`, `STAGED_ACTIVATION.md:79`, `STAGE_C_ACCEPTANCE.md:94,340,670`. `kill-switch.sh` sets **only** worker `MILO_ENABLE_PAID_EXECUTION`/`MILO_ENABLE_CATALOG_EXECUTION` (`:43`), removes provider-key aliases (`:55-64`, `:77-84`), and sets API `MILO_ENABLE_RUN_CREATION`, `PROPOSAL_MUTATIONS`, `PROPOSAL_READS`, `RUN_CANCELLATION`, `EXECUTION_CONTROL`, `PAID_EXECUTION`, `CATALOG_EXECUTION`, `JOB_LAUNCHER=disabled` (`:71`). It does **not** touch `MILO_ENABLE_WORK_SCOPE_*`, `MILO_ENABLE_GOVERNMENT_CATALOG_READ`, `MILO_ENABLE_CATALOG_PROMOTION` or any Vercel variable (no Vercel step). A second scripted kill switch, `scripts/release/stage-d/kill-switch.sh` (325 lines; shellchecked by the locked stage-d glob; tested by `test_stage_d_toolkit.py:1507-1779`), covers the same flags (`:49`, `:77`) with the same gaps. Last touched `6e74e8d` (2026-09-16); 21 commits total, oldest `44198e5` (2026-08-12) |
| S4 | `scripts/release/stage-d/` | 23 / 7,100 | **SUPERSEDED**, with locked files → **D8** | no `backend/` or non-stage-d script **imports** it. `release_inventory.py:84` reads `probe_db.py` **text**. `run_identity.py:74`, `runtime_policy.py:20`, `check-production-config.sh:123` are comments, and `tier2_profile.py:142` is a string literal citing a line. **Pre-scan correction:** `run_identity.py:74` is a docstring, not an import | `policy_envelope.py:114` `PINNED_POLICY_FINGERPRINT`: `test_runtime_policy_authority.py:327-340,388-413,789-791,816-818`, `test_standalone_search.py:1196-1198`, `test_release_inventory.py:47,192,267`, `test_stage_d_toolkit.py:819-870`. `probe_db.py:301` `REQUIRED_RPC_ARGS`: `test_release_inventory.py:46,99-102,143`, `test_stage_d_toolkit.py:1258,4052-4111,4179,4260-4267`. `semantic_acceptance.py`: `test_canonical_finalization.py:711,794-795`. `stage-d-env.sh`: `test_runtime_policy_authority.py:348`, `test_stage_d_toolkit.py:526,2453,3253,3265`. `verify_caps.py`: `test_runtime_policy_authority.py:358`, `test_release_inventory.py:299`, `test_stage_d_toolkit.py:586,659,935,981`. `verify_images.py`: `test_release_inventory.py:276`, `test_stage_d_toolkit.py:2455,2626` | `ci.yml:59` shellcheck glob `scripts/release/stage-d/*.sh` **locked** by `test_ci_workflow_static.py:267-273`; `-P …:scripts/release/stage-d:…` | Still actively edited: last touched `28d1e8e` (2026-09-25, PR #124 re-pinned the fingerprint); 24 commits total. It is the release-binding authority for the policy pin, so "SUPERSEDED" applies to the Stage D run procedure, not to the pins |
| S5 | `tests/test_stage_d_toolkit.py:360-382` | — | blocker for S3 | `git diff --name-only 84cd8696119c24662a954d0f0e23195268dab23f -- scripts/release/stage-c` (`RELEASE_SHA` at `:51`); skips only if git fails (`:381`) | — | — | Passes at HEAD (diff empty). Any stage-c edit or deletion fails it |
| S6 | `production-readiness.sh` (290) + `check-production-config.sh` (507), `check-migration-state.sh` (338), `check-gcp-resources.sh` (272), `check-vercel-config.sh` (253), `check-secret-metadata.sh` (206), `check-redis-config.sh` (119), `check-service-connections.sh` (114) | 8 / 2,099 | **DUPLICATE, not this round** | orchestrator `run_subaudit` (`production-readiness.sh:169-221`) | `test_ci_workflow_static.py:260-279`; `test_release_tooling.py:165` | `ci.yml:62-68` (step name must contain "OFFLINE" and "does NOT verify live production") | `check-migration-state.sh` is DO-NOT-TOUCH |
| S7 | `generate-deployment-plan.sh` (557) vs `scripts/deploy/cloud-run.sh` (798) | — | **DUPLICATE, not this round** | both source `deployment-contract.sh` | `test_deployment_tool_alignment.py` (25 tests), `test_deployment_plan_hardening.py:28,365`, `test_release_tooling.py:432,460`, `test_catalog_execution_flag.py:486`, `test_work_scope.py:761`, `test_provider_concurrency_ownership.py:927`; `tier2_profile.py:141` cites `generate-deployment-plan.sh:297` | shellcheck | — |
| S8 | `NEXT_PUBLIC_API_URL` | — | **UNREACHABLE** | no frontend reader (the frontend reads only `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`) | none | `ci.yml:24,79` | `check-production-config.sh:182` (`deprecated`); `ENVIRONMENT_MATRIX.md:57` ("remove from CI when convenient"); `docs/deployment/vercel-deployment.md:39,46`, `docs/deployment/stage-10-runbook.md:15,34` (both deleted by 1g) |
| S9 | `check-production-config.sh` inventory | — | **gap (add, not delete)** | — | — | — | **Still open at HEAD:** 0 occurrences of `MILO_RELEASE_SHA` or `GATEWAY_ALLOW_RUN_START_ROUTES`. PR #124 added only the model-contract block (`:346-368`). `MILO_RELEASE_SHA` is also absent from `ENVIRONMENT_MATRIX.md`. **Also stale at HEAD:** rows `:133-135` (`MILO_SWARM_WORKER_MODEL`, `MILO_COMMANDER_MODEL`, `MILO_COMMANDER_MODEL_ALLOWLIST`) name `backend/worker/main.py` as their source. The reads moved to `backend/model_profiles.py` in `f0a7ec9`, and the freshness check already WARNs on all three |
| S13 | `scripts/staging/redis-shim/Dockerfile` | 1 / 5 | **UNKNOWN** | nothing builds it. `staging-cloud-run.sh:34,209` only `describe`s an already-deployed `milo-redis-shim-staging` | sibling `main.py` (136) is loaded by `test_rate_limit.py:195-208` | none | `STAGING.md:21`. Needs Gilad: is the staging redis-shim service still deployed, and from what? |
| S14 | `generate-membership-backfill.sh` (162), `generate-proposal-backfill.sh` (153), `templates/proposal-backfill.example.json` (10) | — | **UNKNOWN, do not touch** | `migrations/008_workflow_proposal_ownership.sql:19`; `MIGRATIONS.md:494,497`; `AUTHORIZATION_AND_RLS.md:220,223` | `test_release_tooling.py:190,405-407,421` | shellcheck (`scripts/release/*.sh`) | one-time backfills; the template is referenced only by the script |
| CI | `.github/workflows/`: `ci.yml` jobs `offline-checks` (`:13`), `frontend-and-docker` (`:70`), `postgres-checks` (`:112`), `e2e` (`:150`); `repo-scan.yml` job `scan` "Lint & Test" (`:25`); `deploy-supabase-migrations.yml`; `backup-supabase-production.yml` | — | **ACTIVE** | — | `MANDATORY_CI_JOBS` (`test_ci_workflow_static.py:43`), `:142-148` (runbook names them; no extra jobs) | — | Job names are unchanged by every set below |

### 2.4 Docs

| # | Path | Lines | Class | Inbound refs | Test reads | Evidence |
|---|---|---|---|---|---|---|
| S10 | `docs/deployment/cloud-run-smoke-tests.md` | 37 | **SUPERSEDED** | 0 | none | exact-name and path search |
| S10 | `docs/deployment/stage-10-runbook.md` | 58 | **SUPERSEDED** | 0 | none | still promotes `NEXT_PUBLIC_API_URL` (`:15,34`). Its `:58` "never run test_websearch.py" is already in `AGENTS.md:9` |
| S10 | `docs/deployment/supabase-migrations.md` | 187 | **SUPERSEDED** | 0 exact (basename hits are all `deploy-supabase-migrations.yml`) | none | superseded by `MIGRATIONS.md` + `SUPABASE_BACKUP.md` + the SHA-bound workflows |
| S10 | `docs/deployment/vercel-deployment.md` | 46 | **SUPERSEDED** | 0 | none | promotes `NEXT_PUBLIC_API_URL` (`:39,46`) |
| S10 | `docs/auth-production-readiness.md` | 214 | **SUPERSEDED** | 0 | none | superseded by `production-readiness/AUTHENTICATION.md` / `AUTHORIZATION_AND_RLS.md` |
| S10 | `docs/target-architecture.md` | 38 | **SUPERSEDED** | 0 | none | superseded by `production-readiness/ARCHITECTURE.md` |
| S10 | `docs/project-workspace-foundation.md` | 44 | **SUPERSEDED** | 0 | none | |
| S11 | `docs/deployment/cloud-run-production.md` | 145 | **SUPERSEDED** | comments in `frontend/lib/sanitize.ts:38`, `frontend/tests/redactSecretText.test.ts:71`, `frontend/tests/secretSentinels.ts:26`; `docs/swarm-v2-final-result.md:212` | none | delete together with repointing those four references (to `production-readiness/DEPLOYMENT.md` / `ENVIRONMENT_MATRIX.md`) |
| S10b (new) | `docs/production-readiness/STAGING.md` | 77 | **UNKNOWN** | 0 (not in the README index) | none | depends on S13 (does staging still exist?) |
| S10b (new) | `docs/production-readiness/OPERATOR_1_PRODUCTION_MIGRATION_ALIGNMENT_2026-09-18.md` | 444 | **ACTIVE (historical record)** | 0 (not in the README index; OPERATOR_2/3 are) | none | Dated operator evidence record. **Do not delete.** Add to the README index as HISTORICAL in PR-4 |
| S10b (new) | `docs/production-readiness/OPERATOR_4_PRODUCTION_ACTIVATION_AUDIT_2026-09-22.md` | 326 | **ACTIVE (historical record)** | 0 | none | same as OPERATOR_1 |
| S10b (new) | `docs/vehicle-catalog-v1-engine.md` | 25 | **UNKNOWN** | 0 | none | V1 is ACTIVE (D1). Decide with D1 |
| — | `docs/proofs/R5_VEHICLE_EVIDENCE_PROOF.md` | 717 | **ACTIVE** | docstring `test_swarm_v2_r5_vehicle_proof.py:2429` | **none**. **Pre-scan correction:** no test reads it | Proof record. Keep |
| S12 | runbook contradictions | — | content fix (PR-4) | — | — | see §5.7 |

**Docs locked by tests** (content editable; do not delete or move without updating the test):

| Doc | Test that reads it |
|---|---|
| `SCOPED_BATCH_PRODUCTION_RUNBOOK.md` | `test_ci_workflow_static.py:143` |
| `ENVIRONMENT_MATRIX.md` | `test_catalog_execution_flag.py:539` (frontend only names it: `secretBundleCheck.test.ts:75`) |
| `MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md` | `test_catalog_execution_flag.py:546-547,559-560` |
| `STAGED_ACTIVATION.md` | `test_catalog_execution_flag.py:548`; `test_stage_d_toolkit.py:2572` |
| `FINAL_ACCEPTANCE.md`, `SMOKE_TESTING.md`, `DEPLOYMENT.md` | `test_catalog_execution_flag.py:549-551` (DEPLOYMENT path string also in `test_cloud_run_deploy_static.py:404`, `test_deployment_plan_hardening.py:272`, `test_cloud_run_deploy_apply_mock.py:679`) |
| `docs/deployment/swarm-v2-smoke.md` | `test_catalog_execution_flag.py:552` |
| `MIGRATIONS.md` | `test_migration_state_verification.py:655,661`; `test_production_config.py:448` |
| `SUPABASE_BACKUP.md` | `test_supabase_backup_workflow_static.py:5,71,91,104` |
| `docs/catalog-pr2-government-ingestion.md` | `test_catalog_government_ingestion.py:2164-2165` |

### 2.5 Legacy

| # | Path | Class | Evidence |
|---|---|---|---|
| L1 | `MILO-main-original/MILO-main/` (6 files) | **DUPLICATE → D6** | `diff -rq MILO-main-original/MILO-main legacy/milo-streamlit-v1` shows no difference, and `sha256sum -c archive/SHA256SUMS.txt` passes all 12 lines. Protected by `archive/SHA256SUMS.txt:1-12`; `repo-scan.yml:8-11,35-46,48-71,79-101` (integrity, change detection, `ruff check .` and prototype pytest with `working-directory: MILO-main-original/MILO-main`); `ci.yml:41` ignore; `test_ci_workflow_static.py:181,311,367-375,394-398,420-422`; `AGENTS.md:3-4,9`; `CLAUDE.md:4`; `.dockerignore:7-8`; `docs/current-milo-baseline.md:3`. `test_vehicle_catalog_engine.py:34` loads **`legacy/`** (not the duplicate) |

---

## 3. Delete sets

Every set: exact files, exact dependent edits, tests to delete or adjust, and
the checks of §8 of the audit prompt (routes, gateway, engine keys, tool
registry, env list, `git ls-files backend`, deployment contract, the three
fingerprints, `git grep` for every deleted name). No set contains a migration.
**No set changes a fingerprint, a route, a gateway rule, an engine key or a tool name.**

### PR-1 — zero-risk

| Set | Delete | Dependent edits | Tests deleted / adjusted | Needs |
|---|---|---|---|---|
| **1a** | the 11 shims under `backend/engines/vehicle_catalog_v1/` (B1) | none | none | explicit OK that touching `vehicle_catalog_v1/**` is allowed for pure dead-shim removal (roadmap `:144`) |
| **1b** | `backend/api/__init__.py` (B2) | none | delete `test_compat_api_entrypoint_uses_protected_app` (`tests/test_api.py:417-425`). Coverage stays via `test_api.py:279-283,346-355,358-361,399-401` | — |
| **1c** | `backend/internet_governance.py`, `tests/test_internet_governance.py` (B3) | optionally append "(removed in PR-1c)" to `MILO_UNIFIED_ENGINE_3_STAGE_ROADMAP.md:380` | whole test file (tests only the deleted module) | — |
| **1d** | `frontend/components/run/RunOutputPanel.tsx` (F1) | `frontend/scripts/static-ui-check.mjs:44` `'Final artifacts'` → `'Pipeline quality'`, and rewrite `:45-49` to name the V1 typed result panel; `docs/swarm-v2-final-result.md:5,22`; `docs/production-readiness/FRONTEND_ACCEPTANCE.md:175,189,245,327` (not test-locked) | `staticUiCheck.test.ts:25` (marker list) and `:35-40` (`rmSync` target → `components/result/VehicleCatalogResultPanel.tsx`, expected stderr → `Missing UI marker: Pipeline quality`); `finalResultPanel.test.tsx:14` import, delete tests at `:269-280` (5c/5d) and `:528-535` (9d) | — |
| **1e** | exports `clientConfig` (`api.ts:9`), `isActiveRunStatus` + `ACTIVE_SET` (`runStatus.ts:33,39-41`), `safeUrl` (`sanitize.ts:2`) (F2; **not** `api.proposal`, which goes with D3, and **not** `normalizeRateLimitKey`, because `frontend/lib/server/*` is DO-NOT-TOUCH) | none | none | — |
| **1f** | `scripts/runbooks/mock-e2e.md` (S1) | none | none | — |
| **1g** | the 7 S10 docs (624 lines) | none | none | — |
| **1h** | `Settings.rate_limit_per_minute` and `Settings.environment` (`backend/config.py:13,19`) (B14) | `check-production-config.sh:80` (delete the inventory row `RATE_LIMIT_PER_MINUTE`); `check-production-config.sh:74` (repoint the `ENVIRONMENT` row's source from `backend/config.py` to `backend/production_config.py`, otherwise the freshness check emits a new `[WARN] inventory:ENVIRONMENT`); `ENVIRONMENT_MATRIX.md:32`. `ENVIRONMENT` itself stays (read directly at `production_config.py:118,390`, `gateway_auth.py:86`) | none | confirm that DO-NOT-TOUCH `config/*` means the top-level `config/` directory only, not `backend/config.py` |
| **1i** | `NEXT_PUBLIC_API_URL` (S8) | `ci.yml:24,79` (env lines only; job names and steps unchanged); `check-production-config.sh:182`; `ENVIRONMENT_MATRIX.md:57` | none | lands after or with 1g (two S10 docs mention it) |
| **1j** (new) | `eslint`, `eslint-config-next` from `frontend/package.json:30-31` (F4) | regenerate `frontend/package-lock.json` with `npm` (never by hand) | none | — |

**Acceptance for PR-1:** identical fingerprints (`8f4ef66c…`, `c96c49ed…`, pin
`8f4ef66c…`), identical 44-route list, identical gateway sha256, identical
engine/tool registry, and a `git ls-files backend` diff limited to 1a/1b/1c/1h files.

### PR-2 — historical rollout tooling (part 1)

| Set | Delete | Dependent edits | Tests |
|---|---|---|---|
| **2a** | `scripts/release/swarm-v2-smoke/` (6 files, 982 lines) (S2) | remove `scripts/release/swarm-v2-smoke/*.sh` from the shellcheck command in `ci.yml:59` (not locked); rewrite the `parse_env_contract` rows in `RUNTIME_POLICY.md:18`, `ENVIRONMENT_MATRIX.md:44`, `STAGED_ACTIVATION.md:80`; HISTORICAL banner on `docs/deployment/swarm-v2-smoke.md` (stays, locked; also fix `:11`, which names `test_release_tooling_swarm_smoke.py`); `FINAL_ACCEPTANCE.md:68` | delete `tests/test_release_tooling_swarm_smoke.py`; remove `test_the_smoke_env_contract_keeps_the_catalog_flag_off_in_every_posture` (`test_catalog_execution_flag.py:496-502`). Its property (catalog flag off in every posture) is still covered for the **current** contract by `deployment-contract.sh` + `check_unsafe_defaults.py`. The current-contract assertion exists at `test_catalog_execution_flag.py:479-482`, so no property is lost |
| **2b** | `docs/deployment/cloud-run-production.md` (S11) | repoint `frontend/lib/sanitize.ts:38`, `frontend/tests/redactSecretText.test.ts:71`, `frontend/tests/secretSentinels.ts:26`, `docs/swarm-v2-final-result.md:212` | none |
| **2c** | stage-c / stage-d, **only after D5 and D8** | — | — |

**2c prerequisites.**
- **D5 = new kill switch:** a separate PR adds `scripts/deploy/kill-switch.sh` with tests in the style of `test_kill_switch.py`. It must cover every flag opened by `website-execution-activate.sh` and `deployment-contract.sh`: `MILO_ENABLE_WORK_SCOPE_*`, `MILO_ENABLE_GOVERNMENT_CATALOG_READ`, `MILO_ENABLE_CATALOG_PROMOTION` and the Vercel `GATEWAY_ALLOW_RUN_START_ROUTES` / `GATEWAY_ALLOW_EXECUTION_ROUTES`. The new file falls under the locked shellcheck glob `scripts/deploy/*.sh`.
- **Deleting stage-c requires:**
  - deleting or rewriting `test_stage_c_toolkit.py`, `test_kill_switch.py`, `test_catalog_execution_flag.py:521-535` and `test_release_tooling_swarm_smoke.py:742-751` (already gone with 2a);
  - `test_stage_d_toolkit.py:49,360-382,425-443,2564` (S5 fails on any deletion);
  - `ROLLBACK.md:41`, `STAGED_ACTIVATION.md:79` and `docs/deployment/swarm-v2-smoke.md:31,49`;
  - `release_inventory.py:85` (`TOOLING_SOURCES`; no RPC is lost).
- **Deleting stage-d (D8) requires an extraction PR first.** Move `PINNED_POLICY_FINGERPRINT`, `REQUIRED_RPC_ARGS` and `semantic_acceptance.py` (and anything `verify_caps.py` / `verify_images.py` tests still need) to a permanent path such as `scripts/release/pins/`. Prove every referencing test passes from there **with no value change**. Update `release_inventory.py:84` (it reads `probe_db.py` text), the shellcheck `-P` path (not locked) and the `scripts/release/stage-d/*.sh` glob (**locked** by `test_ci_workflow_static.py:267-273`). `policy_envelope` is also loaded by `test_stage_d_toolkit.py:935-1068,1535,1860,3387-3393`.

### PR-3 — backend test-only code and repository

| Set | Change | Dependent edits | Tests |
|---|---|---|---|
| **3a** | delete `backend/model_pricing.py` (B5b) | none in backend | `test_corrective_blockers.py:10,111`: oracle → `get_profile("kimi-k2.6").usage_cost(…)`; `test_model_profiles.py:8,37-45`: delete the compat-surface assertions |
| **3b** | remove the mock re-export from `backend/tools/__init__.py:4-6` and the six `Mock*` names from `__all__` (`:10-12`) (B7); **keep** `tools/mock.py` | — | 4 imports → `from backend.tools.mock import …` (`test_swarm_v2_tool_contract.py:45`, `test_swarm_v2_runtime.py:31`, `test_swarm_v2_worker_output_repair.py:38`, `test_swarm_v2_r3_evidence_contract.py:53`) |
| **3c** | **blocked:** removing the reconcile re-export edits `backend/catalog/government/__init__.py:38-39,44-52`, which is DO-NOT-TOUCH (only `reconcile.py` itself is exempt). Folded into D9 | — | none (tests import the submodule) |
| **3d** | delete `list_unread_agent_messages` (Protocol `supabase.py:168`, impl `:1159-1160`) (B8) | none (no RPC; not in `REQUIRED_RPC_ARGS`) | none |
| **3e** | S9: add `MILO_RELEASE_SHA` and `GATEWAY_ALLOW_RUN_START_ROUTES` to the `check-production-config.sh` inventory (and `MILO_RELEASE_SHA` to `ENVIRONMENT_MATRIX.md`); repoint rows `:133-135` to `backend/model_profiles.py` | — | — |
| **3f** | delete `gateway_auth_configured` (`gateway_auth.py:80`), `RunCheckpoint` (`schemas.py:328`), `profile_summary` (`model_profiles.py:262` + `__all__` `:283`) | — | none. Verify `event_registry.fingerprint()` is unchanged (it hashes only the event-type names, `event_registry.py:287-299`, and imports nothing from `schemas`) |
| **3g** (optional) | B13: one `backend/flags.py::env_flag_enabled(name, env=None)` over `{1,true,yes,on}` | — | **mandatory** before/after parity test over `""`, `"1"`, `"true"`, `"TRUE"`, `" on "`, `"yes\n"`, `"0"`, `"false"`, `"off"`, `"2"`. Five of the 8 sites are in DO-NOT-TOUCH files (`budget.py`, `runtime_policy.py`, `catalog/execution.py`, `catalog/operator_capture.py` ×2). Without an owner exception, 3g may only change `execution_guard.py`, `gateway_auth.py` and `production_config.py`, which captures little value. **Recommendation: skip 3g this round** |

Not in PR-3 (evidence shows they are locked or protected): B4 (D10), B5, 3c
(D9), and every B8 method except `list_unread_agent_messages`.

### PR-4 — docs and runbooks

- Fix the S12 contradictions (§5.7), the stale `parse_env_contract.py:58-62` comment (if S2 is not deleted first), `website-integration.md:136,193` (`reconstructRun`) and the stale unpinned-`openai` statements (§5.8).
- Add HISTORICAL banners; index OPERATOR_1 and OPERATOR_4 in `docs/production-readiness/README.md` under a HISTORICAL section; do not move locked docs.

---

## 4. Decisions required (Phase B). Default until decided: **do not touch**

| # | Decision | Options | Change set | Risk |
|---|---|---|---|---|
| D1 | V1 engine | (a) keep (default; work plan §19); (b) remove | move `worker_provider_api_key` (`worker/main.py:393,395,909`), re-key `MockLifecycleEngine` (`mock_engine.py:39`) and staging (`staging-cloud-run.sh:119`), replace the fallback `product_outcome.py:634`, drop `MILO_V1_*` from the policy (**policy fingerprint changes**), 13+ test files | 3 V1 projects and 4 historical V1 runs must stay readable |
| D2 | supervisor shadow | (a) keep; (b) remove shadow (`worker/main.py:272-295` + call sites `:299,350,463,559`) and `supervisor.py` | frees V2 from `vehicle_catalog_v1/workflow.py`; the 3 tables stay (8/13/8 rows) | writes stop for `run_blackboards`/`agent_messages`/`supervisor_decisions`; event `supervisor_shadow_failed` leaves the registry (**event fingerprint changes**) |
| D3 | workflow proposals | (a) keep dormant; (b) remove backend (`main.py:876-933`, `workflow_proposals.py`), UI (`WorkflowProposalPanel`, `page.tsx` wiring, `api.ts:90-108` incl. `api.proposal`), gateway rules (`gatewayPolicy.ts:116-123,141,197`), `MILO_ENABLE_PROPOSAL_*` in the contracts, stage-c kill switch reference | **routes, gateway and static marker `'Workflow proposal'` change**; e2e specs | 0 prod rows |
| D4 | worker HTTP mutation routes + `tool_access_*` | (a) keep; (b) remove `main.py:973-1096` | **event registry fingerprint** + regenerate `frontend/lib/eventRegistry.generated.json`; route list changes | tables for sources/claims/conflicts/tool_usage stay (ACTIVE) |
| D5 | kill switch | (a) new `scripts/deploy/kill-switch.sh` covering every current flag incl. Vercel; (b) rewrite `ROLLBACK.md` as manual-only | then stage-c can go (2c) | both scripted kill switches (`stage-c/kill-switch.sh`, `stage-d/kill-switch.sh`) miss the whole current production path (§2.3 S3). Deleting stage-d (D8) also removes the second one |
| D6 | `MILO-main-original/` duplicate | (a) keep; (b) delete with authorized edits to `AGENTS.md`, `CLAUDE.md`, `repo-scan.yml` (must still emit "Lint & Test", and point ruff/prototype tests at `legacy/`), `archive/SHA256SUMS.txt`, `ci.yml:41`, `test_ci_workflow_static.py`, `.dockerignore` | CI | protected by governance files |
| D7 | `pytest` out of images | (a) keep; (b) `backend/requirements-dev.txt` | Dockerfiles unchanged, but `ci.yml:37,133,165` install both | image contents change |
| D8 | Stage D | (a) keep; (b) extract pins, then delete | see 2c | stage-d is still edited by current PRs (last `28d1e8e`, 2026-09-25): the policy pin **is** the release-binding contract |
| **D9 (new)** | `reconcile.py` (B6) | (a) keep as built-not-connected capability; (b) delete module + tests + gap-audit successor claim | 386 + test lines | product roadmap question |
| **D10 (new)** | `tier2_profile.py` (B4) | (a) keep; (b) port its literal pins (3.00, 1800, ceilings, provider limits) to `POLICY` tests, then move the prose to `docs/` and delete | `test_run_safety_contracts.py`, `test_runtime_policy_authority.py:41,398-407,798-803`, `test_provider_concurrency_ownership.py:1440-1480`, doc/comment refs | losing regression pins if not ported first |

---

## 5. Findings that are NOT cleanup (bugs / risks / gaps)

1. **B18: the worker has no production-config guard.** `worker/main.py` never calls `validate_production_config`. The only runtime rejection of `MILO_WORKER_ENGINE=mock` (`production_config.py:294-295`, `TEST_ADAPTER_IN_PRODUCTION`) runs in the API only (`main.py:79-81`). With mock selected, the worker also forces the budget kill switch true (`worker/main.py:530`). The guard today is release tooling only (`check-production-config.sh:176,390-392`). No test proves the worker refuses mock in production.
2. **B17: memory-repository parity gap.** `MemoryRepository` lacks `list_sources_for_ids`, `list_evidence_fragments_for_sources`, `list_structured_facts_for_sources` and `patch_run_blackboard_evidence`. Production callers: `swarm_v2/grounding.py:407,424,473` (the last via `getattr(..., None)` → silently empty) and `swarm_v2/evidence.py:540`. Tests use per-test fakes.
3. **S9: check-production-config inventory gap.** It is missing `MILO_RELEASE_SHA` and `GATEWAY_ALLOW_RUN_START_ROUTES` (still true after PR #124), and `ENVIRONMENT_MATRIX.md` lacks `MILO_RELEASE_SHA`. Rows `:133-135` point at the wrong source file since `f0a7ec9` and WARN today. Fix in set 3e.
4. **Neither scripted kill switch covers the current path** (S3 / D5). `stage-c/kill-switch.sh` and `stage-d/kill-switch.sh` cover none of `MILO_ENABLE_WORK_SCOPE_*`, `MILO_ENABLE_GOVERNMENT_CATALOG_READ`, `MILO_ENABLE_CATALOG_PROMOTION` or any Vercel gateway variable, and `stage-c/*.sh` is not shellchecked in CI.
5. **B15:** two readers of `MILO_SWARM_MAX_ACTIVE_WORKERS` with different defaults, reconciled by `min()` in the worker. Document only.
6. **Backend disagreement on `mark_run_failed` / `mark_run_complete`.** Supabase refuses (`CANONICAL_FINALIZER_REQUIRED`); memory succeeds. Tests use the memory versions as setup helpers, so a memory-backed test can reach a state production cannot. The docstring at `test_catalog_pr3_swarm_promotion.py:1814-1834` ("the worker takes its mark_run_complete branch") is stale.
7. **S12: runbook contradictions (PR-4).**
   - **Emergency order differs in three documents.**
     - `ROLLBACK.md:9-19`: paid → run creation (+ gateway) → launcher → routes → provider secret.
     - `SCOPED_BATCH_PRODUCTION_RUNBOOK.md:804-819`: "Close the website first", with Vercel first. It adds `WORK_SCOPE_BATCHES` and, at `:825-828`, `GOVERNMENT_CATALOG_READ`. It claims to be "from ROLLBACK.md".
     - `PRODUCTION_ACTIVATION_RUNBOOK.md:230-248`: paid → provider key → run creation, with no gateway and no launcher.
   - **`DEPLOYMENT.md:3-5`** says "no script in this repository deploys anything". It is contradicted by `scripts/deploy/cloud-run.sh:749,758`, `production-activate.sh:16-21`, `website-execution-activate.sh:213-251` and `deploy-supabase-migrations.yml:197-204`.
   - **`ROLLBACK.md:21-22`** ("no disable-all script") contradicts **`:40-43`**, which points to `stage-c/kill-switch.sh` (a disable-all script).
   - **`STAGED_ACTIVATION.md:11-12`** ("apply … migrations manually") contradicts `SCOPED_BATCH_PRODUCTION_RUNBOOK.md:259-263` and `MIGRATIONS.md:3-12` (SHA-bound workflows only).
8. **Stale statements.**
   - `parse_env_contract.py:58-62` ("refused at boot") contradicts `f29c0e6`.
   - Stage D docs and scripts (`stage-d-env.sh:70`, `verify_images.py:11`, `01-verify-release-images.sh:12`, `stage-d/README.md:156`, `STAGE_D_AUTHORIZATION.md:222`) describe an unpinned `openai>=1.30.0`, while `backend/requirements.txt` pins `openai==3.16.2`. **The literal is test-locked** (`test_stage_d_toolkit.py:2450-2459` requires `openai>=1.30.0` in `stage-d-env.sh` and `STAGE_D_AUTHORIZATION.md`). PR-4 may only **add** a note ("at Stage D time"; current pin `openai==3.16.2`), never remove the literal.
   - `layout.tsx:2` says "Realtime".
   - `website-integration.md:136,193` names `reconstructRun`.
9. **`tests/realtimeDisabled.test.ts`** only asserts mode `'polling'`, which the hook sets synchronously (`useRunRealtime.ts:159`). It does not prove that no channel is created.
10. **`production_config.TRUE_VALUES`** is a mutable `set` copy of `runtime_policy.TRUE_VALUES` (a `frozenset`).

---

## 6. UNKNOWN list (what evidence is missing)

| Item | Missing evidence | Who |
|---|---|---|
| S13 `scripts/staging/redis-shim/Dockerfile` | Does the `milo-redis-shim-staging` Cloud Run service still exist, and was it built from this Dockerfile? Does staging still exist at all? | Gilad (read-only `gcloud run services describe`) |
| S14 backfill generators | Are the membership/proposal backfills complete in every environment (so the generators are one-shot history)? | Gilad (a SELECT on the backfilled columns) |
| `docs/production-readiness/STAGING.md` | same as S13 | Gilad |
| `docs/vehicle-catalog-v1-engine.md` | follows D1 | Gilad |
| B6 `reconcile.py` | is catalog reconciliation still on the roadmap (D9)? | Gilad |

---

## 7. Validation run for this PR (PR-0 changes one new Markdown file)

Environment: Python 3.11 venv with `backend/requirements.txt` (CI uses 3.12, `ci.yml:35`), the CI env of
`ci.yml:20-25` (`SUPABASE_URL=https://example.supabase.co`,
`SUPABASE_SERVICE_ROLE_KEY=offline-placeholder`, `JOB_LAUNCHER=disabled`,
`NEXT_PUBLIC_API_URL`, `MILO_REQUIRE_PG_TESTS=1`, PostgreSQL 16 binaries on
`PATH`), and a full (unshallowed) clone. The exact results are in the PR
description. Postgres suite, Docker builds and Playwright were **not** run: this
PR touches no code, repository, migration, Dockerfile or frontend file, so CI's
`postgres-checks`, `frontend-and-docker` and `e2e` jobs are the evidence.

---

### 7.1 Independent review (§8.5)

A separate review agent re-verified more than 100 citations in this document
against `a8fec45` and looked specifically for DO-NOT-TOUCH violations, missed
dynamic references and fingerprint impact. It found no dynamic reference to
any SAFE_TO_DELETE / UNREACHABLE / SUPERSEDED item, and no set that changes a
fingerprint. It found 14 errors in the first draft, all corrected here:
- two DO-NOT-TOUCH conflicts (1e `rateLimit.ts`, 3c `government/__init__.py`);
- the 3g site count;
- a missing 1h inventory row (`ENVIRONMENT`);
- three already-stale `check-production-config.sh` rows (`:133-135`);
- the second kill switch (`stage-d/kill-switch.sh`);
- the test-locked `openai>=1.30.0` literal;
- 3b `__all__`;
- B11/D2 line ranges and the B4 test count;
- missing doc references in 1d/2a;
- the shellcheck `-P` lock claim;
- minor citations;
- the D8 extraction scope.

The reviewer's CI verdict per set was obtained by reading, not by applying the
sets. Each cleanup PR still runs the full §8 gates.

## Appendix A — Python import graph (run from outside the repo)

Roots `backend.main`, `backend.worker.main`, `backend.catalog.operator_capture`.
It resolves absolute and relative imports and `from pkg import submodule`, adds
implicit parent-package imports (importing `a.b.c` executes `a/__init__` and
`a/b/__init__`), and follows literal `importlib.import_module("…")` /
`__import__("…")`. Usage: `python pygraph.py <repo> [summary|json|who <mod>…]`.

```python
import ast, sys, json
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
ROOTS = ["backend.main", "backend.worker.main", "backend.catalog.operator_capture"]

def modname(p):
    parts = list(p.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)

files = {}
for base in ["backend", "tests", "scripts"]:
    for p in (ROOT / base).rglob("*.py"):
        if "__pycache__" not in p.parts:
            files[modname(p)] = p
backend_mods = {m for m in files if m == "backend" or m.startswith("backend.")}

def resolve(cur, cur_is_pkg, level, module):
    if level == 0:
        return module or ""
    pkg = cur.split(".") if cur_is_pkg else cur.split(".")[:-1]
    if level > 1:
        pkg = pkg[: len(pkg) - (level - 1)]
    base = ".".join(pkg)
    return (f"{base}.{module}" if base else module) if module else base

def parents(m):
    parts = m.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]

def edges(m):
    p = files[m]
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = resolve(m, p.name == "__init__.py", node.level, node.module)
            out.add(target)
            for a in node.names:
                cand = f"{target}.{a.name}" if target else a.name
                if cand in files:
                    out.add(cand)
        elif isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if (name in ("import_module", "__import__") and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                out.add(node.args[0].value)
    full = set()
    for t in out:
        parts = t.split(".")
        while parts and ".".join(parts) not in files:
            parts.pop()
        if parts:
            k = ".".join(parts)
            full.add(k)
            full.update(x for x in parents(k) if x in files)
    return full

graph = {m: edges(m) for m in files}
seen, stack = set(), list(ROOTS) + [x for r in ROOTS for x in parents(r) if x in files]
while stack:
    m = stack.pop()
    if m in seen or m not in files:
        continue
    seen.add(m)
    stack.extend(graph[m])
importers = {}
for m, es in graph.items():
    for e in es:
        importers.setdefault(e, set()).add(m)
for m in sorted(backend_mods - seen):
    print(f"UNREACHABLE {m}  importers={sorted(importers.get(m, set()))}")
```

Result at `a8fec45`: 151 backend modules, 118 reachable. Unreachable:
`backend.api`, `backend.catalog.digest`, the 11 V1 shims,
`backend.internet_governance`, `backend.model_pricing`, `backend.tier2_profile`,
and `backend.testing` with its submodules. `backend.testing.e2e_app` is launched by
`frontend/playwright.config.ts:90,98` (`uvicorn backend.testing.e2e_app:app`), and
`backend/testing/r5_proof/web.py` is imported by `scripts/r5_capture_fixtures.py:50`.
`reconcile.py` and `tools/mock.py` show as reachable **only** through their
package `__init__` re-exports, so they were checked symbol by symbol (B6, B7).

## Appendix B — TS/TSX import graph (run from outside the repo)

A dependency-free Node script (139 lines, regex-based). It walks `frontend/`
(skipping `node_modules`, `.next*`), resolves the tsconfig `paths` alias
`@/*` → `./*`, relative specifiers with `.ts/.tsx/.mts/.mjs/.js/.jsx/.json/.d.ts`
and `index.*`, and edges from `import … from`, `export … from`, side-effect
`import '…'`, dynamic `import('…')`, `require('…')` and `vi.mock('…')` (comments
stripped first). Roots are App Router conventions
(`app/**/{page,layout,loading,error,not-found,template,default,global-error}`,
`app/**/route.{ts,js}`), `next.config.*` and `middleware`/`instrumentation`.
It prints reachable files and, for every unreachable file under `app/`,
`components/`, `lib/`, its importers. A companion script (49 lines) lists
exports whose name appears in no reachable production file other than their
own. Result: 62 reachable files; 1 unreachable (`RunOutputPanel.tsx`, imported
only by `tests/finalResultPanel.test.tsx`); dead exports as in §2.2 F2/F6.
