# Catalog PR2 — deterministic Government ingestion

> **Superseded in part by Catalog PR3.** Everything below describes what PR2
> established and remains accurate about the CAPTURE path. Three statements are
> no longer true of `main`: the production `ToolRegistry` is no longer empty
> (it registers `catalog.government_vehicle`), the canonical catalog is no
> longer unwritable (it is writable only through one lease-guarded promotion
> RPC with a verified verdict per promoted field), and §11's "deferred to PR3"
> list is delivered. See `docs/catalog-pr3-swarm-and-promotion.md`.

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
| Canonical rows after ingestion | **zero**, and unwritable by every role at PR2. Catalog PR3 opens the canonical pair to `INSERT` only, behind field-level verified provenance |
| Production tool registrations | **zero at PR2** — `ToolRegistry()` was still constructed empty. Catalog PR3 registers one |
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
| Package | `degem-rechev-wltp`, allowlisted and checked **before** the transport is invoked |
| Resources | `142afde2-6228-49f9-8a29-9b6c3a0cbe40` (WLTP models — identity material), `5e87a7a1-2f6f-41c1-8aec-7216d52a6cf6` (quantities by manufacturer/model/production year) |
| URL construction | built by `action_url` from the closed action allowlist |
| Identity checks | `require_allowed_package` and `require_allowed_resource` run **first**, so an identity this code may not read is never asked for; the response is then held to the same identity independently |
| Dataset metadata | always read through `package_show`; `ResourceMetadata` is a **result** of that path and can never be supplied by a caller |
| Paging | the client chooses its own offsets at a fixed page size (`DEFAULT_PAGE_LIMIT = 100`, hard ceiling `MAX_PAGE_LIMIT = 1000`); a caller may supply only `q` and/or `filters` |
| Bounds per capture | `MAX_PAGES_PER_CAPTURE = 200`, `MAX_RECORDS_PER_CAPTURE = 120 000`, `MAX_RESPONSE_BYTES = 8 MiB`, one row bounded by the durable `MAX_RAW_PAYLOAD_CHARS = 16 384` |
| Timeouts | `CONNECT_TIMEOUT_SECONDS = 10.0`, `READ_TIMEOUT_SECONDS = 30.0` — finite and separate |
| Retry | `MAX_ATTEMPTS_PER_REQUEST = 3` total, backoff `(1.0, 4.0)`, only for a network failure, HTTP 429 and 500/502/503/504 |
| Redirects | never followed; a response whose final URL is not on the approved scheme, host and `/api/3/action/` path is refused |
| Credentials | none exist on this path — no token, no cookie, no `Authorization` header, and `trust_env` is off on the session |
| Provider/model calls | none anywhere in the package (asserted by test over every module) |

### What is and is not caller-controlled

Stated exactly, because the earlier wording ("no caller-controlled query
parameter") was an overclaim:

| | |
| --- | --- |
| **Not caller-controlled** | the URL, the scheme, the host, the path, the CKAN action, the package and the resource — each from a closed allowlist checked before the transport is invoked |
| **Not caller-controlled** | `limit` and `offset`. Paging is server-owned, because the completeness gate is arithmetic over the offsets the client chose |
| **Caller-selectable, bounded** | `q` and `filters`, and nothing else. `_validated_query` refuses any other key, bounds the value, and requires every page to echo it back in both directions |

Every value is handed to the transport as a **parameter** and encoded by the
HTTP client. Nothing concatenates a value into a URL —
`canonical_request_url` percent-encodes for provenance only and is never the
string the transport is given — and nothing on this path builds SQL at all.

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
all); the reported total, identical on every page; the estimation flag (below);
`records_format`; the row count the page's position implies; the declared field
schema, identical on every page; every row an object; every row's `_id`.

**`total_was_estimated` is a strict JSON boolean.** An estimated total cannot
gate completeness, so the flag is part of the response contract and is read by
type as well as by value:

