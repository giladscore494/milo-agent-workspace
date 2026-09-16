# Authorization, ownership and RLS

## Project membership — `COMPLETED_IN_CODE`

`public.project_members` (migration `007`) is the authorization spine:
`(project_id, user_id, role)` with roles `owner | admin | member | viewer`.
Every browser-facing read/write path resolves the verified user (gateway
token first — see [AUTHENTICATION.md](AUTHENTICATION.md)) and requires
membership of the target project (`backend/auth.py`,
`tests/test_authorization.py`). Cross-user access returns 403/404, never
data.

Projects are created atomically with their owner row
(`create_project_from_proposal_with_owner`, migration `011`), so no project
can exist without an owner going forward. Legacy projects without owners
are backfilled manually (below).

## Proposal ownership and lifecycle — `COMPLETED_IN_CODE`

`public.workflow_proposals` carries `created_by` and `project_id`
(migration `008`); RLS requires non-NULL ownership plus membership for
every browser operation, and ownership is protected against tampering
(migration `011`). Proposal mutations additionally sit behind
`MILO_ENABLE_PROPOSAL_MUTATIONS` / `MILO_ENABLE_PROPOSAL_READS`
(default off). Proposal creation requires an `idempotency_key`
(`backend/schemas.py`).

## RLS — `COMPLETED_IN_CODE`

Every `public` table enables row level security explicitly in the
migrations. Browser-reachable tables carry membership-based policies; all
other tables are service-path only and carry RLS with zero policies
(deny-all for browser roles). Migration
`20260810000200_enable_rls_on_service_only_tables.sql` added the explicit
enablement for the service-only tables from migrations 002/004/005/015 —
previously that invariant silently depended on an environment-specific
`ensure_rls` event trigger (a platform guardrail some Supabase projects
install, not part of this repository). The service-role connection used by
the API/worker bypasses RLS by design, which is why it is server-only and
why application authorization (membership checks) runs on every
browser-facing route regardless. Executable RLS validation runs against
real PostgreSQL in CI (`tests/test_migrations_postgres.py`, zero skips
enforced), including a guard that every public table has RLS with no
external trigger present.

## Durable catalog namespace — `COMPLETED_IN_CODE`

Migration `20260914200000_catalog_evidence_foundation.sql` (Catalog PR1) adds
six service-path-only relations: `catalog_source_snapshots`,
`catalog_raw_records`, `catalog_candidate_variants`,
`catalog_candidate_evidence_links`, and the canonical `catalog_models` /
`catalog_model_variants`. All six enable RLS with **zero policies**, so
browser roles (`PUBLIC` / `anon` / `authenticated`) are denied outright, and
all six additionally hold **no grant at all** for those roles — two
independent barriers, neither depending on the other.

`service_role` privileges are deliberately narrow and differ per relation:

| Relation | `service_role` holds |
| --- | --- |
| `catalog_raw_records`, `catalog_candidate_evidence_links` | `SELECT`, `INSERT` — append-only; no `UPDATE`, no `DELETE` |
| `catalog_source_snapshots`, `catalog_candidate_variants` | `SELECT`, `INSERT`, `UPDATE` — the one reviewed transition each carries (snapshot completion counters, candidate status); no `DELETE` |
| `catalog_models`, `catalog_model_variants` | **`SELECT`, `INSERT`** after Catalog PR3 — still no `UPDATE`, no `DELETE`, and still immutable by trigger |
| `catalog_canonical_field_provenance` (PR3) | `SELECT`, `INSERT` — append-only; no `UPDATE`, no `DELETE` |
| `catalog_canonical_field_current`, `catalog_canonical_variant_current` (PR3, views) | **`SELECT` only**, `security_invoker = true`, nothing for any browser role |

**Catalog PR3 opened the canonical pair, and the shape of that opening is the
point.** PR1 and PR2 kept it `SELECT`-only because a single row-level
`promoted_from_verdict_id` cannot verify a row of several independent facts.
PR3 adds `catalog_canonical_field_provenance` — ONE append-only row per promoted
FACT, carrying the candidate, evidence link, snapshot, source, claim, verified
verdict, run and worker lease, source version, exact locator and idempotency key
— and then grants `INSERT` and nothing else.

Two triggers make that grant safe FOR EVERY WRITER, which matters because
`service_role` holds direct DML and the RPCs are `SECURITY INVOKER`:

