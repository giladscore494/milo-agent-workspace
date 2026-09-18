# OPERATOR-1 — Production migration alignment (2026-09-18)

Status: **COMPLETE**. Production migration history and the repository
migration set are aligned at 34/34. The nine-migration pending tail recorded
in `OPERATOR_0_PRODUCTION_SCHEMA_INSPECTION_2026-09-17.md` has been applied
under an SHA-bound authorization gate, against a verified encrypted backup.

Execution remains disabled. No deployment, no Government capture, no
promotion, no MILO/Swarm run and no paid provider call occurred as part of
this work.

This document is a record. It contains no credential, database password,
access token, service-role key, backup passphrase or concrete Production
project reference, and none is required to read it.

---

## 1. Source

| Item | Value |
| --- | --- |
| Reviewed source SHA | `b3d3a4b3d57d6823ab9ae418b122e20305658024` |
| Commit | Merge of PR #98 — *OPERATOR-1 — bind migration apply to reviewed SHA and clarify backup verification* |
| Local migration files | 34 |
| `supabase/migrations` tree hash | `d3f4b5e944be8d0068f42dd0a80f235fd9d8b578` |

No migration SQL changed at any point during OPERATOR-1. The
`supabase/migrations` tree hash is byte-identical across the pre-PR-98 base
(`2fb68d4`), the PR #98 head (`6e64149`) and the applied SHA (`b3d3a4b`).
PR #98 changed five files — one workflow, two documents, two test files — and
nothing under `supabase/migrations/`.

The gate that made this apply possible is the one PR #98 added: a manual
`workflow_dispatch` must supply the full 40-character `expected_sha`, the run
refuses to continue unless `GITHUB_SHA` equals it, and no production
credential is declared at job scope — so an unauthorized dispatch is refused
before any credential exists in a step environment.

## 2. Production migration state before apply

Observed through the read-only Production connector immediately before
dispatch. Staging was never used at any point.

| Metric | Value |
| --- | --- |
| Applied migrations | 25 |
| Latest applied version | `20260823000100` |
| Versions above `20260823000100` | 0 |
| Pending local migrations | 9 |
| `public.source_evidence_fragments` | absent |
| R3/R4 and catalog objects | absent |

## 3. Encrypted Production backup

| Item | Value |
| --- | --- |
| Workflow | `Backup Supabase Production` |
| Backup run ID | **`35287149441`** |
| Run head SHA | `b3d3a4b3d57d6823ab9ae418b122e20305658024` |
| Conclusion | success |
| Artifact ID | **`10524533221`** |
| Artifact name | `supabase-production-backup-35287149441` |
| Artifact retention | 7 days (expires 2026-09-24) |

The workflow decrypted its own encrypted bundle using the approved stored
passphrase and verified the inner checksums before upload:

```
schema.sql: OK
data.sql: OK
roles.sql: OK
migration-history.txt: OK
Encrypted backup created and decryptability/checksums verified.
```

It then asserted the backup directory held exactly two files, and
`upload-artifact` reported `With the provided path, there will be 2 files
uploaded`. The passphrase was never printed, logged or committed, and no step
of this verification required its value.

### Independent artifact verification

**Backup artifact independently verified after Phase A; encrypted hash and
byte size match manifest.**

The byte-level verification in `SUPABASE_BACKUP.md` was carried out by an
independent reviewer who downloaded artifact `10524533221`. It was **not**
performed by the agent session that dispatched the backup — that session's
environment denied egress to the Actions artifact blob host, which is why the
Phase A report recorded the check as outstanding. That is no longer the final
state.

| Check | Expected | Observed | Result |
| --- | --- | --- | --- |
| Artifact contents | one `manifest.json` + one `*.tar.gz.enc` | exactly that | match |
| `manifest.source_sha` | `b3d3a4b3d57d6823ab9ae418b122e20305658024` | same | match |
| `manifest.workflow_run_id` | `35287149441` | same | match |
| SHA-256 of extracted `.enc` vs `manifest.encrypted_sha256` | `8afba2981f0844405d5e7dd8233662bde49b1f022acda8f10ea4b5efe02b89ed` | `8afba2981f0844405d5e7dd8233662bde49b1f022acda8f10ea4b5efe02b89ed` | **MATCH** |
| `.enc` size vs `manifest.encrypted_size_bytes` | `347600` | `347600` | **MATCH** |

