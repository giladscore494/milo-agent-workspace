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
                                    SEARCH_QPS_VERIFIED, WINDOW_SECONDS, QuotaConfig)
from backend.engines.swarm_v2.model_gateway import ROLE_OUTPUT_CAPS, ROLE_POLICIES
from backend.runtime_policy import (CAP_ENV_PREFIXES, ENGINE_ENV_PREFIXES,
                                    MANDATORY_FOR_PAID_EXECUTION, POLICY_ENV_KEYS,
                                    PROVIDER_ENV_PREFIXES, reviewed_first_run_policy)

#: The ONE canonical runtime policy, resolved at its reviewed values. Every
#: number in ``active_profile`` below is READ from it rather than restated:
#: this module is a document ABOUT the policy, and a document that can
#: disagree with the thing it documents is the defect
#: ``backend.runtime_policy`` exists to remove.
REVIEWED_POLICY = reviewed_first_run_policy()
_P = REVIEWED_POLICY.values

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
#: Declared by the canonical policy; re-exported here for the callers that
#: have always imported it from this module.
FIRST_PAID_RUN_EXECUTION_CAP = int(_P["first_paid_run_execution_cap"])

#: Hard monetary cap. Kept well under USD 10; USD 3 is the estimated-cost
#: ceiling and the recorded-cost ceiling is tighter still, because the V1
#: evidence run cost $0.34 and no evidence says more is needed.
HARD_MONETARY_CAP_USD = float(_P["hard_monetary_cap_usd"])