* `catalog_check_field_provenance` (BEFORE INSERT) holds every promoted fact to
  its whole support chain and DERIVES its source, claim, verdict, version and
  locator from the cited evidence link. A forged row, a row citing an
  unverified verdict, a row citing a `legacy_reference` snapshot, a row whose
  claim states a different field or a different value, and a row whose claim
  sits in an unresolved conflict are all refused for a direct `INSERT` exactly
  as they are through the RPC.
* `catalog_require_field_provenance` (DEFERRED constraint trigger) refuses to
  COMMIT a canonical variant whose stated fields are not all covered by
  revision-1 provenance, and holds the PR1 row-level back-pointer to being a
  verdict and a candidate from that row's own provenance. A companion deferred
  trigger refuses a bare canonical model with no promoted variant.

So "no canonical fact exists without exact verified evidence for that exact
field" is a database property rather than a claim about the code. The views are
created with `security_invoker = true` and have `REVOKE ALL` applied to
`service_role` before the single `SELECT` grant, because a view is a new object
and Supabase default privileges would otherwise hand it everything.

Catalog PR2 (`20260915180000_catalog_raw_record_source_locator.sql`) changes
none of this. It adds one column to `catalog_raw_records` and one immutable
constraint predicate, `public.catalog_source_locator_valid(jsonb)`, which is
revoked from `PUBLIC`/`anon`/`authenticated` and granted to `service_role`
exactly like the other constraint helpers in this namespace. No grant widens,
no relation gains `UPDATE` or `DELETE`, and the canonical pair is untouched —
after a full Government ingestion, both canonical relations still hold zero
rows and still refuse `INSERT` for every role.

**And canonical rows are now immutable outright.** The original trigger froze
identity and provenance but allowed the factual columns to be rewritten as long
as a revision counter advanced — so a row could state values that the single
`promoted_from_verdict_id` attached to it had never seen, with the advancing
counter making it look reviewed. Row-level provenance cannot verify a
multi-field row: a verdict that confirmed the drivetrain says nothing about the
model year beside it. **Catalog PR3 added exactly that field-level,
append-only revision provenance before enabling any insert** (see above), and
the row-level back-pointer is now a CHECKED pointer into the row's own field
provenance rather than a substitute for it.

**Catalog PR3's read functions** (`catalog_readable_snapshot`,
`catalog_candidate_manufacturers`, `catalog_candidate_models`,
`catalog_candidate_model_years`, `catalog_candidate_variant_page`,
`catalog_raw_record_by_upstream_id`, `catalog_snapshot_candidate_diff`,
`catalog_page_limit`) take no lease, because a lease authorizes a durable WRITE.
They are `SECURITY INVOKER`, have a fixed `search_path`, name every relation as
a literal, order deterministically, bound every page with a server-owned
constant and return the exact total — including on an EMPTY page, which comes
back as one COUNT ROW rather than as no rows at all. Each has `EXECUTE` revoked
from `PUBLIC`/`anon`/`authenticated` before the narrow `service_role` grant,
exactly like every other function in this namespace, and so do the three
IMMUTABLE pure helpers PR3 adds (`catalog_record_locator_id`,
`catalog_claim_entity_key`, `r4_normalized_scope_text`).

**The repository's catalog write path** goes through six lease-guarded RPCs
(`record_catalog_snapshot_guarded`, `record_catalog_raw_record_guarded`,
`activate_catalog_snapshot_guarded`, `record_catalog_candidate_guarded`,
`link_catalog_candidate_evidence_guarded` and, from Catalog PR3,
`promote_catalog_variant_guarded`), each calling
`assert_worker_lease` before writing anything, each `EXECUTE`-revoked from
`public`/`anon`/`authenticated` and granted only to `service_role`, and each
idempotent on a derived key that fails closed when replayed with different
content.

**Stated exactly: these RPCs are *not* the only database write path.** They
are `SECURITY INVOKER`, and `service_role` retains the direct staging-table
DML in the table above — so a caller holding the service-role credential can
write those tables without them. The accurate guarantee is therefore about the
repository, not the database:

- *the repository's catalog write path uses guarded RPCs* — true, and enforced
  by `tests/test_catalog_persistence.py`, which proves every repository method
  calls a literal RPC name and never a direct insert;
