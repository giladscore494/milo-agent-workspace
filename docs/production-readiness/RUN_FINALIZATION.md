# Run finalization: one authority, one product outcome

`COMPLETED_IN_CODE` unless noted.

This document answers two questions that used to be answered in several
places at once:

1. **who decides that a run is over, and what it ended as** —
   `backend/finalization.py`;
2. **what the run actually produced** — `backend/product_outcome.py`.

Nothing else in the system may answer either. The RuntimePolicy (#103) still
owns a run's *limits* and the ExecutionUsageLedger (#104) still owns its
*consumption*; this owns its *ending*.

## The problem this replaces

`backend/worker/main.py` closed a run from eight independent places — the
cancellation handler, the budget-stop helper, the Swarm V2 failure handler,
the Swarm V2 success path, the V1 success path, the V1 failure path, the
summary-checkpoint resume path, and five pre-execution refusals. Each one
picked its own durable status and wrote it itself. Three consequences
followed directly:

* **A run's status depended on which branch finished last.** Nothing stopped
  a normal completion from being written after a cancellation was accepted or
  a budget rail had tripped.
* **A V1 run could be upgraded by its own resume.** The summary-checkpoint
  fast path called `mark_run_complete` unconditionally and read the final
  document's status with a `"success"` default, so a run whose final builder
  recorded `partial_success` — or recorded no status at all — resumed into
  durable `completed`.
* **Semantic success was inferred from technical completion.** V1 was
  classified by `result["status"] in {...}` with a truthy-`result` fallback,
  so "the engine returned a dictionary" was enough to be `completed`.

## ProductOutcome — `backend/product_outcome.py`

One engine-neutral, machine-readable record, derived from a finished run's
payload. It is pure (no I/O, no state) and every field is a static allowlisted
value, a count, or a digest — never prompt, provider, exception or model text
— so it is always safe to record in an event or an acceptance gate.

| field | meaning |
| --- | --- |
| `semantic_status` | `complete` / `partial` / `unusable` / `refused` / `not_produced` |
| `usability` | `usable` / `partial` / `unusable` / `refused` / `none` (derived from the status, so the two cannot contradict) |
| `coverage` | `produced`, `outstanding`, and a `ratio` that is `null` rather than `1.0` when nothing is known |
| `blocking` | the gaps and failures, as allowlisted codes with counts (`OUTSTANDING_REVIEW_ITEMS`, `FAILED_AGENTS`, `DEGRADED_VERIFICATION`, `NO_USABLE_RESULT`, `OUTCOME_CONTRACT_VIOLATION`, …) |
| `payload` | a **safe reference** to the final payload: presence, SHA-256 digest, byte size and top-level shape — never its content |
| `result_kind` | the existing Swarm V2 classification, derived rather than independently decided |

The rule every derivation obeys: **a declared status is a floor, never a
lift.** Evidence that the product exists (a verified field entry, a settled
model) is what produces `complete`; every unresolved item demotes. A declared
`complete` alongside outstanding review items derives `partial`, and a
declared status the contract does not recognise derives `partial`, not
`complete`.

* **Swarm V2** is validated through its own contract
  (`backend/engines/swarm_v2/outcome.py`), which stays authoritative for V2. A
  payload that contract would not have produced derives `refused`, which is
  the `SWARM_V2_OUTCOME_INVALID` failure stated in the shared vocabulary.
* **vehicle_catalog_v1** is read from what its deterministic assembly actually
  recorded: settled models, the `needs_review` / `rejected` views (which
  `apply_israel_source_policy` recomputes *after* the builder chose its
  status), `failed_agents`, and `pipeline_quality`. It is also the reader for
  any engine with no contract of its own; a payload that is not the
  deterministic catalog document cannot be counted, so only the declared
  status is read — downwards.

`backend/export_envelope.py` now classifies through this module too, so the
exported `result_kind` and the durable run status cannot disagree.

## The finalizer — `backend/finalization.py`

Every terminal path builds a `TerminalClaim` and hands it to the run's one
`RunFinalizer`. A claim states a **reason** and its evidence; it never states
a durable status.

| reason | raised by | durable status |
| --- | --- | --- |
| `product` | the engine returned | from the ProductOutcome |
| `cancelled` | `CancellationRequested`, or cancellation observed before start | `cancelled` |
| `budget_stop` | `BudgetExceeded`, or `tracker.stop` | the rail's own terminal status (`timed_out` / `budget_exhausted` / `failed`) |
| `failure` | the engine broke, or reported a failure | `failed` |
| `refusal` | a gate declined before execution (budget config, runtime policy, provider key, provider limits, routing) | `failed` |

### Terminal authority

Terminal statuses are ordered by what they assert, not by arrival:

```
cancelled (50)  >  timed_out / budget_exhausted (40)  >  failed (30)
                >  partial_success (20)  >  completed (10)
```

`completed` is the strongest good news and therefore the **weakest claim**:
it is the one every other status contradicts, so it can never overwrite one.
Ties keep the earlier claim, so the outcome never depends on thread
scheduling.

Claims that are true but not yet being written take part in the same
comparison — `note()` for one already known, and `add_claim_source()` for a
live one. The worker registers the budget tracker as a live source, so a rail
that trips on a worker thread *after* the main thread checked it and *before*
the write still outranks the product claim: the consultation happens inside
the decision lock.

### Idempotence and races

* A repeated equivalent claim (same reason, status, error code, semantic
  status and payload digest) is a no-op that reports the decision in force.
* A *different* later claim never overwrites it; the result says `superseded`.
* A run found already terminal in the database — a previous attempt, a
  replacement worker, the operator — is **adopted**, not rewritten.
* A write rejected at the database boundary triggers a re-read: if the run is
  now terminal, that decision is adopted; if it is not, this worker has lost
  the run and the error escapes, so a stale worker never reports an outcome.
* A `cancellation_requested` run admits only `cancelled` and `failed`. A
  product that finished anyway keeps its payload as the run's output, and the
  run is recorded as `cancelled` with `RUN_CANCELLED_AFTER_RESULT`. This
  previously raised `INVALID_RUN_TRANSITION` out of `execute_run`, which Cloud
  Run reads as a failed task and relaunches.
* A repository that cannot express the decision raises
  `RUN_FINALIZATION_UNAVAILABLE` (503). Silently downgrading to
  `mark_run_complete` is exactly the defect this replaces.

Fencing is unchanged and remains the outer boundary: every durable write and
every event append still carries the active lease, so only the lease holder
can terminalize at all.

### Terminal events

The finalizer is the only emitter of `run_completed`, `run_partial_success`,
`run_failed` and `run_cancelled`, and a product event carries the canonical
outcome record in `payload.product_outcome`. Budget and timeout stops emit no
extra terminal event: the tracker already emitted the cause event
(`budget_exhausted`, `run_timed_out`, `token_limit_reached`) when the rail
tripped. A failure or refusal event carries its static code **only** — no
fragment of a refused payload, not even its key names, rides out on the event
that rejects it.

## Stage D: technical success is not semantic acceptance

Stage D's existing gates all answer a technical question — did the execution
terminate cleanly, exactly once, inside its caps, against the accepted digest.
`scripts/release/stage-d/semantic_acceptance.py` asks the other one, of this
same canonical module:

* the worker records the ProductOutcome on the run's terminal event;
* `probe_db.py` copies that bounded record into its evidence verdict and
  fails closed if a product terminal state carries none — it never derives or
  judges an outcome, because a second implementation of the rule could
  disagree with the one that decided the run's status;
* `06-collect-evidence.sh` step 5 runs the host-side gate, which rebuilds the
  record through the canonical reader and applies the canonical acceptance
  rule.

A technically successful execution whose outcome is `unusable`, `refused`,
`not_produced` or absent does **not** pass. A truthful `partial` with real
verified content does pass by default (it is the expected shape of a first
government run); `--require-complete` tightens that.

## Regression coverage

`tests/test_canonical_finalization.py`:

* AST proof that the worker no longer calls `mark_run_complete` /
  `mark_run_failed` and never transitions a run to a terminal state itself;
* both engines' product paths, and every non-product terminal path, observed
  going through the finalizer;
* a V1 partial result surviving the summary phase, the final checkpoint and a
  resume — including a clean checkpoint whose recorded failures keep it
  partial, and a checkpoint with no status at all;
* `completed` losing to a noted stop, to a live rail, to an accepted
  cancellation and to a terminal state already in the database;
* duplicate finalization idempotent, a different later claim superseded, six
  concurrent finalizations producing exactly one decision and at most one
  terminal event;
* the canonical outcome's vocabulary, coverage, safe payload reference,
  record round-trip and agreement with the V2 useful-outcome table;
* the Stage D semantic gate accepting a partial product and refusing an
  unusable one, a refused one, a missing record, a forged record and every
  non-product terminal state.
