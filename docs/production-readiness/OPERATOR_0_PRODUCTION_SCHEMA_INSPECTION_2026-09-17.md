# OPERATOR-0 — production schema inspection (read-only)

**Outcome: `OPERATOR-0 BLOCKED — production project identity is ambiguous`**

OPERATOR-0 stopped at §2 (target identity). The schema inspection in §4–§12 of
the OPERATOR-0 brief was **not performed**, because the brief forbids querying a
target whose identity cannot be established conclusively. What follows records
the evidence actually collected, the expected-object inventory built from this
repository, and the exact blocker.

`OPERATOR-0 performed no production mutation.`

---

## 1. Repository baseline

| Item | Value |
| --- | --- |
| Repository | `giladscore494/milo-agent-workspace` |
| Inspected SHA | `9af24c3c42def31b4fc6ea17831b744cd8e45f16` |
| `origin/main` at start of inspection | `9af24c3c42def31b4fc6ea17831b744cd8e45f16` |
| `origin/main` re-fetched after inspection | `9af24c3c42def31b4fc6ea17831b744cd8e45f16` (unchanged) |
| Working tree | clean throughout |
| PR #94 | **merged** 2026-09-17T12:41:13Z (`base` `d0155599…`, `head` `88a87737…`); `main` tip is its merge commit |
| Open pull requests | **none** (zero open PRs) — no overlapping PR touching migrations, catalog persistence, production readiness, RLS/grants or Government capture is possible |
| Instruction files read | `AGENTS.md`, `CLAUDE.md` (both repository-root; no nested copies exist) |

All nine authoritative documents named by the brief exist at this SHA and were
read: the Gap Audit, `FINAL_ACCEPTANCE.md`, `MIGRATIONS.md`,
`AUTHORIZATION_AND_RLS.md`, `ENVIRONMENT_MATRIX.md`, `STAGED_ACTIVATION.md`,
`ROLLBACK.md`, `docs/catalog-code1-operator-capture.md`,
`docs/catalog-code3-review-surface.md`.

## 2. Inspection window

| Item | Value |
| --- | --- |
| Inspection date/time (UTC) | 2026-09-17T12:54Z – 2026-09-17T13:01Z |
| Connector used | `Supabase — MILO Production Read-Only` (exclusively) |
| Connector **not** used | `Supabase — MILO Staging` — not called once, for any purpose |

---

## 3. Target identity — the blocker

### 3.1 What was proven

The connection is **provably read-only at three independent layers**:

| Layer | Observation | Meaning |
| --- | --- | --- |
| Transaction | `transaction_read_only = on` | every statement runs in a read-only transaction |
| Role | `current_user` = `session_user` = `supabase_read_only_user` | Supabase's dedicated read-only role, not `postgres`/`service_role` |
| Role default | `default_transaction_read_only` present in `pg_db_role_setting` | read-only is a stored default, not a per-session courtesy |

Tool surface is also non-mutating. The production connector exposes exactly five
tools — `execute_sql`, `list_extensions`, `list_migrations`, `list_tables`,
`search_docs`. It exposes **no** `apply_migration`, `deploy_edge_function`,
`create_branch`, `merge_branch`, `reset_branch`, `rebase_branch` or
`delete_branch`. Those mutating tools exist only on the Staging connector, which
was not used.

Server facts observed:

| Fact | Value |
| --- | --- |
| `current_database()` | `postgres` |
| `current_user` | `supabase_read_only_user` |
| PostgreSQL version | 17.6 |
| `pg_is_in_recovery()` | `false` |
| `application_name` | `mgmt-api` |
| `cluster_name` | `main` |
| Non-template databases | `postgres` (only) |
| Schemas present | `auth`, `extensions`, `graphql`, `graphql_public`, `pgbouncer`, `public`, `realtime`, `storage`, `supabase_migrations`, `vault` |

### 3.2 What could not be proven

The brief requires the connected project to be verified as the exact protected
production project **from trusted configuration or a project reference available
to the authenticated operator environment**, and explicitly forbids inferring it
from a display name, the first project returned, a URL copied from
documentation, a branch, a local `.env` or a stale migration log.

No such source exists in this environment. Every avenue was tried and each
failed for a specific, recorded reason:

