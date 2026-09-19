"""The machine-readable first-run profile for V1 and V2 under Kimi Tier 2.

Two different numbers live here and must not be confused:

* the **organization ceiling** -- the most MILO may ever draw from the shared
  Kimi account, ``floor(provider_limit * 0.80)``; and
* the **active profile** -- what the first paid run is actually configured to
  use, which is deliberately far below the ceiling.

The ceiling exists so MILO cannot exhaust the account. The active profile
exists because approaching a ceiling is not a goal: capacity is only worth
taking when it shortens a run that is otherwise latency-bound, and every extra
concurrent call is extra exposure if something is wrong.

Evidence behind the active numbers
----------------------------------

Run ``3772fc84-420c-4a66-9e79-d58649d4e9b4`` (V1, 2026-09-19) hit
``RUN_DURATION_EXCEEDED`` at 1800s having done 113 model calls, 389,879 input
and 41,443 output tokens for $0.337535, with **0 retries and 0 provider
backpressure events**. It was not rate limited and did not stall: 1621s of its
1808s went to the technical-enrichment phase, which
``vehicle_catalog_v1/core.py`` runs as a nested ``for`` loop -- 36 sequential
calls at roughly 45s each. Effective parallelism was ~1 while the account
allows 40.

So the timeout was throughput, not pacing, and the fix is a modest amount of
real parallelism rather than a longer clock. The duration limit is NOT raised.

The V1 technical parallelism below is the one number chosen to change that,
and it is chosen small: at 4, that phase falls to roughly 405s, which fits
inside 1800s with room for a slower provider day. It is not set to 32 because
nothing about the evidence says the phase needs 32, and because provider
latency -- not MILO's ceiling -- is what the run is waiting on.
"""

from __future__ import annotations

from typing import Any

from backend.provider_quota import (KIMI_TIER2_PROVIDER_LIMITS, MAX_INFERENCE_CONCURRENCY,
                                    MAX_RPM, MAX_TPD, MAX_TPM, SAFETY_FACTOR,
                                    SEARCH_BASIC, SEARCH_PRO, SEARCH_QPS_FALLBACK,
                                    SEARCH_QPS_VERIFIED, WINDOW_SECONDS)
from backend.engines.swarm_v2.model_gateway import ROLE_OUTPUT_CAPS

#: Where the account-specific Tier evidence came from and when.
TIER_VERIFICATION = {
    "source": "Kimi official documentation + operator-supplied account console evidence",
    "official_paths": ("/docs/pricing/limits", "/docs/introduction", "/docs/api/errors",
                       "/docs/api/tools-search", "/docs/api/tools-search-pro",
                       "/docs/guide/troubleshooting"),
    "verified_on": "2026-09-19",
    "tier": "Tier 2",
    "scope": "account/organization — NOT per API key, NOT per model, NOT per process",
    "shared_by": ("vehicle_catalog_v1", "swarm_v2", "all runs",
                  "all Cloud Run executions", "all worker processes/replicas"),
}

#: The one authorized paid worker execution for the first paid stage.
FIRST_PAID_RUN_EXECUTION_CAP = 1

#: Hard monetary cap. Kept well under USD 10; USD 3 is the estimated-cost
#: ceiling and the recorded-cost ceiling is tighter still, because the V1
#: evidence run cost $0.34 and no evidence says more is needed.
HARD_MONETARY_CAP_USD = 3.00


def _search_profile() -> dict[str, Any]:
    return {
        endpoint: {
            "verified": SEARCH_QPS_VERIFIED[endpoint],
            "qps": SEARCH_QPS_FALLBACK[endpoint],
            "basis": ("floor(L * 0.80) of a verified limit" if SEARCH_QPS_VERIFIED[endpoint]
                      else "CONSERVATIVE FALLBACK — exact Tier 2 Web Search QPS was not "
                           "recoverable from the official tier table and is not invented"),
            "bucket": "independent per endpoint; shared by V1 and V2",
            "consumes_chat_quota": False,
        }
        for endpoint in (SEARCH_BASIC, SEARCH_PRO)
    }