Per `SUPABASE_BACKUP.md`, the GitHub artifact ZIP digest
(`592689cc1e33787dbc882612f1ee2bccae081999174e2a64bad0ab05e03a8166`) covers
the transport container GitHub builds around the upload, not the encrypted
bundle inside it. It is recorded as transport evidence only and was correctly
**not** compared against `encrypted_sha256`.

## 4. SHA-bound dry-run

| Item | Value |
| --- | --- |
| Workflow | `Deploy Supabase Migrations` |
| Dry-run run ID | **`35287356011`** |
| Event | `workflow_dispatch` |
| Inputs | `mode=dry-run`, `expected_sha=b3d3a4b…`, no apply confirmation |
| Head SHA | `b3d3a4b3d57d6823ab9ae418b122e20305658024` |
| Conclusion | success |

The SHA authorization gate ran at step 3 — before the Supabase CLI was
installed and before any credential-bearing step (the first was step 7). The
dry-run proposed exactly the expected nine, in order. `Apply production
migrations` and `Display remote migration history after apply` were both
**skipped**, and the run ended `Manual dry-run completed. No production
migrations were applied.`

Production was re-read afterwards and was unchanged: 25 applied, latest
`20260823000100`, no catalog or R3/R4 objects.

## 5. Apply

| Item | Value |
| --- | --- |
| Workflow | `Deploy Supabase Migrations` |
| Apply run ID | **`35349142551`** |
| Event | `workflow_dispatch` |
| Inputs | `mode=apply`, `expected_sha=b3d3a4b…`, `confirmation=APPLY_PRODUCTION_MIGRATIONS` |
| Head SHA | `b3d3a4b3d57d6823ab9ae418b122e20305658024` |
| Conclusion | success |

Step-level evidence from GitHub Actions:

| # | Step | Result |
| --- | --- | --- |
| 3 | Validate manual authorization against the reviewed SHA | success |
| 5 | Validate repository migrations | success |
| 8 | Link production Supabase project | success |
| 9 | Display remote migration history | success |
| 10 | Run mandatory production dry-run preflight | success — proposed exactly the nine |
| 13 | **Apply production migrations** | **success** |
| 14 | Display remote migration history after apply | success |

`main` was re-fetched immediately before dispatch and confirmed still at
`b3d3a4b3d57d6823ab9ae418b122e20305658024`; Production was re-read and
confirmed still at 25 applied, so the backup remained a valid recovery point
for the state being migrated.

### The nine migrations applied, in order

1. `20260828000100_canonical_scope_conflict_identity.sql`
2. `20260828000200_source_evidence_fragments.sql`
3. `20260902000100_r3_versioned_focused_evidence.sql`
4. `20260907000100_r4_deterministic_verification.sql`
5. `20260914200000_catalog_evidence_foundation.sql`
6. `20260915120000_catalog_integrity_corrections.sql`
7. `20260915180000_catalog_raw_record_source_locator.sql`
8. `20260916090000_catalog_bounded_candidate_queries.sql`
9. `20260916120000_catalog_field_level_promotion.sql`

The run emitted `NOTICE … does not exist, skipping` lines throughout. These
are the migrations' own `DROP … IF EXISTS` idempotency guards firing on first
application — expected, and not errors. No migration was repaired,
reconciled or marked; no history row was edited; there was no SQL failure, no
schema conflict and no link mismatch.

## 6. Production migration state after apply

| Metric | Value |
| --- | --- |
| Applied migrations | **34** |
| Latest applied version | **`20260916120000`** |
| Missing | 0 |
| Unexpected | 0 |
| Remote tables with no matching local migration | 0 |

The workflow's own post-apply `supabase migration list` shows all 34 rows
with Local and Remote matched, with no remote-only row and no gap.

## 7. Migration-state classifier

`scripts/release/check-migration-state.sh` reaches its remote branch through
`psql` and an operator-supplied read-only connection string, which was
deliberately not requested for this task. The classification itself lives in
the pure helper `scripts/release/migration_state.py`, so the authoritative
comparison was reproduced exactly: the local side was generated offline with
`migration_state.py local`, the 27 marker probes were taken from
`migration_state.py markers`, every observation was gathered read-only
through the Production connector in the same TAB format the shell entrypoint
emits, and `migration_state.py classify` was run over it.

```
state                  fully-migrated
blocked                false
applied_count          34
local_total            34
missing                []
unexpected             []
marker_disagreements   []
summary                all 34 local migrations are recorded in remote migration history
```

All 27 declared marker objects were probed and all 27 were present, including
the eight markers belonging to the newly applied tail. There is no
disagreement between the migration history and the schema objects it should
have created.

## 8. Installed objects

