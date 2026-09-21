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

## Search accounting

QPS is the provider's pacing bucket for the **standalone** `/v1/tools/search`
and `/v1/tools/search_pro` endpoints. It never said anything about how many
searches a run may perform, and it does not touch V1's builtin
`$web_search` at all — the provider runs and bills that inside a chat call.

The adapter accounts both routes, and it **reserves** rather than checks.

A check is not a ceiling. The provider decides how many `$web_search` tool
calls one response asks for, so admitting a request against a single
invocation while the response can carry several means the run is admitted,
the provider bills two searches, and MILO finds out during post-response
recording — detection of money already spent. A reservation also holds across
concurrent workers, which a read-then-check never could: two requests could
both see the same single remaining invocation and both be admitted.

* **builtin** — before **each attempt** (not once per logical call: a retry is
  a real provider request that can search on its own),
  `BudgetTracker.reserve_search` holds `max_builtin_searches_per_request`,
  the worst case. The request is dispatched only if the run can pay for that.
  `settle_search` then releases the hold and charges what really happened.
* **standalone** — `ProviderAdapter.search(endpoint)` reserves one, takes the
  endpoint's QPS bucket, then settles; a QPS refusal releases the hold rather
  than charging for a search MILO never performed.

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
`max_search_invocations_per_run` (60), `max_builtin_searches_per_request` (4 —
MILO's own enforced ceiling, not a provider fact) and
`search_cost_per_invocation` (0.00 — a price interface, not an invented
number). Recorded search cost is real money, so it lands in `actual_cost` and
`max_cost_per_run` binds on it.

One deliberate tail effect: when fewer than `max_builtin_searches_per_request`
invocations remain, a search-enabled request is refused even though it might
have used only one. That is the fail-closed direction, and the alternative is
a ceiling that is not one.

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