| # | Identity source attempted | Result |
| --- | --- | --- |
| 1 | Production connector management tools | **No project-ref readout exists.** `get_project_url` and `get_publishable_keys` are present only on the *Staging* connector, which is out of bounds. |
| 2 | Local connector / MCP configuration on disk | **Absent.** No `mcpServers` block exists in `/root/.claude.json` or any `settings.json`/`.mcp.json`; connectors are configured server-side and are not readable from the session. |
| 3 | Repository | **Does not record the production ref, by design** — see §3.3. |
| 4 | Database metadata | **No project-ref signal.** `_realtime.tenants` (the usual carrier of `external_id` = project ref) does not exist — only a `realtime` schema holding `messages`, `subscription`, `schema_migrations`. `cluster_name` is the generic `main`. The database comment is stock PostgreSQL text. `pg_settings` carries no ref. `vault.secrets` is empty. `pg_db_role_setting` holds only `search_path`/`statement_timeout`/`log_statement`-class entries (names read; **values deliberately not printed**). |

The project ref supplied in the operating instruction occurs in exactly one
place on the entire filesystem: **this session's own transcript**. It is
therefore an *expected* value asserted by the operator, never an *observed* one.
Under the brief's own rule it is the comparison target, not the proof.

### 3.3 The repository cannot supply the production ref — and this is deliberate

This is a structural property of the codebase, not a gap in the search:

- `ENVIRONMENT_MATRIX.md` line 50 scopes `MILO_EXPECTED_SUPABASE_PROJECT_REF` to
  **`api+worker (staging only)`**, "required when `ENVIRONMENT=staging`: runtime
  refuses any other Supabase project (`STAGING_DEPENDENCY_UNPINNED`/`_MISMATCH`)",
  and records its production value as **`unset`**.
- The only Supabase project ref committed to the repository,
  `cxlwavxvwgrfikkudtzf`, is **staging**: it is the `BASE_STAGING` fixture in
  `tests/test_production_config.py:159-165` and the
  `STAGING_SUPABASE_PROJECT_REF` default in
  `scripts/deploy/staging-cloud-run.sh:33`.
- In `STAGING.md`'s resource table the **Production** column for the Supabase row
  is the literal placeholder text `production project` — the production ref is
  intentionally not committed.

So the production Supabase project is **unpinned in production** and
**unrecorded in the repository**. There is nothing to verify against.

### 3.4 The authoritative audit hedges on exactly this point

