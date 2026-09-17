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

### Query parameters — a per-route allowlist, and everything else fails closed

The **set of parameter names** is part of the contract. A name outside the
route's allowlist is refused; a name stated twice is refused.

| Parameter | Canonical | Review | Semantics |
| --- | :---: | :---: | --- |
| `limit` | ✓ | ✓ | whole number in `[1, 100]`; default `25` |
| `offset` | ✓ | ✓ | whole number in `[0, 1 000 000]`; default `0` |
| `manufacturer` | ✓ | ✓ | exact match, ≤ 120 characters |
| `commercial_model` | ✓ | ✓ | exact match, ≤ 120 characters |
| `model_year` | ✓ | ✓ | whole number in `[1900, 2200]`; selects rows whose stated range CONTAINS it |
| `canonical_key` | ✓ | | exact match, ≤ 120 characters |

Anything else — including `order`, `sort`, `select`, `table`, `columns`,
`status`, `snapshot_key`, `snapshot_id`, `resource_id`, `allow_incomplete`,
`p_limit`, `p_status`, `q`, `filter`, `where` — is `400
CATALOG_REVIEW_QUERY_PARAMETER_UNSUPPORTED`. The message is static and authored
in `backend/catalog/review.py`; it echoes neither the name nor the value, so the
refusal cannot be used to enumerate what the server recognises and cannot
reflect a caller's input.

**Why refusal rather than silence.** The first implementation declared its
parameters and let Starlette discard the rest, and its tests asserted that an
undeclared parameter returned the same page as omitting it. Independent review
rejected that, correctly: "never read" and "refused" are not the same thing to
the person reading the answer. `?snapshot_key=X` returned the ACTIVE snapshot
while the operator believed X had been inspected, and `?status=promoted`
returned `ready_for_review` rows under a heading nobody asked for. Several of
those names are real query controls one layer down — `p_limit` and `p_status`
are arguments of `catalog_candidate_variant_page`, `allow_incomplete` is the
acknowledgement that lets an incomplete snapshot answer at all — which is
exactly why silence was the wrong answer.

**A repeated parameter fails closed.** `?limit=10&limit=20` is ambiguous. No
repository-wide policy defines a reviewed behaviour for a repeated query
parameter (`after_event_id` on the run-events route is the only other query
parameter in the API and states none), so there is nothing to follow, and
selecting the first or the last would answer a question the caller did not
unambiguously ask.

**Neither handler declares its query parameters**, and that is load-bearing. A
declared `limit: int` is bound and validated by FastAPI *before* the handler
body runs, so `?limit=abc` would answer `422` before the membership check — and
a non-member would then receive a different response depending on what they
sent, which is a disclosure through the authorization boundary. Reading the raw
query string inside the handler is what keeps the four steps in order:

1. authenticate;
2. authorize membership (the non-disclosing 404);
3. validate the query contract (names, duplicates, then values);
4. read the catalog.

**A numeric value is bounded before it is converted, and the parser is total.**
`limit`, `offset` and `model_year` are matched against one anchored pattern —
an optional single leading minus and at least one ASCII digit — and the raw
text is length-checked against `MAX_REVIEW_NUMERIC_CHARS` (16) *first*, so the
conversion is only ever reached on text already proven convertible.

The earlier parser assembled its rule out of `str` predicates
(`raw.isascii() and raw.lstrip("-").isdigit()`), and those do not agree with
`int()` in either direction. Two inputs escaped it as an unhandled
`ValueError`, which is a `500`, not a refusal:

| Input | Why it escaped |
| --- | --- |
| `?limit=--5` | `lstrip("-")` strips *every* leading minus, so `'5'.isdigit()` passed and `int('--5')` then raised |
| `?limit=` + 5 000 digits | every predicate passed; `int()` refuses an integer literal past `sys.int_info.str_digits_check_threshold` (4 300 digits) |

Both now return `400 CATALOG_REVIEW_PAGE_INVALID` (`model_year` keeps its own
`CATALOG_REVIEW_FILTER_INVALID`) with a static message that echoes no input.
A `500` here would also have been a disclosure: the two refusals a non-member
and a member receive must not differ, and an unhandled exception is a third
response shape that only the malformed request produces.

16 characters is chosen to sit far above every domain this surface accepts — a
page size is at most 3 digits, an offset 7, a model year 4 — so a plainly
out-of-range value such as `?offset=999999999` still reaches its honest
**range** refusal rather than being reported as malformed text; and far below
any parser threshold.
`test_the_numeric_bound_is_far_above_every_accepted_domain` pins both sides,
and `test_the_parser_itself_is_total` drives the parser directly over the
hostile inputs above.

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

