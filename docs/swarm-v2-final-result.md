# Stage F4 — the Swarm V2 Final Result surface

**Scope: presentation of an existing contract.** F4 adds no endpoint, no
environment variable, no gateway route, no migration and no backend behaviour.
It replaces the raw-JSON display Swarm V2 runs used to get in `RunOutputPanel`
with a typed, deterministic, fail-closed product surface, and it changes
nothing for any other workflow.

## 1. Why a separate surface

Technical execution finishing is not a product result. `SwarmRunCard` reports
that the engine ran — stages, logical tasks, model calls, verifier batches,
the durable terminal status. None of that is an answer, and none of it may be
promoted into one. F4 adds the surface that states the answer, and keeps the
two apart:

| Surface | Reports | Lives in |
| --- | --- | --- |
| `SwarmRunCard` | execution: lifecycle, tasks, usage, terminal status | `components/swarm/` |
| **Final Result** | the product answer: verified fields, outstanding items | `components/result/` |
| `RunInspector` | technical detail, raw events, developer payloads | `components/inspector/` |
| `RunOutputPanel` | the V1 sanitized-output path, unchanged | `components/run/` |

**Workflow identity comes from trusted project state.** `app/page.tsx` selects
the surface from `project.workflow_key` (via `swarm.isSwarmV2`), exactly as it
already selects the run surface. A payload can never route a run to a surface:
a `vehicle_catalog_v1` project carrying a perfectly valid Swarm V2 product
payload still renders the V1 panel, and a test pins that.

## 2. The contract

`lib/finalResult.ts` is the frontend mirror of
`backend/engines/swarm_v2/outcome.py` (`validate_product_outcome`). The durable
`run.output` of a Swarm V2 run is exactly four keys:

```
{ status, result_kind, fields, needs_review }
```

* `status` ∈ `complete | partial_success`
* `result_kind` ∈ `usable_result | partial_result | no_usable_result | not_found`
* only four pairings are constructible, so status and kind can never contradict
* `fields` is `{ <field>: [ { value, provenance } ] }` — a LIST per field, so
  several verified values for one field are native to the contract
* `needs_review` is heterogeneous by contract and holds exactly three literal
  shapes: `{field, value, reason, provenance}` (verdict review),
  `{task_id, code}` (task failure or coverage gap) and the static
  `{code: "NO_USABLE_RESULT"}` marker

`not_found` is implemented and rendered, and is **unreachable in production**:
no registered tool can return `TRUSTED_SOURCE_NO_MATCH`, so
`finalize_product_outcome` always receives `None` from the engine
(`backend/engines/swarm_v2/outcome.py`, `docs/catalog-pr3-swarm-and-promotion.md` §10).

### Fail-closed, exactly

`parseFinalResult` is TOTAL: every input becomes `result`, `absent` or
`invalid`. It re-checks every invariant the backend enforces on the way out —
allowlisted vocabulary, the status/kind pairing, kind versus verified fields,
`complete` carrying no review items, and the exactly-one-trailing-marker rule —
because the browser must not depend on a server-side check it cannot see. It
then adds a closed read of the payload's interior:

* a field entry must be exactly `{value, provenance}`, and a field key must
  carry a non-empty list and stay within `EvidenceReference`'s 200-character
  bound
* **provenance must be exactly the trace `FinalBuilder` writes** — `claim_id`,
  `source_id`, `run_id`, `task_id` and `scope`, with `scope` in turn exactly
  `entity`, `field`, `geography`, `market` and `time_scope`. A missing key, an
  extra key (a fragment, a locator, a hash), a wrong type, an empty required
  identifier, an identifier past its backend bound, or a malformed `time_scope`
  invalidates the whole outcome. `run_id` and `time_scope` are **required and
  validated** even though the surface deliberately does not display them — a
  trace that fails there is not a builder trace, and a field must never be
  shown as verified on the strength of one. An incomplete trace is refused,
  never silently omitted
* **a `needs_review` item that matches none of the three shapes invalidates the
  whole outcome.** There is deliberately no "unrecognised item" category: an
  outstanding item nobody can classify must never be rendered beside verified
  fields as though the result were sound
