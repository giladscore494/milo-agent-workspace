# Kimi Tier 2 limits, MILO ceilings, and how they are enforced

Research date: **2026-09-19**. Re-check the sources below, and any fresher
operator-supplied organization-console evidence, **before** changing any
constant in `backend/provider_quota.py`.

## 1. Sources consulted

Official Kimi documentation paths:

- `/docs/pricing/limits`
- `/docs/introduction`
- `/docs/api/errors`
- `/docs/api/tools-search`
- `/docs/api/tools-search-pro`
- `/docs/guide/troubleshooting`

plus **operator-supplied account-specific Tier 2 evidence** for this
organization.

## 2. Verified account-specific evidence

| Provider limit | Value |
| --- | --- |
| chat/inference concurrency | 40 |
| RPM | 100 |
| TPM | 3,000,000 |
| TPD | **Unlimited** |

### Scope — this is the part that matters

These are **account/organization** limits. They are:

- **not** scoped per API key — the rate-limit documentation states inference
  limits are not API-key scoped, and the error documentation describes
  concurrency/RPM/TPM/TPD violations as organization-level;
- **not** per model — they are shared across models;
- **not** per process, per Cloud Run execution, or per engine.

So `vehicle_catalog_v1`, `swarm_v2`, every run, every Cloud Run Job execution,
every worker process and every replica draw from **one** allowance. Where the
documentation is ambiguous, the widest shared scope is treated as authoritative
for safety.

## 3. Official limit → 80% ceiling → enforcement

| Dimension | Provider (Tier 2) | ×0.80 | MILO ceiling | Enforced by | Window / semantics |
| --- | --- | --- | --- | --- | --- |
| Inference concurrency | 40 | 32.0 | **32** | `ProviderQuotaCoordinator.try_acquire_inference` | Redis sorted-set lease per acquisition; unique id, TTL, heartbeat; released on success/failure/timeout/cancellation |
| RPM | 100 | 80.0 | **80** | `try_admit_request` (RPM leg) | Rolling 60s sorted-set window; no burst allowance above the ceiling |
| TPM | 3,000,000 | 2,400,000.0 | **2,400,000** | `try_admit_request` (TPM leg) | Rolling 60s window, weighted by `estimated_input_tokens + explicit max_completion_tokens`; never released early |
| TPD | Unlimited | — | **none derived** | `backend.budget` | No provider number exists to take 80% of. MILO's own token / daily / cost / call / step / tool / duration budgets remain mandatory |
| Web Search Basic (`/v1/tools/search`) | **unverified** | — | **1 QPS (fallback)** | `try_admit_search("search")` | Minimum interval between globally admitted requests; independent bucket |
| Web Search Pro (`/v1/tools/search_pro`) | **unverified** | — | **1 QPS (fallback)** | `try_admit_search("search_pro")` | Independent bucket; Basic and Pro never consume each other |

`floor(provider_limit * 0.80)` is the only derivation used. Nothing is rounded
up, and no ceiling may be raised without fresh authoritative evidence and a
reviewed configuration change.

## 4. Web Search QPS — explicitly unresolved

Kimi documents that:

- `/v1/tools/search` and `/v1/tools/search_pro` each have their **own** Web
  Search QPS quota;
- the two endpoints are counted **independently**;
- search QPS does **not** consume chat RPM, TPM, TPD or chat concurrency, and
  chat usage does not consume search QPS;
