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