def tier2_first_run_profile() -> dict[str, Any]:
    """The whole profile as one serializable document."""
    return {
        "schema_version": "tier2-first-run-profile/1",
        "verification": dict(TIER_VERIFICATION),
        "provider_limits": {
            "inference_concurrency": KIMI_TIER2_PROVIDER_LIMITS["inference_concurrency"],
            "rpm": KIMI_TIER2_PROVIDER_LIMITS["rpm"],
            "tpm": KIMI_TIER2_PROVIDER_LIMITS["tpm"],
            "tpd": "Unlimited",
        },
        "milo_organization_ceiling": {
            "safety_factor": SAFETY_FACTOR,
            "inference_concurrency": MAX_INFERENCE_CONCURRENCY,
            "rpm": MAX_RPM,
            "tpm": MAX_TPM,
            "tpd": MAX_TPD,
            "tpd_treatment": (
                "no provider-derived number exists because the provider value is "
                "Unlimited; MILO's own token, daily, cost, call, step, tool and "
                "duration budgets remain mandatory and are enforced by backend.budget"),
            "window_seconds": WINDOW_SECONDS,
        },
        "web_search_qps": _search_profile(),
        "enforcement": {
            "implementation": "backend/provider_quota.py",
            "store": "shared Upstash Redis (UPSTASH_REDIS_REST_*); fails closed in production",
            "atomicity": "one Lua EVAL per admission decision",
            "rolling_window": (
                "sorted-set rolling window over the last 60s for RPM and TPM; "
                "no burst allowance above the ceiling is assumed"),
            "concurrency_lease": (
                "unique lease id per acquisition, TTL + heartbeat, deterministic "
                "release on success/failure/timeout/cancellation; an expired "
                "lease can never release a replacement holder's lease"),
            "tpm_admission_value": "estimated_input_tokens + explicit max_completion_tokens",
            "tpm_release_policy": (
                "never released early on lower actual output — the provider "
                "admitted against the requested cap, so the window ages out naturally"),
            "search_qps": "minimum interval between globally admitted requests per endpoint",
            "key_scope": "server-owned (MILO_PROVIDER_QUOTA_SCOPE); never client-supplied",
        },
        "active_profile": {
            "engine_active_concurrency": {
                # Bounded by the coordinator either way; these are the
                # per-engine values the first paid run should be configured with.
                "vehicle_catalog_v1_technical_parallelism": 4,
                "swarm_v2_max_active_workers": 2,
                "note": ("engine parallelism is additionally clamped to the provider "
                         "capacity actually available; more logical workers than "
                         "provider slots only queue and burn run duration"),
            },
            "max_simultaneous_provider_using_worker_executions": FIRST_PAID_RUN_EXECUTION_CAP,
            "first_paid_run_execution_cap": FIRST_PAID_RUN_EXECUTION_CAP,
            "no_automatic_relaunch_after_terminal_failure": True,
            "rpm": 40,
            "tpm": 1_200_000,
            "search_basic_qps": SEARCH_QPS_FALLBACK[SEARCH_BASIC],
            "search_pro_qps": SEARCH_QPS_FALLBACK[SEARCH_PRO],
            "role_output_caps": {f"{kind}:{phase}": cap
                                 for (kind, phase), cap in sorted(ROLE_OUTPUT_CAPS.items())},
            "max_model_calls_per_run": 150,
            "max_input_tokens_per_run": 500_000,
            "max_output_tokens_per_run": 120_000,
            "max_total_tokens_per_run": 600_000,
            "max_tasks": 23,
            "max_agent_steps": 56,
            "max_tool_calls": 24,
            "max_replans": 1,
            "max_retries": 15,
            "max_provider_attempts_per_call": 1 + 5,
            "sdk_automatic_retries": 0,
            "max_run_duration_seconds": 1800,
            "worker_lease_seconds": 300,
            "worker_heartbeat_interval_seconds": 30,
            "provider_backpressure_stall_limit_seconds": 240,
            "estimated_cost_usd": 3.00,
            "hard_monetary_cap_usd": HARD_MONETARY_CAP_USD,
            "recorded_cost_cap_usd": 1.00,
        },
        "evidence": {
            "run_id": "3772fc84-420c-4a66-9e79-d58649d4e9b4",
            "outcome": "timed_out (RUN_DURATION_EXCEEDED at 1800s)",
            "model_calls": 113, "input_tokens": 389_879, "output_tokens": 41_443,
            "actual_cost_usd": 0.337535, "agent_steps": 40,
            "retries": 0, "provider_backpressure_events": 0,
            "cause": ("throughput, not pacing: 1621s of 1808s in a technical phase "
                      "that runs 36 calls sequentially at ~45s each"),
            "duration_limit_raised": False,
        },
        "external_usage": {
            "isolated_per_api_key": False,
            "note": ("Kimi inference quota is account/organization scoped and shared "
                     "across models, so another application on the same account can "
                     "consume capacity MILO cannot see. A dedicated API key does NOT "
                     "prove quota isolation. MILO caps ITSELF at 80% and surfaces a "
                     "limiter-drift diagnostic when the provider refuses while MILO "
                     "still believed it had headroom; it never responds by exceeding "
                     "its own ceiling."),
        },
    }


__all__ = ["FIRST_PAID_RUN_EXECUTION_CAP", "HARD_MONETARY_CAP_USD",
           "TIER_VERIFICATION", "tier2_first_run_profile"]