- a search-rate violation returns **HTTP 429**;
- search 429 responses may carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`
  and, for per-second limiting, `X-RateLimit-Reset`;
- the endpoint error vocabulary includes `project qps limit exceeded`.

**The exact Tier 2 numeric Web Search QPS was not independently recoverable**
from the public text representation of the official tier table during this
research. It is therefore **not invented**. Both endpoints enforce a
deliberately conservative **1 request per second, globally, across all MILO
engines, runs and executions**, tracked in
`provider_quota.SEARCH_QPS_FALLBACK` with
`provider_quota.SEARCH_QPS_VERIFIED[...] = False`.

When an authoritative limit `L` is verified for an endpoint:

1. set that endpoint's `SEARCH_QPS_VERIFIED` entry to `True`;
2. set its `SEARCH_QPS_FALLBACK` entry to `floor(L * 0.80)`;
3. record the evidence and its date here.

`QuotaConfig` refuses a configured search QPS above the fallback while the
endpoint remains unverified, so the number cannot drift upward by
configuration alone.

### The standalone limiter now gates the production search path

`admit_search` gates the **standalone** `/v1/tools/search` and
`/v1/tools/search_pro` endpoints, and **that is the path production takes.**
V1 offers a model MILO's own `web_search` function tool and performs each
admitted invocation itself against those endpoints
(`backend/standalone_search.py`, `ProviderAdapter.run_search`). The first-run
profile records this as `called_by_a_production_engine_today: true` and
`guards_the_v1_production_search_path: true`.

This closed the gap this section used to describe. V1 previously used the
provider-side built-in `$web_search` (`builtin_function` in a Chat Completions
request), which the standalone limiter never saw and whose multiplicity the
provider alone decided — so a run's search volume could only be observed
after the money was spent. No production engine offers the built-in any more
(`builtin_web_search_offered_by_a_production_engine: false`); the provider
authority keeps accounting for one if any caller ever sends it.

One provider-capacity fact remains **UNVERIFIED** and is not claimed:

- the exact Tier 2 numeric Web Search QPS (the conservative 1 QPS fallback
  stands, and `QuotaConfig` refuses a configured value above it).

The standalone **wire contract is now pinned from current official Kimi API
documentation**: `POST /v1/tools/search` and `/v1/tools/search_pro` accept
`text_query`, `limit` (1..20) and `timeout_seconds` (1..60), and return the
result list as `search_results`. MILO sends explicit server-owned bounds
(`limit=8`, `timeout_seconds=30`) instead of provider defaults. The transport
still reads responses defensively and never invents data from an unknown
shape. Live endpoint reachability/authentication remains a release/probe
concern; it is not a reason to describe the documented schema as unknown.

What bounds search volume regardless of either: every invocation is admitted
against `max_search_invocations_per_run` **before** it executes, so the run
ceiling holds whatever the endpoints turn out to charge or pace.

## 5. No assumed burst allowance

No authoritative burst allowance or complete provider window algorithm was
verified, so none is relied on:

- concurrency never exceeds the global permit count;
- RPM stays ≤ 80 in any rolling 60-second MILO admission window;
- TPM reservations stay ≤ 2,400,000 in any rolling 60-second window;
- each search endpoint enforces at least one second between globally admitted
  requests.

Being under MILO's ceiling does **not** make a provider 429 impossible: the
provider may apply temporary capacity or risk-control throttling below nominal
tier limits, and another application on the same organization can consume
capacity MILO cannot see (§7).

## 6. TPM admission uses the requested cap, not actual output

Kimi admits a request against request/input tokens **plus
`max_completion_tokens`** — it does not wait to observe generated output. So:

- every model request must carry a numeric output cap known **before**
  admission (`ROLE_OUTPUT_CAPS` for Swarm V2 roles; V1 has always required one;
  `budget.DEFAULT_OUTPUT_CAP` is the server-owned fail-safe);
- the admission value is `estimate_admission_tokens(messages, cap)` =
  input estimate + the cap;
- a call whose actual generation was smaller does **not** return the difference
  to the rolling window. The provider already counted the cap, so refunding it
  locally would let the account exceed real TPM. The entry ages out with the
  window instead.

Actual usage is still recorded separately for billing, analytics and MILO
budget settlement — that is a different ledger from provider rate-limit
admission.

## 7. Shared-account usage MILO cannot see

Because inference quota is not isolated per API key, another application using
the same Kimi account can consume capacity outside MILO's limiter.

MILO's response:

- its own 80% ceiling still caps **MILO**, and is never exceeded to compensate;
- a provider 429 is surfaced, not hidden. When MILO's distributed accounting
  still claimed substantial headroom,
  `ProviderQuotaCoordinator.record_rate_limit_signal` emits a
  `provider_limiter_drift` diagnostic carrying the dimension, the numeric
  header values the provider published, and MILO's own headroom — and no
  credential, URL or response body;
- provider feedback may only **pause** admissions. A header advertising more
  capacity never widens a ceiling.

**A dedicated API key does not prove quota isolation.** MILO invents no
visibility into external provider usage that Kimi does not expose.

## 8. Retry amplification

The OpenAI SDK retries retryable failures **twice by default**, turning one
logical request into up to three provider attempts. Those attempts consume
organization RPM and concurrency, but they happen inside the SDK — MILO's
scheduler, budget, attempt accounting and distributed limiter never see them.

Both client constructions therefore pass `max_retries=0`
(`backend/budget.py`, `backend/engines/vehicle_catalog_v1/core.py`), verified
by `test_hidden_sdk_retries_are_disabled_in_every_client_construction`. Retries
are MILO-owned, bounded, and re-enter `_acquire_global`, so **every real
provider attempt is admitted against the organization ceiling**.

### The 429 classes are handled distinctly

| Class | Response |
| --- | --- |
| `rate_limit_reached_error` | No spin-retry. Honour the provider wait/reset, reconcile the limiter, record which quota was reached, surface drift when MILO believed it had headroom |
| `engine_overloaded_error` | Provider capacity pressure, not proof the tier is wrong. Obey `Retry-After`, bounded exponential backoff, consumes retry/attempt budget, **never** raises concurrency |
| `exceeded_current_quota_error` | **Fail closed** for the call (`ProviderQuotaExceeded`). Not transient; retrying cannot create quota |
| search `rate_limited` | Respect `X-RateLimit-*` when present; never exceed the reviewed 80%/fallback |
| search `rate_limit_unavailable` | Back off safely; never bypass the limiter |

## 8a. Who owns a concurrency permit, and when it comes back

> **A unit of organization inference concurrency is returned to the pool ONLY
> when MILO can prove the request that took it is over.**

The corollary is the design: **uncertainty must reduce MILO's available
capacity, never increase it.** Not knowing whether a request finished is not a
reason to reuse its slot; it is a reason to keep holding it.

### What the previous version of this section got wrong

It argued safety from `provider_request_deadline + margin <= lease_ttl`, with a
transport that enforces a total wall-clock deadline. That argument bounds the
**MILO side and only the MILO side**. It proves *no MILO thread is still
awaiting this response* — which is not the same statement as *this request is
no longer consuming organization concurrency*. Nothing in the httpx or OpenAI
contract cancels an in-flight request provider-side, and a server may keep
computing after a client disconnects. At the moment the deadline fired, the
honest state of the request was **unknown**, and reclaiming the slot on a
request-sized TTL answered "unknown" by freeing capacity. A second worker could
take it while the first request might still be running.

Two things that would **not** have fixed it, and are deliberately not used:

* changing httpx timeouts (`read`/`connect`/`write`/`pool`) or the retry count
  — they move when MILO stops waiting, not when the request stops;
* wrapping the call in `future.result(timeout=…)`, `ThreadPoolExecutor`,
  `thread.join(timeout=…)` or `asyncio.wait_for(asyncio.to_thread(…))` — those
  return control to MILO while the socket and the provider-side work continue.
  **Returning control is not termination.**

### How it is established now

By making **holding the default** and **releasing the deliberate act**:

| Event | Effect on the shared slot |
| --- | --- |
| acquire | stamped as **held**, with no expiry at all by default |
| the request is **proven** over | released immediately |
| deadline fired / read timed out / anything unrecognised | **held** until an operator returns it |
| the process is killed mid-request | **held** until an operator returns it |

Quarantine is therefore not an operation that has to run — **it is the absence
of one**. A process `SIGKILL`ed between issuing a request and returning cannot
fail to quarantine its slot, because quarantining is what happens when nothing
happens. There is no failure mode in which "uncertain" degrades to "free".

`request_completion_is_proven` (`backend/provider_scheduler.py`) decides, and
its default is **NO**. Every proof is **structural** — an object the transport
or the SDK built, or a statement from code that ran on one side of the
request. None of them is the **text** of a message:

1. the call **returned** — the response was read to completion;
2. the exception carries a response **object** with an integer status code —
   the provider answered; 400, 429 and 500 alike mean the exchange is over
   (this is what keeps ordinary backpressure fast). This is the shape of the
   OpenAI SDK's `APIStatusError` family, raised only after `response.read()`.
   A bare `status_code` **attribute** with no response object is not this:
   MILO's own `AppError` carries one (its API status, not the provider's), and
   any wrapper can set one. An attribute is a claim; a response is evidence;
3. the failure happened **before anything was sent** — connect timeout,
   refused connection, unusable URL;
4. the exception carries `provider_request_completed`, set by code that knows
   which side of the request it ran on (`backend/budget.py` marks budget
   refusals, including one raised while settling a provider error).

**`classify_provider_error` is deliberately not consulted here.** A previous
revision released the slot whenever that classifier recognised the exception,
and review rightly called it out. The classifier matches raw message text —
`rate_limit_reached_error`, `error code: 429`, `overloaded` — on purpose,
because for a **retry** decision a permissive reading is the safe direction:
treating something as backpressure only costs a wait. For **concurrency** it
is the opposite. Text is not evidence that a request reached the provider, let
alone that it finished, so any exception whose message happened to contain
"429" could have freed an organization slot. The two questions want opposite
defaults, so different code answers them: the classifier stays permissive for
retries, and settlement requires structure.

### Nothing reclaims a held slot by itself

An earlier revision reclaimed an unreleased slot after
`worker_max_lifetime + margin = 3600 + 900 = 4500 s`, derived from the Cloud
Run task timeout. Review found the flaw, and it is the same species of error
as the two before it.

That evidence bounds **MILO's process**. The ceiling is stated over whether
**Kimi** is still counting the request. Those are different propositions:

> `MILO's process and socket are definitely dead`
> does **not** entail
> `the provider is definitely no longer counting this request in-flight`

