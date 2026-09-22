# Website → MILO integration

This document describes, from the code as it is, how the website creates a
run, how a run's engine is decided and never re-decided, how the live view and
the final result are produced, how the Government vehicle catalog feeds a
bounded MILO task, and which concurrency and provider limits bind. Every
statement names the module it comes from.

## 1. The one run-creation path

```
browser (frontend/app/page.tsx startRun)
  → POST /api/gateway/conversations/{id}/runs        (frontend/app/api/gateway/[...path]/route.ts)
  → POST /conversations/{id}/runs                     (backend/main.py create_run)
  → _create_and_launch_run                            (backend/main.py)
      rate limit → reserved-scope refusal → idempotency lookup → project.workflow_key (TRUSTED relation)
      → V1 only: project.configuration → VehicleCatalogScope (backend/vehicle_catalog_scope.py)
        bound into input.metadata.vehicle_catalog_scope, or 409 before anything is written
      → RunIdentity.bind(run_id, workflow_key)        (backend/run_identity.py)
      → execution_identity_problems(identity)          (release/policy/registry must match the image)
      → repo.create_message_and_run(..., run_id, run_identity)
          = create_message_and_run_v3                 (supabase/migrations/20260921000200)
            ONE transaction: user message + run row + immutable identity
      → require_identity(run) re-read → try_acquire_launch CAS → launcher.launch(run_id)
  → Cloud Run job execution (RUN_ID override)         (backend/worker/main.py)
  → EngineResolver.resolve from runs.run_identity     (backend/worker/engine.py)
  → RunFinalizer.finalize(TerminalClaim)              (backend/finalization.py)
      = finalize_run_guarded                          (supabase/migrations/20260920000200)
        terminal status + terminal event (with the ProductOutcome record) in ONE transaction
  → GET /runs/{id}  { status, output, run_identity, product_outcome, limits, usage }
  → frontend result surface selected from run_identity only
```

### What a Vehicle Catalog V1 run maps

A V1 run maps exactly the manufacturer, market and period its PROJECT
configures, and nothing else (`backend/vehicle_catalog_scope.py`):

* **Source.** `projects.configuration`
  `{"manufacturer", "market", "period": {"from", "to"}}` — server-owned, read
  through the same trusted run → conversation → project relation the workflow
  key comes from. The typed task (`content`) is the task description and is
  never parsed for a scope; request `metadata` can never name one.
* **Bound at creation.** `_create_and_launch_run` validates it and writes the
  record under the reserved key `input.metadata.vehicle_catalog_scope` inside
  the same atomic V3 insert as the message, run and identity. A request that
  supplies that key is refused (`422 VEHICLE_CATALOG_SCOPE_RESERVED`), so the
  stored value can only be the server's. The request fingerprint is computed
  over the client's own `(content, metadata)`, so an idempotent replay is still
  the same logical request, and it returns the run with the scope it was
  created with even if the project was reconfigured since.
* **Explicit refusal, never a default.** A V1 project whose configuration
  states no scope is refused with `409 VEHICLE_CATALOG_SCOPE_NOT_CONFIGURED`,
  one that states it wrongly with `409 VEHICLE_CATALOG_SCOPE_INVALID`, before
  any message, run or launch exists.
* **Executed exactly.** The worker re-validates the bound record and hands it
  to `VehicleCatalogV1Adapter` explicitly; a V1 run without a readable bound
  scope is refused through the canonical finalizer
  (`VEHICLE_CATALOG_SCOPE_MISSING` / `_INVALID`) before any provider path is
  constructed. The adapter reads no scope from run input and has no default.
* **Unchanged for the canonical project.** `milo-vehicle-catalog` is seeded
  with `Hyundai / Israel / 2010 – June 2026` (`001_project_workspace.sql`),
  which resolves to exactly the configuration the engine always ran with, so
  its prompts are unchanged.

Before this, the adapter read top-level `input.manufacturer/market/period`,
which no creator ever wrote, and silently fell back to the engine defaults —
every website V1 run was a Hyundai run whatever its project said.

**Operator note.** The Production V1 smoke projects `stage-c-smoke` and
`stage-d-smoke` carry `{"stage": ...}` only. Their V1 runs used to run the
engine defaults implicitly; they are now refused until their configuration
states an explicit scope. That is a Production data change for an operator,
not something a release performs.

