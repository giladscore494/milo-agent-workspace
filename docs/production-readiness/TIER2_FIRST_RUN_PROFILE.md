# Tier 2 first-run profile (Kimi)

This document replaces `backend/tier2_profile.py`, a module that published the
first-run profile as a machine-readable dictionary but only **re-read** its
numbers from the authorities below (removed in cleanup D10). The numbers are
not restated here; they live in exactly one place each:

| What | Authority | Pinned by |
|---|---|---|
| Reviewed first-run limits (tasks, steps, tool calls, replans, cost caps, duration, RPM/TPM, engine widths, execution cap) | `backend/runtime_policy.py` (`reviewed_first_run_policy()`) | `tests/test_runtime_policy_authority.py`, `tests/test_run_safety_contracts.py` §G |
| Provider limits, the MILO organization ceiling, search QPS | `backend/provider_quota.py` (`KIMI_TIER2_PROVIDER_LIMITS`, `MAX_*`, `SEARCH_QPS_*`) | `tests/test_run_safety_contracts.py` §G |
| Effective provider concurrency | `backend/provider_scheduler.py` (`ProviderLimitsConfig`) | `tests/test_provider_concurrency_ownership.py` |

## Two numbers that must not be confused

* the **organization ceiling** — the most MILO may ever draw from the shared
  Kimi account, `floor(provider_limit * 0.80)`: inference concurrency 32,
  RPM 80, TPM 2,400,000 against provider limits of 40 / 100 / 3,000,000 (TPD
  unlimited); and
* the **active profile** — what the first paid run is actually configured to
  use, which is deliberately far below the ceiling.

The ceiling exists so MILO cannot exhaust the account. The active profile
exists because approaching a ceiling is not a goal: capacity is only worth
taking when it shortens a run that is otherwise latency-bound, and every extra
concurrent call is extra exposure if something is wrong.

## Where the Tier evidence came from

Kimi official documentation (`/docs/pricing/limits`, `/docs/introduction`,
`/docs/api/errors`, `/docs/api/tools-search`, `/docs/api/tools-search-pro`,
`/docs/guide/troubleshooting`) plus operator-supplied account console evidence,
verified 2026-09-19 for **Tier 2**. The limits are account/organization scoped
— NOT per API key, NOT per model, NOT per process — and shared by
`vehicle_catalog_v1`, `swarm_v2`, all runs, all Cloud Run executions and all
worker processes and replicas.

## Evidence behind the active numbers

Run `3772fc84-420c-4a66-9e79-d58649d4e9b4` (V1, 2026-09-19) hit
`RUN_DURATION_EXCEEDED` at 1800s having done 113 model calls, 389,879 input and
41,443 output tokens for $0.337535 (40 agent steps), with **0 retries and 0
provider backpressure events**. It was not rate limited and did not stall:
1621s of its 1808s went to the technical-enrichment phase, which
`vehicle_catalog_v1/core.py` ran as a nested loop — 36 sequential calls at
roughly 45s each. Effective parallelism was ~1 while the account allows 40.

So the timeout was throughput, not pacing, and the fix is a modest amount of
real parallelism rather than a longer clock. The duration limit is NOT raised
(it stays at 1800s).

The V1 technical parallelism is the one number chosen to change that, and it
is chosen small: at 4, that phase falls to roughly 405s, which fits inside
1800s with room for a slower provider day. It is not set to 32 because nothing
about the evidence says the phase needs 32, and because provider latency — not
MILO's ceiling — is what the run is waiting on. Engine parallelism is a
queueing width, not a provider-concurrency grant: the scheduler admits the
policy's `provider_max_concurrency` (2) at a time, so a "parallelism 4" profile
still makes two simultaneous provider calls.

## Operating rules of the first paid run

* The first paid stage authorizes exactly one provider-using worker execution,
  with no automatic relaunch after a terminal failure.
* The hard monetary cap is USD 3.00, kept well under USD 10; the recorded-cost
  cap is no higher. The V1 evidence run cost $0.34 and no evidence says more
  is needed.
* SDK automatic retries are off: every client MILO builds passes
  `max_retries=0`, so provider attempts are counted and admitted by MILO alone
  (pinned structurally by `tests/test_run_safety_contracts.py`).

## Web Search QPS

For both standalone endpoints (`search`, `search_pro`) the QPS is a
CONSERVATIVE FALLBACK of 1 per second: the exact Tier 2 Web Search QPS was not
recoverable from the official tier table and is not invented. Each endpoint has
an independent bucket shared by V1 and V2, and search does not consume the chat quota.

These buckets never paced the provider-executed builtin `$web_search`, which
runs inside a chat call and is paced by the chat concurrency/RPM/TPM gate;
nothing in production offers that builtin any more. They do guard V1's production search path: V1 offers MILO's own `web_search` function tool, and
every invocation is admitted, performed and accounted by the one provider
authority. Volume and price are the run's: every search is counted into the
run ledger and bounded by the policy's `max_search_invocations_per_run` before
it executes.

## External usage

Kimi inference quota is account/organization scoped and shared across models,
so another application on the same account can consume capacity MILO cannot
see. A dedicated API key does NOT prove quota isolation. MILO caps itself at
80% and surfaces a limiter-drift diagnostic when the provider refuses while
MILO still believed it had headroom; it never responds by exceeding its own
ceiling.

## Provider concurrency leases (the request-lease invariant)

A unit of organization inference concurrency is returned to the pool ONLY when
MILO can prove the request that took it is over; uncertainty reduces available
capacity, never increases it. Acquire stamps the lease as held; only a
PROVEN-FINISHED request releases it; every other outcome, including a dead
process, leaves the slot held. Quarantine is the absence of an action.

* **Completion is proven when** the call returned; the provider produced a
  complete HTTP response object (any status); the failure occurred before
  anything was sent; or code on one side of the request set
  `provider_request_completed`. The proof is structural, never textual.
* **Completion is not proven when** the total request deadline fired, a read
  timed out, or only the message text of an exception looks like a provider
  error. (Retry classification still reads message text, because for a retry
  decision a permissive reading only costs a wait; it is never consulted for
  settlement.)
* **A held slot is returned by** a proven-finished release or an explicit
  operator reclaim — never automatically. The provider defines no server-side
  request lifetime, so no timer can be derived and none is invented; the Cloud
  Run `--task-timeout 3600` proves MILO's process is gone, not that Kimi has
  stopped counting the request, and the run-duration cap is cooperative.
* The opt-in `MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS` downgrades the
  guarantee, is floored by the worker lifetime and is refused in production
  (`QuotaConfig.__post_init__`, `resolve_coordinator` →
  `assert_abandoned_lease_reclaim_safe`).
* Operator recovery: `ProviderQuotaCoordinator.held_inference_leases` lists
  held slots with their age and `operator_reclaim_inference` returns one,
  recording who asserted what — through `scripts/release/provider_quota_leases.py`
  (one lease per invocation). The minimum reclaim age is necessary, not
  sufficient, and its check is atomic with the removal.
* The provider request deadline is a liveness bound, enforced by
  `backend/provider_transport.py` on total elapsed time per chunk (the SDK's
  600s read timeout would be unsafe; an inactivity timeout bounds only the
  silent header phase). It guarantees no MILO thread still awaits the response
  after the deadline and the connection is closed; it does not claim
  provider-side cancellation, which is why a fired deadline quarantines the slot
  instead of releasing it, and part of why the ceiling is 80% rather than 100%.
* The lease TTL is not a reclaim trigger; the ownership probe renews nothing
  and is visibility only; the process-local slot is taken first and the
  organization permit second, immediately before the request.