The provider's own documentation does not close the gap. It defines
concurrency as "the maximum number of requests from you that we can process at
the same time" and says concurrency is "released as requests finish" — but it
never defines when a request finishes from the **server's** side, promises no
cancellation on client disconnect, and states no maximum server-side request
lifetime. The one adjacent signal points the other way: HTTP **499** is logged
for client disconnects, and 499 means by definition that the client went away
*while the server-side process is still running*.

So there is no authoritative provider-side bound to derive a timer from, and
inventing one is exactly what this document must not do. A timer would not be
a proof; it would be an assumption wearing a derivation's clothes.

**Therefore nothing reclaims an unreleased slot automatically.** A lease not
released by a proven-finished request is held until a human returns it. That
makes the guarantee one MILO can actually keep:

> MILO never admits more concurrent requests than it can prove have finished —
> whatever the provider does after a disconnect.

Mechanically: the lease's score is `+inf`, and the Redis key is `PERSIST`ed.
Both halves matter — a hygiene TTL on the key would have been a timed reclaim
by the back door, expiring every held lease in the set at once.

### The cost, and why it is the right one

A worker SIGKILLed mid-request **permanently** consumes one of the 32 slots
until an operator reclaims it. That is a real operational burden and it is
deliberate: the alternative is handing the slot to a second worker on an
assumption nobody can check.

