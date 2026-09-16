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

* a field entry must be exactly `{value, provenance}`; a field key must carry a
  non-empty list
* a provenance trace may carry only the keys `FinalBuilder` writes; an
  unrecognised key (a fragment, a locator, a hash) invalidates the outcome
* **a `needs_review` item that matches none of the three shapes invalidates the
  whole outcome.** There is deliberately no "unrecognised item" category: an
  outstanding item nobody can classify must never be rendered beside verified
  fields as though the result were sound
* a value of a type JSON cannot carry (`NaN`, `Infinity`, a function) is a
  contract violation, not a value to bound
* the run's terminal status must agree with the product status; a run that
  failed, was cancelled, timed out or exhausted its budget reaches no product
  outcome at all, so carrying one is a contradiction

Every refusal is a static code. The offending payload is never rendered.

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

## 4. Security posture

* **closed rendering.** Nothing is rendered that the contract does not name.
  There is no payload pass-through, no walk over unknown contract keys, and no
  `JSON.stringify` of durable data anywhere in `components/result/` — enforced
  by `scripts/static-ui-check.mjs`, which fails the build on `JSON.stringify`
  or `dangerouslySetInnerHTML` in that directory
* **no reasoning, no evidence, no secrets.** The contract has no field for a
  prompt, a chain of thought, a provider error, a source fragment, a content
  hash, a locator or a token, so there is no code path that could carry one.
  Redaction (`lib/sanitize.ts`) stays defense in depth; it is NOT what keeps
  these out — the typed contract is
* **all free-form text is rendered through `safeText`**, and the contract
  carries no HTML to begin with
* a hostile `__proto__` key that `JSON.parse` materialises is inert data read
  through `Object.entries`, never a prototype mutation

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
| `frontend/tests/finalResult.test.ts` | the contract and parser: all four kinds, multiple values, conflicts/gaps/failures, every backend invariant, malformed and hostile payloads, secret sentinels, status/output mismatch, bounded structured values, determinism |
| `frontend/tests/finalResultPanel.test.tsx` | rendering, the five non-result states, surface separation, hostile input, accessibility and responsive structure |
| `frontend/tests/finalResultRouting.test.tsx` | which surface mounts for which workflow, driven through the real page; V1 regression |
| `frontend/e2e/enabled.final-result.spec.ts` | a real terminal Swarm V2 run backed by mocks, refresh, mobile width, keyboard, V1 control |

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
