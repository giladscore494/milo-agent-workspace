# R5 — one real, read-only vehicle evidence proof

Status: **COMPLETE — all three real source families captured, pinned and
exercised through the real engine with zero model calls.**

This document describes what the R5 proof actually establishes, and is written
so that a reviewer never has to infer which parts are real. Where something is
not established it says so plainly rather than describing an intended end state.

Base commit: `220b5e6871b8d14fc8c647179363c3de1689f0e8` (merge of
[PR #82](https://github.com/giladscore494/milo-agent-workspace/pull/82), the merged
R4 state). `origin/main` still pointed at exactly that commit when this branch
was created.

## 1. Acceptance gate

| R5 requirement | State |
| --- | --- |
| Real pinned Yeda fixture, tool, mapper | **Done** |
| Real pinned `data.gov.il` fixture, tool, mapper | **Done** |
| Real pinned saved Web document, tool, mapper | **Done** |
| Whole path through the real engine with zero model calls | **Done** |
| Conservative variant identity; make+model+year cannot merge variants | **Done** |
| Three-source scenario, all three families participating | **Done** |
| One explicit real coverage gap | **Done** |
| One deliberately incorrect value rejected deterministically | **Done** |
| At least one verified field in the final result | **Done** (8) |
| `partial_success` / `partial_result` end state | **Done** (derived, not asserted) |
| Replay / checkpoint-resume idempotency | **Done** |
| No production registration, no write path, no migration | **Done** |

### How the two previously blocked sources were captured

`data.gov.il` and `www.toyota.co.il` are refused by this environment's egress
proxy with **HTTP 403 to `CONNECT`** — a policy denial before any request is
sent. They were therefore captured **outside** this environment by a
server-side capture application, and delivered as a signed-off archive:

| | |
| --- | --- |
| Archive | `milo-r5-source-capture-20260914T161110Z.zip` |
| Archive SHA-256 | `67736c3bb19f94b850c2a63dcac29559bc309bd11b290807af805362b0e65cc8` |
| Archive size | 192,981 bytes |
| Capture window (UTC) | `2026-09-14T16:11:10.024Z` → `2026-09-14T16:11:16.271Z` |
| Capture tool | `MILO-R5-streamlit-evidence-capture/2.0` |
| Capture manifest SHA-256 | `3c48a4d8545f5ab6404e4573920c0c1b18c9d5bea0da04cf79fea3c840ca309f` |
| Overall status | `ready_for_r5_bundle_review` |
| Requests | 11, all `GET`, all HTTP 200, all zero-redirect |
| Authentication | none — no API key, token, cookie or `Authorization` header |

The archive was treated as **untrusted input**. Before a byte was extracted it
was listed without extraction and rejected for absolute paths, `..` traversal,
symlinks, device files, executables, nested archives, excessive file count and
expanded size, and files outside one capture root. After extraction its own
`SHA256SUMS.txt` was verified, and then — independently of it — every entry's
byte count, SHA-256, HTTP status, redirect chain, host, scheme and
authentication flags were re-checked against its manifest, the Government
records were confirmed to preserve their original `_id`, and the derived
consolidations were confirmed reproducible from the raw pages. Only then was
anything imported: the Government responses byte-for-byte, and the Web response
as the deterministic visible-text projection described below.

The obsolete URL `https://www.toyota.co.il/models/rav4-plugin` returned HTTP
404 and was replaced by the official Toyota Israel archive pages. It appears
**only** as provenance explaining that 404; it is never requested, never a
canonical URL, and never a document this proof reads. A test asserts it appears
nowhere in the manifest or in any proof module.

## 2. The selected vehicle

**Toyota RAV4 Plug-in Hybrid — Israeli market.** Chosen from the real catalog
before any code was written, on four grounds:

1. **It makes conservative identity necessary rather than decorative.** The
   pinned catalog states **five** RAV4 variants covering model year 2021, and
   the register holds thirteen plug-in RAV4 rows, so make + commercial model +
   year genuinely does not identify a variant in either source.
2. **It has a real official archived-model page**, so vehicle identity and
   marketing status are attributable without inference.
3. **It carries a real, non-manufactured coverage gap** — see §9.
4. It is a high-volume Israeli-market model, so it is present in the government
   resource named in the roadmap.

| Dimension | Yeda catalog | Government register (`_id` 36327) | Toyota page |
| --- | --- | --- | --- |
| make | Toyota | Toyota (`tozar` = `טויוטה`) | Toyota |
| commercial model | RAV4 | RAV4 (`kinuy_mishari`) | **RAV4 Plug-in** |
| market | IL | IL (dataset scope) | IL |
| model year(s) | 2021–2026 | 2021 | not stated |
| body style | SUV | `suv` (`merkav`) | not stated |
| drivetrain | AWD | `awd` (`hanaa_cd` 3 / `4X4`) | not stated |
| engine | `2.5L` label | 2487 cc homologated | not stated |
| transmission | `cvt` | not published | not stated |
| official/model code | **absent** | `AXAP54L ANXMBK` (`degem_nm`) | not stated |
| trim | **absent** | `PRIME AWD SE` (`ramat_gimur`) | not stated |
| generation | **absent** | **absent** | not stated |

The three sources do **not** share one identity, and the proof does not pretend
they do. See §6 and §9.

## 3. Source, version and locator table

### Yeda — pinned aggregated catalog

| | |
| --- | --- |
| Source kind | `git_repository_file` |
| Repository / path | `giladscore494/reliabilityAIModelsR2` / `my-flask-app/app/data/model_technical_catalog_il.json` |
| Source version | `git_commit:f7bf132abf1b1ec8f9d81076560a5a618447a9e6` |
| Git blob SHA | `383ba9bf12ddfafeac79fe42ea449c11204dc017` |
| Upstream file SHA-256 | `0a8d0e4240cbf11ad908012168d9dd60cafc2c9f75b92caecbd73f126c349ad6` |
| Upstream catalog hash | `76d2b5fbaa141577` (the catalog service's own `get_catalog_hash`) |
| Canonical URL | immutable GitHub blob URL **containing the commit** — never `/blob/main/` |
| Retrieved (UTC) | `2026-09-09T14:06:59Z` |
| Record locator | `models[860]`, `technical_variants_il[4]` |
| Committed fixture | `yeda/rav4_model_record.json` (15,225 bytes) |
| Fixture SHA-256 | `a2ce25de43f315517a5584eed94658bd54de721b5d9cee791f9ba952b09ad8c6` |
| Fixture kind | `exact_record_subset`, `upstream_committed: false` — one record of 965; the 7.3 MB catalog is **not** committed |

### Government — Israeli Ministry of Transport vehicle-model register

Publisher `ministry_of_transport` (משרד התחבורה והבטיחות בדרכים), CKAN package
`degem-rechev-wltp`, resource `142afde2-6228-49f9-8a29-9b6c3a0cbe40`.

Source version is the **dataset's own** `last_modified`,
`dataset_version:2026-09-14T02:41:31.842626`, read from the captured
`package_show` metadata. The resource carries **no `revision_id` key at all**,
so none is recorded and none is invented. Its published `hash`
(`79ab5917935c722fa6de8460a594a778`) is kept as provenance and is deliberately
**not** the version: it is an MD5 of the full 53 MB CSV export, not of the JSON
the datastore API served. **No ETag and no Last-Modified header was returned**
on any request; that absence is recorded as explicit nulls and declared in
`absent_source_metadata`, which the manifest gate checks in both directions.

| Fixture | Bytes | Fixture SHA-256 = response SHA-256 | Query | Records used |
| --- | --- | --- | --- | --- |
| `government/package_show.json` | 7,821 | `4e8f30b9047f5600740542bcfebc0697d97d7a0edb1e26fb1fa97e9295c1721a` | `id=degem-rechev-wltp` | — (dataset scope + version) |
| `government/wltp_page_000001.json` | 250,013 | `e1160dc47222ead6f54cf6b3f8f1b65d50a0a4526ac659aea61f8db1b91b2036` | `q=RAV4&limit=100&offset=0` | `_id` **36327** at index 74 |
| `government/wltp_page_000002.json` | 249,979 | `e5b0bb90178191478cec8b1468818d6f3962cbc6146ca10188410bd72eeec6fb` | `q=RAV4&limit=100&offset=100` | `_id` **37392** (index 89), **37393** (index 90) |

Retrieved (UTC) `2026-09-14T16:11:10.360Z`, `…:11.739Z`, `…:12.858Z`
respectively. Requested URL and final URL are identical, over HTTPS, on
`data.gov.il`, with an empty redirect chain.

These are **`exact_response` fixtures** (`upstream_committed: true`): the
committed bytes are the response bytes, which is why `fixture_sha256` equals
`upstream_sha256` here and deliberately does **not** for the Yeda record subset
or the Web projection. The archive's third WLTP page, the whole `5e87a7a1-…`
resource and the derived consolidations hold no record this proof reads and are
**not** committed.

### Web — official Toyota Israel archived-model page

| | |
| --- | --- |
| Source kind | `saved_web_document`, `document_id` `toyota_il_rav4_phev` |
| Canonical URL | `https://www.toyota.co.il/cars/RAV4-PHEV` |
| Retrieved (UTC) | `2026-09-14T16:11:16.237Z` |
| Response SHA-256 (`upstream_sha256`) | `41ad54208a8ed274ddead7218ac305d3f0a4f928a5e966c688885c89b64fa61c` (356,019 bytes) |
| **Source version** | `content_sha256:41ad5420…` — the digest of the **exact full** body |
| Committed fixture | `web/toyota_il_rav4_phev.visible_text.txt` (17,458 bytes) |
| Fixture SHA-256 | `85606cf6c41e32bb683076f6308b562f464ce5399257052111d56e44df98f320` |
| Fixture kind | `deterministic_projection`, `upstream_committed: false` |
| Text projection | `r5.visible_text.1`, 10,541 characters |
| Absent | `etag`, `last_modified` — the response carried neither |

The page publishes no ETag, no Last-Modified and no site revision, so the
SHA-256 of the whole captured body is the only immutable identifier it has —
precisely the case `SourceVersion`'s `content_sha256` kind exists for. It is the
digest of the whole response, never of a quoted span, and it remains the source
version even though the whole response is not committed.

**The raw HTML is deliberately not committed.** What is committed is the page's
deterministic visible-text projection, recorded as a `deterministic_projection`
with `upstream_committed: false` — exactly as the 7.3 MB upstream Yeda catalog
is not committed while its digest is. Two reasons, in this order:

1. **The projection is the evidence surface.** Every document-span locator
   points into it; the markup around it supports no claim.
2. **The raw page carries the site's own client-side tokens** — Mapbox
   publishable (`pk.`) keys and a `pub`-prefixed analytics key that every
   visitor to the public site receives. GitHub push protection classifies them
   as secrets and refuses the push. Bypassing that protection to commit a third
   party's token is not a call a proof gets to make, and redacting bytes would
   break the digest chain anyway. Every one of those tokens lives inside a
   `<script>`, so the projection excludes them **by construction** rather than
   by editing, and a test asserts the committed file contains no token-shaped
   material at all.

**What this costs, stated plainly.** A reviewer working only from this
repository cannot re-derive the projection from the raw page, because the raw
page is not here; that check belongs to whoever holds the capture archive, whose
SHA-256 is recorded above and in the pull request. What *can* be checked inside
the repository is that the committed file is a **fixed point** of the same
versioned rule — projecting it again changes nothing — which an edited copy of a
page, or a file produced by a different rule, would not be. The importer
enforces it before writing, a test asserts it, and the tool refuses a committed
document that is not a fixed point.

## 4. Fixture capture and import method

`scripts/r5_capture_fixtures.py` is a **manual development action**. It refuses
to run when `CI`, `GITHUB_ACTIONS` or `PYTEST_CURRENT_TEST` is set, because a
refresh changes evidence and evidence must never change because a pipeline
happened to execute. **Tests and CI never fetch, refresh or import a source.**

* `yeda` performs no network access at all: an operator clones the public
  repository read-only, and the script verifies the checkout commit, the git
  blob SHA and the file SHA-256 before parsing a byte.
* `import-capture` imports the Government and Web fixtures from an extracted
  capture archive. It re-verifies the archive's `SHA256SUMS.txt`, its
  `overall_status`, its authentication flags, its failure lists, and then per
  entry the HTTP status, the validation result, the byte count, the SHA-256 and
  the host allowlist — before importing anything. Government responses are
  copied byte-for-byte. The Web response is verified against its recorded digest
  and byte count and then projected; the importer refuses a projection that is
  not idempotent, so a non-projection can never be committed as one. Every
  manifest entry is written from the archive's own recorded values, so no
  provenance field is transcribed by hand.

`backend/testing/r5_proof/manifest.py` is the gate at read time. Every proof
read re-hashes its fixture and re-checks its recorded `fixture_byte_count`
**before** the bytes reach a JSON decoder or a text projection, so a single
changed byte fails closed rather than producing different evidence under
unchanged provenance. `fixture_byte_count` and `response_byte_count` are
separate fields because they are separate objects for two of the three sources,
and `upstream_committed` says outright whether the repository holds the whole
upstream object or a bounded piece of it.
Fixture paths cannot escape the committed root. Absent response metadata is
machine-checked in both directions: a declared-absent key holding a value, and
a null nobody declared, are equally a refusal — which is the shape a later edit
would need in order to fill a gap in quietly.

## 5. Mapped fields, and the fields deliberately left unmapped

### Yeda → `YedaCatalogEvidenceMapper`

| Fact field key | Value | Unit | Upstream field (locator) |
| --- | --- | --- | --- |
| `fuel_type` | `plug_in_hybrid` | — | `fuel_type` |
| `horsepower_hp` | `306` | `hp` | `horsepower_hp` |
| `nominal_engine_displacement_l` | `2.5` | `l` | `engine_displacement_l` |

### Government → `GovernmentRegistryEvidenceMapper`

| Fact field key | Value | Unit | Upstream field (locator) | Semantic reading |
| --- | --- | --- | --- | --- |
| `engine_displacement_cc` | `2487` | `cc` | `nefah_manoa` | exact homologated displacement |
| `fuel_type` | `plug_in_hybrid` | — | `delek_nm` | `delek_cd` 7 = `חשמל/בנזין` (electricity/petrol) |
| `official_model_code` | `AXAP54L ANXMBK` | — | `degem_nm` | the register's own model designation |

Identity dimensions taken from the row: `body_style` (`merkav` = `פנאי-שטח`),
`drivetrain` (`hanaa_cd` 3 = `4X4`), `model_code` (`degem_nm`) and `trim`
(`ramat_gimur`). Time scope is the single model year the row is homologated for.

Every meaning-bearing field is read from the record's **code** through a closed
table, and the record's own **name** field must still be the name that code is
paired with. A code the table does not name, or a pairing that has drifted,
fails closed. Nothing is guessed from a Hebrew string.

**Deliberately unmapped, and reported as such in the tool result:**

| Field | Why it is not evidence |
| --- | --- |
| `koah_sus` | The dataset publishes no definition of this power figure, and across the captured plug-in rows of one commercial model it takes both engine-scale (177/185/186) and system-scale (302/324) values. Its semantics are unresolved **in the source**, so mapping it to `horsepower_hp` on the strength of its name would be exactly the guess this proof refuses to make. |
| `dg_metach_solela` | Battery voltage is stated as `650.0`, `12.0`, `0.01` and `null` across the captured plug-in rows, which cannot all describe one traction battery. |
| `mishkal_kolel` | A total mass stated with no unit anywhere in the dataset or its metadata. |

### Web → `ToyotaArchivedDocumentEvidenceMapper`

| Fact field key | Value | Span in the `r5.visible_text.1` projection |
| --- | --- | --- |
| `manufacturer_model_designation` | `טויוטה ראב4 פלאג אין - Toyota RAV4 PLUGIN (PHEV)` | `[0, 48)` |
| `archived_model_heading` | `ראב4 פלאג-אין - RAV4 Plug-in` | `[7696, 7724)` |
| `marketing_status` | `ended` | `[7725, 7757)` |

`marketing_status` is the one interpretive step, and it is a closed one: the
exact sentence `שיווק הדגם ראב4 פלאג-אין הסתיים.` — "marketing of the RAV4
Plug-in model has ended" — maps to the single value `ended`. No other wording
produces this fact, and its **absence fails the call** rather than defaulting to
"still marketed".

**No technical specification is extracted from the page, at all.** It states
none, and an official page saying what a car *is* does not thereby say what it
*measures*.

Scripts, styles, templates, inline SVG and `<head>` are removed **with their
contents** before any text is considered. This matters concretely here: the
captured page states the ended-marketing wording four times — in an
`og:description`, a `twitter:description`, a `name="description"` meta tag and a
JSON-LD `<script>` — none of which is something the page *says* to a reader.
The projection keeps exactly one occurrence: the sentence the page renders. A
test asserts precisely that.

Because a 10,541-character Hebrew projection exceeds `MAX_TOOL_OUTPUT_JSON_BYTES`
once JSON-escaped — a real production bound a proof does not get to widen — the
projection is not returned across the tool boundary. Instead the span is proven
**inside the tool**, where the document is: it reads the offsets back out of the
projection, confirms they hold exactly the expected phrase, and returns the
offsets, that text, and the SHA-256 of the whole projection. A reviewer
re-derives the projection from the committed bytes, checks that digest, and
reads the same span.

## 6. Source authority

Field-specific, versioned, and fail-closed in both directions
(`conflict_policy.SOURCE_TYPE_AUTHORITY`, policy `r4.authority.2`):

| Source | `source_type` | Authoritative for |
| --- | --- | --- |
| Government register | `government_registry` | `regulatory_identity` and `technical_specification` **only** — so `engine_displacement_cc`, `official_model_code` and `fuel_type`, and **not** a price, a reliability figure, a marketing status or an undefined power field |
| Yeda catalog | `aggregated_vehicle_catalog` | **nothing** — the policy does not name this type, and an unknown type is authoritative for nothing |
| Toyota archived page | `manufacturer_archived_model_page` | **nothing** — deliberately a type the policy does not name |

The Web source type is deliberately **not** `manufacturer_specification`.
Typing it that way would hand an archived-model page authority over
displacements and model codes on the strength of the brand, for statements it
never makes. An authentic official page may establish who a model *is* and that
its marketing *ended* without acquiring authority over anything it measures.

Each row above is asserted by test, including the negative entries.

## 7. The three-source proof flow

```text
                       DeterministicProofCommanderClient
                        (one closed, server-owned plan)
                                    ↓
                        Commander → PlanValidator (real firewall)
                                    ↓
   ┌──────────────── BoundedTaskExecutor → GenericWorker ────────────────┐
   │                                                                     │
   │  yeda_variant          government_record       web_archived_status  │
   │  git_commit:f7bf132a   dataset_version:2026-…  content_sha256:41ad… │
   │        ↓                      ↓                        ↓            │
   │   manifest checksum + byte-count gate (before any parse/projection) │
   │        ↓                      ↓                        ↓            │
   │   ToolRegistry.execute — closed input and output schemas            │
   │        ↓                      ↓                        ↓            │
   │   ToolCallRecord — server-resolved provenance, unforgeable          │
   │        ↓                      ↓                        ↓            │
   │   Yeda mapper          Government mapper        Web mapper          │
   │   3 facts / 3          3 facts / 3              3 facts / 3         │
   │   structured           structured               verbatim            │
   │   projections          projections              excerpts            │
   │                                                                     │
   │  government_record_2026 → R5_GOV_RECORD_AMBIGUOUS → task failure    │
   │                            (allow_partial: a real unresolved item)  │
   └─────────────────────────────────────────────────────────────────────┘
                                    ↓
        EvidenceBoard.record_evidence_bundle — lease-guarded, idempotent
              write order: source → fragments → claims, per source
                                    ↓
        RepositoryEvidenceResolver — bounded, run-scoped internal reads
                                    ↓
        compare_structured — deterministic R4 verification, zero model calls
                                    ↓
        9 × VerificationVerdict (verified / R4_STRUCTURED_MATCH,
             mode=deterministic_structured, with durable support links)
                                    ↓
        FinalBuilder → finalize_product_outcome → R1 product outcome
```

The run uses the real `SwarmV2Engine`, `Commander`, `PlanValidator`,
`BoundedTaskExecutor`, `GenericWorker`, `ToolRegistry`, `ToolCallRecord`,
`TrustedEvidenceAcquisition`, `EvidenceBoard`, `RepositoryEvidenceResolver`,
`Verifier` and `FinalBuilder`. **No parallel engine, evidence store, verifier,
scheduler, provider client, run lifecycle or persistence layer was built.**
`ProofRepository` subclasses the guarded repository the R3 suite already proves.

Observed durable state: **3 sources, 9 fragments, 9 claims, 9 verdicts, 9
support links**, every verdict `verified` under
`verification_mode=deterministic_structured`.

## 8. The zero-model path, and the evidence for it

Two pieces of trusted, proof-only wiring make a tool-complete task finish
without a model call. Neither is a production capability, and neither is
reachable from run input, a plan, or model output.

**`GenericWorker.task_output_strategy`** — an optional, constructor-injected
`TaskOutputStrategy`. Absent (the production default) the model-backed path is
byte-for-byte unchanged. Present, it is reached only after the tool loop has
executed and validated every planned call, so it cannot influence tool material,
evidence acquisition or verification. Its return value is re-validated against
the task's own closed `output_schema` through the same `validate_worker_output`
a completion travels. Its two failure codes — `WORKER_OUTPUT_STRATEGY_FAILED`,
`WORKER_OUTPUT_STRATEGY_INVALID` — sit deliberately **outside**
`WORKER_OUTPUT_REASONS`, so a strategy defect can never earn a paid model repair
and quietly break the guarantee. The strategy dispatches on the task's own
approved `(tool, operation)` pairs through a closed reader table, so a result
can never route itself to a reader that would read it more generously.

**`DeterministicProofCommanderClient`** — compiles one recognised proof request
into one exact plan from a closed, server-owned table, selected by constructor
argument. The run's `objective` and `context` — the only client-influenced
fields — are recorded for assertions and **never read**. The compiled plan is
inert until the real `PlanValidator` approves it, and it grants nothing: scopes
live on the server-owned `ToolContext`, unreachable from any plan.

**Evidence of zero model use.** Every model dependency in the proof run — the
Commander client, the worker gateway and the verifier gateway — is a **poison**
gateway that raises on use. A mock returning a response is not evidence of zero
model use, because the call still happened; here the run can only complete if no
provider path is reached at all. The observed run makes **0** calls, and the
verifier plans **0** batches because every claim is settled by deterministic
structured comparison.

A dedicated test disables each required stage in turn — `ToolRegistry`
validation, `TrustedEvidenceAcquisition`, and the structured-fact grounding read
— and asserts the proof **fails** rather than quietly succeeding.

## 9. Final R1 outcome, and the real unresolved item

```text
status       = partial_success
result_kind  = partial_result
fields       = archived_model_heading, engine_displacement_cc, fuel_type,
               horsepower_hp, manufacturer_model_designation, marketing_status,
               nominal_engine_displacement_l, official_model_code
needs_review = [{"task_id": "government_record_2026",
                 "code": "R5_GOV_RECORD_AMBIGUOUS"}]
```

**This pair is derived, not asserted.** Nothing in the proof sets a status. The
builder is handed what the run found and `decide_outcome` pairs "at least one
usable verified field" with "at least one unresolved item". A test removes each
half in turn and shows the outcome changes accordingly — the same verified
evidence with nothing outstanding is `complete / usable_result`, and the same
unresolved item with no usable field is `partial_success / no_usable_result`.
Only both together produce what the real run produces.

### The unresolved item is a property of the data, not of the plan

The catalog describes **one** RAV4 plug-in hybrid variant spanning model years
2021–2026, and states **no trim and no model code** for it. Asking the register
for that same identity:

* **model year 2021** → exactly **one** committed row matches (`_id` 36327).
  Resolvable, so it produces real evidence.
* **model year 2026** → exactly **two** committed rows match (`_id` 37392 and
  37393). They differ **only** by `ramat_gimur` — `SE-PLUGIN` versus
  `XSE-PLUGIN` — which is precisely the dimension the catalog does not state.

There is therefore no conservative answer for 2026, and the register is asked to
refuse rather than choose. `R5_GOV_RECORD_AMBIGUOUS` reaches `needs_review`
through the ordinary task-failure path, on the one task whose plan entry sets
`allow_partial`. Naming a trim resolves it, and a test shows both rows being
retrieved that way — which is what makes the refusal a real limit of the sources
rather than a tool that cannot find anything.

`expected_record_id` is an **assertion**, never a tiebreak: a 2026 request fails
whether it names one of the two matching rows or neither. If naming an `_id`
could select, a caller could resolve a genuine ambiguity by fiat.

### Reconciliation: what these sources may and may not conclude jointly

* **A nominal label is not a homologated measurement.** The catalog's `2.5`
  (unit `l`) is recorded as `nominal_engine_displacement_l`; the register's
  `2487` (unit `cc`) as `engine_displacement_cc`. Different field keys can never
  reach one comparison scope, so neither a contradiction nor an agreement is
  manufactured between two statements about different things.
* **Missing identity dimensions are never agreement.** The catalog states no
  model code and no trim; the register states both. Their `fuel_type` claims
  carry the *same value* and still occupy *different comparison scopes*, so they
  do not corroborate each other. Absence means unknown, never "equal".
* **Incompatible variants do not merge**, even inside one source: the two 2026
  rows state the same displacement under different trims and stay two
  statements, with no conflict and no merge into one better-supported claim.
* **The official page cannot widen into a shared or technical claim.** Toyota
  Israel's own commercial name is `RAV4 Plug-in`, not the bare `RAV4` the other
  two sources use, so its entity is `toyota:rav4-plug-in:il` and its statement
  never merges into theirs. Normalizing the name to force a merge would be an
  inference this proof declines to make; leaving it keeps the naming difference
  visible, which is what it is. The page states no identity dimension and no
  time scope, so it can neither narrow nor be narrowed by a variant statement.

## 10. Wrong-fact rejection, by controlled mutation

Every wrong value is a **real captured value with exactly one thing changed**,
so what is under test is the comparison contract rather than a fixture written
to fail. All are rejected with **zero verifier batches planned**, and the
unmutated fact still verifies against the same durable evidence.

| Source | Mutation of a real fact | Reason |
| --- | --- | --- |
| Yeda | 306 hp → 305 | `R4_VALUE_MISMATCH` |
| Yeda | unit hp → kW | `R4_UNIT_NOT_CONVERTIBLE` |
| Yeda | unit removed | `R4_UNIT_MISSING` |
| Yeda | model year → 2019–2020 | `R4_SCOPE_MISMATCH` |
| Yeda | market → DE | `R4_SCOPE_MISMATCH` |
| Yeda | drivetrain → FWD | `R4_IDENTITY_MISMATCH` |
| Yeda | source version → another commit | `R4_SOURCE_VERSION_MISMATCH` |
| Government | 2487 cc → 2488 | `R4_VALUE_MISMATCH` |
| Government | unit cc → kg | `R4_UNIT_NOT_CONVERTIBLE` |
| Government | unit removed | `R4_UNIT_MISSING` |
| Government | model year 2021 → 2026 | `R4_SCOPE_MISMATCH` |
| Government | market → DE | `R4_SCOPE_MISMATCH` |
| Government | trim → `XSE` | `R4_IDENTITY_MISMATCH` |
| Government | dataset version → an earlier one | `R4_SOURCE_VERSION_MISMATCH` |
| Government | `AXAP54L ANXMBK` → `AXAP54L ANXMBX` | `R4_VALUE_MISMATCH` |
| Web | `marketing_status` `ended` → `active` | `R4_VALUE_MISMATCH` |
| Web | content digest → another | `R4_SOURCE_VERSION_MISMATCH` |
| Web | market → DE | `R4_SCOPE_MISMATCH` |
| Web | an identity the page never states | `R4_IDENTITY_MISMATCH` |

Fixture-level tamper detection is separate and earlier: one flipped byte of a
captured response — a displacement off by one, or the archived-status word
turned into its opposite — fails the checksum gate before the bytes reach a
parser, and a truncated file is classified as truncation rather than as a
generic checksum failure.

## 11. Replay, resume and exactly-once semantics

Proven:

- the source is persisted before its fragments and claims, per source;
- every durable evidence write carries the active worker lease, and a stale
  lease fails **every** write closed;
- replay is idempotent — one run and a replay both leave 3 sources, 9
  fragments, 9 claims, 9 verdicts and 9 support links;
- changing a version creates **distinct** provenance rather than merging;
- resume from **every** normal saved checkpoint duplicates no durable record,
  changes no product result and reaches no model path.

**Known limitation — this is at-least-once, not exactly-once.** There is no
exactly-once guarantee across the window between a tool call completing and the
checkpoint that records its task result being persisted. A crash inside that
window causes the task, and therefore its tool call, to be executed again on
resume. That is safe here for two specific reasons, and only those:

1. every proof tool operation is **read-only**, so re-execution has no external
   effect; and
2. every durable evidence write is **idempotent on `evidence_key`**, derived
   from the content itself, so a replayed acquisition lands on the same rows.

The same at-least-once boundary exists between a verifier batch and the
checkpoint recording its verdicts. Neither is claimed as exactly-once, because
durable call-result persistence, stable call identity and reconciliation — which
would be required to prove it — do not exist in the current system.

## 12. What remains fixture-only

**All of it.** The proof runs entirely from committed fixtures and establishes
an architecture, not a live integration:

- **Yeda is not production-connected.** No GitHub catalog synchronizer, no
  snapshot table, no coverage index, no write path, no `PatchProposal`.
- **Government is not production-connected.** No CKAN client, no live
  `datastore_search`, no snapshot database, no crosswalk, no scheduled refresh.
  Three captured response bodies are committed and read from disk.
- **Web is not production-connected.** No live research tool, no browser, no
  fetcher. One captured page's visible-text projection is committed and read
  from disk; the raw response is not committed at all.
- The production `ToolRegistry` is **empty**. `PRODUCTION_EVIDENCE_MAPPERS` is
  **empty**. None of the three proof tools and none of the three proof mappers
  is registered in either, and `backend/worker/main.py` injects no deterministic
  strategy, so production Swarm V2 behaviour is unchanged.
- A test asserts that no module under `backend/testing/r5_proof/` so much as
  imports `requests`, `httpx`, `socket`, `urllib.request`, a Supabase client or
  a provider SDK, so the proof is offline by construction rather than by
  convention.

## 13. Confirmation of what did not happen

No deployment occurred. No migration was written or applied — R5 adds no schema
change. No production Supabase, Google Cloud, IAM, Secret Manager or Vercel
change was made. No paid model execution, no provider call and no live source
call occurred at any point in the test suite. No credential of ours is
committed. No production tool registration or grant was created. Nothing under
`legacy/` or `MILO-main-original/` was modified and V1 is unchanged.
`MILO-main-original/MILO-main/test_websearch.py` was never run.

## 14. Verification commands and results

All executed on this branch:

```text
pytest -q tests/test_swarm_v2_r5_vehicle_proof.py                 -> 111 passed
pytest -q tests/test_swarm_v2_tool_contract.py                    ->  64 passed
pytest -q tests/test_swarm_v2_r3_evidence_contract.py
       tests/test_swarm_v2_r4_deterministic_verification.py
       tests/test_swarm_v2_outcome_contract.py
       tests/test_swarm_v2_stage1_e2e.py                          -> 360 passed
pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py
                                                                  -> 2042 passed, 1 skipped
MILO_REQUIRE_PG_TESTS=1 pytest -q -rs tests/test_migrations_postgres.py
                                                                  -> 131 passed, zero skips
MILO_REQUIRE_PG_TESTS=1 pytest -q -rs tests/test_worker_rpc_acl_postgres.py
                                      tests/test_evidence_migration_static.py
                                                                  ->  31 passed
python scripts/check_migrations.py                                -> passed
python scripts/secret_scan.py                                     -> passed
python scripts/check_unsafe_defaults.py                           -> passed
```

The single skip is pre-existing and environmental — `shellcheck` is unavailable
in this container — and is not caused by R5. The PostgreSQL suites run with zero
skips using the container's PostgreSQL 16 binaries.