The burden is visible and actionable rather than mysterious:

| | |
| --- | --- |
| list what is held, and since when | `ProviderQuotaCoordinator.held_inference_leases()` — each lease with its acquisition time, age, an `inf` expiry (nothing will ever return it on its own) and whether it is `recovery_eligible` |
| return one | `operator_reclaim_inference(lease_id, reason=…)` — refuses a blank reason, refuses a lease younger than the process-lifetime floor, and emits `provider_lease_operator_reclaimed` |
| every hold | `provider_lease_quarantined`, carrying the reason, the lease id, and whether anything will ever return it without a human |

### Operator reclaim: what MILO checks, and what only the human can

The acquisition time is written beside every lease in the **same atomic Lua
step** (a companion hash, persisted like the lease set). It buys two things.

**The floor.** A lease younger than `worker_max_lifetime + margin` (3600 + 900
= **4500 s**) may belong to a process that is still alive and will still
settle it on proof; reclaiming it would put two real requests under one
admission. So `operator_reclaim_inference` refuses it — `LEASE_TOO_YOUNG` —
and the age check and the removal are **one atomic store operation**, so an
operator cannot race a live holder. A lease whose acquisition time is unknown
is refused the same way. This is the Cloud Run evidence used for the one thing
it proves: a **necessary** condition, never a sufficient one.

