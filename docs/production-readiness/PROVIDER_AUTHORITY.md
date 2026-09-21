# The provider authority

Status: `COMPLETED_IN_CODE`.

ONE module owns everything a paid provider request is subject to:
`backend/provider_authority.py`. Both engines — `vehicle_catalog_v1` (V1)
and `swarm_v2` (V2) — route every request through it, and neither states a
retry, a timeout, an admission rule or a provider-error meaning of its own.

## Why it exists

Before it, one provider request was governed by four surfaces that each held
their own opinion:

| surface | held its own |
| --- | --- |
| `vehicle_catalog_v1/core.py` | a rate-limit retry loop with its own 2s delay and its own bound, its own OpenAI client construction, its own admission value |
| `swarm_v2/model_gateway.py` | its own `ProviderScheduler` instance and its own admission value |
| `budget.py` (guarded client) | `is_provider_rate_limit_error` — a classifier that did **not** recognise 503 |
| `provider_scheduler.py` | `classify_provider_error` + `request_completion_is_proven` — two more classifiers |

They shared the organization coordinator, so the account ceiling held. Nothing
else about a request was stated once. The concrete defect that fell out of it:
a **503 / `engine_overloaded_error`** was *backpressure* to the scheduler
(paced and retried) and a *semantic failure* to the ledger (charged against
`MILO_MAX_RETRIES`). One provider event, two verdicts, two counters — and a
run that could die at `RETRY_LIMIT_REACHED` with no model having misbehaved.

## The taxonomy

`classify_outcome(exc) -> ProviderVerdict` is the only classifier. Seven
closed outcomes:

| outcome | backpressure? | consumes a semantic retry? | frees the org permit? |
| --- | --- | --- | --- |
| `SUCCESS` | no | no | yes |
| `RATE_LIMIT` (429 **and** 503/overloaded, search QPS) | **yes** | **no** | only on structural proof |
| `RETRYABLE_FAILURE` (5xx, never-sent transport) | no | yes | on structural proof |
| `NON_RETRYABLE_FAILURE` (4xx, quota exhausted) | no | yes (quota: no) | on structural proof |
| `TIMEOUT` (total deadline fired) | no | yes | **no** |
| `CANCELLATION` | no | no | no |
| `UNKNOWN` | no | yes | **no** |

A semantic retry is what bounds *work the run redoes*. Provider capacity is
not that, so `RATE_LIMIT` never touches it. `TIMEOUT` and `UNKNOWN` do,
because the engine will repair and that repair needs a bound.

`completion_proven` is the **#102 ownership invariant**, unchanged and now
answered by the same classification: every YES is structural (the call
returned, the exception carries a response OBJECT with an integer status, the
failure happened before anything was sent, or code that sat on one side of
the request declared it). A fired deadline and an unrecognised outcome are NO,
so the organization concurrency permit stays **held**, not reused.

## Retry and admission

`ProviderScheduler.execute` is the mechanism the adapter drives. Every
attempt — the first and every retry — re-enters:

1. process-local RPM/TPM window and concurrency slot;
2. the organization-wide coordinator (`try_acquire_inference` +
   `try_admit_request`), so retries spend organization RPM/TPM/concurrency
   visibly rather than silently;
3. the guarded client's `open_call`, so each attempt consumes
   `model_calls` / `provider_attempts` in the ExecutionUsageLedger and is
   settled (`provider_failures`) whatever its outcome.

The number of attempts is `RuntimePolicy.max_provider_attempts_per_call`
(`1 + provider_max_rate_limit_retries`). **SDK retries stay at zero**: the
two places that build a provider client (`backend/budget.py` and
`backend/provider_authority.py`) both pass `max_retries=0`, and a test sweeps
`backend/` for a third.

## Token admission

`chars // 4` is an average for English prose. It is **not a bound**, and it
was being used to prove a hard, shared TPM ceiling. Hebrew — this product's
market — is 2 UTF-8 bytes per character, and a byte-level BPE tokenizer can
emit one token per byte, so the old rule could under-count real input by up
to 8×.

The admission rule is now:

```
admission = input_bound + explicit_output_cap
input_bound = utf8_len(json(messages)) + 8 * len(messages)   [+ tools]
```

A byte-level BPE tokenizer merges bytes into tokens and never splits one byte
into two, so `tokens <= utf8_bytes` holds for any input. When an
**authoritative** counter is registered (`register_token_counter`) it is used
instead, plus the chat template's structural framing; a counter that raises
or returns a non-positive value falls back to the bound, never to an average.

Fail-closed cases:

* no explicit output cap → `MissingOutputCap`, nothing is sent;
* content that cannot be serialized/measured → `UnknownTokenDemand`, nothing
  is sent;
* a bounded demand above the process TPM limit or above the organization
  `max_tpm` → `TokenCeilingExceeded`, before a permit is taken.

`estimate_input_tokens` / `estimate_request_tokens` survive as rough size
signals and are documented as **not** admission values.

## Search: MILO admits every invocation

**The production search path is standalone and MILO-mediated.** V1 no longer
offers the provider's builtin `$web_search`, and no production engine does.

### Why the builtin had to go

A `builtin_function` tool is executed by the *provider*, inside a Chat
Completions request, as many times as the provider decides. MILO could
reserve a worst case before dispatch and reconcile afterwards, but it could
never admit *one* search: if four were reserved and the response performed
five, the fifth had already run and been billed by the time MILO could see
it. `max_search_invocations_per_run` was therefore a number MILO could
*report*, not a ceiling a run could be stopped at.