* a value of a type JSON cannot carry — `NaN`, `Infinity`, a function, or
  `undefined` at a field entry's `value`, inside a list, or inside a structured
  object — is a contract violation, not a value to bound. Only an explicit JSON
  `null` is a recorded absence; a top-level `output === undefined` remains the
  separate *absent* state
* the run's terminal status must agree with the product status; a run that
  failed, was cancelled, timed out or exhausted its budget reaches no product
  outcome at all, so carrying one is a contradiction

Every refusal is a static code — `FIELD_ENTRY_INVALID`, `PROVENANCE_INVALID`,
`REVIEW_ITEM_INVALID`, `VALUE_NOT_JSON` and the contract-level ones — so an
invalid result is diagnosable rather than merely refused. The offending payload
is never rendered.

The bounds mirrored from the backend live in one place, `BACKEND_BOUNDS`, with
each entry naming its source in `contracts.py` / `evidence_bounds.py`.

### Structured values are bounded, not discarded

`safe_durable_value` genuinely permits nested mappings and lists, so a verified
value is not always a scalar and must not be dropped for being one. Structured
values render under explicit bounds — `MAX_VALUE_DEPTH` (3),
`MAX_VALUE_ITEMS` (20) per level, 500 characters per string — and **every bound
that bites is stated on screen**: a depth stop reads "Nested further than this
view shows", and a breadth stop reports how many entries are not shown. Nothing
is truncated silently.

## 3. What the surface shows

* an outcome banner: symbol + text label + a sentence stating what the kind
  means. `partial_result` says in words that it is **not** a completed result
* verified fields, in payload order, with every value listed. A field with more
  than one verified value says so and selects none
* safe, public provenance — source, task, claim, entity, geography, market — in
  a collapsed native `<details>`. `run_id` and `time_scope` are valid contract
  keys that are deliberately not surfaced: the run is the one the user is
  already on (`SwarmRunCard` shows it), and a time scope is technical metadata
  that belongs in the Inspector
* outstanding items grouped by what they are: Conflicts, Needs review,
  Coverage gaps, Task failures — each with a count and a plain-language note
* five distinct non-result states: loading, not finished, delayed poll,
  no payload recorded, and invalid payload — plus a sixth for a run that
  reached a terminal state that produces no product result at all

### A partial result with no itemized rows

`partial_result` does **not** imply a `needs_review` list. A verdict the
verifier REJECTED makes a run partial without writing a review row of its own
(`FinalBuilder` only emits a row for a `needs_review` verdict), so
`partial_success / partial_result` with `needs_review: []` is backend-valid and
reachable. `fixtures/swarmV2FinalResult.json` carries one, generated by the real
builder from one verified and one rejected verdict.

The wording is built for that case rather than around it. The outcome summary
promises no list — it says the run verified the fields shown but that *not every
claim it gathered was verified* — and when there are no itemized rows the
surface says so explicitly instead of pointing at an empty section. It infers
nothing about which claim was rejected or why: the rejected claim's field is
simply absent from `fields`, and the surface leaves it absent. Itemized
conflicts, coverage gaps, task failures and review items still render in full
whenever the payload records them.

## 4. Security posture

* **closed rendering.** Nothing is rendered that the contract does not name.
  There is no payload pass-through, no walk over unknown contract keys, and no
  `JSON.stringify` of durable data anywhere in `components/result/` — enforced
  by `scripts/static-ui-check.mjs`, which fails the build on `JSON.stringify`
  or `dangerouslySetInnerHTML` in that directory
* **no reasoning and no evidence as FIELDS.** The contract has no key for a
  prompt, a chain of thought, a provider error, a source fragment, a content
  hash or a locator, so no code path can carry one as a field