There is exactly one creation authority. `create_message_and_run` and
`create_message_and_run_v2` no longer exist in Production (applied
2026-09-22, see `docs/production-readiness/OPERATOR_3_PRODUCTION_ALIGNMENT_2026-09-22.md`),
and `SupabaseRepository.create_queued_run` refuses with
`RUN_IDENTITY_ATOMIC_CREATION_REQUIRED`. The browser never reaches the
worker: the worker surfaces (`/runs/{id}/tool-*`, `/runs/{id}/sources|claims|conflicts`,
`/internal/runs/{id}/*`) are not in the gateway allowlist
(`frontend/lib/server/gatewayPolicy.ts`) and require a verified Google worker
identity plus the run lease (`backend/worker_auth.py`, `assert_worker_lease`).

### What reaches the browser

Only `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` and
`NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (a display switch, never a security
boundary). The gateway route handler holds the Cloud Run URL and mints the
Google service token server-side (`frontend/lib/server/cloudRunAuth.ts`); the
browser sends its Supabase access token and receives a run id. No lease token,
service-role key, provider key or worker identity is ever serialized to the
browser: `_safe_run_response` strips `lease_token` and `launch_error`, and
`frontend/scripts/no-secret-bundle-check.mjs` scans the built bundle.

### Flags that gate it

`MILO_ENABLE_RUN_CREATION` on the API (403 before body validation,
`backend/execution_guard.py`), `GATEWAY_ALLOW_EXECUTION_ROUTES` on the
gateway (the POST is refused before authentication when off), and
`MILO_ENABLE_EXECUTION_CONTROL` for every worker write. (Operational fact,
verified read-only on 2026-09-22, not a property of the code: all of them are
`false` on the Production API and Worker.)

## 2. Immutable identity decides the engine

`runs.run_identity` is written by `create_message_and_run_v3` and protected by
two triggers (`runs_require_identity_on_insert`, `runs_forbid_identity_rewrite`)
and a validated shape constraint. Supported product workflows are exactly
`vehicle_catalog_v1` (`vehicle_catalog_v1.stage3`) and `swarm_v2`
(`swarm_v2.1`); `operator_capture` is a control-plane identity the product
worker refuses (`PRODUCT_WORKFLOW_KEYS`, `backend/run_identity.py`).

Every consumer reads the identity, never the project's current workflow and
never the payload shape:

| Consumer | Reader | On invalid / missing identity |
| --- | --- | --- |
| Worker routing | `EngineResolver.resolve` (`backend/worker/engine.py`) | 409 `RUN_IDENTITY_REQUIRED` / 403 `ENGINE_NOT_ALLOWED` — never executes |
| Worker terminal surfaces | `require_identity` in `complete_run_from_worker` / `fail_run_from_worker` | 409 / 403 |
| Export | `build_export_envelope` (`backend/export_envelope.py`) | `ExportRefused` |
| API read | `_safe_run_identity` (`backend/main.py`) | `run_identity: null` (read stays available, identity omitted) |
| Browser | `runIdentityWorkflowKey` (`frontend/lib/runIdentity.ts`) | `identityUnavailable` → bounded alert, no engine-specific surface |
| Browser (this change) | `buildLiveRunViewModel` (`frontend/lib/liveRunViewModel.ts`) | `engine: undefined`, "Engine not stated", no work/agent projection |

The 8 Production runs created before Console 6 (verified read-only on
2026-09-22) have `run_identity IS NULL` and are therefore readable history only: not executable, not resumable, not
exportable, and rendered with the identity-unavailable state.

## 3. Live run visualization

Transport is authenticated polling (`frontend/lib/useRunRealtime.ts`, 3 s base,
30 s backoff, `after_event_id` cursor, stops on terminal, Supabase Realtime
deliberately disabled). Refresh and reconnect rebuild state from
`GET /runs/{id}` + `GET /runs/{id}/events` through `reconstructRun`.

`components/run/LiveRunPanel.tsx` renders one view for both engines from
`buildLiveRunViewModel`:

| Shown | Source |
| --- | --- |
| Run state, terminal flag | `run.status` |
| Workflow / engine / engine version | `run.run_identity` |
| Current phase | V1: last `phase` on an event owning the V1 projection; V2: swarm lifecycle |
| Work (queued / active / completed / failed) | V1: chunk events — the engine emits `chunk_completed` / `chunk_failed` (`engines/vehicle_catalog_v1/engine.py`), so V1 shows settled chunks and no queue; V2: `taskCounts` from `task_ready/started/completed/failed` (`lib/swarmReducer.ts`) |
| What each active worker is doing | V1: agents with status `active` and their last message; V2: running tasks and their tool-call count. Swarm V2 has no agent concept and none is invented |
| Research / evidence progress | V1: `source_recorded`, `claim_recorded`, `conflict_detected`; V2: `evidence_added`, `conflict_found`, `verification_batch_completed`, `verification_completed` |
| Provider / backpressure | `run.usage.provider_backpressure_events`, `run.usage.retries` (authoritative), plus a count of the durable pacing events `provider_backpressure_wait`, `provider_rate_limited`, `provider_quota_paused` (exact type membership) |
| Usage / spend / budget | `run.usage` against `run.limits` (new: the API projects `BudgetConfig.from_env()` ceilings — max model calls, total tokens, cost, duration, agent steps) |
| Finalization state | `run.status` terminal + `run.product_outcome` present ("finalized with canonical outcome" / "terminal, no canonical outcome recorded") |

Nothing infrastructural is shown: no worker identity, lease, endpoint,
provider key, prompt or model output text. Event-derived spend telemetry stays
labelled as telemetry in the Inspector; `run.usage` is the only authority.

## 4. Final result delivery

```
execution → evidence / current verdict → RunFinalizer (backend/finalization.py)
  → derive_product_outcome (backend/product_outcome.py)       canonical ProductOutcome
  → finalize_run_guarded: runs.status + runs.output + terminal event{payload.product_outcome}
  → GET /runs/{id}: product_outcome = _safe_product_outcome(run, repo)   (backend/main.py)
       reads repo.terminal_run_event(run_id) (latest run_completed/partial_success/failed/cancelled)
       re-validates through outcome_from_record → ProductOutcomeRecord (backend/schemas.py)
       never derived from output; null when no trustworthy record exists
  → frontend/lib/productOutcome.ts parseProductOutcome (closed vocabulary, total, fail-closed)
  → components/result/ProductOutcomeBanner.tsx  (verdict line on every result surface)
  → V2: components/result/FinalResultPanel.tsx  (lib/finalResult.ts, the V2 contract mirror)
  → V1: components/result/VehicleCatalogResultPanel.tsx (lib/vehicleCatalogResult.ts, typed reader
        of the engine's deterministic final document: models, verdicts, review/rejected counts,
        pipeline quality, data depth; not a JSON dump)
