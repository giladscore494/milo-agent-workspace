# CODE-3 — the bounded, membership-authorized catalog review surface

**Stage:** CODE-3. **Status:** implemented in code, read-only.
**Nothing here has been deployed, enabled, applied or run against a production
database.** See [Execution-safety confirmation](#execution-safety-confirmation).

This closes the **code** half of Gap Audit row `CAT-10` (`MISSING` — "no API
route, script or UI reads `ready_for_review` or the canonical catalog"). It adds
two GET routes and one read-only workspace surface, and nothing else.

## 1. What it is, and what it is not

It answers two durable questions:

1. **What is in the canonical vehicle catalog right now?**
2. **Which Government candidates are waiting for a human to look at them?**

It is an **inspection** surface. It is not a promotion console, a candidate
editor, a snapshot activation console, an operator capture launcher, an
execution control panel, a reconciliation tool, a migration tool or a data
correction tool. There is no mutation endpoint and no mutation control, and
`tests/test_catalog_review_surface.py` enumerates every repository method a
request reaches and holds it to a four-entry read-only allowlist.

It is also **not** CODE-2's question. `frontend/lib/catalogStatus.ts` answers
*"what did THIS RUN do to the catalog?"* from that run's events;
`frontend/lib/catalogReview.ts` answers *"what durable state EXISTS?"* from
durable rows. A run that promoted nothing and a catalog that holds nothing are
not the same fact, so they are two contracts and never one reducer.

## 2. Route table

| Method | Path | Answers | Response model |
| --- | --- | --- | --- |
| `GET` | `/projects/{project_id}/catalog/canonical` | a bounded page of `catalog_canonical_variant_current` | `CatalogCanonicalPage` |
| `GET` | `/projects/{project_id}/catalog/review-candidates` | a bounded page of candidates whose durable status is exactly `ready_for_review` | `CatalogReviewPage` |

There is no `POST`, `PUT`, `PATCH` or `DELETE` counterpart of either path
anywhere in `backend/main.py`, and none is reachable through the gateway.

### Query parameters

Every parameter is declared by name and typed on the handler, so an
unsupported one is never read and a value of the wrong type is refused by
FastAPI before the handler runs.

| Parameter | Both routes | Canonical only | Semantics |
| --- | --- | --- | --- |
| `limit` | ✓ | | whole number in `[1, 100]`; default `25` |
| `offset` | ✓ | | whole number in `[0, 1 000 000]`; default `0` |
| `manufacturer` | ✓ | | exact match, ≤ 120 characters |
| `commercial_model` | ✓ | | exact match, ≤ 120 characters |
| `model_year` | ✓ | | whole number in `[1900, 2200]`; selects rows whose stated range CONTAINS it |
| `canonical_key` | | ✓ | exact match, ≤ 120 characters |

There is **no** parameter for a table, a column, an ordering, a page bound, a
status, a resource, a snapshot or an acknowledgement. Those are all server data
in `backend/catalog/review.py` and in the repository method it calls.

## 3. Authorization matrix

| Caller | Result |
| --- | --- |
| No identity header | `401 AUTHENTICATION_REQUIRED`; no repository call at all |
| Authenticated, **not** a member of `project_id` | `404 PROJECT_NOT_FOUND`; only `get_project` was called, so nothing about the catalog is disclosed — not even whether it holds anything |
| Authenticated, `project_id` does not exist | the same `404 PROJECT_NOT_FOUND` |
| Authenticated **member** | the bounded page |

The project id is an **authorization anchor**, not an owner. The durable catalog
is global; `repo.get_project(project_id, user.user_id)` is the repository's
existing membership contract — the same non-disclosing 404 every other browser
read uses — and only after it passes does the global read happen. Nothing
pretends a canonical row belongs to the project, and no separate authorization
system was created. Possession of a canonical key, a candidate key, a snapshot
id or a URL authorizes nothing.

## 4. Reading is not execution

Neither route appears in `execution_guard.SURFACE_RULES`, deliberately. The
surface requires **none** of:

`MILO_ENABLE_CATALOG_EXECUTION` · `MILO_ENABLE_PAID_EXECUTION` ·
`MILO_ENABLE_RUN_CREATION` · `MILO_ENABLE_RUN_CANCELLATION` ·
`MILO_ENABLE_EXECUTION_CONTROL` · `MILO_ENABLE_PROPOSAL_*` ·
`GATEWAY_ALLOW_EXECUTION_ROUTES` · a `WorkerLease` · Government egress.

**No flag is enabled anywhere by this stage.** The catalog kill switch stops the
catalog WRITE path and is explicitly non-destructive — the rows stay
([ROLLBACK.md](production-readiness/ROLLBACK.md)) — so the operator who has just
pulled it is the one who most needs to see what is already there. Gating this
read behind it would make the rollback blind.

`frontend/e2e/disabled.catalog-review.spec.ts` runs entirely against the
execution-disabled E2E stack and proves both halves in one test: the catalog
reads answer `200` **and** run creation is still refused `403` in the same
session.

## 5. Bounds

| Bound | Value | Where it lives |
| --- | --- | --- |
| Maximum page size | 100 | `MAX_REVIEW_PAGE_ITEMS` |
| Default page size | 25 | `DEFAULT_REVIEW_PAGE_ITEMS` |
| Maximum offset | 1 000 000 | `MAX_REVIEW_OFFSET` |
| Maximum filter length | 120 characters | `MAX_REVIEW_FILTER_CHARS` |
| Model-year filter range | 1900–2200 | `MIN/MAX_REVIEW_MODEL_YEAR` |
| Canonical listing bound (repository) | 100 rows | `SupabaseRepository.MAX_CANONICAL_LIST_ROWS` |
| Candidate page bound (database) | 200 rows | `public.catalog_page_limit()` |

The CODE-3 bound is the tighter of the two on the candidate path, so **both**
apply and the durable bound stays the backstop rather than the only bound.

A malformed or oversized pagination value is **refused**, never clamped: a
caller who asked for `-1` items did not ask for 25 and is told so. An empty
filter (`?manufacturer=`) is refused for the same reason — silently treating it
as "no filter" would answer a different question.

Filtering, ordering and paging all happen **in the database**. Nothing loads a
whole table into Python, and nothing paginates a materialized set.

## 6. Pagination semantics

Both surfaces carry an **exact** total from the database:

* the candidate page takes `total_count` from
  `public.catalog_candidate_variant_page`, which counts the whole filtered set
  and returns a COUNT ROW when a page is past the last matching row — so an
  out-of-range page reports the real total rather than zero;
* the canonical page takes PostgREST's `count=exact` over the same filtered set,
  carried on every returned row in the same shape.

`has_more` is `offset + len(items) < total`. It is **never**
`len(items) == limit`. When the database states no total, `total` and `has_more`
are both `null` — "not reported" — and are never rendered as `0`/`false`, which
would be the claim that the catalog is empty.

## 7. Response field allowlists

Every value is built key by key from a literal set. Nothing iterates a row, so a
column added later cannot appear without an edit to the projection, and the
Pydantic models carry `extra="forbid"` as an independent second check.

**Canonical item** (`CANONICAL_ITEM_FIELDS`): `canonical_key`,
`model_canonical_key`, `manufacturer`, `commercial_model`, `model_year_start`,
`model_year_end`, `official_model_code`, `trim`, `identity_dimensions`,
`promoted_at`, `revised_at`.

**Review candidate** (`REVIEW_CANDIDATE_ITEM_FIELDS`): `candidate_key`,
`status`, `manufacturer`, `commercial_model`, `model_year_start`,
`model_year_end`, `official_model_code`, `trim`, `identity_dimensions`.

**Snapshot context** (`REVIEW_SNAPSHOT_FIELDS`): `snapshot_key`, `resource_id`,
`package_id`, `publisher`, `dataset_title`, `dataset_market_scope`,
`upstream_version`, `upstream_version_kind`, `activated_at`,
`declared_record_count`, `stored_record_count`, `normalization_contract`,
`normalization_issue_count`.

`identity_dimensions` is projected through the closed
`CANDIDATE_IDENTITY_DIMENSIONS` vocabulary, never passed through as stored
jsonb, and an unstated dimension is an **absent key** — the same rule
`stated_identity_dimensions` enforces on the way in.

## 8. Data-exposure / redaction matrix

| Category | Reaches the browser? | Why |
| --- | --- | --- |
| Canonical identity/vehicle fields | ✅ | the reviewed data the surface exists to show |
| Derived keys (`cv1.`/`cc1.`/`cm1.`/`cs1.`) | ✅ | safe operational identifiers: derived, domain-separated, ASCII |
| Snapshot provenance listed above | ✅ | already-reviewed, browser-safe capture metadata |
| `variant_id`, `model_id`, `candidate_id`, `raw_record_id`, `snapshot_id` | ❌ | internal database ids; the derived key is the identifier |
| `promoted_from_candidate_id`, `promoted_from_verdict_id` | ❌ | internal execution linkage |
| `field_revisions` | ❌ | internal revision bookkeeping, and an unprojected jsonb map |
| `upstream_record_id`, `source_locator`, `payload_sha256` | ❌ | capture-internal provenance, not identity review |
| Raw register payload (`tozar`, `kinuy_mishari`, `degem_nm`, `koah_sus`, …) | ❌ | never projected; never read by this path |
| Evidence fragments, claims, verdicts, content hashes | ❌ | never read by this path |
| Model text, chain-of-thought, provider detail | ❌ | no model is constructed or called |
| SQL, PostgREST messages, database error text | ❌ | every repository refusal collapses onto one static `CATALOG_REVIEW_UNAVAILABLE` classification |
| Lease tokens, worker ids, credentials, service keys | ❌ | never read; swept by test and by the bundle scan |
| `content_sha256`, `page_chain_sha256`, the retrieval query | ❌ | not needed to review an identity |

Every browser-visible string additionally passes `redactSecretText` and a
120-character bound in `frontend/lib/catalogReview.ts`, and reaches the DOM only
through `safeText`. There is no `JSON.stringify` of server data and no
`dangerouslySetInnerHTML` anywhere on the surface.

## 9. Candidate status filtering

The status is **fixed**, not a filter a caller supplies:
`p_status => 'ready_for_review'` is applied by
`public.catalog_candidate_variant_page` inside the database, which validates it
against the closed `CANDIDATE_STATUSES` set.

Three independent barriers keep another status off the review surface:

1. the database applies the filter;
2. `review_candidates` re-checks every returned row and refuses the page
   **whole** (`502 CATALOG_REVIEW_UNAVAILABLE`) if any row disagrees — quietly
   dropping the odd rows would publish a page that looks complete and is not;
3. the browser parser drops any item whose status is not exactly
   `ready_for_review`.

## 10. Active-snapshot selection

The snapshot is resolved by `resolve_active_snapshot`
(`backend/catalog/government/projection.py`) — the **same** trusted rule the
Government tool's reader uses — pinned to `src.WLTP_RESOURCE_ID`. A caller
states no resource, no host, no table, no snapshot key and no
`allow_incomplete`: there is no parameter that could name one.

A snapshot answers only when it is active, `complete`, and states a usable
reading of its own rows. Anything else is an **honest typed unavailable state**,
never a fabricated empty catalog:

| Condition | `unavailable_reason` |
| --- | --- |
| No active snapshot for the resource | `no_active_snapshot` |
| Active snapshot records no reading at all | `snapshot_not_read` |
| Captured raw-only; states no identities | `snapshot_not_normalized` |
| Holds rows its reviewed vocabulary could not read | `snapshot_incomplete` |
| Recorded reading is malformed or disagrees with its own rows | `snapshot_state_invalid` |
| Named snapshot is unknown | `snapshot_unknown` |
| Anything else | `snapshot_unavailable` |

An unavailable page states `available: false`, **no** items, **no** snapshot and
`total: null` — never `0`, which would be the claim that a snapshot was read and
found empty. A repository failure is a different condition and stays
distinguishable: it is a `502`, not an unavailable page, so "there is no
snapshot" and "the database could not answer" never collapse into one answer.

## 11. Gateway

`frontend/lib/server/gatewayPolicy.ts` gains exactly two rules, both in
`SAFE_RULES` and both `GET`. They are **not** behind
`GATEWAY_ALLOW_EXECUTION_ROUTES`, and the allowlist names the two exact paths
rather than a `/catalog/*` prefix — a prefix rule would proxy any catalog route
a later release adds, including a mutating one, without anyone revisiting the
list. `GatewayRule.method` admits only `GET`/`POST`, and the route module
exports only `GET` and `POST` handlers, so `PUT`/`PATCH`/`DELETE` are
structurally unreachable (405) rather than merely refused by a rule.

## 12. No migration

CODE-3 adds **no migration**. `supabase/` is untouched.

* The canonical read reuses `public.catalog_canonical_variant_current`
  (`20260916120000_catalog_field_level_promotion.sql`), queried with
  server-owned columns, filters, ordering and range.
* The `ready_for_review` read reuses `public.catalog_candidate_variant_page`
  (`20260916090000_catalog_bounded_candidate_queries.sql`), which already
  expresses the status filter and the exact total.

One read-only repository method was added
(`list_canonical_catalog_variants`) because none existed:
`get_canonical_catalog_variant` answers for one named key, which cannot answer
"what is in the catalog".

## 13. Remaining operator work

1. **OPERATOR-0** — the read-only schema inspection of the protected production
   project. Unchanged and still required. **Nothing in CODE-3 performs it.**
2. **OPERATOR-1 / deployment** — no migration is needed by this stage, and no
   deployment has occurred.
3. **AUTH-1 / OPERATOR-3** — a live capture is still separately authorized.
   Until one runs, this surface will honestly report `no_active_snapshot` and an
   empty canonical catalog against a real database.
4. **Canonical field-level provenance** — deliberately out of scope. A bounded
   detail route over `get_canonical_catalog_variant` +
   `list_canonical_field_provenance` could answer *"which verified source
   supports this canonical field?"*, but it is a third response contract, a
   third UI view and a third test set, which would materially widen CODE-3. It
   is future operator-facing work.
5. **Index and plan behaviour** (`OPERATOR-4`) — the canonical page's
   `count=exact` and the candidate page's `total_count` are both exact counts
   over the matching set. Their plan behaviour at real catalog size is
   **unverified** and should be measured against a real snapshot.
6. **Operator UI verification** against a live browser
   ([FRONTEND_PRE_RELEASE.md](production-readiness/FRONTEND_PRE_RELEASE.md)).

## Execution-safety confirmation

**No production database was read or mutated. No migration was created or
applied. No deployment occurred. No flag was enabled. No request was sent to
`data.gov.il`. No capture run was prepared in a real environment. No paid model
call occurred. No canonical promotion occurred.** No Supabase MCP tool and no
gcloud tool was called; no Cloud Run, Vercel, IAM or Secret Manager
configuration was read or changed. `supabase/` is untouched, and `legacy/` and
`MILO-main-original/` were not modified.

**CODE-3 proves no production catalog row exists.** Every row in every test and
in the E2E stack is in-memory, in an ephemeral local PostgreSQL, or derived from
the committed R5 capture fixtures. Whether a durable snapshot exists in the
target database remains unproven either way — Gap Audit row `S3-02b`.