- *the RPCs are the only way to write these tables* — **false**, and the
  migrations no longer claim it.

Anything that must hold for **every** writer therefore lives in the schema
rather than in a function body: the cross-table identity foreign keys
(`catalog_candidate_variants_record_snapshot_fk`,
`catalog_candidate_evidence_links_candidate_snapshot_fk`), the natural
uniqueness indexes, the key domain/shape checks, the append-only and
lifecycle triggers, and the canonical-immutability trigger. Converting the
public RPCs to `SECURITY DEFINER` to close the remaining gap is a separate,
reviewed decision: it would need a safe owner and `search_path`, revoked
default `EXECUTE`, proven inaccessibility to browser roles and its own
executable authorization tests, and it is deliberately **not** done here.

The constraint helper `catalog_identity_dimensions_valid(jsonb)` is revoked
from the browser roles on the same footing as the `r3_*` predicates.
Executable validation: `tests/test_migrations_postgres.py` (the `test_catalog_*`
and corrective cases, `MILO_REQUIRE_PG_TESTS=1`, zero skips).

### Evidence-link provenance is derived, not asserted

A `catalog_candidate_evidence_links` row must cite a real `public.claims` row
(`claim_id` is `NOT NULL`), and its `record_locator`, `source_version_kind` and
`source_version` are **derived** from that claim's `evidence_locator` and that
source's `source_version_kind`/`source_version_id`. A caller may state them and
is then held to them; a mismatch fails closed. A non-null `verdict_id` is
accepted only when `claim_verdicts.verdict = 'verified'` and the verdict
belongs to that exact claim and run. A `legacy_reference` snapshot still cannot
carry a verdict at all.

Snapshot ownership is exclusive to the creating run: only `created_by_run_id`
may append records to, or decide, its own capture, and both decided states
(`complete`+active, and `failed`) are terminal. Evidence **links** are the one
deliberate cross-run allowance — a later run may cite evidence *of its own* for
an existing candidate — which is stated and tested separately
(`test_a_later_run_may_add_evidence_to_an_existing_candidate_deliberately`).

Catalog foreign keys are all `ON DELETE RESTRICT`, never `CASCADE` — unlike
the run-scoped evidence relations. Durable catalog state must not disappear
with a run, and a run that catalog state depends on cannot be deleted out from
under it.

## Service-only RPC ACLs — `COMPLETED_IN_CODE`

Supabase grants EXECUTE on public-schema functions to `anon`,
`authenticated` and `service_role` through default privileges. The service
RPC migrations (011/012/014/015) revoked `public` and `authenticated` but
not `anon`, so every service RPC — including the SECURITY DEFINER budget
functions — remained anonymously callable via PostgREST on a real Supabase
project. Migration `20260810000100_revoke_anon_execute_on_service_rpcs.sql`
revokes `anon` (and re-asserts `authenticated`/`public`) on all eight
service RPCs and removes the schema-level default EXECUTE grants for
`anon`/`authenticated`, so the existing per-function
`revoke ... from public` convention is now sufficient for future
functions. Regression coverage: `tests/test_migrations_postgres.py`
replicates Supabase's default function privileges in its shim and asserts
no non-trigger public function is executable by `anon`.

**Production follow-up (required before Stage C):** both hardening
migrations are applied to staging only. Production must receive them via
the manual migration procedure, and the anon-EXECUTE gap should be assumed
present in production until then.

## Ownership backfills — `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`

Legacy rows created before migrations `007`/`008` may lack ownership.
Generate (never auto-apply) the corrective SQL with:

- `scripts/release/generate-membership-backfill.sh` — real project/user
  UUIDs from an operator-supplied mapping; rejects placeholders, duplicate
  owners, ownerless projects.
- `scripts/release/generate-proposal-backfill.sh` — proposal→owner/project
  mapping; rejects orphans and conflicts; updates only NULL-ownership rows.

Apply manually per [MIGRATIONS.md](MIGRATIONS.md), then validate with the
queries embedded in the generated SQL.

## Worker-route authorization — `COMPLETED_IN_CODE`

Internal routes (`/internal/runs/...`) accept only verified worker
identities plus the active lease token; browser identities and the gateway
identity are rejected (`backend/worker_auth.py`,
`tests/test_worker_auth.py`). The gateway additionally refuses to proxy
`/internal/*` at all (route allowlist).
