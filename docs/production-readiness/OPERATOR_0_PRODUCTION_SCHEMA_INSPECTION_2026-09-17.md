# OPERATOR-0 — production schema inspection (read-only)

**Final outcome: `OPERATOR-0 COMPLETED — NORMAL_ORDERED_APPLY_CANDIDATE, no production mutation authorized`**

This report records two separate read-only inspection phases on 2026-09-17:

1. the original Claude Code inspection, which correctly stopped at the target-identity gate because its connector did not expose a trustworthy production project reference; and
2. a later independent authenticated read-only verification through a separately authorized production channel, which resolved the target identity and completed the migration/schema checks needed to classify the current database state.

The first phase remains valid historical evidence about what that Claude session could and could not prove. It is not the final OPERATOR-0 conclusion.

`OPERATOR-0 performed no production mutation.`

---

## 1. Repository baseline

| Item | Value |
| --- | --- |
| Repository | `giladscore494/milo-agent-workspace` |
| Inspected `main` SHA | `9af24c3c42def31b4fc6ea17831b744cd8e45f16` |
| `main` verification | unchanged during the inspection/review window |
| PR #94 | merged; `main` is its merge commit |
| Scope of this PR | documentation only |

The repository state used for this report includes CODE-1, CODE-2 and CODE-3, including the read-only canonical/review catalog surface. No application code, migration, deployment configuration or runtime flag is changed by this report.

---

## 2. Read-only posture and identity history

### 2.1 Original Claude Code session

The original session used `Supabase — MILO Production Read-Only` and proved the connection itself was read-only:

- `transaction_read_only = on`;
- connected role `supabase_read_only_user`;
- a stored read-only transaction default;
- the connector exposed read/introspection operations and no migration/deployment mutation surface.

That session could not read a trustworthy production project reference from the connector or database metadata. Under the OPERATOR-0 brief, stopping before schema inspection was therefore correct.

The original session's `BLOCKED` result describes only that session's evidence boundary. It must not be read as the final state of OPERATOR-0 after the independent verification below.

### 2.2 Independent production identity verification

A later authorized read-only verification established the exact intended Production Supabase target through a separate trusted channel and then inspected that target directly.

The concrete project reference is intentionally not committed to this repository. The repository must continue to receive the expected production project identity through operator configuration rather than a hard-coded value.

This second channel performed read-only metadata/schema/history inspection only. It did not apply, repair, reconcile or create migrations and did not mutate application rows or database objects.

---

## 3. Migration history — observed production state

The production migration history was read directly from the verified production target.

The applied history currently ends at:

`20260823000100`

The following nine repository migrations are absent from production migration history:

1. `20260828000100_canonical_scope_conflict_identity.sql`
2. `20260828000200_source_evidence_fragments.sql`
3. `20260902000100_r3_versioned_focused_evidence.sql`
4. `20260907000100_r4_deterministic_verification.sql`
5. `20260914200000_catalog_evidence_foundation.sql`
6. `20260915120000_catalog_integrity_corrections.sql`
7. `20260915180000_catalog_raw_record_source_locator.sql`
8. `20260916090000_catalog_bounded_candidate_queries.sql`
9. `20260916120000_catalog_field_level_promotion.sql`

This confirms the earlier nine-missing-versions finding as current production state rather than historical-only evidence.

No migration was applied as part of OPERATOR-0.

---

## 4. Migration-history ↔ schema-object reconciliation

The independent inspection did not stop at migration history. It also checked the schema changes expected from the missing tail.

The late R3/R4/catalog objects checked from those migrations are absent in production as well. This includes the required catalog persistence/read-model surface introduced by the September catalog migrations and the earlier late-August/early-September evidence changes.

Representative confirmed-absent objects include the catalog namespace expected from the missing migrations, including:

- `catalog_source_snapshots`
- `catalog_raw_records`
- `catalog_candidate_variants`
- `catalog_candidate_evidence_links`
- `catalog_models`
- `catalog_model_variants`
- `catalog_canonical_field_provenance`
- `catalog_canonical_field_current`
- `catalog_canonical_variant_current`

The inspection also checked representative columns/triggers from the earlier missing tail and found them absent.

Therefore the current state is **not** a case where migration history is behind while the schema was already applied manually.

### Reconciliation conclusion

| Question | Result |
| --- | --- |
| Are the nine versions absent from migration history? | **YES** |
| Are the corresponding late schema changes already present anyway? | **NO** for the required/representative objects checked |
| Is this a migration-history-only reconciliation case? | **NO** |
| Is normal ordered application the current candidate path? | **YES, subject to preflight/tooling correction and separate operator authorization** |

---

## 5. Required catalog state

Because `catalog_source_snapshots` itself is absent from the verified Production database, a durable Government catalog snapshot cannot exist there under the current schema.

Gap Audit item S3-02b is therefore now:

**`PROVEN_ABSENT`**

It is no longer `UNKNOWN` or merely historical inference.

No Government capture was performed during this inspection.

---

## 6. Authorization / RLS observations

The independent read-only inspection checked the relevant currently-existing service-side authorization posture.

