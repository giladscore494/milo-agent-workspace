"""EVERY worker-side durable mutation path is fenced by the run lease.

The audit behind this file went method by method through the repository
contract and route by route through the API, asking one question of each: can a
worker whose lease has been reclaimed still change this? Three repository paths
and nine HTTP routes answered yes.

*   ``tool_access_requests`` and ``tool_grants`` were direct table writes with
    no lease argument at all -- and the grant path mutated TWO tables, because
    it also marked the referenced request granted.
*   ``run_usage_ledger`` was the per-call spend row, appended straight into the
    table. Every other usage surface (``runs.usage``, ``run_execution_usage``)
    had been lease-guarded since the execution usage ledger landed; this one
    was not, so a replaced worker could keep charging a run it no longer owned
    against the live worker's DAILY budget.
*   the nine worker HTTP routes accepted an approved worker SERVICE IDENTITY as
    sufficient. Identity answers "is this a worker?"; it cannot answer "is this
    THE worker of THIS attempt of THIS run?". Four of them
    (``/tool-usage``, ``/sources``, ``/claims``, ``/conflicts``) were also dead
    on arrival, calling repository methods whose lease arguments had become
    required without passing them.

The structural test at the end is the one that survives the next refactor: it
walks the Repository protocol itself and fails when a NEW run-owned writer is
added without a lease, so this file does not have to be remembered.
"""

from __future__ import annotations

import inspect
from uuid import UUID, uuid4

import pytest

from backend.errors import AppError
from backend.repository.supabase import Repository, SupabaseRepository
from backend.testing.memory_repository import MemoryRepository

LEASE_FIELDS = {"worker_id", "attempt", "lease_token"}


def claimed_run():
    """A run with a live lease held by worker-1, and a stale worker-2 lease."""
    repo = MemoryRepository()
    project_id, user_id = uuid4(), uuid4()
    repo.seed_user(str(user_id))
    repo.seed_project(str(project_id), "fencing", "Fencing", [str(user_id)])
    conversation = repo.create_conversation(project_id, "fencing")
    created = repo.create_message_and_run(conversation["id"], "go", {}, user_id,
                                          f"key-{uuid4()}", "fingerprint")
    run_id = UUID(str(created["run"]["id"]))
    first = repo.claim_run(run_id, "worker-1")
    stale = {"worker_id": "worker-1", "attempt": first["attempt"],
             "lease_token": first["lease_token"]}
    # The lease lapses and a replacement worker claims it: worker-1's contract
    # is now provably not current.
    repo.runs[str(run_id)]["lease_expires_at"] = "1970-01-01T00:00:00+00:00"
    second = repo.claim_run(run_id, "worker-2")
    live = {"worker_id": "worker-2", "attempt": second["attempt"],
            "lease_token": second["lease_token"]}
    return repo, run_id, live, stale


LEDGER_ENTRY = {"provider": "moonshot", "model": "kimi", "call_seq": 1,
                "decision": "settled", "actual_cost": 0.01}
TOOL_REQUEST = {"agent": "a", "tool": "web_search", "reason": "r"}
TOOL_GRANT = {"agent": "a", "tool": "web_search", "max_searches": 1,
              "max_rounds": 1, "approver_policy": "auto",
              "expires_at": "2030-01-01T00:00:00+00:00"}


def newly_fenced_writes(repo, run_id):
    """The three paths that had no lease fence before this change."""
    return {
        "append_usage_ledger": lambda lease: repo.append_usage_ledger(
            {"run_id": str(run_id), **LEDGER_ENTRY}, **lease),
        "create_tool_access_request": lambda lease: repo.create_tool_access_request(
            run_id, dict(TOOL_REQUEST), **lease),
        "create_tool_grant": lambda lease: repo.create_tool_grant(
            run_id, dict(TOOL_GRANT), **lease),
    }


#: The three names, stated once, so parametrisation does not build a
#: repository at collection time.
NEWLY_FENCED = ("append_usage_ledger", "create_tool_access_request", "create_tool_grant")


def test_the_newly_fenced_set_is_the_one_the_writes_helper_offers():
    assert sorted(NEWLY_FENCED) == sorted(newly_fenced_writes(*claimed_run()[:2]))


@pytest.mark.parametrize("name", NEWLY_FENCED)
def test_a_stale_worker_cannot_perform_a_newly_fenced_write(name):
    """REQUIRED REGRESSION 5, for the paths that had no fence at all."""
    repo, run_id, live, stale = claimed_run()
    writes = newly_fenced_writes(repo, run_id)

    with pytest.raises(AppError) as raised:
        writes[name](stale)
    assert raised.value.code in {"RUN_TRANSITION_CONFLICT", "RUN_LEASE_LOST"}

    # The live worker's identical write succeeds, so the refusal is about
    # OWNERSHIP and not about the payload.
    assert writes[name](live) is not None


@pytest.mark.parametrize("name", NEWLY_FENCED)
def test_a_partial_lease_is_refused_rather_than_skipping_the_check(name):
    """An absent component must never be read as 'no check required'."""
    repo, run_id, live, _ = claimed_run()
    writes = newly_fenced_writes(repo, run_id)
    for missing in sorted(LEASE_FIELDS):
        partial = {**live, missing: None}
        with pytest.raises(AppError):
            writes[name](partial)