**The judgement.** Past the floor MILO can say the process is gone and can say
nothing about the provider. Whether Kimi has stopped counting the request —
the provider console, elapsed time against any provider-side lifetime the
operator is willing to assume — is the operator's call, and that judgement is
what the recorded reason is for. The tool cannot check it and does not claim
to.

The only caller is the operator tool, asserted over the source tree by
`test_nothing_in_milo_reclaims_a_held_lease_on_its_own`:

```
python3 scripts/release/provider_quota_leases.py list
python3 scripts/release/provider_quota_leases.py recover \
    --lease-id <hex> \
    --justification "console shows 0 in-flight; worker task killed 2h ago" \
    --i-have-verified-provider-side-completion
```

`list` is read-only. `recover` removes **one** lease per invocation, by id,
with a recorded reason and an explicit attestation flag; the store token never
prints; exit 2 names the refusal (`ATTESTATION_REQUIRED`, `REASON_REQUIRED`,
`LEASE_TOO_YOUNG`, `LEASE_NOT_HELD`). Held leases and the tool are also listed
in `MONITORING_AND_INCIDENTS.md`.

### The opt-in timer: refused in production

A non-production deployment that has independently established a
provider-side bound may set `MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS`.
Doing so **replaces** the guarantee above with a weaker one, and says so —
`QuotaConfig.concurrency_guarantee` reports which is in force and the
first-run profile carries it.

**In production it is refused outright**, however the configuration was built:
`resolve_coordinator` raises on the worker path (the worker never runs
`validate_production_config`, so the check lives where the coordinator is
actually built), and `production_config.validate` reports
`TIMED_LEASE_RECLAIM_IN_PRODUCTION`. The reviewed model is that unknown
occupancy is returned by proof or by a human, never by a clock, because no
provider-side bound exists to derive one from — and one environment variable
is not the separate review that trading that guarantee away would require.
Outside production the timer remains what it is: explicit, reported, and
floored as below.

| | default | opted in |
| --- | --- | --- |
| guarantee | no slot is reused until its request is **proven** finished | …**or** the configured timer elapses, which assumes a provider-side bound MILO cannot verify |
| a held slot returns on | proven release, or operator reclaim | those, plus the clock |

The Cloud Run evidence survives as a **floor** on that timer, never a licence
for one: `--task-timeout 3600` (`scripts/deploy/cloud-run.sh:728`, re-pinned at
`generate-deployment-plan.sh:297`, asserted live at
`tests/test_stage_d_toolkit.py:418`) plus no `SIGTERM` handler means a value
below `4500 s` would reclaim a slot while MILO's *own* process could still be
running — a defect on top of the assumption it is already making. Both
`QuotaConfig` and `resolve_coordinator` refuse that.

`MILO_MAX_RUN_DURATION_SECONDS` (1800 s) is not a candidate either: it is
checked cooperatively inside the worker, so a process wedged in a syscall
sails past it. A number MILO checks is not a guarantee the process is gone.

### The ownership probe renews nothing

The previous design had a renewal loop, and its failure was the reported HIGH.
There is nothing left for renewal to do: a held lease has no expiry to renew,
and under the opt-in timer re-stamping would push a slot past the life of the
process holding it. So renewal is gone. What remains is a read-only probe — no TTL parameter in the
backend signature, no `ZADD`/`PEXPIRE`/`ZREM` in the Lua — that asks *is this
lease still recorded as mine?* and reports when it is not
(`PROVIDER_LEASE_OWNERSHIP_LOST`, `PROVIDER_LEASE_PROBE_FAILED`, static codes
with no URL, credential, provider body or exception text). **The safety
argument does not reference it**: it holds if the probe never runs, fails every
pass, or never starts its thread — which is exactly what the parametrization
over `healthy` / `returns_false` / `raises` demonstrates.

### How the request deadline is still enforced, and what it is for

It is now a **liveness** bound — how long one request may keep a MILO thread —
not the concurrency bound. An httpx timeout cannot even do that: `read` bounds
the gap *between bytes*, so a response that keeps producing data never trips
it. Measured on loopback, a server emitting one chunk every 0.2 s ran for
**30.1 s** under a 1.5 s read timeout, and stopped only because the *server*
gave up. The bound comes from `backend/provider_transport.py`, which fixes a
deadline when the request starts and checks it on every chunk. Both client
constructions are built on it (`http_client=`).