| Value | Outcome |
| --- | --- |
| absent | **accepted** — CKAN omits the key on responses that did not estimate, so refusing an absent key would refuse the ordinary case. Absence means the server made no estimation claim, and the gate then rests on the total alone |
| JSON `false` | accepted — the server states the total is exact |
| JSON `true` | refused, `GOV_TOTAL_ESTIMATED` |
| `1`, `0`, `"true"`, `"false"`, `null`, `{}`, `[]`, anything else | refused, `GOV_TOTAL_ESTIMATION_INVALID` — a server answering any of these is not answering this contract, and it is refused as malformed rather than as an estimate |

A malformed flag on the first page ends the capture there; no later page is
requested and nothing is persisted.

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

## 8. The internal trust boundary, stated plainly

`DataGovClient.capture_resource` is the only thing in this package that turns a
response into a `ResourceCapture`, and it is where the guarantees live: the
allowlists, the `package_show` publisher/identity/version checks, the per-page
echo checks and the completeness gate.

`GovernmentCatalogIngestor.ingest_capture` **trusts its argument**. It is an
internal seam — it exists so a capture can be taken once and landed without a
second transport — and a `ResourceCapture` built by hand is a Python object with
the right fields and nothing more: not remotely verified, not cryptographically
attested, and carrying no evidence that its digests were ever computed over
bytes a server sent. Callers that need the guarantees call `ingest_resource`.

The digests in this document describe **what was captured**, so that two
captures can be compared and a replay recognised. They are not an attestation
that the capture came from `data.gov.il`; that comes from the validated path
above, and from nowhere else.

## 9. An active snapshot states its own reading gap

An active snapshot may legitimately hold rows this catalog could not read: a
code/label contradiction is the register disagreeing with itself, and inventing
an identity for such a row would be worse than not reading it. What must never
happen is that the gap disappears.

**The gap is durable.** Normalization runs over the whole capture BEFORE the
snapshot is opened, so the summary the snapshot carries and the candidates the
database receives come from one computation of one pure function. The snapshot's
`retrieval_metadata` then carries, permanently:

| Field | Meaning |
| --- | --- |
| `normalization_contract` | `gov.wltp.normalize.1`, or `raw_only` for a resource with no reviewed identity normalization |
| `normalized_record_count` | rows that produced a candidate |
| `normalization_issue_count` | rows that could not be read — always exact |
| `normalization_issues` | `[{reason, count}, …]` per reason code, sorted |
| `normalization_issue_records` | the refused ids, sorted, bounded to `MAX_DURABLE_ISSUE_RECORDS` (10) |

Every report — first write, exact replay, cross-run reuse — reads these back
off the snapshot rather than restating what the caller just computed, and a
replay whose freshly computed summary disagrees with the stored one fails closed
with `GOV_SNAPSHOT_NORMALIZATION_DRIFT` rather than reusing a snapshot that was
read under different rules.

**And the stored summary is PARSED, never taken on trust.**
`parse_normalization_state` refuses a snapshot whose recorded reading is
missing, mistyped, out of range or internally inconsistent — both counts must be
JSON integers (a boolean is not one), they must sum to the snapshot's own
`stored_record_count`, every reason must be in the refusal vocabulary with a
positive count and no repeats, the counts must sum to the issue count, and the
bounded id list must be exactly as long as the issue count implies. Zero issues
means both lists are empty. After the rows are read, the candidates must match
too: as many as the summary claims, each naming a **distinct** raw record **of
this snapshot** — so a dropped candidate cannot hide behind a duplicated one.
Every failure is `GOV_PROJECTION_SNAPSHOT_STATE_INVALID`; no `KeyError` or
`ValueError` escapes this layer.

`allow_incomplete=True` acknowledges a real, consistently recorded gap. It never
reaches malformed or self-contradicting state, because nothing about such state
can be relied on — including the count that would be acknowledged.

**An incomplete snapshot is not usable.** The projection answers from the newest
snapshot that is active AND free of unresolved issues, so a newer capture that
is raw-complete but semantically incomplete does not displace the last usable
one. Reading it takes `allow_incomplete=True`, and then the gap travels on every
answer. That acknowledgement covers exactly one refusal:

| Refusal | Acknowledgeable? |
| --- | --- |
| `GOV_PROJECTION_SNAPSHOT_INCOMPLETE` | **yes** — a real capture with a stated, counted gap |
| `GOV_PROJECTION_RESOURCE_NOT_NORMALIZED` | no — a raw-only resource states no vehicle identities at all |
| `GOV_PROJECTION_SNAPSHOT_NOT_READ` | no — a snapshot that records no reading cannot say what it is missing |

**`ambiguous` is not a gap.** An unknown but non-contradictory coded dimension
still produces a candidate; the reading says which dimension it could not settle
and the snapshot stays fully usable. Only a hard refusal — a contradiction, a
missing marque, model or year, an over-long identity — counts as an issue.

**The quantity resource is raw-only, by contract.** Its rows are captured,
stored and preserved; nothing reads them for identity, so it produces no
candidate and no issue, its snapshot records `normalization_contract:
"raw_only"`, and asking the projection for a tree from it is
`GOV_PROJECTION_RESOURCE_NOT_NORMALIZED`. That is a stated contract rather than
233 failures.

**The arithmetic that makes "silently" impossible:** for every active snapshot,
`stored_record_count − candidates == normalization_issue_count`. A raw record
with neither a candidate nor a durably counted issue cannot exist.

**What the metadata bound drops, if anything.** The durable metadata is bounded
at 4096 characters. The normalization summary is never what gives way — it
decides whether a snapshot may answer at all. The per-page checksum list is:
carried verbatim while the page count is within `MAX_INLINE_PAGE_CHECKSUMS` (16,
fixed by measuring a worst-case summary) and the finished object fits, and
otherwise dropped **whole** — never truncated, since a truncated list would be a
snapshot claiming page provenance it does not carry. `page_chain_sha256` commits
to every checksum either way.

## 10. The internal query layer (for PR3)

`backend/catalog/government/projection.py` is a service/query component: a plain
class with typed methods. **It is not a `Tool`** — no operations mapping, no
input or output schema, no required scope, no registration. It reads active
validated snapshots only, orders deterministically in Python (codepoint order,
never the database's text collation, because the identity text is Hebrew),
paginates explicitly with server-owned bounds, returns provenance with every
result, and returns ambiguity rather than collapsing candidates:
`resolve_variant` answers with one variant or with every match it found, and
never picks a first row.

A pinned `snapshot_key` is resolved by an EXACT repository lookup —
`find_active_catalog_snapshot`, an equality match on family, resource, key and
active state, capped at one row — never by searching the bounded newest-first
listing, which would make every active snapshot older than the listing bound
unreachable. The listing is used only to choose the newest usable snapshot, and
both repositories order it `activated_at DESC, snapshot_key ASC`.

A snapshot larger than `MAX_PROJECTION_CANDIDATES` (5 000) is a REFUSAL rather
than a silent truncation. Stated plainly: the pinned `q=RAV4` capture (233 rows)
fits easily; a capture of the whole ~101 000-row WLTP resource does not, and is
deliberately out of scope for PR2 — answering it needs database-side aggregation
and ordering, which belongs with the tool that will consume it.

## 11. What remains fixture-only, and what is deferred

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

**Stated limitations.**

* The unpinned snapshot search covers the bounded newest-first listing (50
  rows), so a usable snapshot sitting behind more than 50 unusable ones is not
  found by it. This is deliberate — an unbounded scan is not a read this layer
  performs — and such a snapshot is still reachable by name through the exact
  lookup, which has no such bound.
* `MAX_PROJECTION_CANDIDATES` (5 000) bounds one snapshot's projection; a whole
  ~101 000-row WLTP capture exceeds it and is refused rather than truncated.
* The durable issue list names at most 10 refused rows individually. The count
  and the per-reason breakdown are always exact.

**Not proven here.** Complete production coverage of Israel. The tests exercise
one bounded query of one resource. The code is generic enough for bounded
complete pagination of a whole resource; that has not been run against the live
service from this repository.