```

A worker request may not append any of the four terminal event types
(`TERMINAL_EVENT_RESERVED`, `backend/main.py create_worker_run_event`), so
nothing but the finalizer can write the event the projection reads. Residual:
a worker process holding the lease could still append one through the
repository RPC; the projection re-validates the record and drops anything
outside the vocabulary, so the worst case is a shadowed verdict, never an
unsafe one.

The raw durable payload is no longer a product surface. It is developer
telemetry under the Inspector's Developer tab, redacted through
`redactSecrets`. A V1 payload that is not a catalog document is refused with a
static code (`V1_NOT_A_CATALOG_DOCUMENT`), exactly as the V2 contract refuses a
payload it did not emit.

### Survival across refresh, reconnect, restart and history

* Refresh / reconnect: the run id is in session storage per conversation; the
  hook re-reads the run and events and `reconstructRun` rebuilds the state.
* Browser restart / another device: session storage is gone, so the workspace
  now reads `GET /conversations/{id}/runs` (new; `backend/main.py
  list_conversation_runs`, bounded to 50, membership-scoped through the
  conversation) and reopens the newest run; `components/run/RunHistoryList.tsx`
  lists every run of the conversation as the engine it WAS with the verdict the
  finalizer recorded, and reopens any of them.
* The result itself is `runs.output` plus the terminal event, both durable, so
  a completed run renders identically whenever it is opened, with no worker.

## 5. Government vehicle catalog → bounded MILO task

Everything below is the existing canonical path (`backend/catalog/`); this
change connects the website to it and documents it, it does not rebuild it.

| Step | Code | Facts |
| --- | --- | --- |
| Government source | `backend/catalog/government/source.py` | `data.gov.il` CKAN, package `degem-rechev-wltp`, WLTP resource `142afde2-6228-49f9-8a29-9b6c3a0cbe40`, quantity resource `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6`; only `package_show` / `datastore_search`; market `IL`, publisher `ministry_of_transport` |
| Pagination / fetch | `backend/catalog/government/client.py` `DataGovClient.capture_resource` | server-owned `limit`/`offset` paging, default page 100, max 1000, ≤200 pages and ≤120,000 records per capture, 8 MiB response bound, 10 s/30 s timeouts, 3 attempts with backoff on 429/5xx; every page must echo the query and the exact total (`_check_total_is_exact`, `_check_query_echo`); upstream `_id` values de-duplicated |
| Refresh / update detection | `backend/catalog/government/refresh.py` `GovernmentCatalogRefresh.sync_if_changed` | one bounded `package_show`; unchanged `upstream_version` + `upstream_version_kind` → `catalog_refresh_unchanged`, zero writes; changed → capture, snapshot with `snapshot_content_sha256` (`gov.snapshot.1`, retrieval time excluded), page-chain digest (`gov.pagechain.1`); diff via the DB RPC `catalog_snapshot_candidate_diff` |
| Normalization | `backend/catalog/government/normalize.py` (`gov.wltp.normalize.1`), `vocabulary.py` | WLTP rows only; bounded manufacturer/model/code/trim fields; coded fuel / propulsion / drivetrain / body vocabularies; model-year range 1900–2100; declared-unknown handling |
| Canonical identity | `backend/catalog/keys.py`, `backend/catalog/contracts.py` | `cm1.<32 hex>` = manufacturer + commercial model; `cv1.<32 hex>` = model key + year range + official model code + trim (identity dimensions deliberately excluded as revisable facts); candidate key excludes status; every key domain-separated and versioned |
| Reconciliation / deduplication | `backend/catalog/government/reconcile.py` | ≤5,000 variants; match by official model code, then normalized identity + year; `matched` / `ambiguous` / `under_enriched` states; reviewed alias rules empty by default |
| Persisted pre-agent state | `backend/catalog/government/ingest.py` → `record_catalog_snapshot_guarded`, `record_catalog_raw_record_guarded`, `record_catalog_candidate_guarded`, `activate_catalog_snapshot_guarded` | `catalog_source_snapshots`, `catalog_raw_records` (with `source_locator`), `catalog_candidate_variants`; append-only triggers; lease-guarded |
| Government preparation (after the lease, before the provider) | `backend/catalog/government/preparation.py` `prepare_government_work`, called from `backend/worker/main.py` between the lease/heartbeat and the block that constructs `ProviderAdapter` | for a trusted `swarm_v2` run with `MILO_ENABLE_GOVERNMENT_CATALOG_READ` on: resolve the newest USABLE snapshot through `resolve_active_snapshot` and PIN it (`GovernmentVehicleTool(snapshot_key=…)`), select the first 25 unread (`status='candidate'`) variants in the repository's deterministic order (codepoint identity text, then `candidate_key` — the same order PostgreSQL and the in-memory mirror return), write ONE `run_checkpoints` row (phase `government_prepared`, `artifacts.government`) under the lease, then continue. No usable snapshot → `RunFinalizer` refusal `GOVERNMENT_SNAPSHOT_UNAVAILABLE` before any paid path exists; the worker never constructs a `data.gov.il` transport |
| Resumable work queue | `prepared_artifact` / `government_work_progress` (same module); the worker copies `artifacts.government` into every later engine checkpoint | a replacement attempt reads the record from `latest_checkpoint`, resolves the SAME snapshot by exact key (never "newest"), walks the SAME queue, writes no second record, and hands the engine no preparation checkpoint (the Swarm resume path sees only its own `swarm_state`). Per-item progress is reconstructed from durable state: `catalog_run_pending_promotions` (verified evidence of this run) → `evidenced`, a durable `catalog_variant_promoted` event → `promoted`, else `pending`. No new event type was registered; the event-registry fingerprint is unchanged. Residual: with promotion OFF, candidates never leave `candidate`, so every run of the same snapshot selects the same first 25 |
| Work handoff | `engine_run.input.context.government_work` (server-written; the API never writes `input.context`) → `Commander.plan(context=…)` | the Commander receives the selection (snapshot key, resource, items with identity text, candidate key and progress; `remaining`, `bounded`, `total_candidates`) through its existing context seam; a browser cannot supply it |
| Bounded task selection | `backend/tools/government_vehicle.py` (`catalog.government_vehicle`, scope `catalog:government:read`, 8 read operations, `resolve_variant` is the promotable one), wired in `backend/worker/main.py` only when `government_read_enabled()`, pinned to the prepared snapshot | an agent can only READ the persisted candidate state through bounded operations (≤200 items, ≤20 resolution matches); it cannot invent a manufacturer, model or variant identity — identities exist only as persisted `cv1.`/`cm1.` rows |
| MILO execution | Swarm V2 tasks calling the tool; V1 evidence through `V1EvidenceAuthority` | tool calls bounded by `MILO_MAX_TOOL_CALLS_PER_RUN=24`, `max_tool_calls_per_task=4` |
| Evidence / review | `backend/catalog/government/evidence.py` (`government_register` source, strength `strong`, confidence 0.95) → R3/R4 claims, fragments, verdicts; current verdict via `claim_current_verdict_states` | field-level provenance; only a CURRENT `supported` verdict authorizes |
| Finalization / ProductOutcome | `backend/catalog/pipeline.py` `CatalogPromotionPipeline` (swarm_v2 only, only when `catalog_promotion_enabled()`), then `RunFinalizer` | `catalog_variant_promoted` / `catalog_promotion_refused` events; ≤25 promotions per run |

Flags (`backend/catalog/execution.py`): `MILO_ENABLE_CATALOG_EXECUTION`
(master), `MILO_ENABLE_GOVERNMENT_CATALOG_READ`, `MILO_ENABLE_CATALOG_PROMOTION`;
promotion armed without read is a startup refusal. Operational fact verified
read-only on 2026-09-22: all are OFF in Production and every catalog table
holds zero rows. Live Government egress is only the
operator-only `catalog.government.capture` run (`backend/catalog/operator_capture.py`),
which requires `MILO_ENABLE_PAID_EXECUTION` and two typed acknowledgements.

The website reads the durable catalog through `GET /projects/{id}/catalog/canonical`
and `/review-candidates` (`backend/catalog/review.py`, ≤100 items per page) into
`components/catalog/CatalogReviewPanel.tsx`.

## 6. Concurrency and provider limits (derived from current code)

Canonical registry: `backend/runtime_policy.py`; rendered by
`scripts/release/stage-d/policy_envelope.py` (fingerprint
`7ffc0f6d36220ecdd773955ef4e89289d804fa86e2e279ddd93a6bd6c37ed52b`).

The website does not transcribe any of this. `GET /runs/{id}` (and the
history read) carry `limits.concurrency`, computed by
`backend/main.py _effective_concurrency` from `resolve_runtime_policy()`,
`policy.provider_limits()`, `policy.budget_config()` and the organization
`QuotaConfig`: V1 technical parallelism, V2 max active workers, per-process
provider concurrency, the organization ceiling, the provider-effective width
(`min(per-process, ceiling)`), the provider-ADMITTED width per engine
(`min(engine width, provider effective)` — the executor's own rule), per-user
and per-project concurrent-run caps, Search Basic / Pro QPS and whether those
are verified, and the paid posture. `frontend/lib/liveRunViewModel.ts
parseEffectiveConcurrency` reads exactly those fields and
`components/run/LiveRunPanel.tsx` shows the LOGICAL engine width and the
PROVIDER-ADMITTED width as two separate facts. A policy that refuses to
resolve states `concurrency: null`; the browser then says so rather than
showing a default. In the default (unpaid, unset) posture the projection
states the runtime defaults it really runs with (V1 1, V2 4, provider 2 of
32), not the reviewed paid profile.

### Per run (both engines)

| Dimension | Reviewed value | Enforced by |
| --- | --- | --- |
| model calls / input / output / total tokens | 150 / 500,000 / 120,000 / 600,000 | `backend/budget.py` BudgetTracker |
| estimated / actual cost | $3.00 / $1.00 | BudgetTracker |
| duration / agent steps / semantic retries | 1800 s / 56 / 15 | BudgetTracker |
| concurrent runs per user / per project | 1 / 1 | `_enforce_concurrency_limits` (`backend/main.py`), DB count of active runs before any row is written; also applied to operator capture |
| daily user / project budget | $4.00 / $4.00 | budget reservations |
| search invocations per run / per request | 60 / 4 | BudgetTracker |

### vehicle_catalog_v1

| Limit | Value | Where |
| --- | --- | --- |
| technical-enrichment parallelism | `MILO_V1_TECHNICAL_PARALLELISM=4` (reviewed), structural bound 1–32 | `core.py` ~1281 |
| in-process Kimi concurrency | `MAX_PARALLEL_KIMI_CALLS=2` (`KIMI_CONCURRENCY_SEMAPHORE`) | `core.py:48-51` |
| technical chunk / verifier chunk | 4 models / 6 models | `core.py:85,87` |
| tool rounds | 15 | `core.py:47` |
| correction / commander | none (V1 has no commander; verifier is one phase) | — |

### swarm_v2

| Limit | Value | Where |
| --- | --- | --- |
| active workers (queueing width) | `MILO_SWARM_MAX_ACTIVE_WORKERS=2` (reviewed; code default 4, range 1–32, then `min(value, provider concurrency)`) | `engines/swarm_v2/executor.py` |
| tasks / tool calls / replans per run | 23 / 24 / 1 | PLAN dimensions |
| tool calls per task / graph depth / recursion | 4 / 12 / 4 | PLAN dimensions |
| correction rounds | 1 | `correction.py:43` |
| verifier batch | 25 claims, 32 KiB, 12,000 evidence chars | `verifier.py:83-85` |

### Provider (Kimi / Moonshot, organization-wide)

| Dimension | Kimi Tier 2 | MILO ceiling (×0.80) | Reviewed per-process posture | Enforced by |
| --- | --- | --- | --- | --- |
| concurrent requests | 40 | 32 | `MILO_PROVIDER_MAX_CONCURRENCY=2` | Redis lease (`ProviderQuotaCoordinator.try_acquire_inference`) then process semaphore |
| RPM | 100 | 80 | `MILO_PROVIDER_RPM_LIMIT=40` | Redis rolling 60 s window, then process window |
| TPM | 3,000,000 | 2,400,000 | `MILO_PROVIDER_TPM_LIMIT=1,200,000` | Redis rolling window weighted by estimated input + max completion tokens |
| TPD | unlimited | none | — | `backend/budget.py` daily budgets |
| rate-limit retries / backpressure wait | — | — | 5 / 240 s, backoff 2 s → 30 s | `backend/provider_scheduler.py` + `backend/provider_authority.py` (429 and 503 are backpressure, never a semantic retry) |
| web search QPS | unverified | — | 1 / 1 (fallback, `SEARCH_QPS_VERIFIED=False`) | coordinator `admit_search` |

`ProviderLimitsConfig.from_env` refuses any posture above the ceiling
(`assert_within_organization_ceiling`: concurrency > 32, RPM > 80, TPM >
2,400,000 raise). In Production the shared Upstash Redis coordinator is
mandatory (`resolve_coordinator` raises without it); the per-process semaphore
and windows are not the authority.

### Which limit wins

For one provider request: the Redis organization lease (32) → the Redis RPM/TPM
windows (80 / 2.4 M) → the process `MILO_PROVIDER_MAX_CONCURRENCY` (2) → the
engine width (V1 `MAX_PARALLEL_KIMI_CALLS=2` and technical parallelism 4, V2
active workers `min(2, provider concurrency)`) → the run's own budget rails.
The smallest binds; today that is the per-process concurrency of 2 for
inference and 40 RPM per process, with the Redis gate binding first if
several processes run. Multiple website runs cannot bypass any of it: the
per-user and per-project caps of 1 are enforced from a database count before a
row exists (`tests/test_website_product_path.py` proves two concurrent
creations are refused with 429), the Cloud Run job runs `--parallelism 1
--tasks 1` per execution, and every provider request from every execution
passes through the same organization-scoped Redis keys (`MILO_PROVIDER_QUOTA_SCOPE`,
default `kimi-org`).

API request rate limits (`backend/rate_limit.py`, Upstash-backed, fail-closed in
production): run creation 5/min per user, 20/min per project; cancellation
10/min; worker mutations 600/min. Gateway (`frontend/lib/server/rateLimit.ts`):
unauthenticated 30/min, authenticated 120/min, polling 120/min, run creation
5/min, cancellation 10/min.

## 7. Canonical export

```
GET /runs/{id}/export  (backend/main.py export_run; gateway SAFE rule, GET only)
  → repo.get_run(run_id, user_id)            membership authorization, 404 for a non-member
  → build_export_envelope(run)               backend/export_envelope.py — the ONE export authority
  → validate_export_envelope(envelope)       re-validated before it leaves
  → usage replaced by the bounded public RunUsage projection
  → 409 RUN_NOT_EXPORTABLE with a static reason for: a live run, an absent or
    unreadable immutable identity, a non-product workflow, an identity not
    bound to a release, a stored Swarm V2 product that is not contract-valid
  → frontend/lib/api.ts exportRun → lib/runExport.ts parseExportEnvelope (closed read of
    schema_version, run_id, engine, terminal_status, result_kind, generated_at,
    government_provenance.present; identity must name the same run and engine)
  → components/result/RunExportControl.tsx: one button for a terminal run with a
    trustworthy identity; shows the summary and offers the server's document
    verbatim as `milo-run-<id>.json`. No export logic in React; `result` is
    downloaded, never rendered.
```
