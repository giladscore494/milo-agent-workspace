# R5 — one real, read-only vehicle evidence proof

Status: **INCOMPLETE — one of three real source families captured.**

This document describes what the R5 proof actually establishes today, and is
written so that a reviewer never has to infer which parts are real. Where a
requirement is unmet it says so plainly rather than describing the intended
end state.

Base commit: `220b5e6871b8d14fc8c647179363c3de1689f0e8` (merge of
[PR #82](https://github.com/giladscore494/milo-agent-workspace/pull/82), the merged
R4 state). `origin/main` still pointed at exactly that commit when this
branch was created.

## 1. What is proven, and what is not

| R5 requirement | State |
| --- | --- |
| Real pinned Yeda fixture, tool, mapper | **Done** |
| Real pinned `data.gov.il` fixture, tool, mapper | **Not captured — blocked** |
| Real pinned saved Web document, tool, mapper | **Not captured — blocked** |
| Whole path through the real engine with zero model calls | **Done** (one source) |
| Conservative variant identity; make+model+year cannot merge variants | **Done** |
| One explicit real coverage gap | **Done** |
| One deliberately incorrect value rejected deterministically | **Done** |
| At least one verified field in the final result | **Done** |
| Replay / checkpoint-resume idempotency | **Done** |
| No production registration, no write path, no migration | **Done** |
| Three-source reconciliation scenario | **Not reachable — blocked** |
| `partial_success` / `partial_result` end state | **Not reachable — blocked** |

### Why two sources are missing

`data.gov.il` and `www.toyota.co.il` are both refused by this environment's
egress proxy with **HTTP 403 to `CONNECT`** — a policy denial that happens
before any request is sent, so no redirect or CDN hostname is implicated.
`api.github.com` returns `200` throughout, so the tunnel itself is healthy;
only these hosts are blocked.

Their tools and mappers are deliberately **not written**. Their input and
output schemas must be derived from the real CKAN field names and the real
document structure. Authoring them from assumption and swapping data in later
is precisely how fabricated provenance reaches a repository, so the work stops
at the boundary of what was actually retrieved.

## 2. The selected vehicle

**Toyota RAV4 Plug-in Hybrid — Israeli market, model years 2021–2026.**

Chosen from the real catalog before any code was written, on four grounds:

1. **It makes conservative identity necessary rather than decorative.** The
   pinned catalog states **five** RAV4 variants covering model year 2021, so
   make + commercial model + year genuinely does not identify a variant. The
   plug-in hybrid is separated from the other four by its fuel type.
2. **Its web document is the one the catalog itself attributes.** This
   variant's only source index is `official_importer`
   `https://www.toyota.co.il/models/rav4-plugin` — a variant-specific
   specification page, so vehicle identity is attributable without inference.
3. **It carries a real, non-manufactured coverage gap.** The catalog records
   no generation, no official/model code and no trim for this variant.
4. It is a high-volume Israeli-market model, so it is expected in both
   government resources named in the roadmap.

Identity as the catalog states it:

| Dimension | Value | Role |
| --- | --- | --- |
| make / commercial model | Toyota / RAV4 | entity |
| market | IL | scope |
| model years | 2021–2026 | time scope |
| body style | SUV | identity dimension |
| drivetrain | AWD | identity dimension |
| engine | `2.5L` | identity dimension |
| transmission | `cvt` | identity dimension |
| generation | **absent** | gap |
| official/model code | **absent** | gap |
| trim (`version_or_trim`) | **absent** (`null`) | gap |

## 3. Source, version and locator table

| | Yeda |
| --- | --- |
| Source kind | `git_repository_file` |
| Repository | `giladscore494/reliabilityAIModelsR2` |
| Path | `my-flask-app/app/data/model_technical_catalog_il.json` |
| Commit (source version) | `f7bf132abf1b1ec8f9d81076560a5a618447a9e6` |
| Git blob SHA | `383ba9bf12ddfafeac79fe42ea449c11204dc017` |
| Upstream file SHA-256 | `0a8d0e4240cbf11ad908012168d9dd60cafc2c9f75b92caecbd73f126c349ad6` |
| Upstream catalog hash | `76d2b5fbaa141577` (the catalog service's own `get_catalog_hash`) |
| Upstream `generated_at` | `2026-06-25T00:20:49Z` |
| Canonical URL | immutable GitHub blob URL **containing the commit** — never `/blob/main/` |
| Retrieved (UTC) | `2026-09-09T14:06:59Z` |
| Record locator | `models[860]`, `technical_variants_il[4]` |
| Committed fixture | `backend/testing/r5_proof/fixtures/yeda/rav4_model_record.json` |
| Fixture SHA-256 | `a2ce25de43f315517a5584eed94658bd54de721b5d9cee791f9ba952b09ad8c6` |
| Fixture kind | `exact_record_subset` (15,225 bytes) |

The upstream catalog is ~7.3 MB and **is not committed**. One model record of
965 is, verbatim. The upstream digest and the committed-fixture digest are
recorded separately, because they are different objects.

Durable evidence locators produced from that record:

| Fact field key | Value | Unit | Locator field path |
| --- | --- | --- | --- |
| `fuel_type` | `plug_in_hybrid` | — | `fuel_type` |
| `horsepower_hp` | `306` | `hp` | `horsepower_hp` |
| `nominal_engine_displacement_l` | `2.5` | `l` | `engine_displacement_l` |

The locator names the **upstream** field; the fact's `field_key` names what
the value **means**. Keeping them apart is what allows §5's naming rule.

## 4. Fixture capture method

`scripts/r5_capture_fixtures.py` is a **manual development action**. It
refuses to run when `CI`, `GITHUB_ACTIONS` or `PYTEST_CURRENT_TEST` is set,
because a refresh changes evidence and evidence must never change because a
pipeline happened to execute. **Tests and CI never fetch or refresh a source.**

For Yeda it performs no network access at all: an operator clones the public
repository read-only, and the script verifies three things before parsing a
byte — the checkout is at the expected commit, the catalog file is the
expected git blob, and its content hashes to the expected SHA-256.

`backend/testing/r5_proof/manifest.py` is the gate at read time. Every proof
read re-hashes its fixture against the manifest **before** the bytes reach a
JSON decoder, so a single changed byte fails closed rather than producing
different evidence under unchanged provenance. Fixture paths cannot escape the
committed root.

## 5. Two semantic decisions

**`market` in this catalog is a presence label, not a market identifier.**
Across the 965 records it takes the values `IL` (461), `IL-confirmed` (390),
`global-reference-only` (62) and `IL-likely` (52); the RAV4's is
`IL-confirmed`. Reading it as a market name would let a global-reference
record answer an Israeli-market question. It is therefore read through a
closed vocabulary that maps a value to `(market, presence confidence)` and
**fails closed** on `global-reference-only` and on anything unrecognised. The
market that enters the R4 comparison scope is `IL`, from the catalog root; the
presence confidence travels separately.

**A nominal engine-class label is not a homologated displacement.** The
catalog states `2.5`, an engine-class label. It is recorded as
`nominal_engine_displacement_l` (unit `l`), never as `engine_displacement_cc`.
Because the R4 comparison scope includes the field key, this label can never
share a scope with an exact homologated figure, so no contradiction and no
agreement is manufactured between two statements that are simply about
different things. When the government source lands, its exact displacement
will be a **separate field**, and the relationship between the two will remain
explicitly unresolved rather than asserted.

## 6. Source authority

Yeda's `source_type` is `aggregated_vehicle_catalog`. R4's authority policy
(`conflict_policy.SOURCE_TYPE_AUTHORITY`) does not name that type, and the
policy fails closed on an unknown type — so **Yeda is authoritative for
nothing** and can never close a conflict on its own, however confident the
catalog sounds. That is the correct classification for a derived, aggregated
catalog, and it is asserted by test rather than assumed.

## 7. The proof flow

```text
real pinned Yeda catalog record (commit f7bf132a…)
  → manifest checksum gate (SHA-256 re-hashed before parsing)
  → ToolRegistry.execute — closed input schema, closed output schema
  → ToolCallRecord (server-resolved provenance; unforgeable by a model)
  → YedaCatalogEvidenceMapper (registered for ONE exact tool.operation pair)
  → VersionedEvidenceSource (git_commit:f7bf132a…)
      + 3 StructuredEvidenceFacts (value, unit, identity, locator)
      + 3 FocusedEvidenceFragments (structured_projection, exact locator)
  → EvidenceBoard.record_evidence_bundle — lease-guarded, idempotent,
      write order: source → fragments → claims
  → RepositoryEvidenceResolver — three bounded, run-scoped internal reads
  → compare_structured — deterministic R4 verification, zero model calls
  → VerificationVerdict ×3 (verified / R4_STRUCTURED_MATCH,
      mode=deterministic_structured, with durable support links)
  → FinalBuilder → R1 product outcome
```

The run uses the real `SwarmV2Engine`, `Commander`, `PlanValidator`,
`BoundedTaskExecutor`, `GenericWorker`, `ToolRegistry`, `ToolCallRecord`,
`TrustedEvidenceAcquisition`, `EvidenceBoard`, `RepositoryEvidenceResolver`,
`Verifier` and `FinalBuilder`. **No parallel engine, evidence store, verifier,
scheduler, provider client, run lifecycle or persistence layer was built.**

`ProofRepository` subclasses the guarded repository the R3 suite already
proves, adding the third bounded grounding read and the two R4 durable writes.

## 8. The zero-model path, and the evidence for it

Two pieces of trusted, proof-only wiring make a tool-complete task finish
without a model call. Neither is a production capability, and neither is
reachable from run input, a plan, or model output.

**`GenericWorker.task_output_strategy`** — an optional, constructor-injected
`TaskOutputStrategy`. Absent (the production default) the model-backed path is
byte-for-byte unchanged. Present, it is reached only after the tool loop has
executed and validated every planned call, so it cannot influence tool
material, evidence acquisition or verification. Its return value is
re-validated against the task's own closed `output_schema` through the same
`validate_worker_output` a completion travels. Its two failure codes —
`WORKER_OUTPUT_STRATEGY_FAILED`, `WORKER_OUTPUT_STRATEGY_INVALID` — are held
deliberately **outside** `WORKER_OUTPUT_REASONS`, so a strategy defect can
never earn a paid model repair and quietly break the guarantee.

**`DeterministicProofCommanderClient`** — compiles one recognised proof
request into one exact plan from a closed, server-owned table, selected by
constructor argument. The run's `objective` and `context` — the only
client-influenced fields — are recorded for assertions and **never read**, so
untrusted input can neither choose a tool nor build an argument. The compiled
plan is inert until the real `PlanValidator` approves it, and it grants
nothing: scopes live on the server-owned `ToolContext`, unreachable from any
plan.

**Evidence of zero model use.** Every model dependency in the proof run — the
Commander client, the worker gateway and the verifier gateway — is a **poison**
gateway that raises on use. A mock returning a response is not evidence of
zero model use, because the call still happened; here the run can only
complete if no provider path is reached at all. The observed run makes **0**
calls, and the verifier plans **0** batches because every claim is settled by
deterministic structured comparison.

A dedicated test disables each required stage in turn — `ToolRegistry`
validation, `TrustedEvidenceAcquisition`, and the structured-fact grounding
read — and asserts the proof **fails** rather than quietly succeeding. The
third case falls through to the grounded model verifier and trips the poison,
which is the guard against re-implementing the path.

## 9. Final R1 outcome

With the Yeda source alone the run produces:

```text
status      = complete
result_kind = usable_result
fields      = fuel_type, horsepower_hp, nominal_engine_displacement_l
needs_review = []
```

This is the **truthful** classification under the canonical outcome contract
(`backend/engines/swarm_v2/outcome.py`): there is at least one usable verified
field and nothing outstanding, which `decide_outcome` maps to
`complete / usable_result`.

R5 specifies `partial_success / partial_result`, and that remains correct for
the **full** scenario: the unresolved item comes from the Government and Web
sources, which supply the reconciliation the proof cannot yet perform. The
current outcome is not a weakening of the gate — it is what the evidence
actually available supports, and it will change when the missing sources land.

Every field in `fields` is verified material carrying full provenance (claim,
source, run, task and canonical scope). No rejected or unverified claim
reaches it.

## 10. Replay, resume and exactly-once semantics

Proven:

- the source is persisted before its fragments and facts (observed write
  order: `source → fragment ×3 → claim ×3 → verdict ×3`);
- every durable evidence write carries the active worker lease, and a stale
  lease fails **every** write closed;
- replaying the same source version, locator and content returns the same
  durable rows — one run and a replay both leave 1 source, 3 fragments,
  3 claims, 3 verdicts, 3 support links;
- changing a version creates **distinct** provenance rather than merging
  (2 sources, 6 claims, 6 fragments);
- resume from **every** normal saved checkpoint duplicates no durable record,
  changes no product result and reaches no model path.

**Known limitation — this is at-least-once, not exactly-once.** There is no
exactly-once guarantee across the window between a tool call completing and
the checkpoint that records its task result being persisted. A crash inside
that window causes the task, and therefore its tool call, to be executed again
on resume. That is safe here for two specific reasons, and only those:

1. every proof tool operation is **read-only**, so re-execution has no
   external effect; and
2. every durable evidence write is **idempotent on `evidence_key`**, derived
   from the content itself, so a replayed acquisition lands on the same rows.

The same at-least-once boundary exists between a verifier batch and the
checkpoint recording its verdicts, and is documented in the engine. Neither is
claimed as exactly-once, because durable call-result persistence, stable call
identity and reconciliation — which would be required to prove it — do not
exist in the current system.

## 11. What remains fixture-only

**All of it.** The proof runs entirely from committed fixtures and proves an
architecture, not a live integration:

- **Yeda is not production-connected.** No GitHub catalog synchronizer, no
  snapshot table, no coverage index, no write path, no `PatchProposal`.
- **Government is not connected at all.** No CKAN client, no snapshot
  database, no crosswalk. The fixture was never captured.
- **Web is not connected at all.** No live research tool, no browser capture.
  The document was never retrieved.
- The production `ToolRegistry` is **empty**. `PRODUCTION_EVIDENCE_MAPPERS` is
  **empty**. Neither the proof tool nor the proof mapper is registered in
  either, and `backend/worker/main.py` injects no deterministic strategy, so
  production Swarm V2 behaviour is unchanged.

## 12. Confirmation of what did not happen

No deployment occurred. No migration was written or applied — R5 adds no
schema change. No production Supabase, Google Cloud, IAM, Secret Manager or
Vercel change was made. No paid model execution, no provider call and no
external write occurred. No credential or token is committed. No production
tool registration or grant was created. Nothing under `legacy/` or in V1 was
modified. `test_websearch.py` was never run.

## 13. Verification commands and results

All executed on this branch:

```text
pytest -q tests/test_swarm_v2_r5_vehicle_proof.py                 -> 59 passed
pytest -q tests/test_swarm_v2_tool_contract.py                    -> 64 passed
pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py
                                                                  -> 1990 passed, 1 skipped
MILO_REQUIRE_PG_TESTS=1 pytest -rs tests/test_migrations_postgres.py
                                                                  -> 131 passed, zero skips
MILO_REQUIRE_PG_TESTS=1 pytest -rs tests/test_worker_rpc_acl_postgres.py
                                  tests/test_evidence_migration_static.py -> 31 passed
python scripts/check_migrations.py                                -> passed
python scripts/secret_scan.py                                     -> passed
python scripts/check_unsafe_defaults.py                           -> passed
```

The single skip is pre-existing and environmental (`shellcheck` unavailable in
this container); it is not caused by R5. The PostgreSQL suites required
installing `postgresql-16` locally, after which they run with zero skips.