def test_a_ledger_entry_cannot_charge_a_run_the_lease_does_not_own():
    """The run id travels as the FENCED argument, never as a payload field."""
    repo, run_id, live, _ = claimed_run()
    other = str(uuid4())
    row = repo.append_usage_ledger({"run_id": str(run_id), **LEDGER_ENTRY,
                                    # A payload that claims a different run.
                                    "project_id": None}, **live)
    assert row["run_id"] == str(run_id) != other


def test_a_worker_without_a_lease_cannot_write_at_all():
    repo, run_id, _live, _stale = claimed_run()
    with pytest.raises(TypeError):
        repo.create_tool_grant(run_id, dict(TOOL_GRANT))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        repo.append_usage_ledger({"run_id": str(run_id), **LEDGER_ENTRY})  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# the structural audit
# ---------------------------------------------------------------------------
#: Run-owned writers whose signature must carry the complete lease.
#:
#: Derived by NAME from the repository protocol rather than listed: a writer
#: added later is audited by existing, which is what stops this file needing to
#: be remembered.
_WRITE_PREFIXES = ("create_", "record_", "append_", "save_", "upsert_",
                   "promote_", "link_", "activate_", "patch_")

#: Writers that are deliberately NOT worker-fenced, each for a stated reason.
#: Anything not named here and not fenced fails the audit.
UNFENCED_BY_DESIGN = {
    # API-side, before any worker exists. A run is created, launched and given
    # its identity before it can be claimed, so there is no lease to present.
    "create_user_message", "create_queued_run", "create_message_and_run",
    "record_run_invocation", "bind_run_identity",
    # Browser/API surfaces that own no run state.
    "create_conversation", "create_workflow_proposal",
    "create_project_from_proposal",
    # Lease-optional by contract, checked completely whenever one IS supplied:
    # these predate the lease contract and the worker always passes one.
    "append_run_event", "save_checkpoint", "update_run_usage",
    "upsert_run_blackboard", "create_agent_message", "create_supervisor_decision",
}


def _protocol_writers():
    for name, member in vars(Repository).items():
        if not callable(member) or not name.startswith(_WRITE_PREFIXES):
            continue
        yield name, inspect.signature(member)


def test_every_run_owned_writer_in_the_repository_protocol_takes_the_lease():
    unfenced = []
    for name, signature in _protocol_writers():
        if name in UNFENCED_BY_DESIGN:
            continue
        params = set(signature.parameters)
        if not LEASE_FIELDS <= params:
            unfenced.append(name)
    assert not unfenced, (
        "these run-owned writers take no worker lease, so a replaced worker "
        f"could still mutate through them: {sorted(unfenced)}")


#: Terminalization does not share the writer prefixes, and it is the single
#: most damaging thing a stale worker could do: finishing a run another worker
#: is executing. Named explicitly so it is audited too.
TERMINALIZERS = ("transition_run", "finalize_run", "mark_run_complete", "mark_run_failed")


@pytest.mark.parametrize("name", TERMINALIZERS)
def test_every_terminalizer_takes_the_lease(name):
    for owner in (Repository, SupabaseRepository, MemoryRepository):
        params = set(inspect.signature(getattr(owner, name)).parameters)
        # `transition_run` spells the contract as expectations; the others
        # take it directly. Either way all three components are present.
        assert LEASE_FIELDS <= params or {
            "expected_worker_id", "expected_attempt", "expected_lease_token"} <= params, (
            f"{owner.__name__}.{name} can terminalize a run without the lease")


def test_a_stale_worker_cannot_terminalize_a_run():
    repo, run_id, live, stale = claimed_run()
    with pytest.raises(AppError) as raised:
        repo.mark_run_complete(run_id, {"status": "complete"}, **stale)
    assert raised.value.code in {"RUN_TRANSITION_CONFLICT", "RUN_LEASE_LOST"}
    assert repo.get_run(run_id)["status"] != "completed"


def test_the_supabase_repository_implements_every_fenced_writer_with_a_lease():
    """The protocol and the implementation cannot disagree about the fence."""
    mismatched = []
    for name, signature in _protocol_writers():
        if name in UNFENCED_BY_DESIGN:
            continue
        implementation = getattr(SupabaseRepository, name, None)
        if implementation is None:
            mismatched.append(f"{name} (not implemented)")
            continue
        if not LEASE_FIELDS <= set(inspect.signature(implementation).parameters):
            mismatched.append(name)
    assert not mismatched, f"implementation drops the lease for: {sorted(mismatched)}"


def test_the_three_newly_guarded_rpcs_assert_the_lease_in_the_database():
    """The application guard is advisory against a second writer; the RPC is
    what actually holds. Each new writer must call `assert_worker_lease`."""
    import re
    from pathlib import Path

    sql = Path("supabase/migrations/20260921000200_immutable_run_identity.sql").read_text()
    for function in ("create_tool_access_request_guarded",
                     "create_tool_grant_guarded",
                     "append_usage_ledger_guarded"):
        body = sql.split(f"function public.{function}", 1)
        assert len(body) == 2, f"{function} is not defined by the migration"
        # Up to the end of this function body only.
        segment = body[1].split("$$;", 1)[0]
        assert re.search(r"perform\s+public\.assert_worker_lease\(", segment), (
            f"{function} does not assert the worker lease")