| Phase | Behaviour | Bounded by |
| --- | --- | --- |
| waiting for response headers | genuinely silent while the provider computes | the `read` inactivity timeout — the case it *does* bound correctly |
| reading the body | may trickle | the transport's total-elapsed check |

**What it guarantees:** no MILO thread is still awaiting the response after the
deadline, and the connection is closed rather than left to drain.
**What it does not claim:** provider-side cancellation, or cancellation from
another thread. That is precisely why a fired deadline **quarantines** the slot
instead of releasing it, and part of why the ceiling is 80 % rather than 100 %.

### Measured, server-side, against a provider that keeps working

`tests/test_provider_concurrency_ownership.py`. Ceiling of one, a real loopback
server that **does not stop when its client hangs up**, two coordinators over
one shared store, worker A's deadline firing while the provider is still busy.
Concurrency is counted by the server — the only honest witness — and each row
is the mean of the three ownership-probe states (healthy / disowned /
store-unreachable):

| design | requests the provider served | peak concurrent | sustained overlap | worker B |
| --- | --- | --- | --- | --- |
| **held until proven (shipped)** | **1** | **1** | **0.000 s** | refused, `ProviderBackpressureExceeded` |
| released on uncertainty (superseded) | 2 | **2** | **1.506 s** | made a second real provider request |

The second row is a **negative control** that must keep failing the first row's
assertion: it restores the superseded behaviour with a single override, so a
harness that could not see the defect could not certify its absence either.

Two further tests take the same measurement **past** the point any timer would
have fired, which is what the previous revision never tested:

| test | what it does | result |
| --- | --- | --- |
| `…not_reused_while_the_provider_is_still_busy_however_long` | keeps asking for the slot for longer than the scaled timer, while the provider is still serving | refused every time; peak **1**, overlap **0.000 s** |
| `…timed_reclaim_opt_in_does_admit_a_second_worker` | same harness, timer enabled | B enters while the provider is still working; peak **2** |

The second is the guarantee being traded away, measured rather than described
— and the reason it is not the default.

> The SDK default was the original defect: `read=600 s` against a 120 s lease,
> **five times the TTL** — and a read timeout would not have bounded the
> request even had it been shorter.

## 9. Two different numbers: ceiling vs active profile

`MILO_ORG_*` are the **organization ceilings** above. `MILO_PROVIDER_*` are one
process's **active profile** — how much of MILO's share that engine may use —
and `ProviderLimitsConfig.assert_within_organization_ceiling` refuses a profile
that exceeds the ceiling.

> Production carried `MILO_PROVIDER_RPM_LIMIT=350` against an 80 RPM ceiling,
> and `MILO_SWARM_MAX_ACTIVE_WORKERS=8` against a provider concurrency of 2.
> Both are now refused or clamped; the Stage D pinned envelope must be
> re-derived before it is used.

The recommended first-run values live in `backend/tier2_profile.py`
(`TIER2_FIRST_RUN_PROFILE`). Approaching the ceiling is **not** a goal.

### Logical parallelism is not provider concurrency

The first-run profile documents V1 technical parallelism **4** and V2 active
workers **2**. Those are *logical* widths. Every one of those threads still goes
through the one process-local `ProviderScheduler`, whose slot count is
`MILO_PROVIDER_MAX_CONCURRENCY` — default **2**, and pinned to **2** by the
Stage D envelope. So under the documented profile:

| Engine | Logical width | Effective simultaneous Kimi calls |
| --- | --- | --- |
| `vehicle_catalog_v1` technical phase | 4 | **2** |
| `swarm_v2` workers | 2 | **2** |

The technical-phase estimate of ~405 s assumed four simultaneous calls; at the
effective two it is roughly **810 s**, which still fits the unchanged 1800 s
duration cap. Four simultaneous calls require raising
`MILO_PROVIDER_MAX_CONCURRENCY` to 4 as a deliberate, separately reviewed
change. Nothing here does that.