def _request_lease_invariant() -> dict[str, Any]:
    """Who owns a unit of organization concurrency, and when it comes back.

    Stated here because it is the thing an operator has to be able to check
    without reading the scheduler: which outcomes return a shared slot, and
    how long one is held when MILO cannot tell.
    """
    config = QuotaConfig()
    return {
        "statement": ("a unit of organization inference concurrency is returned "
                      "to the pool ONLY when MILO can prove the request that "
                      "took it is over; uncertainty reduces available capacity, "
                      "never increases it"),
        "rule": ("acquire stamps the lease as held; only a PROVEN-FINISHED "
                 "request releases it; every other outcome, including a dead "
                 "process, leaves the slot held"),
        "quarantine_is_the_absence_of_an_action": True,
        "completion_is_proven_when": [
            "the call returned",
            "the provider produced a complete HTTP response object (any status)",
            "the failure occurred before anything was sent",
            "code on one side of the request set provider_request_completed",
        ],
        "completion_is_not_proven_when": [
            "the total request deadline fired",
            "a read timed out",
            "only the MESSAGE TEXT of an exception looks like a provider error",
            "the outcome is anything else not listed as proof",
        ],
        "proof_is_structural_never_textual": True,
        "retry_classification_is_separate_and_stays_permissive": (
            "classify_provider_error still matches message text, because for a "
            "RETRY decision a permissive reading only costs a wait; it is not "
            "consulted for settlement, where the same permissiveness would free "
            "an organization slot on the strength of a string"),
        # --- what returns a held slot, and what deliberately does not ------
        "guarantee": config.concurrency_guarantee,
        "reclaims_abandoned_leases_automatically": config.reclaims_abandoned_leases,
        "a_held_slot_is_returned_by": (
            ["a proven-finished release", "an explicit operator reclaim"]
            if not config.reclaims_abandoned_leases else
            ["a proven-finished release", "an explicit operator reclaim",
             f"the configured {config.abandoned_lease_reclaim_seconds:g}s timer"]),
        "no_provider_side_bound_exists": (
            "the provider documents concurrency as released 'as requests finish' "
            "but defines no server-side request lifetime, promises no "
            "cancellation on client disconnect, and logs 499 for a client that "
            "left 'while the server-side process is still running'; so no timer "
            "can be DERIVED, and none is invented"),
        "why_the_worker_lifetime_is_not_that_bound": (
            "Cloud Run --task-timeout 3600 plus no SIGTERM handler proves MILO's "
            "process and socket are gone; it says nothing about whether Kimi is "
            "still counting the request, which is what the ceiling is about"),
        "run_duration_cap_is_not_the_bound_either": (
            "MILO_MAX_RUN_DURATION_SECONDS is checked cooperatively inside the "
            "worker, so it does not guarantee the process or its socket is gone"),
        "worker_max_lifetime_seconds": config.worker_max_lifetime_seconds,
        "worker_max_lifetime_source": ("Cloud Run Job --task-timeout 3600 "
                                       "(scripts/deploy/cloud-run.sh:728; "
                                       "scripts/release/generate-deployment-plan.sh:297; "
                                       "asserted live in tests/test_stage_d_toolkit.py:418)"),
        "worker_max_lifetime_role": ("a FLOOR on an opt-in timer, never a licence "
                                     "for one"),
        "minimum_abandoned_lease_reclaim_seconds":
            config.minimum_abandoned_lease_reclaim_seconds,
        "opt_in_variable": "MILO_PROVIDER_ABANDONED_LEASE_RECLAIM_SECONDS",
        "opting_in_downgrades_the_guarantee": True,
        "opt_in_is_refused_in_production": True,
        "operator_recovery": ("ProviderQuotaCoordinator.held_inference_leases lists "
                              "held slots with their age; operator_reclaim_inference "
                              "returns one and records who asserted what"),
        "operator_tool": "scripts/release/provider_quota_leases.py (one lease per invocation)",
        "operator_reclaim_minimum_age_seconds": config.minimum_abandoned_lease_reclaim_seconds,
        "operator_reclaim_minimum_age_is_sufficient": False,
        "operator_reclaim_age_check_is_atomic_with_removal": True,
        "enforced_by": ("QuotaConfig.__post_init__ and resolve_coordinator -> "
                        "assert_abandoned_lease_reclaim_safe"),
        "configuration_may_only_lengthen_the_worker_lifetime": True,
        # --- the nominal request window, a liveness bound and not the above --
        "provider_request_deadline_seconds": config.request_deadline_seconds,
        "provider_request_deadline_is_a_liveness_bound": True,
        "lease_safety_margin_seconds": config.safety_margin_seconds,
        "lease_ttl_seconds": config.lease_ttl_seconds,
        "lease_ttl_is_not_a_reclaim_trigger": True,
        "ownership_probe_interval_seconds": config.ownership_probe_interval_seconds,
        "ownership_probe_is_load_bearing": False,
        "ownership_probe_renews_nothing": True,
        "ownership_probe_role": ("visibility only; the invariant holds if it "
                                 "never runs, fails every time, or never starts"),
        "acquisition_order": ("process-local slot first, organization permit "
                              "second, so the permit is taken immediately before "
                              "the request"),
        "sdk_default_read_timeout_seconds": 600,
        "sdk_default_would_be_unsafe": True,
        # An inactivity timeout does not bound a request at all: measured on
        # loopback, a trickling server ran 30.1s under a 1.5s read timeout.
        "deadline_enforced_by": "backend/provider_transport.py (total elapsed, per chunk)",
        "inactivity_timeout_bounds": "the silent header phase only",
        "guarantees": ("no MILO thread still awaiting the response after the "
                       "deadline, and the connection closed"),
        "does_not_claim": ("provider-side cancellation, or cancellation from "
                           "another thread; a server may keep computing after a "
                           "client disconnects, which is why a fired deadline "
                           "quarantines the slot instead of releasing it, and "
                           "part of why the ceiling is 80% rather than 100%"),
    }


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
            # These limiters guard the standalone /v1/tools/* endpoints, and
            # that IS the production search path now: V1 offers a model MILO's
            # own `web_search` function tool, and every invocation the model
            # asks for is admitted, performed and accounted by the one
            # provider authority (`ProviderAdapter.run_search`).
            #
            # They still do not guard the provider-executed builtin
            # `$web_search`, which runs inside a chat call and is paced by the
            # chat gate. Nothing in production offers it any more; the
            # statement is kept because the authority still accounts for one
            # if any caller ever sends it.
            "guards_the_builtin_web_search_path": False,
            "builtin_web_search_is_paced_by": "the chat concurrency/RPM/TPM gate",
            "builtin_web_search_offered_by_a_production_engine": False,
            "called_by_a_production_engine_today": True,
            "guards_the_v1_production_search_path": True,
            # PACING is the provider's bucket; VOLUME and PRICE are the run's,
            # and they exist for both routes. Every search -- mediated or
            # builtin -- is counted into the ExecutionUsageLedger by the one
            # provider authority and bounded by the runtime policy's
            # `max_search_invocations_per_run`, which is what the QPS buckets
            # never said anything about. On the mediated path that bound is
            # taken BEFORE each individual search executes, so it is a ceiling
            # the run cannot cross rather than a total it can only report.
            "accounted_in_run_ledger": True,
            "run_volume_bound_admitted_before_execution": True,
            "run_volume_bound": _P["max_search_invocations_per_run"],
            "per_request_bound": _P["max_builtin_searches_per_request"],
            "run_price_per_invocation": _P["search_cost_per_invocation"],
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
                "unique lease id per acquisition, held until completion is "
                "proven or an operator reclaims it, deterministic "
                "release on success/failure/timeout/cancellation; an expired "
                "lease can never release a replacement holder's lease"),
            # The one relationship that keeps a real request inside the life of
            # the permit it was admitted under, whatever the heartbeat does.
            "request_lease_invariant": _request_lease_invariant(),
            "tpm_admission_value": "estimated_input_tokens + explicit max_completion_tokens",
            "tpm_release_policy": (
                "never released early on lower actual output — the provider "
                "admitted against the requested cap, so the window ages out naturally"),
            "search_qps": "minimum interval between globally admitted requests per endpoint",
            "key_scope": "server-owned (MILO_PROVIDER_QUOTA_SCOPE); never client-supplied",
        },
        # Every value below is READ from the canonical runtime policy.
        # Nothing here is a second, independently maintained copy of a limit.
        "runtime_policy": {
            "authority": "backend/runtime_policy.py",
            "schema_version": REVIEWED_POLICY.document()["schema_version"],
            "fingerprint": REVIEWED_POLICY.fingerprint(),
            "required_environment": REVIEWED_POLICY.env_expectations(),
            "mandatory_for_paid_execution": sorted(
                POLICY_ENV_KEYS[name] for name in MANDATORY_FOR_PAID_EXECUTION),
            "deployment_may": "tighten any reviewed limit",
            "deployment_may_never": "widen one, silently or otherwise",
        },
        "active_profile": {
            "engine_active_concurrency": {
                # Bounded by the coordinator either way; these are the
                # per-engine values the first paid run should be configured with.
                "vehicle_catalog_v1_technical_parallelism":
                    int(_P["v1_technical_parallelism"]),
                "swarm_v2_max_active_workers": int(_P["v2_max_active_workers"]),
                # These are QUEUEING widths, not provider-concurrency grants.
                # The scheduler admits `provider_max_concurrency` at a time,
                # which the policy pins at 2 -- so a "parallelism 4" profile
                # still makes two simultaneous provider calls, and reading 4 as
                # a throughput estimate would overstate the speed-up by 2x.
                "effective_simultaneous_provider_calls":
                    int(_P["provider_max_concurrency"]),
                "effective_simultaneous_provider_calls_source": (
                    "the canonical runtime policy's provider_max_concurrency, "
                    "which ProviderLimitsConfig and Stage D both derive from"),
                "note": ("engine parallelism is a queueing width, additionally "
                         "clamped to the provider capacity actually available; "
                         "more logical workers than provider slots only queue "
                         "and burn run duration"),
            },
            "max_simultaneous_provider_using_worker_executions": FIRST_PAID_RUN_EXECUTION_CAP,
            "first_paid_run_execution_cap": FIRST_PAID_RUN_EXECUTION_CAP,
            "no_automatic_relaunch_after_terminal_failure": True,
            "rpm": int(_P["provider_rpm_limit"]),
            "tpm": int(_P["provider_tpm_limit"]),
            "search_basic_qps": int(_P["search_basic_qps"]),
            "search_pro_qps": int(_P["search_pro_qps"]),
            "role_output_caps": {f"{kind}:{phase}": cap
                                 for (kind, phase), cap in sorted(ROLE_OUTPUT_CAPS.items())},
            # PR-R: the cap bounds reasoning AND answer together; a call whose
            # budget cannot grant `min_answer_reserve` is refused, and every
            # call reserves its worst-case cost before it is sent.
            "role_call_policies": {
                f"{kind}:{phase}": {"effort": policy.effort,
                                    "max_output": policy.max_output,
                                    "min_answer_reserve": policy.min_answer_reserve,
                                    "structured_output": policy.structured}
                for (kind, phase), policy in sorted(ROLE_POLICIES.items())},
            "max_model_calls_per_run": int(_P["max_model_calls_per_run"]),
            "max_input_tokens_per_run": int(_P["max_input_tokens_per_run"]),
            "max_output_tokens_per_run": int(_P["max_output_tokens_per_run"]),
            "max_total_tokens_per_run": int(_P["max_total_tokens_per_run"]),
            "max_tasks": int(_P["max_tasks"]),
            "max_agent_steps": int(_P["max_agent_steps"]),
            "max_tool_calls": int(_P["max_tool_calls"]),
            "max_tool_calls_per_task": int(_P["max_tool_calls_per_task"]),
            "max_replans": int(_P["max_replans"]),
            "max_retries": int(_P["max_retries"]),
            "max_provider_attempts_per_call": REVIEWED_POLICY.max_provider_attempts_per_call,
            "sdk_automatic_retries": 0,
            "provider_request_timeout_seconds": QuotaConfig().request_deadline_seconds,
            "provider_lease_ttl_seconds": QuotaConfig().lease_ttl_seconds,
            "provider_lease_ownership_probe_interval_seconds":
                QuotaConfig().ownership_probe_interval_seconds,
            "provider_lease_abandoned_reclaim_seconds":
                QuotaConfig().abandoned_lease_reclaim_seconds,
            "max_run_duration_seconds": int(_P["max_run_duration_seconds"]),
            "max_concurrent_runs_per_user": int(_P["max_concurrent_runs_per_user"]),
            "max_concurrent_runs_per_project": int(_P["max_concurrent_runs_per_project"]),
            "worker_lease_seconds": 300,
            "worker_heartbeat_interval_seconds": 30,
            "provider_backpressure_stall_limit_seconds":
                int(_P["provider_max_backpressure_wait_seconds"]),
            "estimated_cost_usd": float(_P["max_estimated_cost_per_run"]),
            "hard_monetary_cap_usd": HARD_MONETARY_CAP_USD,
            "recorded_cost_cap_usd": float(_P["max_cost_per_run"]),
            "daily_user_budget_usd": float(_P["daily_user_budget"]),
            "daily_project_budget_usd": float(_P["daily_project_budget"]),
            "estimated_cost_per_call_usd": float(_P["estimated_cost_per_call"]),
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


#: The exact environment a deployment must carry to BE the reviewed profile,
#: grouped the way the release toolkit pins it. Generated, never transcribed.
def reviewed_environment() -> dict[str, dict[str, str]]:
    return {
        "caps": REVIEWED_POLICY.env_expectations(prefixes=CAP_ENV_PREFIXES),
        "provider_limits": REVIEWED_POLICY.env_expectations(prefixes=PROVIDER_ENV_PREFIXES),
        "engine_limits": REVIEWED_POLICY.env_expectations(prefixes=ENGINE_ENV_PREFIXES),
    }


__all__ = ["FIRST_PAID_RUN_EXECUTION_CAP", "HARD_MONETARY_CAP_USD",
           "REVIEWED_POLICY", "TIER_VERIFICATION", "reviewed_environment",
           "tier2_first_run_profile"]