`catalog` is **not** a schema. These objects live in `public` under
`catalog_*` names, so the existence of a schema named `catalog` is not a
success or failure signal for this work and must not be used as one — it
reads absent both before and after a fully successful apply. Tables and views
are distinguished below.

### R3 / R4

| Object | Kind | Status |
| --- | --- | --- |
| `claims.canonical_scope_hash` | column (`text`) | present |
| `claims.scope_normalization_version` | column (`integer`) | present |
| `claims.evidence_locator` | column (`text`) | present |
| `claims.identity_scope` | column (`jsonb`) | present |
| `sources.source_version_kind` | column (`text`) | present |
| `sources.source_version_id` | column (`text`) | present |
| `source_evidence_fragments` | TABLE | present |
| `claim_verdicts` | TABLE | present |
| `claim_verdict_supports` | TABLE | present |
| `conflict_resolutions` | TABLE | present |

### Catalog

| Object | Kind | Status |
| --- | --- | --- |
| `catalog_source_snapshots` | TABLE | present |
| `catalog_raw_records` | TABLE | present |
| `catalog_candidate_variants` | TABLE | present |
| `catalog_candidate_evidence_links` | TABLE | present |
| `catalog_models` | TABLE | present |
| `catalog_model_variants` | TABLE | present |
| `catalog_canonical_field_provenance` | TABLE | present |
| `catalog_canonical_field_current` | **VIEW** | present |
| `catalog_canonical_variant_current` | **VIEW** | present |

`catalog_raw_records.source_locator` (`jsonb`, `NOT NULL`) is present, as is
`sources.evidence_key` from the prior baseline.

## 9. Security verification

Read-only inspection only. Nothing was remediated, and no change was made.

### Aggregate posture

| Check | Result |
| --- | --- |
| Public tables | 36 |
| Public tables with RLS enabled | **36** |
| Public tables with RLS disabled | **0** |
| Materialized views | 0 |
| Public objects granted to `anon`, `authenticated` or `PUBLIC` | **0** |
| Functions executable by `anon` that are not trigger functions | **0** |
| Remote tables with no matching local migration | 0 |

Every one of the eleven tables created by this tail has RLS enabled and
carries **no table grant to any role at all**. They are reachable only by
`service_role`, which bypasses RLS — the service-only posture established by
`20260810000200_enable_rls_on_service_only_tables.sql`.

### Views

Both new views are `security_invoker=true`, so the caller's own privileges
and the base tables' RLS apply rather than the view owner's. Neither grants
`SELECT` to `anon` or `authenticated`.

### Service-only RPCs

Every callable function introduced or touched by this tail is executable by
`service_role` only, with `anon` and `authenticated` both denied. This was
verified per-function with `has_function_privilege`, not inferred from the
migration text. The verified set includes the guarded write path
(`record_evidence_fragment_guarded`, `record_claim_verdict_guarded`,
`record_conflict_resolution_guarded`, `create_conflict_guarded`,
`link_catalog_candidate_evidence_guarded`,
`patch_run_blackboard_evidence_guarded`), the guarded promotion path
(`promote_catalog_variant_guarded`, `catalog_run_pending_promotions`,
`catalog_promotable_field`) and the bounded query path
(`catalog_snapshot_candidate_diff`, `catalog_candidate_variant_page`,
`catalog_candidate_models`, `catalog_candidate_manufacturers`,
`catalog_candidate_model_years`, `catalog_page_limit`,
`catalog_raw_record_by_upstream_id`, `catalog_readable_snapshot`). All carry
`search_path=pg_catalog`.

### Integrity triggers

All fourteen append-only and identity-integrity triggers on the new tables
exist and are **enabled**:

| Table | Trigger |
| --- | --- |
| `source_evidence_fragments` | `source_evidence_fragments_append_only` |
| `claim_verdicts` | `claim_verdicts_append_only` |
| `claim_verdict_supports` | `claim_verdict_supports_append_only` |
| `conflict_resolutions` | `conflict_resolutions_append_only` |
| `catalog_source_snapshots` | `catalog_source_snapshots_append_only` |
| `catalog_raw_records` | `catalog_raw_records_append_only` |
| `catalog_candidate_evidence_links` | `catalog_candidate_evidence_links_append_only` |
| `catalog_candidate_variants` | `catalog_candidate_variants_identity_immutable` |
| `catalog_models` | `catalog_models_identity_immutable`, `catalog_models_require_variant` |
| `catalog_model_variants` | `catalog_model_variants_identity_immutable`, `catalog_model_variants_require_field_provenance` |
| `catalog_canonical_field_provenance` | `catalog_canonical_field_provenance_append_only`, `catalog_canonical_field_provenance_checked` |

