# MILO Gap Audit — 2026-09-16

Reconciliation of the original three-stage roadmap with the system that exists
after Catalog PR3 (#88), F4 (#89) and F5 (#90).

**This audit performed no implementation and no production mutation.** See §16.

---

## 1. Executive conclusion

The engine is built and connected. Nothing in this repository can fill the
catalog it was built to fill, and the state of that catalog in the protected
production project is **not established by anything this audit can read**.

Concretely, and each of these is first-hand evidence rather than a document
claim. Where a statement is about the live production project rather than about
this repository, it says so and stops at what the evidence actually proves:

1. **Swarm V2 is production-wired.** One `ToolRegistry` is constructed in a
   release path (`backend/worker/main.py:382`), holding exactly one tool —
   `catalog.government_vehicle`, read mode, one server-owned scope. One
   evidence mapper is registered. The promotion pipeline is constructed in the
   same wiring and called in `execute_run` after verdicts settle, under the
   run's lease. None of this is test-only.
2. **Evidence can enter the system by exactly one route.** `GenericWorker` has
   no evidence capability at all; a durable source/fragment/claim is produced
   only by a registered mapper from a registry-validated tool result
   (`backend/engines/swarm_v2/worker.py:185-221`,
   `evidence_mapping.py:226-256`). The only registered mapper is
   `catalog.government_vehicle.resolve_variant`.
3. **A Swarm V2 run over a database holding no usable Government snapshot can
   therefore produce no verified field at all.** Not "few" — none. The honest
   product outcome of such a run is `partial_success` / `no_usable_result`,
   which is exactly what `docs/deployment/swarm-v2-smoke.md` already requires of
   its smoke. This is a conditional about the code path, and the repository
   proves the conditional; whether the antecedent currently holds in production
   is item 5.
4. **No supported capture entrypoint exists.** `DataGovClient`,
   `GovernmentCatalogIngestor` and `GovernmentCatalogRefresh` are constructed
   **only in tests**. There is no production entrypoint, no operator script, no
   CLI, no `__main__`, no Cloud Run job and no workflow that captures from
   `data.gov.il`. This is a **code** gap, not only an authorization gap: an
   operator with full credentials and explicit authorization has no supported
   way to run a capture from this repository today. It is a statement about the
   repository, and the repository proves it. It is **not** a statement that no
   snapshot has ever been created by some external or manual action — that
   would be a claim about a database this audit has not read.
5. **The production catalog's schema and snapshot state are UNVERIFIED, and
   this audit cannot settle them.** What the dated workflow run proves is
   narrower than it first appears: at 2026-09-16T03:18:35Z, nine local migration
   **versions** were absent from the linked project's **migration history**, and
   `db push --dry-run` proposed them (§2.3). Migration history is a ledger, not
   the schema. The workflow's own failure message says so —
   *"The production schema may already contain objects that are not recorded in
   Supabase migration history."* Objects can exist without a history row, a
   history row can exist without its objects being intact, and an operator may
   have acted after that timestamp. The current remote object state is therefore
   classified `BLOCKED_EXTERNAL` and is decided only by the read-only inspection
   in §13.2 (OPERATOR-0) — never from migration history alone.
6. **The catalog has no kill switch, no catalog-specific monitoring or rollback
   procedure, and no typed or operator-facing status.** No `MILO_*` flag gates
   the Government tool or the promotion path; both are unconditional in every
   `swarm_v2` run. `MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md`,
   `STAGED_ACTIVATION.md` and `ENVIRONMENT_MATRIX.md` contain no catalog,
   Government, snapshot or promotion entry, and `FINAL_ACCEPTANCE.md`
   classifies no catalog item. The two events the path emits **are** retained
   and rendered as raw developer telemetry in the Run Inspector (item stated
   precisely in CAT-12/CAT-13); what is missing is typed recognition and any
   operational signal, not all visibility.
7. **The first original requirement that is neither completed nor legitimately
   replaced is roadmap §3.2** — a durable local Government source snapshot that
   does not depend on `data.gov.il` availability during a chat run. No
   repository evidence and no accepted production record demonstrates one, and
   no supported entrypoint can produce one. Everything downstream of it
   (§3.3 tree, §3.4 tool usefulness, §3.5 reconciliation, §3.8 end-to-end,
   §3.9 refresh) depends on that same fact.

Green CI proves what its jobs execute. It does not prove that any of the above
was deployed, configured, captured or authorized. And a migration-history
listing proves what a ledger records, not what a schema contains.

---

## 2. Verified repository and GitHub baseline

### 2.1 Git

| | |
| --- | --- |
| Repository | `giladscore494/milo-agent-workspace` |
| `origin/main` | `75f1590d2e6edb78fc2886aa9b6535e681038066` |
| Local HEAD at audit start | `75f1590d2e6edb78fc2886aa9b6535e681038066` |
| Audit branch | `claude/compassionate-galileo-z7sp8d`, branched from that exact `origin/main` |
| Merge-base | `75f1590d2e6edb78fc2886aa9b6535e681038066` |
| Working tree at scan | clean (`git status --short` empty) |

### 2.2 Pull requests

| Stage | PR | State | Merge SHA | Reviewed head | Base | Checks on head |
| --- | --- | --- | --- | --- | --- | --- |
| Catalog PR3 | [#88](https://github.com/giladscore494/milo-agent-workspace/pull/88) | MERGED 2026-09-16T03:18:20Z | `18c5f6402afc6a210a5d44ddd98a012231e350de` | `9ba2bd6e0f8ed67303b3edf1437c4c98c9491777` | `ef32a5f0…` | offline-checks, postgres-checks, e2e, Lint & Test, Vercel Preview Comments — all success |
| F4 | [#89](https://github.com/giladscore494/milo-agent-workspace/pull/89) | MERGED 2026-09-16T16:22:15Z | `0fdab212ce3c6277fa6d88502a4213401e1a3bcc` | `39603e53c596adf173f60ef83ba97abd2e2242dd` | `18c5f640…` | same five — all success |
| F5 | [#90](https://github.com/giladscore494/milo-agent-workspace/pull/90) | MERGED 2026-09-16T18:26:02Z | `75f1590d2e6edb78fc2886aa9b6535e681038066` | `94e68accc4f251e54d8b5bc6bdf02da996b6d08b` | `0fdab212…` | same five — all success |

All three merge commits verified as ancestors of `origin/main`
(`git merge-base --is-ancestor`). **No pull request is open**, and no Gap Audit
PR exists, so this audit opens the first one.

Checks on `origin/main` (`75f1590d…`): workflow `ci` run `35134373596` —
success; `Repo Scan` run `35134373618` — success.

### 2.3 Production Supabase migration HISTORY — what the dated run proves, and what it does not

> **Read the scope of this section before using it.** It is about
> `supabase_migrations.schema_migrations` — a ledger of migration **versions** —
> at one dated moment. It is **not** an observation of the schema. Nothing in
> this section establishes whether any table, function, view, trigger, policy or
> grant exists in the production project, and no decision to apply, reconcile or
> correct anything may be taken from it alone.

`.github/workflows/deploy-supabase-migrations.yml` runs on pushes to `main`
that touch `supabase/migrations/**`, in the `production` GitHub environment,
and performs a **dry-run preflight only** (`supabase migration list` +
`supabase db push --linked --dry-run`); the apply step is gated on the
repository variable `SUPABASE_MIGRATIONS_AUTO_APPLY`.

The last such run is workflow run
[`35051288215`](https://github.com/giladscore494/milo-agent-workspace/actions/runs/35051288215),
job `104652031279`, on `18c5f6402afc6a210a5d44ddd98a012231e350de` at
2026-09-16T03:18:35Z. It succeeded; step 13 *Apply production migrations* was
**skipped** (`SUPABASE_MIGRATIONS_AUTO_APPLY` empty), and step 11 printed
"Automatic production migration apply is disabled." Its log shows
`supabase db push --linked --dry-run` reporting **"Would push these
migrations"**:

| Proposed by `db push --dry-run`; version not recorded in the linked project's migration history |
| --- |
| `20260828000100_canonical_scope_conflict_identity.sql` |
| `20260828000200_source_evidence_fragments.sql` |
| `20260902000100_r3_versioned_focused_evidence.sql` (R3) |
| `20260907000100_r4_deterministic_verification.sql` (R4) |
| `20260914200000_catalog_evidence_foundation.sql` (Catalog PR1) |
| `20260915120000_catalog_integrity_corrections.sql` (PR1 corrective) |
| `20260915180000_catalog_raw_record_source_locator.sql` (Catalog PR2) |
| `20260916090000_catalog_bounded_candidate_queries.sql` (Catalog PR3) |
| `20260916120000_catalog_field_level_promotion.sql` (Catalog PR3) |

`supabase migration list` in the same job shows the remote history column
stopping at `20260823000100`. Versions `001`–`015`, `20260706192500`, the
`20260810*` set, `20260818000100/200` and `20260823000100` are **recorded in
that history**.

**Exactly what this proves.** At commit `18c5f640…` and timestamp
2026-09-16T03:18:35Z, the Supabase project the `production` GitHub environment
links recorded no migration version newer than `20260823000100`, the nine
versions listed above were absent from that history, `db push --dry-run`
proposed them, and the tooling could reach the project.

**Exactly what this does NOT prove — and the workflow says so itself.** The
same file defines a failure message it would print on a history read or dry-run
error (`.github/workflows/deploy-supabase-migrations.yml:43-46`):

> The production schema may already contain objects that are not recorded in
> Supabase migration history. No migrations were applied. Compare the remote
> schema and migration history with migrations 001–006 before deciding whether
> a one-time migration history reconciliation is required.

Accordingly, this log does **not** establish any of the following, and no
statement anywhere in this audit may rest on them:

| Not established | Why |
| --- | --- |
| that the tables, functions, views, triggers, policies or grants of those nine versions are **absent** | objects can exist without a recorded history row — the workflow's own warning |
| that the objects of the recorded versions are **present and intact** | a history row records that a version ran, not that its objects still exist unaltered |
| that nothing was applied **after** that timestamp | an operator may have acted by hand at any point since |
| that no objects were created **outside** migration history | manual DDL leaves no history row |
| that any catalog snapshot, candidate or canonical row does or does not exist | no relation was queried |
| which project this is beyond "the one the `production` environment secret points at" | the project ref is a masked secret, and another tool may link a different project |

**Therefore the current remote schema and object state is classified
`BLOCKED_EXTERNAL`** and stays that way until the read-only inspection in
§13.2 (**OPERATOR-0**) is run against that exact protected project. Only that
later evidence may choose among a normal apply, a migration-history
reconciliation, a corrective forward migration, or no action at all. This audit
selects none of them.

Two further facts from the same log, recorded because they are operator-visible
drift: `supabase link` reported **local `supabase/config.toml` differs from the
linked project** (`site_url`, `additional_redirect_urls`, MFA TOTP enrolment,
email confirmations, OTP frequency/length), and the pinned CLI is `2.31.8`
against an available `2.117.0`. Neither is in scope to change here.

---

## 3. Authority and methodology

Authority order used, highest first:

1. the GAP-AUDIT prompt and the attached handoff
   (`MILO_CURRENT_ROADMAP_HANDOFF_2026-09-15_HE(1).md`, read complete);
2. `AGENTS.md` and `CLAUDE.md`;
3. **merged production code, migrations, schemas and executable tests** — the
   definition of actual behaviour;
4. `docs/production-readiness/` where it declares older documents superseded;
5. merged Catalog PR3 / F4 / F5 documentation over conflicting older roadmap text;
6. `docs/roadmap/MILO_UNIFIED_ENGINE_3_STAGE_ROADMAP.md` as the requirement
   inventory to reconcile — not as a description of current architecture.

Method. All 47 repository Markdown files were enumerated before reading, so no
newer authoritative document was missed. Every classification below was reached
by reading the merged code path and tracing it to its real caller with
`rg`/`git`, never from a document claim, a test name, a comment or a PR
description. Where a document and the code differ, the difference is recorded in
§10 rather than resolved in favour of the more favourable reading.

Status vocabulary — exactly the eight allowed labels:
`COMPLETED_AND_CONNECTED`, `COMPLETED_IN_CODE_NOT_ACTIVATED`, `FIXTURE_ONLY`,
`REPLACED_BY_NEW_ARCHITECTURE`, `INTENTIONALLY_DEFERRED`, `BLOCKED_EXTERNAL`,
`MISSING`, `OBSOLETE`.

`COMPLETED_AND_CONNECTED` is used only where the requirement is reachable
through the intended production code path. Test-only registration, an exported
but uncalled function, or an isolated class is never sufficient.

---

## 4. Requirement / evidence / status matrix

Columns: **ID** · **Source** · **Requirement (atomic)** · **Production
evidence** · **Test evidence** · **Deployment/activation evidence** ·
**Status** · **Gap** · **Work type** · **Proposed follow-up** ·
**Dependencies and risk**.

### 4.1 Stage 1 — Swarm Core (roadmap §1.1–§1.15)

| ID | Source | Requirement | Production evidence | Test evidence | Activation evidence | Status | Gap | Work | Follow-up | Dependencies / risk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1-01 | §1.1 | V1 stays the frozen control engine | `backend/engines/vehicle_catalog_v1/**`; `worker/main.py:343` default adapter | `tests/test_vehicle_catalog_engine.py`, `tests/test_engine_routing.py` | Stage C Attempt 7 ran V1 in production (`STAGE_C_ACCEPTANCE.md`, run `8b4a4277-…`) | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-02 | §1.2 | Engine chosen from trusted `project.workflow_key`, not run metadata | `backend/worker/engine.py:21-61` (`EngineRegistry.require`, `EngineResolver.resolve`), `worker/main.py:157-166` | `tests/test_engine_routing.py` | not proven for `swarm_v2` (no production swarm run recorded) | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-03 | §1.2 | Unknown `workflow_key` fails closed `ENGINE_NOT_ALLOWED` | `backend/worker/engine.py:33,52,56,60` | `tests/test_engine_routing.py` | n/a | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-04 | §1.2/§1.14 | Checkpoint lookup uses the resolved workflow, no cross-workflow contamination | `worker/main.py:191`; `swarm_v2/state.py:57-62` (`resume` refuses another workflow/version) | `tests/test_swarm_v2_resume_budget.py`, `tests/test_swarm_v2_runtime.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-05 | §1.3 | Independent `swarm_v2` package, dependencies injected | `backend/engines/swarm_v2/**` (27 modules) | `tests/test_swarm_v2*.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-06 | §1.4 | Strict dynamic task-graph contracts (`extra=forbid`) | `swarm_v2/contracts.py:16-330` (`StrictContract`, `DynamicTask`, `TaskGraph`, `CommanderPlan`, …) | `tests/test_swarm_v2.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-07 | §1.5 | Deterministic plan firewall; model output inert until validated | `swarm_v2/validation.py:203-260` (`PlanValidator`), wired `worker/main.py:399` | `tests/test_swarm_v2.py`, `tests/test_swarm_v2_repair.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-08 | §1.6 | Commander model resolved from an allowlist, fail-closed | `swarm_v2/models.py`; `worker/main.py:365-370` refuses an unset/un-allowlisted model | `tests/test_swarm_v2.py` | env contract pinned in `docs/deployment/swarm-v2-smoke.md` (read-only inspection 2026-08-24) | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-09 | §1.7 | Every model call passes one BudgetTracker + ProviderScheduler | `swarm_v2/model_gateway.py:24-80`; `worker/main.py:386-396` | `tests/test_provider_scheduler.py`, `tests/test_provider_backpressure.py`, `tests/test_budget.py` | not proven for V2 | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-10 | §1.8 | Bounded generic worker pool, dependency-ordered | `swarm_v2/executor.py`, `worker.py`; `worker/main.py:433-446` | `tests/test_swarm_v2.py`, `tests/test_swarm_v2_stage1_e2e.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-11 | §1.9 | Tool Registry as an allowlist with schema/scope/mode enforcement | `backend/tools/registry.py` (`ToolRegistry.execute` re-validates scope, mode, input and output) | `tests/test_swarm_v2_tool_contract.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-12 | §1.9 | Stage-1 mock tools for testing | `backend/tools/mock.py` | `tests/test_swarm_v2_tool_contract.py` | never registered in production (correct) | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-13 | §1.10 | Every V2 durable evidence/tool write is lease-guarded | migration `20260823000100_lease_guarded_evidence_writes.sql`; `repository/supabase.py` `_guarded_rpc` | `tests/test_migrations_postgres.py::test_stale_worker_full_scenario_every_mutation_rejected` (real PostgreSQL) | `20260823000100` is **recorded in the linked project's migration history** (§2.3), the newest version that is; whether its objects are present and intact is unverified | `COMPLETED_AND_CONNECTED` | none for the rule itself | `NONE` | none | the rule is implemented and connected in code. Whether the R3 fragment and R4 verdict relations the current evidence path writes through (`20260828000200`, `20260902000100`, `20260907000100`) exist in the target database is unverified (OPS-01); if they do not, those writes fail closed rather than bypassing the lease |
| S1-14 | §1.11 | Evidence Board on the existing sources/claims/conflicts | `swarm_v2/evidence.py` (`EvidenceBoard`, `WorkerLease`), constructed `worker/main.py:415-418` | `tests/test_swarm_v2_evidence.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-15 | §1.12 | Commander replanning loop with hard caps | `swarm_v2/commander.py`, `engine.py`, `validation.py` `PlanLimits` | `tests/test_swarm_v2.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-16 | §1.13 | Verifier issues structured verdicts; deterministic builder assembles the result | `swarm_v2/verifier.py`, `builder.py`, `outcome.py`; wired `worker/main.py:475-476` | `tests/test_swarm_v2_r4_deterministic_verification.py`, `tests/test_swarm_v2_outcome_contract.py` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S1-17 | §1.14 | Real checkpoint/resume: completed tasks are not re-run | `swarm_v2/state.py`; `worker/main.py:502-519` restores the component-wise max of run row and checkpoint usage | `tests/test_swarm_v2_resume_budget.py` | not proven against a real Cloud Run task restart | `COMPLETED_IN_CODE_NOT_ACTIVATED` | a real restart has never been observed | `OPERATOR` | part of OPERATOR-3 | low risk; proven in memory + PostgreSQL |
| S1-18 | §1.15 | Swarm event vocabulary visible without a new dashboard | `swarm_v2/engine.py` emits `commander_plan_created`, `task_*`, `tool_called`, `evidence_added`, `conflict_found`, `verification_*`; `frontend/lib/eventVocabulary.ts:57-63` mirrors them | `tests/test_swarm_v2_runtime.py`, `frontend/tests/eventProjection.test.ts` | not proven | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | `backend/runtime.py` `EVENT_TYPES` does **not** list the V2 vocabulary; `SupabaseEventSink` does not validate against it, so this is a naming gap, not a block (see §10) |

### 4.2 Stage 2 — "ידע רכב" / Yeda (roadmap §2.1–§2.9)

The roadmap itself carries an explicit `SUPERSEDED` decision banner for the
whole of Stage 2 (lines 486–545): the aggregated JSON is incomplete and
incorrect, the canonical catalog starts empty, and the JSON is demoted to an
unverified `legacy_reference`. The traceable successor is Catalog PR1 (#85/#86),
PR2 (#87) and PR3 (#88). These rows are therefore `REPLACED_BY_NEW_ARCHITECTURE`
on the strength of a stated decision **and** a named successor — not on absence.

| ID | Source | Requirement | Successor / evidence | Status | Gap | Work |
| --- | --- | --- | --- | --- | --- | --- |
| S2-01 | §2.1 | Lock the Yeda JSON schema contract | superseded; no `backend/tools/yeda/` exists and none is invented | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-02 | §2.2 | Read-only pinned GitHub source adapter for the JSON | superseded by `catalog_source_snapshots` + the `legacy_reference` family (`backend/catalog/contracts.py:35,52`) | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-03 | §2.3 | Snapshot + normalized index of the JSON in Supabase | superseded by the `catalog_*` namespace (migration `20260914200000`) | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-04 | §2.4 | `YedaCatalogTool` compact query API | superseded by `catalog.government_vehicle`; no Yeda tool is registered | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-05 | §2.5 | Coverage/gap objects against the JSON | superseded by `backend/catalog/government/reconcile.py` outputs | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-06 | §2.6 | Yeda records become sources/claims | superseded — `is_evidence_family("legacy_reference")` is False (`contracts.py:204`); legacy can never carry a verdict | `REPLACED_BY_NEW_ARCHITECTURE` | none | `NONE` |
| S2-07 | §2.7 | Deterministic `YedaPatchBuilder` | superseded — there is no write-back to the aggregated JSON under the corrected decision | `OBSOLETE` | none | `NONE` |
| S2-08 | §2.8 | Controlled GitHub write path, `MILO_ENABLE_YEDA_WRITES` | superseded — no write tool is registered anywhere; `write_approved` is `False` and no `tool:write:*` capability is granted (`worker/main.py:409`) | `OBSOLETE` | none | `NONE` |
| S2-09 | §2.9 | Stage 2 E2E: read → gaps → patch proposal | superseded together with §2.7/§2.8 | `OBSOLETE` | none | `NONE` |
| S2-10 | §2 banner | A bounded `legacy_reference` side exists to reconcile against, without becoming truth | the *rules* exist (`contracts.py:35,52,204`; `reconcile.py:6-15,102`), but **no code anywhere creates a `legacy_reference` snapshot** — verified by grep across `backend/` and `scripts/` | `MISSING` | nothing can produce the legacy side, so reconciliation has one input | `CODE` | CODE-4 (low priority) | only needed when reconciliation is wanted; the empty-catalog decision deliberately does not require it |

### 4.3 Stage 3 — `data.gov.il` (roadmap §3.1–§3.9)

| ID | Source | Requirement | Production evidence | Test evidence | Activation evidence | Status | Gap | Work | Follow-up | Dependencies / risk |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S3-01 | §3.1 | Deterministic bounded CKAN client (pagination, envelope, schema, timeouts, retries) | `backend/catalog/government/client.py:203-400`; `transport.py` is the only socket-capable module | `tests/test_catalog_government_ingestion.py` (fixture-backed, sockets disabled at module level) | **never constructed outside tests** — verified by grep: `DataGovClient(` appears only in `tests/` and `backend/testing/` | `COMPLETED_IN_CODE_NOT_ACTIVATED` | no caller | `CODE` | CODE-1 | see S3-02 |
| S3-02a | §3.2 | **A supported, guarded live Government capture entrypoint exists in the repository** | ingestion code `backend/catalog/government/ingest.py:188-225` exists; **nothing constructs it outside tests** — `DataGovClient(` and `GovernmentCatalogIngestor(` appear only under `tests/` and `backend/testing/`, and `backend/catalog/` contains no `__main__` and no `argparse` | `tests/test_catalog_government_ingestion.py` over the pinned `q=RAV4` 233-row capture | n/a — there is nothing to activate | `MISSING` | no operator or production entrypoint can perform a capture | `CODE` | **CODE-1** | proven from the repository alone; the code half of §14 |
| S3-02b | §3.2 | **A durable Government snapshot exists in the target database, so chat does not depend on `data.gov.il`** | nothing in this repository observes the production database; §2.3 is a migration-history ledger, not a schema or row observation | fixture-backed tests prove the shape of a snapshot, never the existence of one in production | **not proven either way.** No repository evidence and no accepted production record demonstrates a durable live catalog snapshot; equally, this audit cannot assert that none exists | `BLOCKED_EXTERNAL` | the state is unknown until OPERATOR-0 reads it | `OPERATOR` | **OPERATOR-0**, then OPERATOR-3 if none is found | **the state half of §14** — do not read this row as either presence or absence |
| S3-03 | §3.2 | Capture is idempotent, checksum/row-count verified, never active while partial | `client.py:330-360` (`GOV_PAGINATION_INCOMPLETE`), `snapshot.py`, `activate_catalog_snapshot_guarded` | `tests/test_catalog_government_ingestion.py`, `tests/test_migrations_postgres.py` | not activated | `COMPLETED_IN_CODE_NOT_ACTIVATED` | never exercised against live pagination | `OPERATOR` | OPERATOR-3 | live CKAN paging behaviour is unproven |
| S3-04 | §3.2 | A whole ~101 000-row resource can actually be captured | bounds `source.py:97-107`: `DEFAULT_PAGE_LIMIT=100`, `MAX_PAGE_LIMIT=1000`, `MAX_PAGES_PER_CAPTURE=200`, `MAX_RECORDS_PER_CAPTURE=120_000` | pagination bounds tested on 233 rows | none | `COMPLETED_IN_CODE_NOT_ACTIVATED` | **at the default page size a ~101 000-row resource is refused** (`ceil(101000/100)=1010 > 200` → `GOV_PAGE_BUDGET_EXCEEDED`); it fits only when the client is constructed with `page_limit=1000`, and whether a 1 000-record page stays under `MAX_RESPONSE_BYTES` (8 MiB) is unproven | `CODE` | CODE-1 must set and justify the page size | a wrong page size turns the first authorized capture into a guaranteed refusal |
| S3-05 | §3.3 | Deterministic normalization into a manufacturer→model→year→variant structure | `backend/catalog/government/normalize.py`; candidates land in `catalog_candidate_variants` | `tests/test_catalog_government_ingestion.py` | not activated | `COMPLETED_IN_CODE_NOT_ACTIVATED` | no rows exist | `OPERATOR` | OPERATOR-3 | depends on S3-02 |
| S3-06 | §3.3 | Dedicated `government_vehicle_model_years` / `government_vehicle_variants` tables | none, deliberately | n/a | n/a | `REPLACED_BY_NEW_ARCHITECTURE` | none — successor is `catalog_candidate_variants` plus query layers (`projection.py`, `query.py`), stated in the roadmap's own §3 banner | `NONE` | none | — |
| S3-07 | §3.3 | The quantity resource builds manufacturer/model/year counts | none, deliberately; `QUANTITY_RESOURCE_ID` is allowlisted for capture (`source.py:72-73`) but has a raw-only contract and no normalization | n/a | n/a | `REPLACED_BY_NEW_ARCHITECTURE` | none — explicit decision recorded in the roadmap §3 banner ("captured and preserved, never read for identity") | `NONE` | none | — |
| S3-08 | §3 scope | **Both** approved CKAN resources are synced deterministically | both allowlisted; only `WLTP_RESOURCE_ID` is bound to the tool (`government_vehicle.py:250`) | n/a | none | `COMPLETED_IN_CODE_NOT_ACTIVATED` | neither resource has been synced; the quantity resource has no reviewed use | `OPERATOR` | OPERATOR-3 (WLTP only) | capturing quantity is not required to finish §3.2 |
| S3-09 | §3.4 | A registered, bounded, read-only `GovernmentVehicleTool` | `backend/tools/government_vehicle.py`; registered `worker/main.py:382`; scope granted `worker/main.py:409` | `tests/test_catalog_pr3_swarm_promotion.py`, `tests/test_swarm_v2_tool_contract.py` | registered in the release path, but answers nothing without a snapshot | `COMPLETED_AND_CONNECTED` | none in wiring | `NONE` | none | inert until S3-02 |
| S3-10 | §3.4 | No operation returns a whole raw resource; every page is bounded and exactly totalled | `MAX_TOOL_PAGE_ITEMS = MAX_RESULT_ITEMS` (200); `resolve_variant` quotes seven identity fields of one row only | `tests/test_catalog_pr3_swarm_promotion.py`; SQL bound `catalog_page_limit()` | the SQL side's version is not recorded in the linked project's migration history (§2.3); whether the functions exist is unverified | `COMPLETED_IN_CODE_NOT_ACTIVATED` | the deployed SQL state is unknown | `OPERATOR` | OPERATOR-0 | — |
| S3-11 | §3.4/PR3 | The complete dataset is queryable database-side, not by Python materialization | migration `20260916090000` (7 read functions, 3 collate-"C" indexes); `government/query.py` | `tests/test_migrations_postgres.py` against real PostgreSQL over 140 and 13 candidates | version not in the linked project's migration history (§2.3), deployed state unverified; and never run over ~101 000 rows anywhere | `COMPLETED_IN_CODE_NOT_ACTIVATED` | index/plan behaviour at real scale is unmeasured | `OPERATOR` | OPERATOR-0, then OPERATOR-4 | a slow plan at 101 k rows would surface only after S3-02b |
| S3-12 | §3.5 | Deterministic tiered Government ↔ legacy crosswalk producing structured gaps | `backend/catalog/government/reconcile.py:258-380` | `tests/test_catalog_government_ingestion.py` | **no production caller** — `reconcile_catalog` is exported from `__init__.py` and called only by tests | `COMPLETED_IN_CODE_NOT_ACTIVATED` | nothing invokes it; `REVIEWED_ALIAS_RULES` is `()` (`reconcile.py:71`), so tier 3 never fires; and no legacy side exists (S2-10) | `CODE` | CODE-4 | not required to finish §3.2 |
| S3-13 | §3.6 | Government facts become versioned, located evidence with field-specific authority | `backend/catalog/government/evidence.py` (`GovernmentVariantEvidenceMapper`); registered `evidence_mapping.py:226-256`; wired `worker/main.py:423-425` | `tests/test_swarm_v2_r3_evidence_contract.py`, `tests/test_catalog_pr3_swarm_promotion.py` | connected in the release path; produces nothing without a snapshot | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | inert until S3-02 |
| S3-14 | §3.6 | `koah_sus` is never mapped to horsepower; no semantic guessing | the mapper does not read the field; no promotable entry exists (`contracts.py:127-145`) | `tests/test_catalog_pr3_swarm_promotion.py` | n/a | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | — |
| S3-15 | §3.7 | Government-first Commander policy keyed by the registered tool | `swarm_v2/validation.py:156-174` `SOURCE_FIRST_TOOL_POLICY`, surfaced through `provider_plan_policy` into `ModelGateway` | `tests/test_israel_source_policy.py` | not proven live | `COMPLETED_AND_CONNECTED` | none | `NONE` | none | the policy tells the Commander to plan "targeted research" for gaps, but **no research tool is registered**, so such a task can produce no evidence (see S3-17) |
| S3-16 | §3.8 | End-to-end: plan → tool → evidence → verdict → promotion → canonical read | `backend/catalog/pipeline.py`; called `worker/main.py:542-550` under the lease, after verdicts settle | `tests/test_catalog_pr3_swarm_promotion.py` (incl. six crash windows, replacement worker, pending-read outage) — memory repository; derivation also against real PostgreSQL | **fixture/offline only**; never run in production | `FIXTURE_ONLY` | the whole chain has never executed against live data | `OPERATOR` + `AUTHORIZATION` | OPERATOR-3 then AUTH-1 | depends on S3-02 and OPERATOR-1 |
| S3-17 | §5 | A `web_research` read capability exists in the Tool Registry | **none** — the production registry holds one tool (`worker/main.py:382`) | R5 web tool/mapper exist only under `backend/testing/r5_proof/` | none | `MISSING` | no production Web research capability; a worker's model text can never become evidence | `CODE` | CODE-5 (conditional, §9.1 of the handoff) | required before any "targeted research for gaps" is real |
| S3-18 | §3.9 | `sync_if_changed` refresh with a bounded diff and no duplicate work on no-change | `backend/catalog/government/refresh.py:225-300`; DB diff `catalog_snapshot_candidate_diff` | `tests/test_catalog_pr3_swarm_promotion.py`; diff against real PostgreSQL | **no schedule and no caller** — a static test asserts `sync_if_changed` appears nowhere in `backend/`, `scripts/` or `.github/workflows/` | `COMPLETED_IN_CODE_NOT_ACTIVATED` | no scheduler is configured or authorized | `OPERATOR` + `AUTHORIZATION` | deferred to handoff §9.4 | must not be scheduled before a controlled bootstrap |

### 4.4 Catalog PR1–PR3 delivered requirements

| ID | Source | Requirement | Production evidence | Test evidence | Activation evidence | Status | Gap | Work | Follow-up |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CAT-01 | PR1 | Durable catalog namespace with RLS and least-privilege grants | `20260914200000`, `20260915120000` | `tests/test_migrations_postgres.py` (`MILO_REQUIRE_PG_TESTS=1`, zero skips) | versions not recorded in the linked project's migration history (§2.3); object state unverified | `COMPLETED_IN_CODE_NOT_ACTIVATED` | establish the deployed state, then decide | `OPERATOR` | OPERATOR-0 |
| CAT-02 | PR1 | The canonical catalog starts empty; no seed, no backfill | migrations insert no row; `check_migrations.py` forbids destructive statements | `tests/test_catalog_migration_static.py` | n/a | `COMPLETED_AND_CONNECTED` | none | `NONE` | none |
| CAT-03 | PR2 | Bounded read-only CKAN ingestion into PR1's relations | `backend/catalog/government/` | fixture-backed suite | no caller | `COMPLETED_IN_CODE_NOT_ACTIVATED` | see S3-01/S3-02 | `CODE` | CODE-1 |
| CAT-04 | PR2 | Every record traces to snapshot, resource, upstream `_id`, version, page and index | `20260915180000` `source_locator`; `snapshot.py` | `tests/test_catalog_government_ingestion.py` | version not in migration history (§2.3); object state unverified | `COMPLETED_IN_CODE_NOT_ACTIVATED` | establish the deployed state, then decide | `OPERATOR` | OPERATOR-0 |
| CAT-05 | PR3 | Field-level, append-only canonical provenance enforced for **every** writer | `20260916120000`: `catalog_canonical_field_provenance` + `catalog_check_field_provenance` (BEFORE INSERT) + two DEFERRED coverage triggers | `tests/test_migrations_postgres.py` | version not in migration history (§2.3); object state unverified | `COMPLETED_IN_CODE_NOT_ACTIVATED` | establish the deployed state, then decide | `OPERATOR` | OPERATOR-0 |
| CAT-06 | PR3 | One authoritative "current canonical value" read model | view `catalog_canonical_variant_current`; `repository/supabase.py:1034` | `tests/test_catalog_pr3_swarm_promotion.py` | version not in migration history (§2.3); whether the view exists is unverified | `COMPLETED_IN_CODE_NOT_ACTIVATED` | establish the deployed state, then decide | `OPERATOR` | OPERATOR-0 |
| CAT-07 | PR3 | Promotion is lease-guarded, bounded (`MAX_PROMOTIONS_PER_RUN = 25`) and never a tool | `contracts.py:265`; `pipeline.py`; `worker/main.py:432,542` | `tests/test_catalog_pr3_swarm_promotion.py` | connected; inert without candidates | `COMPLETED_AND_CONNECTED` | none | `NONE` | none |
| CAT-08 | PR3 | A failed pending-promotion read is never reported as "owes nothing" | `pipeline.py` lets `AppError` escape; `worker/main.py:567` re-raises | `tests/…::…_is_never_marked_complete` (real `execute_run`) | connected | `COMPLETED_AND_CONNECTED` | none | `NONE` | none |
| CAT-09 | PR3 | Memory/PostgreSQL repository parity for every catalog method | `repository/supabase.py:68-109,814-1050`; `testing/memory_repository.py:926-1962` | `tests/test_catalog_persistence.py` (every method calls a literal RPC name, never a direct insert) | whether the RPCs those methods name exist in the target database is unverified (§2.3) | `COMPLETED_IN_CODE_NOT_ACTIVATED` | establish the deployed state, then decide | `OPERATOR` | OPERATOR-0 |
| CAT-10 | PR3 | An operator or product workflow can review and approve what promotion produced | **none** — `ready_for_review` is written by the pipeline itself (`pipeline.py:446`); no API route, script or UI reads it or the canonical catalog (`backend/main.py` has no `/catalog` route; `get_canonical_catalog_variant` is called only by `promotion.py` and tests) | none | none | `MISSING` | there is no human review or read surface for the canonical catalog at all | `CODE` | CODE-3 | needed before anyone can see or trust a bootstrap's output |
| CAT-11 | PR3 | Catalog components are covered by an independent kill switch, monitoring, alerting and rollback | **none** — no `MILO_*` flag gates the tool or the pipeline (grep for `MILO_*CATALOG*`/`*GOVERNMENT*` returns nothing); `MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md`, `STAGED_ACTIVATION.md`, `ENVIRONMENT_MATRIX.md` carry no catalog entry | none | none | `MISSING` | no independent switch, no metric, no alert, no runbook. Raw run-event telemetry exists (CAT-12) and is not monitoring | `CODE` + `OPERATOR` | CODE-2 | **should precede the first live capture** — see §12 |
| CAT-12 | PR3 | A promotion or refusal is observable at all | **yes, as raw developer telemetry.** The worker emits `catalog_variant_promoted` / `catalog_promotion_refused` with a safe message (`worker/main.py:543-550`); `reduceRunEvent` appends **every** event to `state.events` unconditionally (`frontend/lib/runReducer.ts:41`) **before** the `ownsV1Projection` gate on line 43; `RunInspector` renders `state.events.slice(-50)` with type, agent, phase and message through `safeText` (`frontend/components/inspector/RunInspector.tsx:80-85`) | `tests/test_catalog_pr3_swarm_promotion.py` asserts emission; `frontend/tests/eventProjection.test.ts:97,99` asserts an unrecognised type is retained in `state.events` and listed in `swarm.unknownEventTypes` — retention is a deliberate, tested F5 property | the Run Inspector shows them only once events are polled, which is gated behind `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (off by default) | `COMPLETED_IN_CODE_NOT_ACTIVATED` | none for raw observability; the gap is CAT-13 | `NONE` | none | corrected in review round 1 — the first revision of this audit wrongly said these events are visible nowhere |
| CAT-13 | PR3 | A promotion or refusal has typed recognition and an operational signal | **none** — the two types are absent from `backend/runtime.py` `EVENT_TYPES` and from both sets in `frontend/lib/eventVocabulary.ts`, so they stay unrecognised; there is no catalog-specific projection, view-model field, metric, alert or operator-facing status anywhere | none | none | `MISSING` | an operator watching a dashboard, and a user reading the product surface, learn nothing; only a developer reading the raw stream does | `CODE` | CODE-2 | pairs with CAT-11. Adding typed recognition must not weaken F5's rule that an unrecognised type stays inert outside raw telemetry |

### 4.5 Frontend F4 / F5

| ID | Source | Requirement | Production evidence | Test evidence | Activation evidence | Status | Gap | Work |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| FE-01 | F4 | A typed, fail-closed frontend contract for all four result kinds | `frontend/lib/finalResult.ts:47-50,649-790` | 162 F4 unit tests + 10 Playwright cases (PR #89) | UI gated behind `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (default off) | `COMPLETED_IN_CODE_NOT_ACTIVATED` | flag off by default (intended) | `OPERATOR` |
| FE-02 | F4 | Surface chosen from the trusted `workflow_key`, never from the payload | `frontend/app/page.tsx:181-207`; `swarmViewModel.ts:172` | `frontend/tests/*`, E2E | same | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-03 | F4 | V1 keeps `RunOutputPanel` byte-for-byte | `app/page.tsx:598` gated on `!swarm.isSwarmV2`; zero-line diff recorded in PR #89 | V1 regression suite | n/a | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-04 | F4 | `not_found` is rendered | `finalResult.ts:784`; `outcome.py:134` | unit + fixture | **unreachable in production** — no registered tool emits `TRUSTED_SOURCE_NO_MATCH` (`outcome.py:65,80`; `engine.py:503`) | `INTENTIONALLY_DEFERRED` | a production producer is deliberately absent | `NONE` |
| FE-05 | F5 | Session/project/conversation/run ownership on every durable browser write | `frontend/lib/ownership.ts`; `app/page.tsx` | `tests/stateOwnership.test.tsx` (13 recorded defects, each with a failing-first test) | isolated stack only | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-06 | F5 | Closed, application-owned error copy; no upstream prose | `frontend/lib/errorText.ts` `ERROR_COPY`; `supabaseClient.ts` `AuthFailure` from status only | `tests/errorText.test.ts`, `tests/supabaseClient.test.ts` | isolated stack | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-07 | F5 | Unknown/hostile events cannot manufacture state | `frontend/lib/eventVocabulary.ts`; `reduceRunEvent` | `tests/eventProjection.test.ts` (15 cases) | isolated stack | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-08 | F5 | Every one of the six terminal states proven E2E | `frontend/e2e/*` | Playwright 47 cases | isolated stack, mocked auth/worker/provider | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-09 | F5 | Browser bundle carries no secret; only approved `NEXT_PUBLIC_*` | `frontend/scripts/no-secret-bundle-check.mjs` with `MILO_REQUIRE_BUNDLE_SCAN=1` in CI | `npm run test:secrets`, `tests/secretBundleCheck.test.ts`, E2E 28 | scans artifacts this repository builds | `COMPLETED_AND_CONNECTED` | none | `NONE` |
| FE-10 | F5 | Live browser posture on the deployed site | `docs/production-readiness/FRONTEND_PRE_RELEASE.md` | none possible | never run | `BLOCKED_EXTERNAL` | operator pass required | `OPERATOR` |
| FE-11 | F5 | Automatic `launch_unknown` reconciliation | `scripts/release/reconcile-launch-unknown.sh` (manual, list-only by default) | `tests/test_release_tooling_cli.py` | n/a | `INTENTIONALLY_DEFERRED` | deliberate, with a stated condition | `NONE` |

### 4.6 Production readiness, deployment and activation

| ID | Source | Requirement | Evidence | Status | Gap | Work | Follow-up |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OPS-01 | MIGRATIONS.md | The repository's migrations are reflected in the target database | §2.3: nine migration **versions** were absent from the linked project's **migration history** at a dated run, including R3, R4 and the whole catalog namespace. No schema, function, view, trigger, policy, grant or row was observed by anything this audit can read | `BLOCKED_EXTERNAL` | the deployed object state is unknown. It must be read before any apply, reconciliation or corrective migration is even chosen — and this audit chooses none | `OPERATOR` | **OPERATOR-0** (read-only), which alone decides whether OPERATOR-1 is an apply, a history reconciliation, a corrective migration, or nothing |
| OPS-02 | DEPLOYMENT.md | Immutable images built, pushed and deployed at a pinned SHA | plans and scripts exist; no deployment record for any SHA at or after `18c5f640…` | `BLOCKED_EXTERNAL` | operator action + evidence | `OPERATOR` | OPERATOR-2 |
| OPS-03 | STAGED_ACTIVATION.md | Stage A completed on the current release | no Stage A record for this SHA | `BLOCKED_EXTERNAL` | run the two smoke suites | `OPERATOR` | OPERATOR-2 |
| OPS-04 | STAGE_C_ACCEPTANCE.md | Stage C closed PASSED, one-run authorization consumed | `STAGE_C_ACCEPTANCE.md:3-20` (run `8b4a4277-…`, 84 calls, 312 018 tokens, $0.252069) | `COMPLETED_AND_CONNECTED` | none | `NONE` | none |
| OPS-05 | STAGE_C_ACCEPTANCE.md:884 | Stage D remains unauthorized and blocked | "Stage D requires its own fresh, separate, explicit operator authorization" | `BLOCKED_EXTERNAL` | only a fresh explicit operator authorization can lift it | `AUTHORIZATION` | AUTH-2 |
| OPS-06 | swarm-v2-smoke.md | A Swarm V2 controlled production smoke has been executed and accepted | controller and contract exist; **no acceptance record exists anywhere in the repository** | `COMPLETED_IN_CODE_NOT_ACTIVATED` | the smoke has never been run | `OPERATOR` + `AUTHORIZATION` | OPERATOR-2 |
| OPS-07 | swarm-v2-smoke.md | Zero dangling budget reservations before the next smoke acceptance | one named dangling reservation: run `0d44d491-bc40-404e-9642-a5b8f77f3441`, reservation `8b05de80-fa01-4614-bec1-37f72ca63acc`, `status=reserved`, `estimated_cost=0.02`, unsettled, deliberately not mutated by any code | `BLOCKED_EXTERNAL` | a separately authorized production recovery must settle or void it | `OPERATOR` + `AUTHORIZATION` | OPERATOR-5 |
| OPS-08 | AUTHORIZATION_AND_RLS.md | The anon-EXECUTE hardening reaches production | the doc states both migrations are on staging only and that "the anon-EXECUTE gap should be assumed present in production until then". §2.3 shows `20260810000100` and `20260810000200` **recorded in the linked project's migration history**, which is evidence they ran — but a history row is not an ACL observation, and only OPERATOR-0's grant/RLS inspection settles whether `anon` still holds EXECUTE | `BLOCKED_EXTERNAL` | the doc's warning is probably stale, and "probably" is not a security finding. Verify the ACLs directly | `OPERATOR` | **OPERATOR-0** |
| OPS-09 | FINAL_ACCEPTANCE.md | Every major item carries a classification | the table classifies no catalog, Government, Swarm V2 or frontend-stage item | `MISSING` | the authoritative classification set does not cover the last four stages | `CODE` (documentation) | CODE-2 |
| OPS-10 | MONITORING_AND_INCIDENTS.md | External monitoring/alerting is configured | operator territory, unchanged | `BLOCKED_EXTERNAL` | operator action | `OPERATOR` | OPERATOR-6 |
| OPS-11 | handoff §9.2 | A controlled full Government capture with counts, checksums and a normalization issue report | none | `MISSING` | blocked on S3-02 | `OPERATOR` + `AUTHORIZATION` | OPERATOR-3 |
| OPS-12 | handoff §9.3 | Bootstrap metrics (verified rate, ambiguity rate, conflict rate, cost per 1 000 candidates) | none | `MISSING` | cannot be measured before a capture | `OPERATOR` | after OPERATOR-3 |
| OPS-13 | handoff §9.4 | Scheduled refresh with alerting and operator rollback | `sync_if_changed` exists; no scheduler | `INTENTIONALLY_DEFERRED` | deliberately deferred until a controlled bootstrap succeeds | `AUTHORIZATION` | not now |

**Classification totals — 84 atomic rows**, recomputed by parsing the matrix
above rather than by hand, after the review round that split S3-02 and corrected
CAT-12.

| Status | Count | Where |
| --- | ---: | --- |
| `COMPLETED_AND_CONNECTED` | 32 | S1 ×17, S3 ×4, CAT ×3, FE ×7, OPS ×1 |
| `COMPLETED_IN_CODE_NOT_ACTIVATED` | 19 | S1 ×1, S3 ×9, CAT ×7, FE ×1, OPS ×1 |
| `FIXTURE_ONLY` | 1 | S3-16 |
| `REPLACED_BY_NEW_ARCHITECTURE` | 8 | S2 ×6, S3 ×2 |
| `INTENTIONALLY_DEFERRED` | 3 | FE ×2, OPS ×1 |
| `BLOCKED_EXTERNAL` | 9 | S3-02b, FE-10, OPS-01/02/03/05/07/08/10 |
| `MISSING` | 9 | S2-10, S3-02a, S3-17, CAT-10/11/13, OPS-09/11/12 |
| `OBSOLETE` | 3 | S2-07/08/09 |

Per section: Stage 1 — 18 rows; Stage 2 — 10; Stage 3 — 19; Catalog PR1–PR3 —
13; frontend F4/F5 — 11; production readiness — 13.

**What moved in review round 1, and why.** `BLOCKED_EXTERNAL` gained three rows
and `COMPLETED_AND_CONNECTED` lost one, because three statements that had been
filed as facts about the production database were only facts about a migration
ledger (OPS-01, OPS-08) or about the repository (S3-02b). `MISSING` holds at
nine, but its membership changed: CAT-12 left it — the events are observable
after all — and CAT-13 entered for the typed, operational surface that genuinely
does not exist. Two rows were added by splitting compound claims (S3-02a/b,
CAT-12/13).

---

## 5. The mandatory audit questions, answered

**1. Did Catalog PR3 connect the Government tool, evidence mapper, verifier,
promotion and canonical read through the real Swarm V2 production wiring?**
Yes, all five. `backend/worker/main.py` is the only production construction
site: `ToolRegistry([GovernmentVehicleTool(repo)])` (line 382), the scope
granted on the `ToolContext` (line 409), `RegisteredOperationEvidenceSink`
wrapping `TrustedEvidenceAcquisition(mappers=production_evidence_mappers())`
(lines 423-425) passed to `GenericWorker` as `tool_result_sink` (line 441), the
`Verifier` with a `RepositoryEvidenceResolver` (lines 475-476), and
`CatalogPromotionPipeline(repo, board.lease)` (line 432) invoked at line 542
after the engine returns and before the run is finalized. The canonical read
model is reached through `repository/supabase.py:1034` against
`catalog_canonical_variant_current`. None of these is a test-only registration.

**2. Can the complete Government dataset be queried through bounded, indexed,
snapshot-pinned operations, or is proof limited to fixtures or RAV4-shaped
examples?** The *mechanism* exists for any size: migration `20260916090000`
moves the aggregation into PostgreSQL with fixed ordering, server-owned page
bounds, exact totals (including a COUNT ROW on an empty page) and three
`collate "C"` indexes. The *proof* is bounded: `tests/test_migrations_postgres.py`
exercises it against real PostgreSQL over snapshots of 140, 13, 233 and 1
candidates. Nothing anywhere has run it over ~101 000 rows, and that migration's
version is not recorded in the linked project's migration history (§2.3) —
whether the functions and indexes exist there is unverified. So: bounded and
indexed by construction, **unproven at real scale**, and of unknown
availability in production until OPERATOR-0 reads it.

**3. Has a complete live Government capture ever been proven?** No — and note
carefully what that does and does not assert. **No repository or accepted
production record demonstrates a durable live catalog snapshot of any size.**
No capture can be performed *from this repository*: `DataGovClient` is
constructed only in tests, and `scripts/r5_capture_fixtures.py` imports an
archive captured *outside* this environment (whose egress proxy refuses
`data.gov.il` with HTTP 403 to CONNECT). What this audit cannot say is that no
snapshot exists anywhere — no production relation was queried, so the current
contents of `catalog_source_snapshots` are `BLOCKED_EXTERNAL` (S3-02b) and are
read by OPERATOR-0. **Israeli-market coverage is unproven either way.** Every
catalog test reads one pinned bounded query — `q=RAV4`, `limit=100`, offsets
0/100/200, 233 rows — through a checksum gate with sockets disabled at module
level.

**4. Does a production manufacturer/importer source exist?** No. The
`manufacturer` source family exists in the contract vocabulary
(`backend/catalog/contracts.py:35,51`) and nothing creates a snapshot of it. The
only manufacturer/importer material is the R5 proof fixture and its test-only
tool and mapper under `backend/testing/r5_proof/`, whose own document states
"No production registration, no write path, no migration — Done".

**5. Does a production targeted-Web research source exist?** No. The production
registry holds exactly one tool, and it is the Government reader. There is no
Web tool, no egress allowlist for research, no versioning and no Web evidence
mapper in production; the R5 Web tool and mapper are test-only. Consequence
worth stating plainly: because a `GenericWorker` has no evidence capability of
its own, model-authored research text can never become durable evidence, so the
Government-first policy's instruction to "use targeted research for a gap" is
today an instruction with no capability behind it.

**6. Is `legacy_reference` bounded and usable for reconciliation without
becoming evidence or canonical truth?** The *constraints* are real and
enforced: the family's trust state is pinned `unverified`
(`contracts.py:52`), `is_evidence_family("legacy_reference")` is False
(`contracts.py:204`), a `legacy_reference` snapshot can never carry a verdict,
and promotion refuses it. The 7.3 MB JSON is not imported, read or parsed by
any code. But **usable** is currently false: no code creates a
`legacy_reference` snapshot, `REVIEWED_ALIAS_RULES` is empty, and
`reconcile_catalog` has no production caller. Reconciliation is safe and inert.

**7. Does refresh/diff merely exist in code, or is a production schedule
configured and authorized?** It exists in code only.
`GovernmentCatalogRefresh.sync_if_changed` is constructed only in tests, and a
static test asserts the operation name appears nowhere in `backend/`,
`scripts/` or `.github/workflows/`. No scheduler, Cloud Run Job or cron is
configured, and none is authorized.

**8. Is canonical promotion connected to the intended durable
vehicle-knowledge database and current-value read model, rather than only test
repositories?** Yes in wiring; deployment is unverified. The production
`SupabaseRepository` implements every catalog method against the real guarded
RPCs and views (`promote_catalog_variant_guarded`,
`catalog_run_pending_promotions`, `catalog_canonical_variant_current`), and
`tests/test_catalog_persistence.py` proves each method calls a literal RPC name
and never a direct insert. `MemoryRepository` mirrors the rules for tests. The
versions of the two migrations that create those objects are not recorded in the
linked project's migration history (§2.3); whether the RPCs and views exist
there is **unverified** and is read by OPERATOR-0. So the repository is
connected to the intended database by construction, and whether that database
currently answers is a separate, open question (OPS-01).

**9. Is the approval/write boundary usable by an authorized operator or product
workflow?** No. The `ready_for_review` transition is performed by the pipeline
itself (`backend/catalog/pipeline.py:446`), not by a human. There is no API
route (`backend/main.py` exposes no catalog endpoint), no operator script, no
CLI and no UI that lists candidates awaiting review, approves a promotion, or
reads the canonical catalog. The only reader of
`get_canonical_catalog_variant` in the whole repository is the promotion
module's own post-write verification.

**10. Is the F4 Final Result surface connected for every intended Swarm V2
outcome while V1 remains isolated?** Yes. `parseFinalResult` is total over the
four kinds and the four legal `(status, result_kind)` pairs; `FinalResultPanel`
is mounted only when the *project's trusted* `workflow_key` says `swarm_v2`
(`app/page.tsx:181-207`), and `RunOutputPanel` renders only when it does not,
with a zero-line diff to V1. The one caveat is by design: `not_found` is
rendered but unreachable in production, because no registered tool emits
`TRUSTED_SOURCE_NO_MATCH`. Both surfaces sit behind
`NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI`, which is off by default.

**11. Do rollback, monitoring, alerting and kill-switch procedures cover the
catalog ingestion, evidence and promotion paths?** **No — not at all.** Searched
directly: `MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md`, `STAGED_ACTIVATION.md`
and `ENVIRONMENT_MATRIX.md` contain no catalog, Government, snapshot or
promotion entry; `FINAL_ACCEPTANCE.md` classifies no catalog item; no
`MILO_*CATALOG*` or `MILO_*GOVERNMENT*` variable exists anywhere; the only
`catalog` strings in `scripts/release/` refer to `vehicle_catalog_v1`. The
Government tool is registered and the promotion pipeline runs on **every**
`swarm_v2` run with no independent switch — the only controls are the global
`MILO_ENABLE_PAID_EXECUTION` / `MILO_ENABLE_RUN_CREATION` / `JOB_LAUNCHER`
kill switches, which stop all execution rather than the catalog path.
The two events the path emits (`catalog_variant_promoted`,
`catalog_promotion_refused`) are in neither `backend/runtime.py` `EVENT_TYPES`
nor either set in `frontend/lib/eventVocabulary.ts`, so nothing recognises them
as a type — but they are **not** invisible, and the first revision of this audit
was wrong to say so. `reduceRunEvent` appends every event to `state.events`
unconditionally (`frontend/lib/runReducer.ts:41`) before the projection gate,
and `RunInspector` renders the last 50 with type, agent, phase and message
(`RunInspector.tsx:80-85`). A promotion or refusal is therefore readable as raw
developer telemetry by anyone with the Inspector open and event polling on. What
does not exist is any typed projection, metric, alert, runbook or
operator-facing status — the difference between "a developer can see it in a
raw stream" and "an operator is told about it". CAT-12 and CAT-13 state the two
halves separately.

**12. Are migration application, deployment, environment configuration or IAM
changes still required before use?** Deployment, configuration and IAM: yes.
Migration application: **unknown, and that is the answer** — not "yes". Nine
migration *versions* were absent from the linked project's migration *history*
at a dated run (§2.3), which is a ledger difference. Whether the corresponding
relations, RPCs and views exist in that database is unverified, and it is
entirely possible for objects to exist without a history row — the workflow's
own warning says exactly that. The honest sequence is therefore: read the
schema first (OPERATOR-0), then decide between an apply, a history
reconciliation, a corrective migration, or no action. Separately, no deployment
of any image at or after `18c5f640…` is recorded; and Stage A/B posture, Secret
Manager IAM and the Vercel environment remain
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION` throughout
`docs/production-readiness/`, with nothing in this repository able to confirm
them.

**13. Is Stage D still blocked, and who can approve it?**
Yes. `docs/production-readiness/STAGE_C_ACCEPTANCE.md:884-886`: "Stage D
remains unauthorized and blocked. Stage C passing does not enable, authorize or
schedule any Stage D activity; Stage D requires its own fresh, separate,
explicit operator authorization." The authority is the human deployment
operator recorded as `identities.deploy_operator` in the operator manifest copy
(`FINAL_ACCEPTANCE.md` §Manual items), acting through a fresh explicit
authorization. Merging any documentation or tooling PR — including this one —
authorizes nothing.

**14. Which capabilities are present but disabled by safe defaults?**
`MILO_ENABLE_PAID_EXECUTION`, `MILO_ENABLE_RUN_CREATION`,
`MILO_ENABLE_PROPOSAL_MUTATIONS` / `_PROPOSAL_READS` / `_RUN_CANCELLATION` /
`_EXECUTION_CONTROL`, `GATEWAY_ALLOW_EXECUTION_ROUTES`, `JOB_LAUNCHER=disabled`,
and `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` (which hides both F4 and F5
surfaces). The Supabase auto-apply variable `SUPABASE_MIGRATIONS_AUTO_APPLY` is
unset, so the migration workflow is dry-run only. **The catalog path is the
exception and it is worth stating separately: it is not disabled by any flag.**
Whatever currently keeps it inert is a property of the deployed environment —
an unverified one (OPS-01, S3-02b) — and not a switch anybody can reach for.

**15. Which statements in older roadmap or readiness documents are now stale,
superseded or overstated?** See §10. In summary: the roadmap's Catalog PR3 row
still says "this PR"; `STATUS.md` still presents PR #33 on a non-`main` branch
as the live blocking state with test counts an order of magnitude out of date;
`AUTHORIZATION_AND_RLS.md` still warns that `20260810000100` is staging-only
when §2.3 shows that version recorded in the linked project's migration history
(evidence it ran, though not an ACL observation — OPS-08);
`docs/deployment/swarm-v2-smoke.md` still describes
a "no-tool plan" although a production tool has since been registered;
`FINAL_ACCEPTANCE.md` and `MONITORING_AND_INCIDENTS.md` are silent about four
merged stages; and `frontend/lib/eventVocabulary.ts` describes `EVENT_TYPES` as
"the set the API validates worker-written events against", which is true only
of the `/internal/*` route the shipping worker does not use.

**16. What is the first original stage requirement that is neither complete nor
replaced?** **Roadmap §3.2 — "Snapshot metadata + raw ingest", whose Done
condition is "there is a local source-of-truth snapshot that does not depend on
`data.gov.il` availability during chat".** Stage 1 is complete and connected;
Stage 2 is explicitly and traceably replaced; §3.1's client exists but has no
caller, which is a means to §3.2 rather than an end in itself. §3.2 is the first
row where the roadmap's own acceptance condition is neither demonstrated nor
replaced.

It splits into two facts that must not be merged, because they have different
evidence and different remedies: **S3-02a** — no supported capture entrypoint
exists, which the repository proves and which only code can fix; and
**S3-02b** — whether the target database already holds a durable snapshot is
unknown, which only a read-only inspection can settle. The requirement is
unmet as an *acceptance* matter either way, since no repository evidence and no
accepted production record demonstrates one.

---

## 6. Confirmed production-connected capabilities

Reachable through the intended production path, verified by reading the merged
code and its real caller:

- trusted engine routing from `project.workflow_key`, fail-closed on anything
  unknown, with workflow-scoped checkpoint lookup;
- the whole Swarm V2 engine: strict contracts, deterministic plan firewall,
  allowlisted Commander model, one ModelGateway over the existing BudgetTracker
  and ProviderScheduler, bounded worker pool, replanning, verifier, deterministic
  builder, versioned checkpoint/resume;
- lease-guarded durable writes for every worker mutation, including the Evidence
  Board. `20260823000100`, the migration that enforces it, is the newest version
  the linked project's migration history records — evidence it ran, not an
  observation that its objects are present (§2.3, OPS-01);
- one registered production tool, `catalog.government_vehicle`, read-only, one
  server-owned scope granted only in trusted wiring, eight bounded operations,
  no dump operation, no transport;
- one registered production evidence mapper, and a routing sink that makes the
  tool's other seven operations record nothing rather than fail their task;
- the Government-first source policy, derived from the registered tool names;
- the canonical promotion pipeline, lease-guarded, bounded to 25 candidates per
  run, deriving its work from durable rows so a replacement worker resumes it,
  and letting an infrastructure failure escape rather than be laundered into a
  refusal;
- the F4 typed final-result surface for all four result kinds and the F5
  ownership, error-copy, event-projection, accessibility and bundle-secret
  behaviours, with V1 untouched.

---

## 7. Code present but not activated

"Not activated" here means *this repository does not run it*. Where the
statement would be about the production database instead, it appears in §11 as
an external unknown rather than here.

- Government CKAN client, ingestion, refresh/diff and reconciliation — complete,
  tested, and **constructed only in tests**;
- the catalog schema: nine migration versions are not recorded in the linked
  project's migration history (§2.3), including R3 and R4. Whether their objects
  exist there is unverified and is **not** claimed by this section;
- database-side bounded aggregation and the canonical read model — code and SQL
  exist in the repository; their deployed state is unverified;
- the Swarm V2 controlled production smoke — controller, env contract and
  acceptance gate exist; no acceptance record exists anywhere in the repository;
- the F4/F5 surfaces, and with them the Run Inspector's raw event stream that is
  the only place a catalog promotion or refusal currently appears — built and
  tested, hidden behind an execution-UI flag that is off by default.

---

## 8. Fixture-only or test-only capabilities

- every catalog and Government test reads the committed R5 capture
  (`q=RAV4&limit=100`, offsets 0/100/200, 233 rows) through a manifest checksum
  gate, with socket creation an error at module import;
- the end-to-end plan → tool → evidence → verdict → promotion → canonical read
  chain is proven only against that fixture and the in-memory repository (with
  the pending-promotion derivation additionally proven against real PostgreSQL);
- the R5 manufacturer/importer and targeted-Web sources, their tools and their
  mappers exist only under `backend/testing/r5_proof/` and are registered
  nowhere;
- the Playwright E2E suite proves browser behaviour against a mocked auth,
  storage, transport and model stack with the real FastAPI app inside it.

---

## 9. Missing requirements

The nine rows classified `MISSING`, by severity.

| ID | Missing | Severity | Why it matters |
| --- | --- | --- | --- |
| S3-02a | A supported, guarded live Government capture entrypoint | **Blocking** | nothing in the repository can produce a snapshot, so no operator can close §3.2 however well authorized. With no usable snapshot the tool answers nothing, no evidence exists, no verdict exists and no promotion is possible — such a `swarm_v2` run can only end `no_usable_result` |
| OPS-11 | A controlled full Government capture with counts, checksums and an issue report | **Blocking for expansion** | handoff §9.2 cannot be satisfied |
| OPS-12 | Bootstrap metrics (verified/ambiguous/conflict rates, cost per 1 000 candidates) | **Blocking for expansion** | handoff §9.3 gates cannot be evaluated before a capture exists |
| CAT-11 | Catalog kill switch, monitoring, alerting and rollback procedure | **High** | the tool and the promotion path are unconditional in every `swarm_v2` run; the moment a usable snapshot exists they are live with no independent way to stop them |
| CAT-13 | Typed recognition and an operational signal for a promotion or refusal | **High** | both events are outside every typed vocabulary, so there is no projection, metric, alert or operator-facing status. They ARE readable as raw developer telemetry in the Run Inspector (CAT-12), which is why this row is about signalling rather than visibility |
| CAT-10 | Any operator or product read/approval surface for the canonical catalog | Medium | nobody can inspect, review or use what promotion produces |
| S3-17 | A production targeted-Web research capability | Medium | the Government-first policy directs the Commander to research gaps with no capability that can turn research into evidence |
| S2-10 | Any producer of a bounded `legacy_reference` side | Low | reconciliation has one input and can never run |
| OPS-09 | Catalog/Swarm/frontend rows in `FINAL_ACCEPTANCE.md` | Low | the authoritative classification set silently omits four merged stages |

Two rows are deliberately **not** in this list, and the distinction is the point
of the review round that produced it:

- **S3-02b** (does the target database already hold a durable snapshot?) is
  `BLOCKED_EXTERNAL`, not `MISSING`. Nothing this audit can read answers it, and
  "unknown" must not be filed as "absent".
- **CAT-12** (is a promotion or refusal observable at all?) is
  `COMPLETED_IN_CODE_NOT_ACTIVATED`, not `MISSING`. The events are retained and
  rendered as raw developer telemetry; CAT-13 above carries the real gap.

One near-miss is worth naming too: **S3-12** (reconciliation) is
`COMPLETED_IN_CODE_NOT_ACTIVATED` — `reconcile_catalog` is complete and tested,
but it has no production caller, `REVIEWED_ALIAS_RULES` is empty, and S2-10
means it has only one input.

No critical product or security **defect** was found in merged code during this
audit. The findings above are absences, not broken behaviour. The nearest thing
to a safety defect is CAT-11 — an unconditional capability with no independent
kill switch — and its stop condition is stated in §15.

---

## 10. Replaced, deferred, obsolete — and stale statements

**Replaced (explicit decision + traceable successor):** the whole of Stage 2
(§2.1–§2.6) by Catalog PR1–PR3; §3.3's Government-specific normalized tables by
`catalog_candidate_variants` plus the two query layers; §3.3's quantity-resource
tree by the explicit raw-only contract.

**Obsolete:** §2.7 patch builder, §2.8 GitHub write path and
`MILO_ENABLE_YEDA_WRITES`, §2.9 patch E2E — there is no write-back to the
aggregated JSON under the corrected decision, and no write tool is registered.

**Intentionally deferred:** supervisor active autonomy; automatic
`launch_unknown` reconciliation; Realtime as the primary event channel; a
production producer for `not_found`; a scheduled catalog refresh.

**Stale or overstated statements found (documentation only).**

| Location | Statement | Evidence it is stale |
| --- | --- | --- |
| `docs/roadmap/MILO_UNIFIED_ENGINE_3_STAGE_ROADMAP.md:546` | Catalog PR3 is "**this PR**" | merged as #88, `18c5f640…` |
| `docs/production-readiness/STATUS.md` (whole document) | presents PR #33 against branch `claude/production-readiness-j0hhni` as the live state, and says deployment is blocked until that PR merges; records 507 backend / 60 frontend tests | that flow is long merged into `main`; current counts recorded on the F5 head are 2 597 backend and 501 frontend |
| `docs/production-readiness/AUTHORIZATION_AND_RLS.md` §Service-only RPC ACLs | "both hardening migrations are applied to staging only … assume the anon-EXECUTE gap present in production" | §2.3 shows both versions **recorded in the linked project's migration history**, which is evidence they ran. It is not an ACL observation, so the warning is probably — not provably — stale; OPS-08 keeps it `BLOCKED_EXTERNAL` until OPERATOR-0 inspects the grants |
| `docs/deployment/swarm-v2-smoke.md` §Automatic closure | the smoke "exercises the fixed minimal **no-tool** plan … With no tools there is no evidence" | Catalog PR3 registered a production tool and a Government-first policy; the document itself says a change that gives the smoke real tools "owns updating this contract", and it was not updated |
| `docs/production-readiness/FINAL_ACCEPTANCE.md` | "Consolidated classification of every major item" | no catalog, Government, Swarm V2 or frontend-stage row exists |
| `docs/production-readiness/MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md` | complete signal and rollback coverage | no catalog component appears in either |
| `frontend/lib/eventVocabulary.ts` header comment | `EVENT_TYPES` is "the set the API validates worker-written events against" | `backend/main.py:421` validates only the `/internal/*` route; the canonical worker path uses `SupabaseEventSink` (`backend/runtime.py:100-113`), which validates nothing |

Corrections applied by this audit are limited to the first two rows (see §16);
the remainder are recorded for the owning follow-up, because correcting them
would mean writing new operational content rather than fixing a contradiction.

---

## 11. External, operator and authorization dependencies

Facts that cannot be established from this repository and must be obtained by an
authorized operator:

1. **the entire deployed schema and object state of the protected production
   project** — whether the relations, functions, views, triggers, policies and
   grants of the nine unrecorded migration versions exist; whether the recorded
   ones' objects are intact; whether anything was applied by hand or created
   outside migration history since 2026-09-16T03:18:35Z; whether any catalog
   snapshot, candidate or canonical row exists; and whether the project linked
   by other tooling is the same one the protected `production` environment
   links. **This audit read a migration-history ledger, not a schema.** It is
   read by OPERATOR-0 and by nothing in this repository;
2. the live Cloud Run ingress setting and IAM, the real Vercel server
   environment, and Secret Manager per-secret bindings;
3. whether any image at or after `18c5f640…` has been built, pushed or deployed;
4. `data.gov.il` availability, its real page sizes and its response sizes;
5. the disposition of the dangling reservation `8b05de80-…` on run
   `0d44d491-…`;
6. the `supabase/config.toml` drift the link step reported (site URL, redirect
   URLs, MFA, email confirmations, OTP);
7. authorization for any paid run, any live capture and for Stage D.

---

## 12. Risks and dependency ordering

1. **Read the schema before deciding anything about it — including whether to
   apply a migration.** The greatest risk this audit's first revision itself
   created was treating a migration-history ledger as a schema fact. Applying
   nine versions to a database that already holds some of their objects, or
   reconciling history against objects that are not there, are different
   operations with different failure modes, and only a read-only inspection
   (OPERATOR-0) distinguishes them. If `catalog_*` genuinely is absent, the
   failure mode is at least safe: `catalog_run_pending_promotions` would not
   exist, the pipeline's repository call would raise `AppError`, and by design
   that escapes and leaves the run unfinalized and retryable — stuck runs rather
   than silent loss. That is a reason to verify before the first `swarm_v2`
   production run, not a reason to assume the state.
2. **Do not perform a live capture before the catalog has a kill switch and at
   least one operational signal.** Today the tool and the promotion pipeline are
   unconditional in every `swarm_v2` run. A snapshot activated on Monday is live
   for every run from Monday, with no switch short of stopping all execution.
   The only trace is a raw line in the Run Inspector's event stream (CAT-12) —
   real, and not something an operator can alert on (CAT-13).
3. **Size the capture before authorizing it.** At the default page size a
   ~101 000-row resource is refused outright (`GOV_PAGE_BUDGET_EXCEEDED`); it fits
   only at `page_limit=1000`, and the 8 MiB per-page bound at that size is
   unproven. Getting this wrong wastes an authorization on a guaranteed refusal.
4. **The Swarm V2 smoke cannot be accepted while reservation `8b05de80-…` is
   dangling**, and its acceptance contract predates the registration of a
   production tool.
5. **Scale is unmeasured.** The database-side aggregation is proven over
   hundreds of rows, not over 101 000; index and plan behaviour at real scale is
   unknown until a usable snapshot exists and is queried.
6. **Expansion metrics are unobtainable** (handoff §9.3) until a capture exists,
   so no gate on verified rate, ambiguity rate or cost per 1 000 candidates can
   be evaluated yet.

---

## 13. Minimal follow-up sequence

Deliberately short. No PR is proposed for anything already completed, obsolete
or intentionally deferred.

### 13.1 Code PRs

| # | PR | Scope | Why it must be code |
| --- | --- | --- | --- |
| **CODE-1** | Authorized Government capture entrypoint | one small operator-invoked controller (a `scripts/release/`-style script or an explicit trusted wiring function) that constructs the transport, `DataGovClient` with a justified `page_limit`, and `GovernmentCatalogIngestor`; refuses by default; requires an explicit acknowledgement argument; no schedule, no automatic execution, no change to the capture logic itself | there is no entrypoint at all — an authorized operator currently cannot run a capture from this repository |
| **CODE-2** | Catalog safety and typed visibility | one independent execution flag (default off) gating tool registration and the promotion pipeline; give `catalog_variant_promoted` / `catalog_promotion_refused` typed recognition in `backend/runtime.py` `EVENT_TYPES` and `frontend/lib/eventVocabulary.ts` plus a catalog projection an operator can act on; add catalog rows to `MONITORING_AND_INCIDENTS.md`, `ROLLBACK.md`, `STAGED_ACTIVATION.md`, `ENVIRONMENT_MATRIX.md` and `FINAL_ACCEPTANCE.md`. The events are **already** readable as raw telemetry (CAT-12), so this adds recognition and signalling, not visibility — and must not weaken F5's rule that an unrecognised type stays inert outside the raw stream | a capability with no switch and no operational signal must not go live; a flag and an event allowlist are code |
| **CODE-3** | Canonical catalog read/review surface | a bounded, membership-authorized read of `catalog_canonical_variant_current` and of candidates in `ready_for_review`; read-only first | nobody can see or review what promotion produces |
| CODE-4 | *(conditional)* legacy-reference producer, reviewed alias rules, a reconciliation caller | only if reconciliation is wanted | — |
| CODE-5 | *(conditional, handoff §9.1)* a targeted Web research tool with egress allowlist, versioning and its own evidence mapper | only after a Government bootstrap proves the gap shape | — |

**CODE-1 and CODE-2 are the only two that block the recommendation in §14.**
CODE-3 blocks nobody but makes the first capture reviewable, and should be
sequenced immediately after them.

### 13.2 Operator / configuration work

| # | Action |
| --- | --- |
| **OPERATOR-0** | **Read-only schema inspection of the exact protected production project — required before any mutation is even chosen.** Nothing may be applied, reconciled or corrected before this runs, and this audit selects none of those outcomes. It performs, read-only: (a) re-read `supabase_migrations.schema_migrations` for the current history; (b) `to_regclass` on every required relation (`catalog_source_snapshots`, `catalog_raw_records`, `catalog_candidate_variants`, `catalog_candidate_evidence_links`, `catalog_models`, `catalog_model_variants`, `catalog_canonical_field_provenance`, and the R3/R4 evidence relations); (c) inspect required functions **and their signatures** (the six guarded catalog RPCs, `promote_catalog_variant_guarded`, `catalog_run_pending_promotions`, the seven bounded read functions, `assert_worker_lease`); (d) inspect required views (`catalog_canonical_field_current`, `catalog_canonical_variant_current`) and triggers (`catalog_check_field_provenance`, `catalog_require_field_provenance`, `catalog_require_model_variant`, the append-only and immutability triggers); (e) inspect RLS enablement, policies and grants, including whether `anon` holds EXECUTE anywhere (OPS-08); (f) **only if the relations exist**, count rows in the snapshot, candidate and canonical relations; (g) compare the observed objects against the migration history and record every disagreement. Output a written report. |
| **OPERATOR-1** | **Conditional on OPERATOR-0's report, and chosen by it — not by this audit.** The possibilities are a normal ordered apply, a one-time migration-history reconciliation, a corrective forward migration for a partial or divergent state, or **no action at all**. Whichever is chosen: manual, in order where applicable, after a verified encrypted backup whose passphrase is confirmed available, per `MIGRATIONS.md` §Manual database sequence; then re-run the inspection and the RLS/function validations. **Do not treat "apply the nine" as the default** — it is one of four possible answers and the evidence for choosing it does not exist yet. |
| OPERATOR-2 | Build, push and deploy immutable images at a pinned SHA; complete Stage A (`smoke-test-read-only.sh`, `smoke-test-execution-disabled.sh`) and record the evidence |
| **OPERATOR-3** | One controlled, authorized live capture of the WLTP resource: record counts, checksums, the normalization issue report; activate the snapshot only on completeness |
| OPERATOR-4 | Verify index and plan behaviour of the bounded aggregations against the real snapshot |
| OPERATOR-5 | Settle or void the dangling reservation `8b05de80-fa01-4614-bec1-37f72ca63acc` on run `0d44d491-bc40-404e-9642-a5b8f77f3441` under its own authorization |
| OPERATOR-6 | Bind the monitoring signals (including the catalog signals CODE-2 adds) to a real alerting system |
| OPERATOR-7 | Resolve the reported `supabase/config.toml` drift deliberately, in whichever direction is correct |

### 13.3 Production authorization / activation

| # | Authorization |
| --- | --- |
| **AUTH-1** | One bounded live Government capture (OPERATOR-3) — outbound read-only, no model spend |
| AUTH-2 | One Swarm V2 controlled smoke run, and any further paid run; Stage C's one-run authorization is consumed |
| AUTH-3 | Stage D — a fresh, separate, explicit operator authorization; nothing else can grant it |

**Is a code PR genuinely required before operator verification?** Partly, and
the two halves separate cleanly. **Verification comes first for the database:**
OPERATOR-0 needs no code, depends on nothing here, and must precede any decision
about migrations — including the decision not to act. **Code is unavoidable for
the capture:** no amount of operator verification can produce a Government
snapshot, because the repository contains no entrypoint from which a capture can
be run (S3-02a). So OPERATOR-0 and CODE-1/CODE-2 proceed in parallel, and
CODE-2 lands with or before CODE-1 rather than after the capability is live.

---

## 14. Recommendation — the first unfinished, unreplaced original requirement

> **Roadmap §3.2 — "Snapshot metadata + raw ingest".**
> Done condition: *"there is a source-of-truth local snapshot that does not
> depend on `data.gov.il` availability during chat."*
>
> **Status: unmet as an acceptance matter, and it splits into two facts with
> different evidence and different remedies.**
>
> * **S3-02a — `MISSING`, proven from the repository.** No supported, guarded
>   live capture entrypoint exists: `DataGovClient` and
>   `GovernmentCatalogIngestor` are constructed only in tests, and
>   `backend/catalog/` has no `__main__` and no CLI. An operator with full
>   credentials and explicit authorization has no supported way to produce a
>   snapshot from this repository. Only code closes this.
> * **S3-02b — `BLOCKED_EXTERNAL`, not proven either way.** Whether the target
>   database already holds a durable snapshot is unknown. **No repository
>   evidence and no accepted production record demonstrates a durable live
>   catalog snapshot** — and equally, nothing here licenses the claim that none
>   exists. Only OPERATOR-0 settles it.
>
> Every Stage 3 requirement after §3.2 — the manufacturer→model→year→variant
> tree (§3.3), the tool's usefulness (§3.4), reconciliation (§3.5), the
> end-to-end flow (§3.8) and refresh (§3.9) — depends on a usable snapshot, and
> so does every conditional follow-up in handoff §9.2–§9.4.

Everything before it in the original roadmap is genuinely finished: Stage 1 is
complete and connected, and Stage 2 is replaced by an explicit architectural
decision with a named, merged successor.

Minimal path to close it: **OPERATOR-0 (read the schema) in parallel with
CODE-1 + CODE-2 → whatever OPERATOR-1 turns out to be, if anything →
AUTH-1/OPERATOR-3**, with CODE-3 immediately after so the result is reviewable.
If OPERATOR-0 finds a usable snapshot already present, the capture step changes
shape and CODE-1 becomes preparation for the next refresh rather than the
unblocking step — which is precisely why the inspection comes first.

---

## 15. Stop conditions before that recommendation may be implemented

Implementation of CODE-1, and above all the capture it enables, must not begin
until all of these hold. Any one of them failing is a stop.

1. **Explicit user authorization for this stage.** This audit authorizes
   nothing. A Gap Audit PR, merged or not, is not an implementation mandate.
2. **CODE-2 lands with or before CODE-1.** A live capture without an
   independent catalog kill switch and at least one signal an operator can act
   on is not acceptable, because the tool and the promotion path are
   unconditional in every `swarm_v2` run. A raw line in the Inspector's event
   stream (CAT-12) is not that signal.
3. **OPERATOR-0 has run and its report has been read before any database
   mutation is chosen.** §2.3 is a dated migration-history ledger, not a schema
   observation and not a licence. No apply, reconciliation or corrective
   migration may be selected from it; whichever is selected is performed
   manually, after a verified encrypted backup whose passphrase is confirmed
   available.
4. **The capture is separately authorized, bounded and read-only**, with the
   resource, the query, the page size and the record ceiling agreed in advance,
   and with `MILO_ENABLE_PAID_EXECUTION` off — a capture requires no model
   spend and must not be bundled with one.
5. **The page size question is settled first** (§12.3). At the default page size
   the capture is refused.
6. **No schedule is created.** Refresh stays unscheduled until a controlled
   bootstrap has succeeded and handoff §9.4 is separately authorized.
7. **Stage D stays blocked** and is not approached by this work; the dangling
   reservation `8b05de80-…` is settled under its own authorization before any
   Swarm V2 smoke acceptance.
8. **Nothing in `legacy/milo-streamlit-v1` or `MILO-main-original/` is
   touched**, and `test_websearch.py` is never run.

---

## 16. Execution-safety confirmation

This audit **performed no implementation and no production mutation** — in
either revision.

**Revision 2 (review round 1).** An independent review found three
documentation blockers in revision 1, all of them overclaims, and all three are
corrected above:

1. **Migration history was promoted into a schema fact.** Revision 1 said the
   catalog schema "is not in the production database" and that the relations
   "do not exist", and it made "apply the nine pending migrations" the
   unconditional next action. The workflow log proves a migration-**history**
   difference at a dated moment and nothing more — as the workflow's own failure
   message says. §2.3 now states only what was proven, the remote object state
   is `BLOCKED_EXTERNAL` (OPS-01), and the read-only OPERATOR-0 inspection was
   added ahead of any mutation, with OPERATOR-1 reduced to one of four possible
   outcomes that only that report may choose between.
2. **Catalog events were called invisible; they are not.** Revision 1 said a
   promotion or refusal is "visible nowhere". `reduceRunEvent` appends every
   event to `state.events` unconditionally before the projection gate
   (`frontend/lib/runReducer.ts:41`), `RunInspector` renders the last 50 with
   type and message (`RunInspector.tsx:80-85`), and `eventProjection.test.ts`
   asserts that retention deliberately. CAT-12 now records the raw telemetry
   that exists, and the real gap — typed recognition and an operational signal —
   moved to a new CAT-13. F5's rule that an unrecognised type stays inert
   outside the raw stream is unchanged and must stay that way.
3. **Snapshot state and capture entrypoint were conflated.** Revision 1 said "no
   snapshot has ever been created", which is a claim about a database nobody
   read. S3-02 is split into S3-02a (`MISSING` — no supported capture
   entrypoint, proven from the repository) and S3-02b (`BLOCKED_EXTERNAL` — the
   target database's snapshot state is unknown). The recommendation stands and
   its evidence is now honest.

Totals were recomputed by parsing the matrix, not by hand: 82 rows became 84.

**Both revisions.**

No code, test, migration, schema, workflow, dependency, configuration value,
deployment file or activation flag was changed. No migration was applied. No
deployment, IAM change, secret creation or rotation occurred. No live source
capture was performed and no request was sent to `data.gov.il`. No paid model or
provider call was made. No run was created, launched or cancelled.
`test_websearch.py` was not run. `legacy/` and `MILO-main-original/` were read
only.

Everything the audit did was read-only: `git status`, `git fetch origin
--prune`, `git rev-parse`, `git log`, `git merge-base --is-ancestor`, file
reads, `rg` searches, and read-only GitHub API reads of pull requests, check
runs, workflow runs and one workflow job log.

The repository changes in the pull request carrying this audit are exactly:

1. this document;
2. one navigation row in `docs/production-readiness/README.md` so the audit is
   discoverable;
3. two evidence-backed corrections of directly contradictory statements — the
   roadmap's "this PR" cell for Catalog PR3, and a dated superseding banner on
   `docs/production-readiness/STATUS.md`, whose current text describes a branch
   and pull request state that no longer exists.

All three are documentation-only.
