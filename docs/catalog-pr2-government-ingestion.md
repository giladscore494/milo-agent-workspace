# Catalog PR2 — deterministic Government ingestion

**Scope: ingestion only.** This PR reads the Israeli Ministry of Transport's
`degem-rechev-wltp` dataset from `data.gov.il` and writes into the relations
Catalog PR1 established. It connects nothing to Swarm V2, registers no tool,
and promotes nothing to the canonical catalog.

Written so a reviewer never has to infer which parts are real. Where something
is fixture-backed rather than production-exercised, it says so.

## 1. What this PR establishes

| | |
| --- | --- |
| Catalog PR1 | merged as `e8a540b11f6befb762ad071a5629828eab1d0000` (PR #86, reviewed head `73c27a9aaf0c30bc9c8bd6e85c210525f3c3d792`) |
| Base of this PR | `e8a540b11f6befb762ad071a5629828eab1d0000` — branched directly from `origin/main`, not stacked on the PR #86 branch |
| New package | `backend/catalog/government/` |
| New migration | `20260915180000_catalog_raw_record_source_locator.sql` |
| Canonical rows after ingestion | **zero**, and still unwritable by every role |
| Production tool registrations | **zero** — `ToolRegistry()` is still constructed empty |
| Live syncs activated | **none** — no production entrypoint constructs a transport |

The stored data allows deterministic reconstruction of
`manufacturer → model → years → candidate variants`, and every candidate remains
traceable to the exact Catalog snapshot, the Government resource id, the
upstream Government `_id`, the source version, the raw record, and a precise
page/record locator.

## 2. The Government source, and its bounds

`backend/catalog/government/source.py` holds every bound as a server-owned
constant. None is environment-tunable, none is derived from model output, and
none is reachable from run input.

| | |
| --- | --- |
| Scheme / host | HTTPS only, `data.gov.il`, bare hostname comparison (never a suffix match) |
| Actions | `package_show`, `datastore_search` — a closed allowlist; both reads |
| Package | `degem-rechev-wltp` |
| Resources | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` (WLTP models — identity material), `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6` (quantities by manufacturer/model/production year) |
| URL construction | built by `action_url` from the closed action allowlist; there is no caller-controlled URL and no caller-controlled hostname anywhere in the package |
| Paging | the client chooses its own offsets at a fixed page size (`DEFAULT_PAGE_LIMIT = 100`, hard ceiling `MAX_PAGE_LIMIT = 1000`); a caller may supply only `q` and/or `filters` |
| Bounds per capture | `MAX_PAGES_PER_CAPTURE = 200`, `MAX_RECORDS_PER_CAPTURE = 120 000`, `MAX_RESPONSE_BYTES = 8 MiB`, one row bounded by the durable `MAX_RAW_PAYLOAD_CHARS = 16 384` |
| Timeouts | `CONNECT_TIMEOUT_SECONDS = 10.0`, `READ_TIMEOUT_SECONDS = 30.0` — finite and separate |
| Retry | `MAX_ATTEMPTS_PER_REQUEST = 3` total, backoff `(1.0, 4.0)`, only for a network failure, HTTP 429 and 500/502/503/504 |
| Redirects | never followed; a response whose final URL is not on the approved scheme, host and `/api/3/action/` path is refused |
| Credentials | none exist on this path — no token, no cookie, no `Authorization` header, and `trust_env` is off on the session |
| Provider/model calls | none anywhere in the package (asserted by test over every module) |

`backend/catalog/government/transport.py` is the **only** module that can open
a socket. A test asserts every other module in the package names no HTTP
library, no socket, no Supabase client and no provider SDK — so the whole
capture path is offline by construction, not by convention.

## 3. Pagination and the failure policy

A capture is COMPLETE or it is a refusal. The R5 Government round paid for this
lesson: every page of a CKAN query honestly reports the full total, so 200 rows
of a 233-row query look exactly like a complete query whose rows happen to
number 200. Completeness is therefore a property of the whole result set and is
validated before a single row is offered to a caller, and before any durable
write.

Validated, per request: HTTP status; JSON content type; response size; the
final URL's host; CKAN `success`; the `result` shape.

Validated, per page: the echoed resource id; the echoed page size; the echoed
offset; the echoed `q`/`filters` **in both directions** (a parameter that was
sent must come back unchanged, and one that was not sent must not come back at
all); the reported total, identical on every page and **not estimated**;
`records_format`; the row count the page's position implies; the declared field
schema, identical on every page; every row an object; every row's `_id`.

Validated, per capture: the page lengths sum to the reported total, and the
count of distinct `_id`s equals it too.

**The `_id` policy, stated once.** Every captured row must carry `_id` as a JSON
integer. A missing `_id`, a null, a float, a boolean, an array, an object and a
digit STRING are all refusals (`GOV_RECORD_ID_INVALID`), and an `_id` already
seen in the capture is a refusal (`GOV_RECORD_ID_DUPLICATED`). A row without a
usable register identity cannot be stored idempotently or pointed back at the
register; a row reachable twice would become two candidates for one vehicle —
an ambiguity manufactured by the pagination rather than stated by the register.

**What may be retried.** `DataGovClient._request` retries a network failure,
HTTP 429 and a transient 5xx, and nothing else. This is structural rather than a
matter of discipline: `_request` returns only after the transport, status, host,
media type, size and CKAN envelope have passed, and every schema, identity,
pagination and validation rule is applied by its CALLER, outside the retry loop.
A deterministic refusal therefore cannot be retried even by mistake, and a test
asserts the exact request count for each class.

Every refusal is a static, code-owned reason code (`GOVERNMENT_SOURCE_REASONS`)
carrying no URL, no body, no offset and no row.

## 4. The four digests, named apart

Confusing these is how a system ends up believing it verified something it did
not, so each is defined exactly once.

| Name | Where | Exactly what it is |
| --- | --- | --- |
| **Captured-response checksum** | `CapturedPage.body_sha256` | SHA-256 of ONE response body, byte for byte, as the datastore served it |
| **Page-chain digest** | `snapshot.page_chain_digest` | seed `sha256(b"gov.pagechain.1")`, then `accumulator = sha256(accumulator ‖ unhexlify(page_sha))` for each page checksum in capture order; 64 hex characters however many pages |
| **Snapshot identity checksum** | `content_sha256` on the snapshot row | SHA-256 of the canonical JSON manifest in `snapshot_content_basis`: `{contract: "gov.snapshot.1", source_family, package_id, resource_id, query, page_limit, reported_total, schema_fingerprint, pages: [{offset, limit, record_count, sha256}...]}`, sorted keys, compact separators, UTF-8. It deliberately EXCLUDES the retrieval time, so re-reading unchanged content replays onto the same snapshot rather than creating a new one on every run |
| **Stored raw-payload digest** | `catalog_raw_records.payload_sha256` | derived by PostgreSQL over its own `jsonb::text` rendering. Storage-local by design; no caller may supply it, and nothing in this package predicts it |

**Schema fingerprint.** `gov.schema.1:` followed by the SHA-256 of the compact
JSON array `[[field_id, field_type], ...]` in the order the resource declares
its fields. A function of the DECLARED SCHEMA alone — never of the rows, the
query or the byte count — so two captures of one resource at one schema
fingerprint the same however many rows each returned, and a resource that
gained, lost, renamed or retyped a column fingerprints differently.

**Upstream version.** Read from the resource's own `package_show` metadata, in
preference order: `revision_id` → `document_revision`, else `last_modified` →
`dataset_version`. A resource that publishes neither is **refused**
(`GOV_RESOURCE_UNVERSIONED`) rather than pinned to something this code invented.
The resource's published `hash` is kept as provenance and is explicitly not the
version: for this dataset it is an MD5 of the full CSV export, not of the JSON
the datastore serves.

## 5. Before and after

**Before (Catalog PR1).** Six relations existed, all empty. There was no HTTP
path, no client, no normalization and no query layer. Nothing could put a row
into `catalog_source_snapshots`, `catalog_raw_records` or
`catalog_candidate_variants` except a test calling a guarded RPC by hand.

**After (Catalog PR2).**

```
DataGovClient.capture_resource            (package_show + datastore_search,
        │                                  complete-or-refuse, bounded, retried
        │                                  only for transport/429/5xx)
        ▼
ResourceCapture                            (pages, checksums, schema, version)
        │
        ▼   ── nothing durable has been written yet ──
record_catalog_snapshot                    (pending; lease-guarded)
        ▼
record_catalog_raw_record  × N             (append-only, with page/index locator)
        ▼
record_catalog_candidate   × N             (one reading per row)
        ▼
activate_catalog_snapshot                  (LAST; the database's own
                                            stored == declared gate)
        ▼
GovernmentCatalogProjection                (internal, bounded, active-only)
```

The order is the safety property. Because activation is last and gated, a
crash, a cancellation or a lost lease at any point leaves a NON-ACTIVE snapshot,
and nothing downstream reads one — so an interrupted ingestion is invisible to a
reader rather than a smaller truth.

## 6. Normalization, and what it refuses

`backend/catalog/government/vocabulary.py` is the single definition of what the
register's fields mean; `backend/testing/r5_proof/government.py` now SELECTS the
subset it reviewed from it, so a change to the meaning of a shared code breaks
the R5 proof immediately instead of quietly. R5 keeps its own key set
deliberately: its registry tool is conservative, and widening a table it reads
could turn a settled unique match into an ambiguity.

Rules, in the order they bind:

1. **A code decides; its label is the cross-check.** `delek_cd` 7 must travel
   with `delek_nm` `חשמל/בנזין`; `hanaa_cd` 3 with `4X4`. Nothing is read from a
   substring, and nothing from a label while a code exists.
2. **A contradiction is a refusal.** A known code with a label it is not paired
   with means the register and this reading disagree about what the code MEANS.
   There is no conservative way to pick a side, so the row is refused and
   reported (`GOV_NORM_LABEL_CONTRADICTION`) — never read through the code and
   never through the label.
3. **The register's own `לא ידוע קוד` marker is an ABSENCE.** The register is
   stating it has nothing to say, so the dimension is an absent key and the
   reading is complete without it. Matched whole, never as a substring.
4. **A code this vocabulary does not name leaves the reading UNSETTLED.** The
   dimension is left unstated and the candidate is stored with status
   `ambiguous`, listing which dimensions could not be settled.
5. **Identity text is the register's own.** `tozar`, `kinuy_mishari`,
   `degem_nm` and `ramat_gimur` are stored exactly as written. The register
   publishes no code for the marque, so transliterating it would be inventing a
   name it never stated — `RAV4`, `RAV4 HYBRID` and `RAV4 PLUG-IN` stay three
   commercial models, because similarity is not identity.
6. **No row-level market.** Israeli scope belongs to the SOURCE and is recorded
   once, on the snapshot (`dataset_market_scope: "IL"`).
7. **One row is one candidate.** A model year with several trims is several
   candidates. Nothing picks a first row and nothing merges two.

Read and validated but NOT identity: `nefah_manoa` (homologated displacement in
cc). The closed candidate identity vocabulary has no displacement dimension and
inventing one would be a schema change made in a normalizer, so it travels on
the reading and is re-derivable from the preserved raw payload.

Captured and deliberately **not read at all**, each with a reviewer's reason in
code: `koah_sus` (the dataset publishes no definition, and across the captured
plug-in rows of one commercial model it takes both engine-scale and system-scale
values — its semantics are unresolved IN THE SOURCE, so it is never mapped to
horsepower), `dg_metach_solela`, `mishkal_kolel`, `automatic_ind` and
`sug_degem`.

A row that cannot be read is not lost: it is durable as a raw record either way,
and the refusal travels in the ingestion report rather than being inferable from
a missing candidate.

## 7. Replay, refresh and lease behaviour

Every durable write carries `run_id`, `worker_id`, `attempt` and `lease_token`,
and `assert_worker_lease` validates all four atomically before a byte is
written. There is no other way to reach the database from this package: no
direct insert, and no function or table name assembled from data.

* **Exact replay is a deterministic no-op.** The snapshot identity is a function
  of what was captured, so re-reading unchanged content derives the same
  `content_sha256`, `snapshot_key`, record keys and candidate keys. Observed:
  one snapshot, 233 raw records, 233 candidates, before and after.
* **A later run may REUSE an already-active identical snapshot.** It is returned
  by the idempotent snapshot write, recognised as another run's completed work,
  and left completely alone.
* **A later run may not adopt another run's UNFINISHED capture**
  (`GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN`) — a refusal, not an attempt that fails
  halfway.
* **Changed content is a new snapshot.** A different capture derives a different
  `content_sha256`, so a different `snapshot_key` and a different row; the
  previous snapshot and all its raw records are untouched.
* **A no-change refresh creates no duplicate ingestion**, and **a failed refresh
  never replaces the last valid active snapshot**, because it never reaches
  activation and never modifies the previous snapshot.
* **The same record cannot be duplicated under a different caller-selected
  key**: the key is derived from the object's structural identity, and a
  supplied key that disagrees is refused.
* **Candidate creation is idempotent**, and **no partial activation is
  possible** — the completeness gate is a CHECK constraint plus an RPC guard.
* **Cancellation leaves no active partial snapshot.**

**This is at-least-once delivery onto idempotent writes, not exactly-once.**
There is no exactly-once guarantee across the window between a durable write and
the checkpoint that records it; a crash inside that window re-executes the step
on resume. That is safe here for two specific reasons and only those: every
upstream call is read-only, and every durable write is idempotent on a key
derived from content.

## 8. The internal query layer (for PR3)

`backend/catalog/government/projection.py` is a service/query component: a plain
class with typed methods. **It is not a `Tool`** — no operations mapping, no
input or output schema, no required scope, no registration. It reads active
validated snapshots only, orders deterministically in Python (codepoint order,
never the database's text collation, because the identity text is Hebrew),
paginates explicitly with server-owned bounds, returns provenance with every
result, and returns ambiguity rather than collapsing candidates:
`resolve_variant` answers with one variant or with every match it found, and
never picks a first row.

A snapshot larger than `MAX_PROJECTION_CANDIDATES` (5 000) is a REFUSAL rather
than a silent truncation. Stated plainly: the pinned `q=RAV4` capture (233 rows)
fits easily; a capture of the whole ~101 000-row WLTP resource does not, and is
deliberately out of scope for PR2 — answering it needs database-side aggregation
and ordering, which belongs with the tool that will consume it.

## 9. What remains fixture-only, and what is deferred

**Fixture-backed.** Every test reads the committed R5 Government capture —
`q=RAV4&limit=100` over the WLTP resource, offsets 0/100/200, counts
100/100/33, reported total 233 — through the R5 manifest's checksum gate. No
test fetches, refreshes or imports a source, and a module-level fixture makes
creating a socket an error.

**Production behaviour that exists but was not activated.** `DataGovClient`,
`HttpsDataGovTransport`, `GovernmentCatalogIngestor` and
`GovernmentCatalogProjection` are production modules. No production entrypoint
constructs a transport, so no live capture can occur from a release; running one
is a deliberate, separate act.

**Deferred to PR3.** `GovernmentVehicleTool` and its registration; the tool
scope and grant; evidence mapping (source/claim/fragment/verdict) for Government
rows; the Government↔legacy crosswalk; Commander policy; controlled canonical
promotion with FIELD-LEVEL provenance; the scheduled refresh/diff operation; and
database-side aggregation for a whole-resource capture.

**Not proven here.** Complete production coverage of Israel. The tests exercise
one bounded query of one resource. The code is generic enough for bounded
complete pagination of a whole resource; that has not been run against the live
service from this repository.