### Pre-existing observations — not OPERATOR-1 regressions

Two items are recorded separately because they predate this work and were not
introduced or worsened by it.

1. **Trigger functions retain PostgreSQL's default `PUBLIC` EXECUTE grant.**
   Thirteen functions in `public` are executable by `anon`; every one of them
   returns `trigger` or `event_trigger` and none is a callable RPC —
   PostgreSQL refuses a direct call to a trigger function. Six predate this
   tail (`forbid_usage_ledger_mutation` from `013_usage_ledger.sql`,
   `set_updated_at`, `update_conversation_timestamp`, `rls_auto_enable` and
   others), which establishes the pattern; the seven added here follow it.
   The count of `anon`-executable **non-trigger** functions is zero, so no
   RPC surface is exposed. Revoking these would be a repository-wide
   tightening, not an OPERATOR-1 fix.

2. **`stuck_runs` has no `security_invoker`.** This view comes from
   `006_deployment_hardening.sql`, is not referenced by any of the nine, and
   grants `SELECT` to neither `anon` nor `authenticated`, so it is not
   reachable by an untrusted role regardless.

No regression attributable to OPERATOR-1 was found.

### Advisors

The Production connector is read-only and exposes no advisor endpoint, so the
hosted security/performance advisors could not be read for this record. The
equivalent ground truth was obtained by direct catalog inspection above: RLS
coverage, grant surface, function executability, view security semantics and
trigger state were each queried rather than inferred. Running the hosted
advisors remains available to an operator with the appropriate access and is
recommended before Stage A.

## 10. Catalog is empty, as expected

No Government capture has been performed, so the new structures are expected
to hold no rows. Actual counts were read; nothing was inserted.

| Object | Rows |
| --- | --- |
| `catalog_source_snapshots` | 0 |
| `catalog_raw_records` | 0 |
| `catalog_candidate_variants` | 0 |
| `catalog_candidate_evidence_links` | 0 |
| `catalog_models` | 0 |
| `catalog_model_variants` | 0 |
| `catalog_canonical_field_provenance` | 0 |
| `catalog_canonical_field_current` (view) | 0 |
| `catalog_canonical_variant_current` (view) | 0 |
| `source_evidence_fragments` | 0 |
| `claim_verdicts` | 0 |
| `claim_verdict_supports` | 0 |
| `conflict_resolutions` | 0 |

An empty catalog at this stage is correct and is **not** a failure. For the
record:

- no Government capture occurred;
- no promotion occurred;
- no MILO/Swarm run occurred;
- no paid or provider call occurred.

## 11. Execution remains disabled

Verified statically; no environment variable was read, changed or activated.

`scripts/check_unsafe_defaults.py` passes:
`unsafe default check passed (all execution flags default-off, no wildcard
CORS, no public secrets)`. It enforces default-off for every execution flag,
including `MILO_ENABLE_CATALOG_EXECUTION`, `MILO_ENABLE_PAID_EXECUTION`,
`MILO_ENABLE_RUN_CREATION`, `MILO_ENABLE_EXECUTION_CONTROL` and
`NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI`.

`scripts/check_migrations.py` and `scripts/secret_scan.py` also pass.

The migration workflow's auto-apply path remains opt-in through the
repository variable `SUPABASE_MIGRATIONS_AUTO_APPLY`, which is unset —
observed empty in the apply run's own step environment.

## 12. What this does not do

This work aligned migration history and schema. It did not deploy, did not
enable any execution flag, did not capture Government data, did not promote
any catalog record, did not run MILO or the swarm, and made no paid or
provider call. No change was made to GCP, Vercel, Redis or IAM.

## 13. Next stage

**Stage A deployment with execution still OFF.** The schema is now in place
for the R3/R4 verification path and the catalog evidence path, but every
execution flag stays default-off and the catalog stays empty until capture is
separately authorized and reviewed. Recommended before Stage A:

- read the hosted Supabase security and performance advisors with an account
  that can see them, and record the result alongside §9;
- confirm the encrypted backup is still retained, or take a fresh one, since
  artifact `10524533221` expires 2026-09-24;
- keep `SUPABASE_MIGRATIONS_AUTO_APPLY` unset.

Recovery, if ever required, follows `ROLLBACK.md` and `SUPABASE_BACKUP.md`:
restore into a new isolated project first, never directly over Production
without a separately reviewed recovery plan.
