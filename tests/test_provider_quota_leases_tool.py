"""The operator tool is the ONLY way a held lease leaves the store unproven.

Everything here runs over the deterministic in-memory backend through the
tool's injection seam; nothing touches a real store, and the tool itself
refuses to run without the shared store's configuration.
"""

from __future__ import annotations

import io
import json
import time

from backend.provider_quota import (MemoryQuotaBackend, ProviderQuotaCoordinator,
                                    QuotaConfig)
from scripts.release import provider_quota_leases as tool

ENV = {"UPSTASH_REDIS_REST_URL": "https://example.invalid",
       "UPSTASH_REDIS_REST_TOKEN": "secret-token-that-must-never-print"}


def run(argv, backend, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(argv, env=env, backend_factory=lambda _env: backend, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def held_lease(backend, *, age_seconds: float):
    """A lease taken `age_seconds` ago by a process that never settled it."""
    coordinator = ProviderQuotaCoordinator(
        backend, QuotaConfig(), clock=lambda: time.time() - age_seconds)
    lease = coordinator.try_acquire_inference()
    assert lease is not None
    return lease.lease_id


def test_list_is_read_only_and_shows_eligibility():
    backend = MemoryQuotaBackend()
    young = held_lease(backend, age_seconds=60)
    old = held_lease(backend, age_seconds=5000)
    code, out, err = run(["list"], backend)
    assert code == 0 and err == ""
    report = json.loads(out)
    assert report["held"] == 2
    assert report["minimum_age_for_reclaim_seconds"] == 4500.0
    assert report["guarantee"] == "proven_completion_only" or "proven" in report["guarantee"]
    by_id = {item["lease_id"]: item for item in report["leases"]}
    assert by_id[young]["recovery_eligible"] is False
    assert by_id[old]["recovery_eligible"] is True
    assert by_id[old]["expires"] is None, "a held lease carries a finite expiry"
    assert [item["lease_id"] for item in report["leases"]] == [old, young], "oldest first"
    assert len(backend.held_concurrency("milo:pq:kimi-org:conc", 0)) == 2


def test_recover_requires_the_attestation_flag():
    backend = MemoryQuotaBackend()
    old = held_lease(backend, age_seconds=5000)
    code, _out, err = run(["recover", "--lease-id", old, "--justification", "checked"], backend)
    assert code == 2
    assert json.loads(err)["refused"] == "ATTESTATION_REQUIRED"
    assert len(backend.held_concurrency("milo:pq:kimi-org:conc", 0)) == 1


def test_recover_refuses_a_lease_whose_process_may_still_be_alive():
    backend = MemoryQuotaBackend()
    young = held_lease(backend, age_seconds=60)
    code, _out, err = run(["recover", "--lease-id", young, "--justification", "checked",
                           tool.ATTEST_FLAG], backend)
    assert code == 2
    assert json.loads(err)["refused"] == "LEASE_TOO_YOUNG"
    assert len(backend.held_concurrency("milo:pq:kimi-org:conc", 0)) == 1


def test_recover_refuses_without_a_justification():
    backend = MemoryQuotaBackend()
    old = held_lease(backend, age_seconds=5000)
    code, _out, err = run(["recover", "--lease-id", old, "--justification", "  ",
                           tool.ATTEST_FLAG], backend)
    assert code == 2
    assert json.loads(err)["refused"] == "REASON_REQUIRED"


def test_recover_removes_exactly_one_eligible_lease_and_records_why():
    backend = MemoryQuotaBackend()
    first = held_lease(backend, age_seconds=5000)
    second = held_lease(backend, age_seconds=6000)
    code, out, err = run(["recover", "--lease-id", first,
                          "--justification", "console shows 0 in-flight; task killed 2h ago",
                          tool.ATTEST_FLAG], backend)
    assert code == 0, err
    report = json.loads(out)
    assert report["recovered"] == first
    recovered = [e for e in report["events"] if e["event"] == "provider_lease_operator_reclaimed"]
    assert recovered and recovered[0]["lease_id"] == first
    assert "console shows 0 in-flight" in recovered[0]["reason"]
    remaining = [lease_id for lease_id, _, _ in backend.held_concurrency("milo:pq:kimi-org:conc", 0)]
    assert remaining == [second], "more than the one named lease was recovered"


def test_recovering_a_lease_twice_is_refused_not_ignored():
    backend = MemoryQuotaBackend()
    old = held_lease(backend, age_seconds=5000)
    argv = ["recover", "--lease-id", old, "--justification", "checked", tool.ATTEST_FLAG]
    assert run(argv, backend)[0] == 0
    code, _out, err = run(argv, backend)
    assert code == 2 and json.loads(err)["refused"] == "LEASE_NOT_HELD"


def test_the_token_never_reaches_stdout_or_stderr():
    backend = MemoryQuotaBackend()
    old = held_lease(backend, age_seconds=5000)
    for argv in (["list"],
                 ["recover", "--lease-id", old, "--justification", "checked", tool.ATTEST_FLAG],
                 ["recover", "--lease-id", "missing", "--justification", "x", tool.ATTEST_FLAG]):
        _code, out, err = run(argv, backend)
        assert ENV["UPSTASH_REDIS_REST_TOKEN"] not in out + err


def test_the_tool_refuses_to_run_without_the_shared_store():
    """There is no in-memory fallback: a lease that is not in the shared store
    does not exist, and 'recovering' one anywhere else would be theatre."""
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(["list"], env={}, out=out, err=err)
    assert code == 1
    assert json.loads(err.getvalue())["error"] == "PROVIDER_QUOTA_UNAVAILABLE"


def test_the_tool_honours_the_workers_own_lifetime_contract():
    """A shorter lifetime would offer leases whose process may be alive."""
    out, err = io.StringIO(), io.StringIO()
    code = tool.main(["list"], env={**ENV, "MILO_WORKER_MAX_LIFETIME_SECONDS": "600"},
                     backend_factory=lambda _env: MemoryQuotaBackend(), out=out, err=err)
    assert code == 1
    assert json.loads(err.getvalue())["error"] == "CONFIG_INVALID"


def test_the_tool_recovers_one_lease_per_invocation_by_construction():
    source = tool.__file__
    text = open(source, encoding="utf-8").read()
    assert text.count("operator_reclaim_inference(") == 1
    assert "--all" not in text and "for lease in" not in text
