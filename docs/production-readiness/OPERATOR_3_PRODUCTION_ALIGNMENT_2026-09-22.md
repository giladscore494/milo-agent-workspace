> **HISTORICAL — superseded by [`SCOPED_BATCH_PRODUCTION_RUNBOOK.md`](SCOPED_BATCH_PRODUCTION_RUNBOOK.md).** A dated record kept as evidence; do not follow it as current procedure.

# OPERATOR-3 — Production contract alignment to Console 6 (2026-09-22)

Status: **COMPLETE.** Production migration history and the repository
migration set are aligned at **38/38**. The four-migration tail recorded as
BLOCKING in `OPERATOR_2_CONTROL_PLANE_READ_ONLY_REVIEW_2026-09-21.md` has been
applied under the SHA-bound authorization gate, against a verified encrypted
backup, and every runtime dependency the repository derives from itself has
been re-proven present in Production by direct catalog reads.

Execution remains disabled. No deployment, no Government capture, no
promotion, no MILO/Swarm run and no paid provider call occurred as part of
this work. This document contains no credential and no secret.

---

## 1. Source

| Item | Value |
| --- | --- |
| Reviewed source SHA | `7676b1eee941c1a0167b4a91d4fb5e598b0aef2b` |
| Commit | Merge of PR #108 — *Give a run one immutable identity, fence every write to it, and make the release gate verify what it authorizes* |
| Local migration files | 38 |
| CI on that SHA | `ci` run 35669413083 — `offline-checks`, `postgres-checks`, `e2e` all **success** |
| Push-triggered preflight on that SHA | `Deploy Supabase Migrations` run 35669413097 (dry-run only) — proposed exactly the four |

No migration SQL was changed at any point during OPERATOR-3.

## 2. Production state before apply (fresh read, not assumed)

Read through the read-only Production connector immediately before dispatch.
Staging was never used; its migration history (22 rows under different
version numbers) confirmed it is a different database.

| Metric | Value |
| --- | --- |
| Applied migrations | 34 |
| Latest applied version | `20260916120000` |
| Pending local migrations | 4 — `20260920000100`, `20260920000200`, `20260921000100`, `20260921000200` |
| `runs.run_identity` | absent |
| `run_execution_usage` | absent |
| `create_message_and_run_v3`, `finalize_run_guarded`, `record_run_usage_guarded`, `merge_execution_usage`, `claim_current_verdict_states`, `append_usage_ledger_guarded`, `create_tool_access_request_guarded`, `create_tool_grant_guarded` | absent (verified by `pg_proc`) |
| `public.runs` | 8 rows, **all terminal**, 0 non-terminal |
| Cloud Run `milo-agent-worker` executions | 8, all `Completed`, 0 running |
| API `MILO_ENABLE_RUN_CREATION` / `JOB_LAUNCHER` | `false` / `disabled` |
| API + Worker `MILO_ENABLE_PAID_EXECUTION` | `false` |
| Worker `KIMI_API_KEY` binding | absent from the job template |

Because run creation is disabled at the API, the launcher is disabled and no
Worker execution was active, **no run could have executed against a
partially migrated schema** during the apply window. The apply itself is a
single `supabase db push` of four transactional migration files.

## 3. Encrypted Production backup

| Item | Value |
| --- | --- |
| Workflow | `Backup Supabase Production` |
| Backup run ID | **`35670184316`** |
| Run head SHA | `7676b1eee941c1a0167b4a91d4fb5e598b0aef2b` |
| Conclusion | success |
| Artifact ID | **`10670529249`** (`supabase-production-backup-35670184316`, 405,241 bytes, expires 2026-09-29) |

The workflow decrypted its own bundle with the stored passphrase and
verified the inner checksums before upload (its standing behaviour, see
`SUPABASE_BACKUP.md`). The passphrase was never printed or read by this
session. Byte-level independent verification of the artifact was not
performed from this session (no artifact egress); the manifest and artifact
digest are recorded in the workflow run for an operator to compare.

## 4. SHA-bound dry-run

| Item | Value |
| --- | --- |
| Dry-run run ID | **`35670378419`** |
| Event / inputs | `workflow_dispatch`, `mode=dry-run`, `expected_sha=7676b1e…` |
| Conclusion | success — gate step 3 passed before any credential-bearing step; `Would push these migrations:` listed exactly the four, in order; apply steps **skipped** |

## 5. Apply