* **redaction, as a real second barrier.** A closed contract proves that an
  unknown key is never rendered; it proves nothing about what is inside a
  string the contract legitimately allows. A verified value, a review reason, a
  review code, a field key, a structured-value key, a task identifier, a
  provenance identifier and a displayed scope value are all ordinary product
  data to the contract, and any of them could contain a credential.

  So every durable string that can reach this surface passes through one
  deterministic boundary — `safeDurableText` in `lib/finalResult.ts`, which
  applies `redactSecretText` (`lib/sanitize.ts`) and then the length bound.
  Redaction runs FIRST, because bounding first could cut a credential in half
  and leave a fragment no pattern matches. It collapses PEM blocks,
  `Bearer <token>`, JWT / service-role shapes, `sk-` provider keys and labelled
  `secret = value` pairs to `[REDACTED]`. Over-redaction is the intended
  failure direction: a value that merely looks like a key is shown as
  `[REDACTED]`, which is lossy and safe, rather than printed, which is not.

  Redaction applies to DISPLAY text only. Classification — which review group
  an item belongs to, whether a marker is the marker — reads the raw payload,
  so a redaction can never silently change what the surface reports.

  Neither barrier substitutes for the other, and the tests assert both
  independently. `redactSecrets`, the whole-payload redactor the V1
  sanitized-output path and the Inspector use, is untouched by F4
* **all free-form text is rendered through `safeText`**, and the contract
  carries no HTML to begin with
* a hostile `__proto__` key that `JSON.parse` materialises is inert data read
  through `Object.entries`, never a prototype mutation — including when it is a
  structured-value key
* **no payload-controlled string indexes a plain object.** The review-code
  label table is a `Map`: an object literal would return a *function* off the
  prototype chain for `code: "constructor"`, and React throws when handed a
  function as a child, so a hostile payload would have crashed the surface that
  exists to fail closed on it. A regression test pins this, and fails against
  the object-literal form

## 5. Accessibility and UX

Labelled `<section>` region, heading levels that descend without skipping, a
polite `role="status"` for the outcome, native disclosures for provenance (so
keyboard operation needs no custom handler), and status meaning carried by
symbol **and** text so it never depends on colour. The layout uses fluid grids
with no fixed pixel widths, collapses its two-column rows below 560px, and
wraps long durable strings.

**Refresh and resume rebuild the same result.** `parseFinalResult` is a pure
function of the durable `run.output` plus the run status, preserving payload
order, so a reload — which keeps only the stored run id and re-reads the run
through the authenticated gateway — reconstructs an identical surface. This is
the frontend counterpart of the roadmap's §1.13 criterion, that a final result
is reconstructible from durable state without remembering model reasoning.

## 6. Tests

| File | Covers |
| --- | --- |
| `frontend/tests/finalResult.test.ts` | the contract and parser: all four kinds, multiple values, conflicts/gaps/failures, every backend invariant, malformed and hostile payloads, status/output mismatch, bounded structured values, determinism — plus the redaction sweep across every durable string position, strict provenance (missing / empty / mistyped / extra / out-of-bounds / malformed scope / malformed `time_scope`), the partial result with no itemized rows, and `undefined` refused in all three value positions |
| `frontend/tests/finalResultPanel.test.tsx` | rendering, the non-result states, surface separation, hostile input, accessibility and responsive structure — plus the rendered-DOM secret sweep (with every disclosure forced open), malformed provenance never rendering as verified, and the no-itemized-rows case including its headings and refresh |
| `frontend/tests/finalResultRouting.test.tsx` | which surface mounts for which workflow, driven through the real page; V1 regression; refresh/resume for both partial shapes |
| `frontend/e2e/enabled.final-result.spec.ts` | a real terminal Swarm V2 run backed by mocks, all three outcome shapes including the rejected-claim case, refresh, mobile width, keyboard, V1 control |

The sentinels are assembled at runtime in `frontend/tests/secretSentinels.ts` —
never written as literals, because `scripts/secret_scan.py` exists to keep
key-shaped strings out of this repository and a test fixture is not an
exemption from that rule.

`frontend/tests/fixtures/swarmV2FinalResult.json` is **generated output of the
real backend** — `FinalBuilder` over `finalize_product_outcome` — and every
fixture passed `validate_product_outcome` before it was committed. The frontend
contract is therefore asserted against what the engine actually emits.

The E2E stack seeds a `workflow_key = swarm_v2` project ("Gamma Swarm") and its
in-process mock worker builds the payload with the SHIPPED builder and maps it
to a durable status with the SHIPPED `durable_run_status`, so only the evidence
and the verdicts are mocked. No paid model call, no live capture and no real
Cloud Run job is possible in that stack.

## 7. Out of scope

No migration, no Government ingestion work, no production activation, no
workspace redesign, no Commander change, and none of the broader F5 security
sweep beyond the tests F4 itself requires.
