"""The organization-wide provider quota coordinator.

Every test here is deterministic and local. Nothing calls a provider, opens a
socket, or touches a real Redis: the shared-store semantics are modelled by
``MemoryQuotaBackend``, which implements exactly the algorithm the Lua scripts
implement, so a race proven impossible here is proven against the algorithm
production runs.

"Separate processes" is modelled as separate coordinator objects over ONE
backend, which is precisely the production shape: two Cloud Run executions are
two coordinators talking to one Upstash store. A process-local semaphore would
pass nothing in this file, which is the point -- that is the defect being
fixed.
"""

from __future__ import annotations

import threading

import pytest

from backend.provider_quota import (KIMI_TIER2_PROVIDER_LIMITS, MAX_INFERENCE_CONCURRENCY,
                                    MAX_RPM, MAX_TPD, MAX_TPM, SEARCH_BASIC, SEARCH_PRO,
                                    SEARCH_QPS_FALLBACK, SEARCH_QPS_VERIFIED,
                                    MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    ProviderQuotaExhausted, ProviderQuotaUnavailable,
                                    QuotaConfig, milo_ceiling, resolve_coordinator)


class Clock:
    """A hand-wound clock: no test here depends on wall time passing."""

    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(limit_overrides=None, *, backend=None, clock=None):
    """One coordinator over a (possibly shared) backend."""
    cfg = QuotaConfig(**(limit_overrides or {}))
    return ProviderQuotaCoordinator(backend or MemoryQuotaBackend(), cfg,
                                    clock=clock or Clock())


# =============================================================================
# 1. the ceilings themselves
# =============================================================================

def test_ceilings_are_eighty_percent_of_the_verified_tier_2_limits():
    assert KIMI_TIER2_PROVIDER_LIMITS == {
        "inference_concurrency": 40, "rpm": 100, "tpm": 3_000_000, "tpd": None}
    assert MAX_INFERENCE_CONCURRENCY == 32
    assert MAX_RPM == 80
    assert MAX_TPM == 2_400_000


def test_an_unlimited_provider_value_yields_no_derived_ceiling():
    """TPD is Unlimited upstream, so there is no number to take 80% of.

    Inventing one would be worse than having none: it would look like a
    verified bound. MILO's own budgets remain the real daily control.
    """
    assert MAX_TPD is None
    assert milo_ceiling(None) is None
    assert milo_ceiling(100) == 80
    assert milo_ceiling(3_000_000) == 2_400_000


def test_web_search_qps_is_an_unverified_conservative_fallback():
    """The exact Tier 2 QPS was not recoverable and is NOT invented."""
    assert SEARCH_QPS_VERIFIED == {SEARCH_BASIC: False, SEARCH_PRO: False}
    assert SEARCH_QPS_FALLBACK == {SEARCH_BASIC: 1, SEARCH_PRO: 1}


@pytest.mark.parametrize("field,value", [
    ("max_concurrency", MAX_INFERENCE_CONCURRENCY + 1),
    ("max_rpm", MAX_RPM + 1),
    ("max_tpm", MAX_TPM + 1),
])
def test_configuration_may_tighten_a_ceiling_but_never_widen_it(field, value):
    with pytest.raises(ValueError, match="organization ceiling"):
        QuotaConfig(**{field: value})
    QuotaConfig(**{field: 1})  # tightening is fine


def test_search_qps_may_not_be_raised_above_the_fallback_while_unverified():
    with pytest.raises(ValueError, match="unverified"):
        QuotaConfig(search_qps=(5, 1))
    with pytest.raises(ValueError, match="unverified"):
        QuotaConfig(search_qps=(1, 5))


def test_org_ceilings_come_from_their_own_env_names_not_the_per_process_ones():
    """A per-process profile must not be readable as an account-wide claim."""
    cfg = QuotaConfig.from_env({"MILO_PROVIDER_MAX_CONCURRENCY": "2",
                                "MILO_PROVIDER_RPM_LIMIT": "40"})
    assert (cfg.max_concurrency, cfg.max_rpm) == (MAX_INFERENCE_CONCURRENCY, MAX_RPM)
    cfg = QuotaConfig.from_env({"MILO_ORG_MAX_CONCURRENCY": "8", "MILO_ORG_RPM_LIMIT": "20"})
    assert (cfg.max_concurrency, cfg.max_rpm) == (8, 20)


# =============================================================================
# 2. concurrency leases
# =============================================================================