| Item | Value |
| --- | --- |
| Apply run ID | **`35670454619`** |
| Event / inputs | `workflow_dispatch`, `mode=apply`, `expected_sha=7676b1e…`, `confirmation=APPLY_PRODUCTION_MIGRATIONS` |
| Conclusion | success (19 s) |

## 6. Production state after apply — proven by direct reads

| Check | Result |
| --- | --- |
| Migration history | 38 rows; head **`20260921000200`** — identical to the repository set |
| Derived required-RPC inventory (`scripts/release/release_inventory.py`, 45 RPCs / 20 migrations) | **45/45 present; 45/45 advertise every required argument name** (checked with `pg_proc.proargnames` containment) |
| Browser-role EXECUTE on any of the 45 | **0** for `anon`, **0** for `authenticated` |
| `service_role` EXECUTE | 43/45; the two without are `reserve_daily_user_budget` / `reserve_daily_project_budget`, the deprecated daily RPCs whose EXECUTE was deliberately revoked (migration 014 row in `MIGRATIONS.md`); no runtime path calls them |
| `runs.run_identity` | present; `runs_run_identity_shape_check` **validated**; triggers `runs_forbid_identity_rewrite` and `runs_require_identity_on_insert` enabled |
| Atomic V3 creation | `create_message_and_run_v3(p_run_id, p_run_identity, p_conversation_id, p_content, p_metadata, p_requested_by, p_idempotency_key, p_request_fingerprint, p_max_user_active, p_max_project_active)` present; `create_message_and_run`, `create_message_and_run_v2`, `bind_run_identity` **absent** — one run-creation authority |
| Usage ledger | `run_execution_usage` present with `run_execution_usage_monotonic` trigger; `record_run_usage_guarded`, `merge_execution_usage`, `append_usage_ledger_guarded` present; `run_usage_ledger` 465 rows (unchanged historical spend) |
| Canonical finalizer | `finalize_run_guarded(... p_expected_status ..., p_event_type, p_event_message, p_event_payload)` present |
| Current-verdict authority | `claim_current_verdict_id/_state/_states`, `claim_verdict_is_current_support` present; BEFORE INSERT triggers `catalog_candidate_evidence_links_current_verdict` and `catalog_canonical_field_provenance_current_verdict` present |
| Worker fencing | `assert_worker_lease`, lease-guarded `transition_run_worker_guarded`, `heartbeat_run_guarded`, `create_tool_access_request_guarded`, `create_tool_grant_guarded` present; `claim_run_lease(p_run_id, p_worker_id, p_lease_seconds)` is the identity-aware definition from `20260921000200` |
| RLS / grants | 37/37 public tables RLS-enabled; `anon` holds no table privilege; `authenticated` holds no write privilege on any table via grants (its two writable surfaces are policy-guarded); 0 SECURITY DEFINER functions with a mutable `search_path`; `service_role` holds SELECT/INSERT on `run_execution_usage` and `claim_verdicts` (UPDATE/DELETE stay revoked on append-only relations) |
| Catalog promotion | `catalog_source_snapshots` / `catalog_raw_records` / `catalog_candidate_variants` / `catalog_models` / `catalog_model_variants` / `catalog_canonical_field_provenance` = 0 / 0 / 0 / 0 / 0 / 0 — never run |
| Legacy runs | 8 rows, all terminal, **all `run_identity IS NULL`**: historical, readable, never executable, never retrofitted |
| `chat_architect_v1` | 0 projects (`swarm_v2`=1, `vehicle_catalog_v1`=3), 0 proposals (table empty), 0 runs mention it — still non-Production-reachable |
| Government capture `555101dc…` | `cancelled` / `launch_state=none` (retired) |

## 7. What this alignment does NOT change

* The Cloud Run release still serving Production is **`84cd8696…`** on both
  surfaces. That release predates Console 2–6: its API calls
  `create_message_and_run_v2`, which no longer exists, so even if
  `MILO_ENABLE_RUN_CREATION` were flipped on that release **no run could be
  created** (fail-closed, by construction). Its Worker carries the
  pre-policy provider drift (`MILO_PROVIDER_RPM_LIMIT=350`,
  `MILO_SWARM_MAX_ACTIVE_WORKERS=8`) that the current runtime refuses to
  start under, and neither surface states `MILO_RELEASE_SHA`. A release
  built from current `main` and pinned by digest is therefore a hard
  prerequisite for any future paid run, and it is a separate reviewed
  operator action, not part of this alignment.
* Stage D's live baseline is re-pinned in the same PR as this record (see
  `STAGE_D_AUTHORIZATION.md` §9.1 for attempt 1's outcome).