### What replaces it

V1 offers a model MILO's own ordinary `function` tool, `web_search`
(`backend/standalone_search.py`). The provider cannot execute it; it can only
ask. Every ask returns to MILO:

```
V1 decides search is needed
  -> the model emits a `web_search` tool call
  -> ProviderAdapter.run_search
       1. query + transport resolved      (free: refusals here cost nothing)
       2. run allowance taken             max_search_invocations_per_run
       3. endpoint QPS bucket taken       /v1/tools/search
       4. invocation + cost charged       durable, through the ledger snapshot
       5. exactly ONE standalone search   MoonshotStandaloneSearch
  -> results become the tool message
  -> the model reads them and continues
```

Several tool calls in one response are several separate admissions. A
response asking for more searches than the run can still afford has the
affordable ones performed and is stopped at the first it cannot pay for —
**before** that one happens. That is the difference between a ceiling and a
report, and it is what makes `max_search_invocations_per_run` structurally
uncrossable.

**Internet access is not reduced.** The same research happens, over the same
provider's search endpoints, and the results reach the model in the same
conversation. Only their *number* became refusable.

The charge is committed at step 4, *before* the search runs. A settlement
that waited for the reply could be lost to a crash mid-search, and a
performed search that no longer appears in the ledger is a refund by another
name. Charging first can only ever over-report — the fail-closed direction.
Correspondingly, a transport failure after step 4 is **never** refunded:
MILO cannot know whether the provider ran the search before failing, and the
model is told the search produced nothing rather than being handed invented
results.

### The residual builtin accounting

Nothing in production offers the builtin, but the adapter still accounts for
one if any caller ever sends it, and it **reserves** rather than checks.

A check is not a ceiling. The provider decides how many `$web_search` tool
calls one response asks for, so admitting a request against a single
invocation while the response can carry several means the run is admitted,
the provider bills two searches, and MILO finds out during post-response
recording — detection of money already spent. A reservation also holds across
concurrent workers, which a read-then-check never could: two requests could
both see the same single remaining invocation and both be admitted.

* **builtin (residual)** — before **each attempt** (not once per logical call:
  a retry is a real provider request that can search on its own),
  `BudgetTracker.reserve_search` holds `max_builtin_searches_per_request`,
  the worst case. The request is dispatched only if the run can pay for that.
  `settle_search` then releases the hold and charges what really happened.
* **standalone (production)** — `ProviderAdapter.search(endpoint)` reserves
  one, takes the endpoint's QPS bucket, then settles; a QPS refusal releases
  the hold rather than charging for a search MILO never performed.
  `ProviderAdapter.run_search` is that admission plus the one search it
  admits.

Reconciliation, and what "fail closed" means here:

| settlement | charged |
| --- | --- |
| response counted | the exact number of `$web_search` tool calls |
| response **uncountable** (`builtin_searches_in_response` → `None`) | the **whole reservation** — an unmeasurable amount of provider spend is not an absence of spend |
| attempt raised, and provably **never reached the provider** | **0** — nothing ran, and a ledger that over-reports is as wrong as one that under-reports |
| attempt raised otherwise (provider error, fired deadline, unknown) | the **whole reservation** |
| more searches than the reservation | all of them (truthfully), then the run **stops** with `SEARCH_MULTIPLICITY_EXCEEDED` — a per-request bound that can be exceeded without consequence is a comment |

Settling pops the reservation record, so a second settlement is a no-op
rather than a refund, and nothing is counted twice. Reservations are
**in-flight, never durable**: like the token reservations, a resumed worker
starts with none instead of inheriting a phantom hold from a dead process,
while the searches already spent are restored from the ledger snapshot.

Bounds come from the canonical runtime policy:
`max_search_invocations_per_run` (60 — the run-level hard ceiling, now taken
before each individual search executes), `max_builtin_searches_per_request`
(4 — the residual per-request bound for the builtin, which plays no part on
the mediated path) and `search_cost_per_invocation` (0.00 — a price
interface, not an invented number). Recorded search cost is real money, so it
lands in `actual_cost` and `max_cost_per_run` binds on it.

The reviewed policy fingerprint is unchanged by the mediated path: no
dimension was added, removed or re-valued.

Server-owned material bounds live in `backend/standalone_search.py`: the
query is normalized and clipped, the result set is capped, and the JSON that
re-enters a prompt is truncated to a fixed budget with the truncation
declared. Model-authored text is data on its way back into a prompt and is
never interpreted as an instruction.

One deliberate tail effect survives on the **residual** builtin route only:
when fewer than `max_builtin_searches_per_request` invocations remain, a
builtin-search request is refused even though it might have used only one.
The mediated path has no such tail — it admits one at a time, so the last
remaining invocation is usable.

## The deadline is absolute

A request is five operations — pool, connect, write, header wait, body — and
httpx gives each its own timeout. Five sub-operations each allowed D seconds
run for up to 5D with every individual timeout honoured; that is how a
request escapes a total deadline without any timeout being violated.

`allocate_request_timeouts` divides one absolute deadline so that
`pool + connect + write + read <= deadline`, and the transport writes that
allocation into `request.extensions["timeout"]`, overriding whatever
per-phase numbers the client was built with (a caller may only be tighter).
The clock is then re-checked when headers arrive and on every body chunk
against the same absolute deadline. A fired deadline is still **not** proof
the provider stopped working, so it quarantines the permit.

## What is deliberately unchanged

Immutable run identity, the export envelope, the event vocabulary and release
authorization. Those are Console 6.