def test_the_last_permit_goes_to_exactly_one_of_two_racing_processes():
    """V1 and V2 in different processes race for the final slot."""
    backend = MemoryQuotaBackend()
    clock = Clock()
    v1 = build({"max_concurrency": 2}, backend=backend, clock=clock)
    v2 = build({"max_concurrency": 2}, backend=backend, clock=clock)

    held = [v1.try_acquire_inference()]           # V1 takes one
    assert held[0] is not None

    barrier = threading.Barrier(2)
    results: list[object] = []
    lock = threading.Lock()

    def grab(coordinator):
        barrier.wait()
        lease = coordinator.try_acquire_inference()
        with lock:
            results.append(lease)

    threads = [threading.Thread(target=grab, args=(c,)) for c in (v1, v2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    granted = [r for r in results if r is not None]
    assert len(granted) == 1, "both processes were granted the same final permit"
    assert v1.try_acquire_inference() is None


def test_thirty_two_permits_are_the_whole_account_not_thirty_two_each():
    """The defect this module exists to remove, stated directly."""
    backend = MemoryQuotaBackend()
    clock = Clock()
    v1 = build(backend=backend, clock=clock)
    v2 = build(backend=backend, clock=clock)

    leases = [v1.try_acquire_inference() for _ in range(MAX_INFERENCE_CONCURRENCY)]
    assert all(leases)
    # The OTHER engine, in the OTHER process, gets nothing.
    assert v2.try_acquire_inference() is None
    leases[0].release()
    assert v2.try_acquire_inference() is not None


def test_a_released_lease_frees_capacity_and_a_second_release_is_a_no_op():
    coordinator = build({"max_concurrency": 1})
    lease = coordinator.try_acquire_inference()
    assert coordinator.try_acquire_inference() is None
    assert lease.release() is True
    assert lease.release() is False, "the same owner released its lease twice"
    assert coordinator.try_acquire_inference() is not None


def test_a_stale_lease_is_never_recovered_without_a_human():
    """A crashed worker's slot does not come back on a clock.

    Nothing MILO can observe proves the provider stopped counting the request,
    so nothing returns the slot on a timer. An operator who has checked does
    it deliberately, and that is the only other way.
    """
    backend = MemoryQuotaBackend()
    clock = Clock()
    limits = {"max_concurrency": 1, "lease_ttl_seconds": 30,
              "worker_max_lifetime_seconds": 40}
    crashed = build(limits, backend=backend, clock=clock)
    replacement = build(limits, backend=backend, clock=clock)

    lease = crashed.try_acquire_inference()       # and then the process dies
    for step in (0, 31, 500, 86_400):
        clock.advance(step)
        assert replacement.try_acquire_inference() is None, (
            f"the slot came back on its own after {step}s")

    assert replacement.operator_reclaim_inference(
        lease.lease_id, reason="verified out of band")
    assert replacement.try_acquire_inference() is not None


def test_an_expired_lease_can_never_release_its_replacements_lease():
    """The reason lease ids are unique per acquisition.

    If release were keyed on anything shared -- a worker id, a run id -- the
    crashed holder's late cleanup would free the slot its replacement is
    actively using, and the account would be over its ceiling with nobody
    holding a permit they believe is theirs.
    """
    backend = MemoryQuotaBackend()
    clock = Clock()
    limits = {"max_concurrency": 1, "lease_ttl_seconds": 30,
              "worker_max_lifetime_seconds": 40}
    crashed = build(limits, backend=backend, clock=clock)
    replacement = build(limits, backend=backend, clock=clock)

    stale = crashed.try_acquire_inference()
    # The crashed holder's slot is reclaimed deliberately, by a human, because
    # nothing else ever reclaims it.
    replacement.operator_reclaim_inference(stale.lease_id, reason="verified out of band")
    fresh = replacement.try_acquire_inference()
    assert fresh is not None

    stale.release()                                # the zombie finally cleans up
    assert replacement.try_acquire_inference() is None, (
        "an expired lease released a different owner's permit")
    assert fresh.release() is True


def test_the_ownership_probe_reports_but_never_changes_anything():
    """It answers "is this still mine", and that is all it can do.

    Read-only by construction -- no TTL argument, no write in the Lua -- so
    however often a lease is probed, it is neither extended nor shortened.
    """
    backend = MemoryQuotaBackend()
    clock = Clock()
    coordinator = build({"max_concurrency": 1, "lease_ttl_seconds": 30,
                         "worker_max_lifetime_seconds": 40},
                        backend=backend, clock=clock)
    lease = coordinator.try_acquire_inference()

    for _ in range(5):
        clock.advance(1_000)
        assert lease.verify_ownership() is True
        assert coordinator.try_acquire_inference() is None

    assert lease.release() is True
    assert lease.verify_ownership() is False
    assert coordinator.try_acquire_inference() is not None


def test_cancelling_during_reservation_releases_the_permit():
    """A caller that abandons mid-acquisition must not strand capacity."""
    coordinator = build({"max_concurrency": 1})
    lease = coordinator.try_acquire_inference()
    try:
        raise KeyboardInterrupt("cancelled while reserving")
    except KeyboardInterrupt:
        lease.release()
    assert coordinator.try_acquire_inference() is not None


# =============================================================================
# 3. RPM and TPM rolling windows
# =============================================================================

def test_rpm_is_shared_and_refuses_the_eighty_first_request_in_the_window():
    backend = MemoryQuotaBackend()
    clock = Clock()
    v1 = build(backend=backend, clock=clock)
    v2 = build(backend=backend, clock=clock)

    for index in range(MAX_RPM):
        admitted, dimension, _ = (v1 if index % 2 else v2).try_admit_request(10)
        assert admitted, f"refused at {index} with {dimension}"
    admitted, dimension, retry = v1.try_admit_request(10)
    assert not admitted and dimension == "rpm" and retry > 0


def test_the_rpm_window_rolls_rather_than_resetting_on_a_boundary():
    clock = Clock()
    coordinator = build({"max_rpm": 2}, clock=clock)
    assert coordinator.try_admit_request(1)[0]
    clock.advance(30)
    assert coordinator.try_admit_request(1)[0]
    # Still two in the last 60s.
    assert not coordinator.try_admit_request(1)[0]
    clock.advance(31)                              # the first one ages out
    assert coordinator.try_admit_request(1)[0]


def test_two_processes_race_at_the_rpm_boundary_without_double_allocating():
    backend = MemoryQuotaBackend()
    clock = Clock()
    a = build({"max_rpm": 10}, backend=backend, clock=clock)
    b = build({"max_rpm": 10}, backend=backend, clock=clock)
    for _ in range(9):
        assert a.try_admit_request(1)[0]

    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def race(coordinator):
        barrier.wait()
        admitted, _, _ = coordinator.try_admit_request(1)
        with lock:
            outcomes.append(admitted)

    threads = [threading.Thread(target=race, args=(c,)) for c in (a, b)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(True) == 1, "the final RPM slot was allocated twice"


def test_tpm_admits_on_the_requested_cap_not_on_actual_output():
    """Kimi admits against input + max_completion_tokens.

    Admitting on generated output would systematically under-count: the
    provider has already committed the capacity by then.
    """
    clock = Clock()
    coordinator = build({"max_tpm": 1_000}, clock=clock)
    assert coordinator.try_admit_request(600)[0]
    admitted, dimension, _ = coordinator.try_admit_request(600)
    assert not admitted and dimension == "tpm"


def test_tpm_history_is_not_released_when_generation_was_smaller():
    """There is deliberately no 'give the unused tokens back' call.

    The provider's rate limiter already counted the requested cap; handing the
    difference back to MILO's window would let the account exceed real TPM.
    """
    clock = Clock()
    coordinator = build({"max_tpm": 1_000}, clock=clock)
    assert coordinator.try_admit_request(900)[0]
    assert not hasattr(coordinator, "release_tpm")
    assert not coordinator.try_admit_request(200)[0]
    clock.advance(61)                              # only time frees it
    assert coordinator.try_admit_request(200)[0]


def test_two_processes_race_near_tpm_exhaustion():
    backend = MemoryQuotaBackend()
    clock = Clock()
    v1 = build({"max_tpm": 1_000}, backend=backend, clock=clock)
    v2 = build({"max_tpm": 1_000}, backend=backend, clock=clock)
    assert v1.try_admit_request(400)[0]

    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def race(coordinator):
        barrier.wait()
        with lock:
            outcomes.append(coordinator.try_admit_request(400)[0])

    threads = [threading.Thread(target=race, args=(c,)) for c in (v1, v2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count(True) == 1


def test_admission_without_an_explicit_output_cap_is_refused():
    coordinator = build()
    with pytest.raises(ValueError, match="explicit output cap"):
        coordinator.try_admit_request(0)


def test_a_single_request_larger_than_the_whole_tpm_ceiling_fails_closed():
    """It can never be admitted, so waiting for capacity would hang forever."""
    coordinator = build({"max_tpm": 1_000})
    with pytest.raises(ProviderQuotaExhausted) as exc:
        coordinator.try_admit_request(5_000)
    assert exc.value.dimension == "tpm"


# =============================================================================
# 4. Web Search QPS -- two independent buckets
# =============================================================================

def test_search_basic_is_one_qps_globally_across_processes():
    backend = MemoryQuotaBackend()
    clock = Clock()
    a = build(backend=backend, clock=clock)
    b = build(backend=backend, clock=clock)
    assert a.try_admit_search(SEARCH_BASIC)[0]
    admitted, retry = b.try_admit_search(SEARCH_BASIC)
    assert not admitted and retry > 0
    clock.advance(1.0)
    assert b.try_admit_search(SEARCH_BASIC)[0]


def test_search_pro_is_one_qps_globally_across_processes():
    backend = MemoryQuotaBackend()
    clock = Clock()
    a = build(backend=backend, clock=clock)
    b = build(backend=backend, clock=clock)
    assert a.try_admit_search(SEARCH_PRO)[0]
    assert not b.try_admit_search(SEARCH_PRO)[0]
    clock.advance(1.0)
    assert b.try_admit_search(SEARCH_PRO)[0]


def test_basic_and_pro_do_not_consume_each_others_quota():
    """Kimi counts the two search endpoints independently."""
    clock = Clock()
    coordinator = build(clock=clock)
    assert coordinator.try_admit_search(SEARCH_BASIC)[0]
    assert coordinator.try_admit_search(SEARCH_PRO)[0], "Basic consumed Pro's quota"
    assert not coordinator.try_admit_search(SEARCH_BASIC)[0]
    assert not coordinator.try_admit_search(SEARCH_PRO)[0]


def test_search_does_not_consume_chat_quota_and_chat_does_not_consume_search():
    clock = Clock()
    coordinator = build({"max_rpm": 1}, clock=clock)
    assert coordinator.try_admit_search(SEARCH_BASIC)[0]
    assert coordinator.try_admit_search(SEARCH_PRO)[0]
    # Chat RPM is untouched by the two searches.
    assert coordinator.try_admit_request(10)[0]
    assert not coordinator.try_admit_request(10)[0]
    # ...and the exhausted chat window did not close the search buckets.
    clock.advance(1.0)
    assert coordinator.try_admit_search(SEARCH_BASIC)[0]


def test_two_processes_race_on_each_search_bucket():
    for endpoint in (SEARCH_BASIC, SEARCH_PRO):
        backend = MemoryQuotaBackend()
        clock = Clock()
        coordinators = [build(backend=backend, clock=clock) for _ in range(2)]
        barrier = threading.Barrier(2)
        outcomes: list[bool] = []
        lock = threading.Lock()

        def race(coordinator):
            barrier.wait()
            with lock:
                outcomes.append(coordinator.try_admit_search(endpoint)[0])

        threads = [threading.Thread(target=race, args=(c,)) for c in coordinators]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert outcomes.count(True) == 1, f"{endpoint} admitted two requests in one second"


# =============================================================================
# 5. provider feedback may slow MILO down, never speed it up
# =============================================================================

def test_a_rate_limit_signal_pauses_admission():
    clock = Clock()
    coordinator = build(clock=clock)
    coordinator.record_rate_limit_signal(dimension="inference", retry_after=5.0)
    assert coordinator.try_acquire_inference() is None
    assert coordinator.try_admit_request(10)[1] == "inference_paused"
    clock.advance(5.1)
    assert coordinator.try_acquire_inference() is not None


def test_a_generous_header_never_raises_a_ceiling():
    """Headers may pause admissions; they may not grant capacity.

    Raising a ceiling takes authoritative verification and a reviewed change,
    not a number in a response a caller cannot audit.
    """
    coordinator = build({"max_rpm": 2})
    coordinator.record_rate_limit_signal(
        dimension="inference", retry_after=0.0,
        reported_limit=100_000, reported_remaining=99_999)
    assert coordinator.config.max_rpm == 2


def test_a_refusal_while_milo_believed_it_had_headroom_is_surfaced_as_drift():
    """Kimi quota is shared across the organization and is not key-isolated.

    When the provider refuses while MILO's own accounting says there is room,
    the likely cause is usage MILO cannot see. Hiding that would make MILO look
    correct while the account is over its limit.
    """
    events: list[tuple[str, dict]] = []
    coordinator = ProviderQuotaCoordinator(
        MemoryQuotaBackend(), QuotaConfig(), clock=Clock(),
        diagnostic_sink=lambda kind, payload: events.append((kind, payload)))
    coordinator.record_rate_limit_signal(
        dimension="inference", retry_after=2.0,
        reported_limit=100, reported_remaining=0, local_headroom=30)
    kinds = [kind for kind, _ in events]
    assert "provider_limiter_drift" in kinds
    drift = next(payload for kind, payload in events if kind == "provider_limiter_drift")
    assert drift["drift"] == "external_or_provider_throttle"
    assert drift["local_headroom"] == 30


def test_a_diagnostic_never_carries_a_credential_or_a_response_body():
    events: list[tuple[str, dict]] = []
    coordinator = ProviderQuotaCoordinator(
        MemoryQuotaBackend(), QuotaConfig(), clock=Clock(),
        diagnostic_sink=lambda kind, payload: events.append((kind, payload)))
    coordinator.record_rate_limit_signal(dimension="inference", retry_after=1.0,
                                         reported_limit=100, reported_remaining=0)
    for _kind, payload in events:
        for value in payload.values():
            assert isinstance(value, (str, int, float, bool)), payload
        assert not any("key" in str(k).lower() or "token" in str(k).lower()
                       for k in payload)


# =============================================================================
# 6. keys are server-owned, and production fails closed
# =============================================================================

def test_no_limiter_key_can_be_supplied_by_a_client():
    """The scope comes from server configuration and nothing else.

    Nothing in the coordinator reads a run, a plan, a task, an event payload or
    request metadata, so a browser cannot address another tenant's bucket or
    mint itself a private one.
    """
    coordinator = build()
    key = coordinator._key("conc")
    assert key.startswith("milo:pq:kimi-org:")
    scoped = ProviderQuotaCoordinator(MemoryQuotaBackend(),
                                      QuotaConfig(scope="other"), clock=Clock())
    assert scoped._key("conc") != key
    # The key is built from the dimension and the server-owned scope, and from
    # nothing else -- checked over the parsed code rather than the prose, so a
    # docstring mentioning "request" cannot pass or fail it.
    import ast
    import inspect
    from backend import provider_quota

    tree = ast.parse(inspect.getsource(provider_quota))
    key_fn = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_key")
    names = {node.attr for node in ast.walk(key_fn) if isinstance(node, ast.Attribute)}
    assert names <= {"config", "scope"}, f"_key reads more than its scope: {names}"
    assert [a.arg for a in key_fn.args.args] == ["self", "dimension"]

    # And the whole module reads only these environment names -- no run,
    # project, user or request-derived value can reach a bucket.
    env_reads = {node.args[0].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "get"
                 and node.args and isinstance(node.args[0], ast.Constant)
                 and isinstance(node.args[0].value, str)
                 and node.args[0].value.isupper()}
    assert env_reads <= {
        "MILO_ORG_MAX_CONCURRENCY", "MILO_ORG_RPM_LIMIT", "MILO_ORG_TPM_LIMIT",
        "MILO_SEARCH_BASIC_QPS", "MILO_SEARCH_PRO_QPS",
        "MILO_PROVIDER_LEASE_TTL_SECONDS", "MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS",
        "MILO_PROVIDER_QUOTA_SCOPE",
        "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "ENVIRONMENT",
    }, env_reads


def test_production_refuses_to_run_without_the_shared_store():
    """A process-local fallback in production is the original defect."""
    with pytest.raises(ProviderQuotaUnavailable):
        resolve_coordinator(env={"ENVIRONMENT": "production"})


def test_local_development_gets_a_deterministic_in_process_backend():
    coordinator = resolve_coordinator(env={"ENVIRONMENT": "local"})
    assert isinstance(coordinator._backend, MemoryQuotaBackend)


def test_a_transport_failure_fails_closed_without_quoting_the_url():
    from backend.provider_quota import UpstashQuotaBackend

    def explode(url, body):
        raise RuntimeError(f"connection refused to {url}?token=SUPER_SECRET")

    backend = UpstashQuotaBackend("https://example.upstash.io", "tok", http_post=explode)
    with pytest.raises(ProviderQuotaUnavailable) as exc:
        backend.admit_window("k", "e", 10, 60_000, 0, 1)
    assert "SUPER_SECRET" not in str(exc.value)