For the service-only RPCs inspected, `anon` and `authenticated` do not currently hold the prohibited EXECUTE access that the earlier OPS-08 warning required operators to assume until proven otherwise.

Accordingly, the historical OPS-08 warning is not confirmed as an active production exposure for the RPCs inspected.

Existing public tables inspected for the relevant surface have RLS enabled where expected.

This finding does **not** authorize changing grants or policies and does not replace future post-migration verification. After the missing migration tail is eventually applied, ACL/RLS checks must be rerun against the new objects.

---

## 7. Production project pinning gap

The inspection also confirmed a repository/runtime safety gap independent of the database schema state:

- Staging uses `MILO_EXPECTED_SUPABASE_PROJECT_REF` to fail closed if pointed at the wrong Supabase project.
- Production does not currently enforce the same runtime project-ref pin.
- the non-secret production manifest has a `supabase.project_ref` field, but the current Production API/Worker deployment contract does not bind that expected ref into the runtime.

This is recorded as a code/tooling follow-up and is **not fixed in this documentation PR**.

The concrete production project ref remains operator configuration and must not be committed to source control.

---

## 8. Migration tooling/documentation gaps discovered during review

The follow-up repository review found two pre-apply safety defects that must be corrected before OPERATOR-1:

### 8.1 Remote migration-state false-green risk

`scripts/release/check-migration-state.sh` currently uses remote object markers only for migrations `001` through `015` when determining remote migration state.

Because later timestamped migrations are not part of that completeness test, a production-like database containing the old marker set but missing the timestamped tail can be reported as `fully-migrated`.

The verified Production database is exactly the kind of state that exposes this weakness.

The tool must be corrected to compare the complete local migration sequence with production migration history and fail closed on drift/gaps before it is relied upon for OPERATOR-1.

### 8.2 `MIGRATIONS.md` strict-order table is incomplete

The document says migrations must be applied strictly in sequence, but its order table currently omits four real migrations:

- `20260828000100_canonical_scope_conflict_identity.sql`
- `20260828000200_source_evidence_fragments.sql`
- `20260902000100_r3_versioned_focused_evidence.sql`
- `20260907000100_r4_deterministic_verification.sql`

That documentation/tooling defect is also outside this PR's scope and must be fixed in the separate corrective code/tooling PR.

---

## 9. Supabase security-advisor observations

Read-only security inspection also surfaced advisory warnings, including a `SECURITY DEFINER` environment/event-trigger function named `public.rls_auto_enable()` with broad execute visibility, plus mutable-`search_path` warnings on multiple functions.

These findings are **not** remediated by this report and must not trigger a blanket production migration.

Before any remediation, repository-owned functions must be separated from platform/environment-owned functions, and actual callable privileges/security context must be reviewed. Any necessary change should be a separately reviewed forward corrective migration.

These advisories do not change the migration-tail conclusion above.

---

## 10. Final OPERATOR-0 classification

The observed production state supports:

### `NORMAL_ORDERED_APPLY_CANDIDATE`

Reasoning:

- the production target is now independently verified;
- migration history is behind by a contiguous late tail of nine repository migrations;
- the corresponding required late schema changes are absent rather than secretly present;
- no evidence was found that this is merely a migration-history ledger mismatch.

This classification is **not authorization to apply migrations**.

Before any apply action, the migration-state tooling and production project-pin issues described above must be corrected/reviewed, the final ordered migration set must be re-derived from the then-current `main`, and production state must be rechecked read-only.

---

## 11. Stage confirmations / explicit non-actions

During both OPERATOR-0 inspection phases:

- no production migration was applied, repaired or reconciled;
- no DDL or DML mutation was performed;
- no application row was inserted, updated, deleted or upserted;
- no mutating RPC was invoked;
- no Supabase relation, function, trigger, policy, grant or RLS setting was changed;
- no Cloud Run, IAM, Secret Manager, Vercel or Redis configuration was changed;
- `AUTH-1` was not performed;
- `MILO_ENABLE_CATALOG_EXECUTION` was not enabled;
- no Government capture occurred;
- `data.gov.il` was not contacted for capture;
- no CODE-1 capture run was prepared;
- no MILO/Swarm run was launched;
- no paid model/provider call occurred;
- no canonical promotion occurred.

---

## 12. Required next sequence

The safe sequence after this report is:

`correct preflight/tooling defects → independently review/merge corrective code PR → refresh OPERATOR-0 against corrected main → explicit OPERATOR-1 authorization → ordered production alignment if still indicated → deploy with execution disabled → smoke/read-only validation → later staged capture/activation under separate authorization`

No step in this documentation PR advances into OPERATOR-1.

---

## 13. Documentation status

This file is the durable OPERATOR-0 record for 2026-09-17.

It intentionally preserves the distinction between:

- **what the original Claude Code connector session could prove** — read-only connection, but identity-blocked; and
- **what the later independent authorized production inspection proved** — exact target verified, migration tail absent in both history and schema, producing the final `NORMAL_ORDERED_APPLY_CANDIDATE` classification.

This PR is documentation-only and contains no code, migration or configuration changes.