`docs/roadmap/MILO_GAP_AUDIT_2026-09-16.md` refers to **"the linked project"**
14 times and never once asserts that the linked project is the protected
production project — for example lines 106, 191, 244, 324, 367, 368 and the
OPS-08 row at line 422 ("recorded in **the linked project's** migration
history"). The audit at this SHA therefore shares this blocker rather than
resolving it. Closing that gap is the stated purpose of OPERATOR-0 §2, and it
could not be closed with the tooling available to this session.

### 3.5 Safe identity proof of record

| Field | Value |
| --- | --- |
| Environment claimed by connector name | production |
| **Verified expected identity** | **NO — could not be determined** |
| Expected production ref fingerprint (SHA-256, first 12 hex) | `6964ccdff260` |
| Repo-committed **staging** ref fingerprint (`cxlwavxvwgrfikkudtzf`) | `8d4d49c00d6e` |
| Observed project ref | **none — the connected project exposes no project reference** |
| Verification source | none available (see §3.2) |

The full production ref is **not** written into this report: the repository
treats it as sensitive by deliberately never committing it (§3.3). The
fingerprint lets a reviewer confirm which value was expected without publishing
it.

---

## 4. Read-only discipline — every command executed

Five statements were executed against the target, all through the read-only
connector, all pure catalog/metadata reads, and **all issued in service of §2
identity verification**. No application table was read. No function was invoked.
No DDL, DML, `CALL`, migration or repair command was issued.

| # | Category | Statement (summary) |
| --- | --- | --- |
| 1 | session metadata | `SELECT current_setting('transaction_read_only'), current_database(), current_user, session_user, pg_is_in_recovery(), version()` |
| 2 | `pg_catalog` introspection | `SELECT nspname FROM pg_namespace WHERE nspname IN (…)` |
| 3 | `pg_catalog` introspection | `pg_settings` (4 safe names) ∪ `realtime` relation names ∪ `pg_db_role_setting` setting **names only** |
| 4 | `pg_catalog` introspection | database comment, non-template database list, `has_schema_privilege('vault','USAGE')` |
| 5 | metadata | `vault.secrets` **name** column only (empty); existence check of `auth.sso_providers` in `pg_class` |

Read-only query categories executed: **session metadata and `pg_catalog`
introspection only.** Categories *not* reached: `supabase_migrations`
history, `to_regclass` relation inventory, function/signature inspection,
view inspection, trigger inspection, `pg_policies`/RLS, ACL inspection,
and all bounded `COUNT(*)` queries.

**Statement 4 was the only borderline call and it was safe:** it printed
setting *names* and a boolean privilege check, never a setting value, so no
secret could surface. Statement 5 selected only `vault.secrets.name`, never
`decrypted_secret`.

No credential, token, service-role key, JWT, database password, connection
string, lease token, raw Government payload or application row appears in this
report or appeared in any output.

---

## 5. Sections the blocker prevented

Because identity failed at §2, these required sections have **no observed
data**. They are listed so the reader is not left guessing which obligations
went unmet:

| Brief section | Required output | State |
| --- | --- | --- |
| §4 | current migration-history table | **NOT OBSERVED** |
| §5 | relation inventory (PRESENT / MISSING / PRESENT_BUT_DIFFERENT) | **NOT OBSERVED** |
| §6 | function + exact-signature inventory, overload detection | **NOT OBSERVED** |
| §7 | view inventory incl. `security_invoker` and grants | **NOT OBSERVED** |
| §8 | trigger inventory incl. enabled state and definitions | **NOT OBSERVED** |
| §9 | RLS / policy matrix | **NOT OBSERVED** |
| §10 | grant/ACL matrix and the `anon`-EXECUTE finding (OPS-08) | **NOT OBSERVED** |
| §11 | catalog row counts; Government snapshot state (S3-02b) | **NOT OBSERVED** |
| §12 | canonical aggregate consistency | **NOT OBSERVED** |
| §13 | migration-history ↔ object reconciliation matrix | **NOT OBSERVED** |

Two Gap Audit rows therefore remain exactly as they were at the inspected SHA:

- **S3-02b** ("a durable Government snapshot exists in the target database") —
  **UNRESOLVED.** Not PROVEN_PRESENT, not PROVEN_ABSENT, not
  PRESENT_BUT_UNUSABLE. No snapshot relation was queried.
- **OPS-08** (whether `anon` still holds EXECUTE on service-only RPCs) —
  **UNRESOLVED**, still `BLOCKED_EXTERNAL`. The `AUTHORIZATION_AND_RLS.md`
  warning that the anon-EXECUTE gap should be assumed present in production is
  neither confirmed nor retired. **Continue to assume it is present.**

## 6. Expected-object inventory built from this repository

Built by tracing the migration SQL at the inspected SHA, not from any prior
handoff's inventory. This is the **expected** side of §13's matrix; the observed
side is empty. Recorded here so the next stage does not have to re-derive it.

`supabase/migrations/` holds **34** files: 15 sequence-numbered (`001`–`015`)
and 19 timestamped. The nine versions the brief flags as historically missing
from migration **history** are all present as **files**:

| Migration | Expected tables | Views | Functions | Triggers |
| --- | --- | --- | --- | --- |
| `20260828000100_canonical_scope_conflict_identity` | — | — | 2 | — |
| `20260828000200_source_evidence_fragments` | `source_evidence_fragments` | — | 2 | 1 |
| `20260902000100_r3_versioned_focused_evidence` | — | — | 6 | — |
| `20260907000100_r4_deterministic_verification` | `claim_verdicts`, `claim_verdict_supports`, `conflict_resolutions` | — | 5 | 3 |
| `20260914200000_catalog_evidence_foundation` | `catalog_source_snapshots`, `catalog_raw_records`, `catalog_candidate_variants`, `catalog_candidate_evidence_links`, `catalog_models`, `catalog_model_variants` | — | 10 | 6 |
| `20260915120000_catalog_integrity_corrections` | — | — | 6 | 2 |
| `20260915180000_catalog_raw_record_source_locator` | — | — | 2 | — |
| `20260916090000_catalog_bounded_candidate_queries` | — | — | 8 | — |
| `20260916120000_catalog_field_level_promotion` | `catalog_canonical_field_provenance` | `catalog_canonical_field_current`, `catalog_canonical_variant_current` | 12 | 2 |
| **Total** | **11 tables** | **2 views** | **43 distinct** | **14** |

All seven catalog relations the Gap Audit requires are accounted for, each
mapped to its owning migration — six to `20260914200000` and
`catalog_canonical_field_provenance` to `20260916120000`. The R3/R4 evidence
relations are `source_evidence_fragments` (`20260828000200`) and
`claim_verdicts` / `claim_verdict_supports` / `conflict_resolutions`
(`20260907000100`).

Expected triggers named in the migrations include
`catalog_canonical_field_provenance_append_only`,
`catalog_canonical_field_provenance_checked`,
`catalog_models_identity_immutable`, `catalog_model_variants_identity_immutable`,
`claim_verdicts_append_only`, `claim_verdict_supports_append_only`,
`conflict_resolutions_append_only` and `source_evidence_fragments_append_only`.

Key functions locate as: `promote_catalog_variant_guarded` and
`catalog_run_pending_promotions` in `20260916120000`; `assert_worker_lease` in
`20260810000300_lease_guarded_worker_writes.sql` (outside the nine).

**Expected ACL posture** (from the migrations' own `DO` blocks): for each
guarded RPC and evidence relation, `EXECUTE`/`ALL` is revoked from `PUBLIC`,
`anon` and `authenticated`, and only `service_role` is granted — `EXECUTE` on
functions, and `SELECT, INSERT` on tables with `UPDATE, DELETE` revoked even
from `service_role`. This is the expectation §10 was to test against `anon`.

---

## 7. Mismatches found

One, and it is the blocker itself:

| # | Mismatch | Evidence |
| --- | --- | --- |
| M-1 | **The production Supabase project is unpinned and unverifiable.** `MILO_EXPECTED_SUPABASE_PROJECT_REF` is staging-only and `unset` in production, the repository commits no production ref, and the connected project exposes no project reference through any read-only channel. Nothing in the codebase, its configuration, or the connection fail-closes on connecting to the wrong Supabase project **in production** — the very protection that exists for staging. | `ENVIRONMENT_MATRIX.md:50`; `STAGING.md` Supabase row; `tests/test_production_config.py:159-165`; §3.2 probe results |

No schema, function, view, trigger, policy or grant mismatch is reported —
**not because none exists, but because none was observed.**

## 8. Next-step classification (§14)

### **E. BLOCKED**

Target identity could not be established, so the evidence does not support any
other category. Specifically **not** chosen:

- **not NO_ACTION** — nothing was observed to be aligned and healthy;
- **not HISTORY_RECONCILIATION_CANDIDATE** — no object was demonstrated present;
- **not NORMAL_ORDERED_APPLY_CANDIDATE** — no object was demonstrated absent,
  and the brief forbids choosing this merely because `schema_migrations` is
  believed to be behind;
- **not CORRECTIVE_FORWARD_MIGRATION_REQUIRED** — no divergence was observed.

The historical nine-missing-versions finding is carried forward as **historical
evidence only**. This inspection neither confirms nor refutes it.

## 9. Blockers and unknowns

**Blocker B-1 — production project identity is not establishable from this
session.** To clear it, one of the following is needed, in preference order:

1. Re-scope the `Supabase — MILO Production Read-Only` connector so it also
   exposes a read-only project-identity tool (`get_project_url` is sufficient
   and non-mutating), letting OPERATOR-0 observe the ref rather than assume it.
2. Provide the production project ref through configuration the session can
   actually read as trusted input, rather than as prose in a prompt.
3. Set `MILO_EXPECTED_SUPABASE_PROJECT_REF` in **production** — closing M-1
   permanently, and giving the runtime the same wrong-project protection staging
   already has.

**Unknowns** — the entire observed state of the target database: migration
history, relation/function/view/trigger inventory, RLS and policies, ACLs
including `anon` EXECUTE, all row counts, Government snapshot state (S3-02b) and
canonical aggregate consistency.

## 10. Confirmations

- `OPERATOR-0 performed no production mutation.`
- No migration was applied, created, edited, pushed or repaired.
- No row was inserted, updated, deleted or upserted; no mutating RPC was invoked.
- No relation, view, function, policy, trigger or index was created, dropped or altered.
- No RLS setting and no grant was changed.
- Nothing was deployed. No MILO flag was enabled;
  `MILO_ENABLE_CATALOG_EXECUTION` was not touched.
- **AUTH-1 was NOT performed.**
- **No catalog flag was enabled.**
- **No Government capture occurred**; `data.gov.il` was not contacted.
- No CODE-1 capture, no capture-run preparation, no MILO/Swarm launch, no paid
  model call, no canonical-variant promotion.
- No change to Secret Manager, IAM, Cloud Run, Vercel or Redis.
- The `Supabase — MILO Staging` connector was never called.
- Nothing discovered was repaired. M-1 is recorded, not fixed.
