# Catalog PR3 — Government capability in Swarm V2, and verdict-gated promotion

**Scope: connection and promotion.** Catalog PR1 built the persistence, Catalog
PR2 built the deterministic ingestion, and this PR connects that capability to
Swarm V2 and opens the canonical catalog — behind a gate that makes a canonical
fact without exact verified evidence for that exact field impossible to commit.

**This completes the three-PR catalog workstream.** There is no Catalog PR4.
The next work returns to the pre-catalog MILO roadmap.

Written so a reviewer never has to infer which parts are real. Where something
is fixture-backed rather than production-exercised, it says so.

## 1. What this PR establishes

| | |
| --- | --- |
| Catalog PR1 | merged as `e8a540b11f6befb762ad071a5629828eab1d0000` (PR #86) |
| Catalog PR2 | merged as `ef32a5f02044617a5aed800c2e2f1be6296c8a7c` (PR #87, reviewed head `1b6f1aa656264b34d473cbc085c006452377d3f0`) |
| Base of this PR | `ef32a5f02044617a5aed800c2e2f1be6296c8a7c` — branched directly from `origin/main`, not stacked on the PR #87 branch |
| New migrations | `20260916090000_catalog_bounded_candidate_queries.sql`, `20260916120000_catalog_field_level_promotion.sql` |
| Production tool registrations | **one** — `catalog.government_vehicle`, read mode, eight bounded operations |
| Production evidence mappers | **one** — `catalog.government_vehicle.resolve_variant` |
| Canonical rows a release can create | only through one lease-guarded RPC, only with a verified verdict per promoted field |
| Live syncs activated | **none** — no production entrypoint constructs a transport or calls the refresh |

## 2. Before and after

**Before (Catalog PR2).** `GovernmentCatalogProjection` read a whole snapshot
into Python and refused anything past 5 000 candidates. `ToolRegistry()` was
constructed empty, `PRODUCTION_EVIDENCE_MAPPERS` was empty, the worker's
`tool_result_sink` was unwired, and `catalog_models` / `catalog_model_variants`
were `SELECT`-only for every role and immutable outright.

**After (Catalog PR3).**

```
active Government snapshot
        │
        ▼
catalog_candidate_* + bounded database-side aggregation   (PR3 migration 1)
        │
        ▼
GovernmentCatalogQuery                     (bounded, deterministic, paged)
        │
        ▼
GovernmentVehicleTool                      (read, one scope, closed schemas)
        │   registered in the trusted worker construction path only
        ▼
GenericWorker tool loop → ToolCallRecord   (server-resolved identity)
        │
        ▼
RegisteredOperationEvidenceSink            (registered operations only)
        │
        ▼
GovernmentVariantEvidenceMapper            (ONE operation, derived provenance)
        │
        ▼
versioned source + focused fragments + located claims   (R3, existing)
        │
        ▼
verified verdicts                                       (R4, existing)
        │
        ▼
catalog_candidate_evidence_links                        (PR1, existing)
        │
        ▼
promote_catalog_variant_guarded            (PR3 migration 2, lease-guarded)
        │
        ▼
catalog_canonical_field_provenance         (append-only, ONE row per FACT)
        │
        ▼
catalog_canonical_variant_current          (the authoritative read model)
```

## 3. The Tool, exactly

| | |
| --- | --- |
| Name | `catalog.government_vehicle` |
| Mode | `read`. There is no write operation, and no write tool is registered anywhere |
| Scope | `catalog:government:read` — one static, server-owned string, granted in `backend/worker/main.py` and nowhere else |
| Operations | `dataset_meta`, `list_manufacturers`, `get_manufacturer_summary`, `list_models`, `get_model_years`, `get_variants`, `resolve_variant`, `search_codes` |
| Page bound | `MAX_RESULT_ITEMS = 200`, mirrored by `catalog_page_limit()` in SQL. A caller asking for more gets 200 |
| Totals | every page carries the EXACT total of the filtered set, so `has_more` is a fact |
| Ordering | manufacturer, commercial model, year range, code, trim, candidate key — `collate "C"` in SQL and codepoint order in Python, so the two agree on any cluster |
| Provenance | every factual result carries the snapshot key, resource, package, publisher, market scope, upstream version and kind, content hash, schema fingerprint, activation time, stored record count, normalization contract and issue count |
| Raw material | `resolve_variant` alone quotes register FIELDS, only on a unique resolution, and only the seven reviewed identity fields of ONE row |
| Network | none. The tool holds a repository, not a `DataGovClient`; it cannot construct a transport |
| Descriptor size | 15 354 bytes against the 24 576-byte Commander prompt bound |

**No operation can return a complete raw resource.** There is no dump, no
"list every record", no free-text query, no caller-chosen ordering and no
caller-chosen resource: the resource, the pinned snapshot and the
incompleteness acknowledgement are constructor arguments of trusted wiring, not
operation inputs, so a model can neither widen its reach nor acknowledge its
own gap.

## 4. Database-side bounded queries

PR2 refused a Python projection past `MAX_PROJECTION_CANDIDATES` (5 000), and a
complete WLTP resource is around 101 000 rows. `20260916090000` adds the
aggregation that closes that, and **weakens nothing**: the Python projection
keeps its bound and keeps refusing beyond it. This is a second reader with a
different cost model.

| Function | Answers |
| --- | --- |
| `catalog_readable_snapshot(snapshot, allow_incomplete)` | the gate: active, `complete`, states a reading, and free of unresolved issues unless acknowledged |
| `catalog_candidate_manufacturers` | manufacturer → model count, variant count, ambiguous count |
| `catalog_candidate_models` | one manufacturer's commercial models, with first/last model year |
| `catalog_candidate_model_years` | one model's years, expanded from each candidate's stated range |
| `catalog_candidate_variant_page` | the row-level page, with every filter optional and the snapshot never optional |
| `catalog_raw_record_by_upstream_id` | ONE register row by its own `_id` |

All `SECURITY INVOKER`, `search_path` fixed to `pg_catalog`, every relation a
literal, every ordering fixed in the body, `EXECUTE` revoked from
`PUBLIC`/`anon`/`authenticated` before the narrow `service_role` grant. Three
indexes carry the filters and the ordering, created with the same `collate "C"`
the functions order by. No dynamic table name, no caller-supplied SQL, no
caller-chosen ordering and no unbounded materialization.

## 5. Evidence mapping, and Government field authority

**One mapper, one operation.** `catalog.government_vehicle.resolve_variant` is
the only Government read that ends with exactly one register row, so it is the
only one where "this exact row states this exact field" is true. The other
seven are coverage counts and listings; they record **nothing**.

**A registered mapper may DECLINE.** An ambiguous or empty resolution is a true,
useful answer with no single row to quote. `NO_EVIDENCE` is how the mapper says
so without inventing a fact and without failing the task that asked.

**Everything is derived, nothing accepted.** The source family and type, the
host and path (from `source.py` constants), the dataset and resource identity,
the immutable upstream version and its kind, the snapshot identity, the upstream
record `_id`, the exact record/field locator, the structured field identity and
scope, the focused projection text and the content hashes.

| Canonical field | Register field | Government authority |
| --- | --- | --- |
| `model_year_start`, `model_year_end` | `shnat_yitzur` | **yes** — the Israeli model year the register states |
| `official_model_code` | `degem_nm` | **yes** — the register's own model code |
| `trim` | `ramat_gimur` | **yes** — the trim as written |
| `identity_dimensions.fuel_type` | `delek_cd` (cross-checked by `delek_nm`) | **yes** — a reviewed coded dimension |
| reliability, faults, price, market value | — | **no**, and there is no branch that could emit one |
| `koah_sus` → horsepower | `koah_sus` | **never.** PR2 recorded the reviewer's reason; this mapper does not read the field, and the promotable vocabulary has no entry for it |

**Why exactly four fragment groups.** One durable source may carry at most four
focused fragments (`MAX_FRAGMENTS_PER_SOURCE`), and every promoted field needs
its own exact locator, therefore its own fragment. The remaining reviewed
dimensions — body style, drivetrain, propulsion technology — are stated by the
register, are carried on the candidate, and travel as the fact IDENTITY that
scopes every claim. They are **not** promotable canonical fields in this PR, and
the promotion refuses a canonical row that states one without its own verified
provenance rather than dropping it silently: `PromotionPlan.unsupported_fields`
reports exactly which ones were left out.

**Every claim is scoped to the VEHICLE.** `entity_key` is
`<canonical model key>:<model year>`, so a Web claim about the same car meets a
Government claim in one conflict scope instead of passing beside it. Each fact
additionally carries the closed identity dimensions the record stated, so two
trims of one model year are never compared as one vehicle.

## 6. Field-level promotion, and the refusal matrix

`catalog_canonical_field_provenance` holds ONE append-only row per promoted
fact, carrying: canonical model and variant, field key, field value, revision,
candidate, evidence link, snapshot, source, claim, verified verdict, run,
worker id and attempt, source version and kind, exact record locator,
idempotency (promotion) key and creation time.

**Two triggers, and they hold for every writer.** `service_role` holds direct
DML on these relations and the RPCs are `SECURITY INVOKER`, so "the repository
goes through the RPC" is true and is not an enforcement:

* `catalog_check_field_provenance` (BEFORE INSERT) holds each fact to its whole
  support chain and DERIVES its source, claim, verdict, version and locator from
  the cited evidence link;
* `catalog_require_field_provenance` (DEFERRED constraint trigger) refuses to
  commit a canonical variant whose stated fields are not all covered by
  revision-1 provenance, and holds the PR1 row-level back-pointer to being a
  verdict and a candidate from the row's own provenance;
* `catalog_require_model_variant` (DEFERRED) refuses a bare canonical model.

Promotion fails closed when:

| Condition | Refused by |
| --- | --- |
| the source family is `legacy_reference` | the trust state pinned to the family, the link RPC (no verdict on an unverified snapshot) and the provenance trigger |
| the snapshot is inactive, incomplete or incompatible | `catalog_readable_snapshot` and the provenance trigger |
| the candidate is `candidate`, `ambiguous` or `rejected` | the promotion RPC and the provenance trigger |
| the evidence link belongs to another candidate or snapshot | the provenance trigger |
| the claim, source, verdict or run do not agree | the link RPC and the provenance trigger |
| the verdict is not exactly `verified` | the link RPC and the provenance trigger |
| the claim states a different field, value or scope | the provenance trigger |
| a stated field has no evidence | the promotion RPC, the deferred trigger and `build_promotion_plan` |
| two verified sources leave an unresolved conflict | the provenance trigger (`conflicts.outcome = 'unresolved_needs_review'`) |
| a stale worker, attempt or token calls the RPC | `assert_worker_lease`, the first statement |
| an idempotency key is replayed with different content | the promotion RPC's stored-vs-requested comparison, and the derived key in `prepare_promotion` |
| an existing canonical identity would be overwritten | no UPDATE privilege, the immutability trigger, and the unique canonical key |

**Which relation states "the current canonical value".** ONE answer, and it is
the view: `public.catalog_canonical_variant_current`, assembled from the highest
revision of each promoted field. `catalog_model_variants` is the canonical
IDENTITY, frozen at revision 1 — its columns are what the promotion that created
it stated, they can never be updated, and a revision-1 provenance row must equal
them exactly. The two are not two definitions of one thing: the table says WHICH
vehicle and what was first established, the view says what is currently
believed, and a constraint keeps them identical where they overlap.

A later, better source revises a fact by APPENDING revision 2. The identity
fields (`model_year_start`, `model_year_end`, `official_model_code`, `trim`) are
part of the canonical variant key, so a revision of one of them is a different
variant by construction; only `identity_dimensions` is revisable in place, and
for it the view is the only place a reader should look.

## 7. Reconciliation with the legacy reference

One deterministic layer, four tiers and no others: an exact official model code,
an exact normalized identity with overlapping model years, an explicit reviewed
alias rule, then `ambiguous`. `normalize_identity_text` is Unicode NFKC, case
folding and whitespace collapsing — **no** transliteration, stemming, edit
distance, token overlap or prefix match. `NEW TUCSON` and `TUCSON` do not match,
and `RAV4`, `RAV4 HEV` and `RAV4 PLUG-IN` stay three commercial models.

Result states: `matched`, `ambiguous`, `under_enriched`, `government_only`,
`legacy_only`, `missing_years`. Every result states its method and the exact
candidate identifiers on both sides. A tier that finds several government
candidates returns all of them as `ambiguous`; it does not return the first.

**This release ships zero alias rules.** `REVIEWED_ALIAS_RULES` is empty: an
alias rule is exactly the judgement this layer refuses to make on its own, so
the mechanism exists and is tested and carries no entries until a reviewer adds
one. Two rules pointing one legacy identity at two government identities is a
refusal, not a choice.

**The legacy JSON is not imported, read or parsed by anything in this PR.** The
legacy side is `legacy_reference` candidate rows that were landed through the
same guarded write path as everything else, or an explicit bounded input. A
`legacy_reference` snapshot can never carry a verdict and can never support a
canonical fact, and a reconciliation result is a WORK ITEM, never a fact and
never a promotion.

## 8. Swarm V2 policy and wiring

`SOURCE_FIRST_TOOL_POLICY` adds a compact, server-owned rule set to the
provider-visible plan policy, keyed by the REGISTERED tool name so it appears
exactly when the capability does:

* consult `catalog.government_vehicle` before planning broad web research for
  Israeli existence, model-year coverage or official model code;
* if the register settles it, do not add a web discovery task for it;
* use targeted research only for a gap, an enrichment the register does not
  define, or a contradiction;
* an ambiguous resolution stays ambiguous — plan a targeted task, never a merge;
* the register is not authority for reliability, faults, price or market value.

It is **not** a workflow. There is no fixed Government → legacy → Web sequence,
no fixed category list and no fixed task decomposition; the taxonomy and the
task graph stay entirely the Commander's. Every existing plan bound, repair
limit, budget rule and deterministic firewall behaviour is unchanged.

**Promotion is not a Tool.** It is a lease-guarded repository RPC that trusted
server code calls, so `write_approved` and a `tool:write:<name>` capability
never enter this path. The worker grants exactly one read scope, `write_approved`
stays `False`, and no capability is granted at all.

## 9. Refresh and diff

`GovernmentCatalogRefresh.sync_if_changed` reads the resource's published
version through one bounded `package_show` request and ingests only if it moved.

* **No change** → no capture, no snapshot, no durable write, and
  `research_required` is False.
* **Changed** → a full bounded capture lands as a NEW immutable snapshot. The
  previous one is untouched, because activation is the last step and an active
  snapshot is immutable — so a partial or failed refresh never replaces the last
  usable snapshot.
* **Diff** → bounded added / changed / removed candidates, compared on the
  candidate's COMPLETE stated identity (dimensions included, because the register
  publishes rows that differ only in them). Counts are always exact; the item
  lists are dropped WHOLE rather than truncated when they exceed the bound.

**Rollback, stated exactly.** There is no "active pointer" to move and this PR
does not invent one. A snapshot is active or it is not, an active one is
immutable, and raw history is never deleted — so rolling back means READING an
older snapshot again by pinning its `snapshot_key`, which every reader accepts.
Anything stronger is not something the existing immutable contracts permit.

**No schedule is activated.** Nothing registers a cron entry, a timer, a Cloud
Scheduler job or a background thread, and no production entrypoint calls this
operation. A test asserts that nothing in `backend/`, `scripts/` or
`.github/workflows/` names it.

## 10. What remains fixture-only, inactive or not production-proven

**Fixture-backed.** Every test reads the committed R5 Government capture —
`q=RAV4&limit=100` over the WLTP resource, offsets 0/100/200, counts
100/100/33, reported total 233 — through the R5 manifest's checksum gate. No
test fetches, refreshes or imports a source, and a module-level fixture makes
creating a socket an error.

**Production behaviour that exists but is not activated.**

* No production entrypoint constructs a `DataGovClient` transport, so no release
  can perform a live capture.
* `GovernmentCatalogRefresh` is never called from a production entrypoint and no
  schedule exists.
* `REVIEWED_ALIAS_RULES` is empty, so tier 3 of reconciliation never fires in a
  release.
* Nothing in the production worker calls `CanonicalPromotion`. The promotion
  path, its RPC and its gates are complete and proven against real PostgreSQL;
  which run promotes what, and when, is a separate reviewed decision.

**Not proven here.**

* Complete production coverage of Israel. The tests exercise one bounded query
  of one resource (233 rows). The code is generic enough for bounded complete
  pagination of a whole resource, and that has not been executed against the
  live service from this repository.
* The database-side aggregation has not been measured against a ~101 000-row
  snapshot. It is bounded and indexed for it; it has been exercised against
  snapshots of one and of 233 candidates.
* No paid model call, no live source capture, no deployment and no remote
  migration was performed by this PR.

**Memory-repository parity, stated exactly.** `MemoryRepository` mirrors the
RULES the database applies — the lease, the derived keys, the support chain, the
field/value gate, the coverage check and the replay conflict — and does not
reproduce PostgreSQL. Two protections are deliberately DATABASE-ONLY: the
DEFERRED timing of the coverage trigger (the in-memory implementation checks
coverage before it appends anything, so there is no window at all rather than
one that closes at COMMIT), and the concurrency semantics of the unique indexes
under simultaneous writers.

## 11. This completes the catalog workstream

Catalog PR1 (persistence), Catalog PR2 (ingestion) and Catalog PR3 (connection
and promotion) are the whole of the corrected catalog track. There is no PR4,
and the next work returns to the pre-catalog MILO roadmap.