### Redaction happens on the way OUT of the API, not only in the browser

Every projected string passes `backend.redaction.redact_secret_text` before the
response is serialized, in `review._text()`, ahead of the 120-character bound
(truncating first could split a credential across the bound and leave a fragment
the patterns no longer match).

Independent review required this, and the reasoning is worth stating: the
frontend parser redacts before it renders, but by then the response has already
been delivered to the browser. It has sat in the network panel, in whatever
proxies and extensions observe traffic, and in any log that captured it. *Hidden
from the DOM is not never sent*, and only the server can make the second
statement true. `frontend/lib/catalogReview.ts` remains the second, independent
defense.

It **redacts** rather than rejects. The repository's other secret boundary,
`safe_fragment_text` / `_FRAGMENT_SECRET_MARKERS`
(`backend/engines/swarm_v2/evidence.py`), guards PERSISTENCE and rejects — a
caller writing a credential into durable evidence is a bug that should stop. A
READ surface is the opposite case: refusing a whole page because one stored trim
happens to look like a token would be a denial of inspection on the surface an
operator reaches for during an incident. Over-redaction is the intended failure
direction.

That marker vocabulary is deliberately left exactly where it is — it is named in
a migration's comment, and moving it would mean editing `supabase/`. The two are
pinned together by test instead: every marker it names must be something the
response-boundary redactor also neutralizes, so a value the durable boundary
would refuse can never be the value a response prints. A second test holds the
backend and browser redactors to the same sentinel set.

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

## 10a. Which catalog response may become visible state

The browser can have more than one catalog read in flight — switching views,
switching projects, closing and reopening the panel, retrying after a failure,
and turning pages all issue one — so "which answer wins" is a correctness
question, not a detail.

Every request carries a monotonic identity from `beginPending`
(`frontend/lib/ownership.ts`), wrapped with the `view` and `offset` it stands
for. The token is recorded **synchronously**, before the call is issued, exactly
as `scope.current` is. An answer may write state only if BOTH hold:

1. it is still the newest catalog request (`pending.id` matches), and
2. the workspace scope it was issued under still owns the surface
   (`ownsProject`).

Both are needed. The identity is what a project-scope check cannot provide —
two requests inside one project share a scope — and the scope check is what
catches a project switch made while the panel is closed, where no superseding
request is issued and the token would otherwise still match.

A superseded request is a **complete no-op** on every path: it writes no page,
sets no error, and does not clear the loading state its replacement set.
`frontend/tests/catalogReviewRace.test.tsx` drives each interleaving with
deferred promises, so the ordering is written down rather than timed.

**An intent invalidates the request in flight synchronously, in the same event
as the click.** Recording the new token is the effect's job, and an effect runs
*after* the render the state change causes — so between "the operator turned the
page" and "the replacement request exists" there is a window in which the token
of the request now being replaced is still the newest. An answer settling in
that window would pass the identity check and paint a page the operator had
already navigated away from.

So every catalog intent — turning a page (`changeCatalogOffset`), switching
views (`changeCatalogView`), opening or closing the panel (`changeCatalogOpen`),
and clearing the surface (`clearCatalogState`) — calls `endCatalogIntent` first,
which drops the current token in the same synchronous turn as the click. The
window is then owned by nobody: the old answer no longer matches and the new one
does not exist yet, so both are no-ops. `ownsProject` remains as defence in
depth rather than the check that catches this.

Holding that window open is itself the hard part of testing it: React Testing
Library's `fireEvent` is wrapped in `act()`, which flushes passive effects
synchronously, so the replacement token exists again before the next assertion
can run. The five tests in *"a new intent invalidates the old answer BEFORE the
effect runs"* put the click and the resolution inside **one outer
`await act(async () => { … })`** scope, which defers the flush, and assert
`canonicalCalls` has not grown inside that scope — so each test proves the window
it claims to test was genuinely open. One of the five is labelled in the file as
a **guard rather than a discriminating proof**: an old *failure* settling in the
window is masked in every reachable interleaving by the replacement's own
`setCatalogError('')`, so that test would pass without the fix and is kept to
pin the behaviour, not to demonstrate it.

There is deliberately no `AbortController` as a correctness boundary.
Cancellation races too, and a request already past the wire still settles;
aborting could only ever be an optimization.

A page already on screen stays visible while the next one loads, with the region
marked `aria-busy`. Blanking the table on every page turn hid what the operator
was reading and removed the pagination control mid-turn.

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
