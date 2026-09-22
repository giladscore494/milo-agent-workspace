"""R5: append-only verdict HISTORY is not current truth.

The defect this module exists to prevent, in three lines:

    t0  verdict: verified     (the source said 1798 cc)
    t1  verdict: rejected     (a re-verification found 1600 cc)
    exists(verdict = 'verified')  ->  true, forever

Every reader of verified evidence asked that existence question, so an
invalidation, a contradiction, a supersession, or a `verified` row citing no
durable evidence at all could be bypassed by any older `verified` row still in
the table. `backend/engines/swarm_v2/current_verdict.py` is the one resolution
that replaces it, and `supabase/migrations/20260921000100_current_verdict_
authority.sql` implements exactly the same rule in SQL.

What is proven here: the rule itself, the in-memory repository's mirror of the
durable read, the consolidation of every in-process "index verdicts by claim"
onto the same resolution, that the two definitions have not drifted apart, and
that none of it changed what a V2 evidence chain does.

The DURABLE half -- the SQL functions, the two triggers and the rewritten
pending-promotion read -- is proven against real PostgreSQL in
`tests/test_migrations_postgres.py`, and the promotion refusals are proven end
to end in `tests/test_catalog_pr3_swarm_promotion.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from backend.engines.swarm_v2.contracts import SupportLink, VerificationVerdict
from backend.engines.swarm_v2.current_verdict import (CURRENT_VERDICT_CONTRACT_VERSION,
                                                      CURRENT_VERDICT_REASONS,
                                                      CURRENT_VERDICT_STATES, SUPPORTED_STATE,
                                                      CurrentVerdict, CurrentVerdictError,
                                                      contested_claim_ids,
                                                      current_verdict_by_claim,
                                                      current_verdict_row,
                                                      parse_current_verdict,
                                                      resolve_current_verdict,
                                                      resolve_current_verdicts,
                                                      superseded_claim_ids)
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.support import (EVIDENCE_BEARING_MODES, VERIFICATION_MODES,
                                              VERIFIER_CONTRACT_VERSION)
from backend.testing import evidence_fixtures
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs

MIGRATION = Path("supabase/migrations/20260921000100_current_verdict_authority.sql")


# =============================================================================
# helpers
# =============================================================================

def claim_row(claim_id: str = "claim-1", status: str = "active") -> dict:
    return {"id": claim_id, "status": status}


def verdict_row(verdict: str, *, at: str, row_id: str, support: int = 1,
                mode: str = "deterministic_structured",
                contract: str = VERIFIER_CONTRACT_VERSION,
                claim_id: str = "claim-1") -> dict:
    return {"id": row_id, "claim_id": claim_id, "verdict": verdict, "created_at": at,
            "reason": "R4_STRUCTURED_MATCH", "verification_mode": mode,
            "verifier_contract_version": contract, "support_count": support}


def resolve(*verdicts, claim: dict | None = None, **kwargs) -> CurrentVerdict:
    return resolve_current_verdict(claim=claim or claim_row(), verdicts=list(verdicts),
                                   **kwargs)


def leased_run(repository: MemoryRepository, worker: str = "worker-1") -> WorkerLease:
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, f"p-{worker}", "P", [user])
    conversation = repository.create_conversation(project, "c", user)
    run = repository.create_message_and_run(
        conversation["id"], "go", {}, requested_by=user, idempotency_key=None,
        request_fingerprint="fp-go", **identity_kwargs(repository, conversation["id"]))["run"]
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def lease_kwargs(lease: WorkerLease) -> dict:
    return {"worker_id": lease.worker_id, "attempt": lease.attempt,
            "lease_token": lease.lease_token}


def durable_chain(repository: MemoryRepository, lease: WorkerLease, label: str = "r5"):
    """One complete R3/R4 chain through the SHARED evidence fixture.

    The same builders `tests/test_migrations_postgres.py` submits to the real
    guarded RPCs, so a chain that would be refused by PostgreSQL cannot be
    built here either.
    """
    source = repository.create_source(
        lease.run_id, evidence_fixtures.source_payload(f"{label}-source"), **lease_kwargs(lease))
    fragment = repository.record_evidence_fragment(
        lease.run_id, evidence_fixtures.fragment_payload(f"{label}-fragment", source["id"]),
        **lease_kwargs(lease))
    claim = repository.create_claim(
        lease.run_id, evidence_fixtures.claim_payload(f"{label}-claim", source["id"]),
        **lease_kwargs(lease))
    return source, fragment, claim


def settle(repository: MemoryRepository, lease: WorkerLease, claim, fragment, *,
           verdict: str = "verified", key: str = "verdict-1",
           reason: str = "R4_STRUCTURED_MATCH") -> dict:
    payload = evidence_fixtures.verdict_payload(
        key, claim["id"], verdict=verdict, reason=reason,
        support=[evidence_fixtures.support_link(fragment["id"])] if verdict == "verified" else [])
    return repository.record_claim_verdict(lease.run_id, payload, **lease_kwargs(lease))


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


# =============================================================================
# 1. the rule
# =============================================================================

def test_the_newest_verdict_is_the_current_one():
    """The defect, stated as an assertion."""
    older = verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a")
    newer = verdict_row("rejected", at="2026-09-02T10:00:00+00:00", row_id="b", support=0)

    assert current_verdict_row([older, newer])["id"] == "b"
    # Order of READING never changes the answer.
    assert current_verdict_row([newer, older])["id"] == "b"
    state = resolve(older, newer)
    assert state.state == "rejected" and not state.supported
    assert state.verdict_id == "b"


def test_a_newer_needs_review_also_ends_a_verified_state():
    """An invalidation is not only a `rejected`."""
    state = resolve(verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a"),
                    verdict_row("needs_review", at="2026-09-03T10:00:00+00:00", row_id="b",
                                support=0))
    assert state.state == "needs_review" and not state.supported


def test_an_exact_tie_fails_closed():
    """Two verdicts written in one instant: the one that does NOT verify wins.

    PostgreSQL stamps `created_at` from the transaction clock, so two verdicts
    of one transaction are indistinguishable in time. Resolving that by id --
    or by insertion order -- would decide current truth by chance.
    """
    moment = "2026-09-04T12:00:00+00:00"
    verified = verdict_row("verified", at=moment, row_id="zzz")
    rejected = verdict_row("rejected", at=moment, row_id="aaa", support=0)

    assert current_verdict_row([verified, rejected])["id"] == "aaa"
    assert current_verdict_row([rejected, verified])["id"] == "aaa"
    assert resolve(verified, rejected).state == "rejected"


def test_a_verified_verdict_citing_no_evidence_is_unsupported():
    """`verified` is a word. `supported` is a word plus a durable fragment."""
    assert resolve(verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a",
                               support=0)).state == "unsupported"
    # And a `verified` that cannot say HOW it was reached is an assertion too.
    for mode in ("deterministic_local", None, "unknown_mode"):
        assert resolve(verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a",
                                   mode=mode)).state == "unsupported"
    assert resolve(verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a",
                               contract=None)).state == "unsupported"
    # The evidence-bearing modes are the shared contract's, not a second copy.
    assert set(EVIDENCE_BEARING_MODES) <= set(VERIFICATION_MODES)


def test_no_verdict_at_all_is_unverified_and_never_supported():
    state = resolve()
    assert state.state == "unverified" and state.verdict_id is None
    assert not state.supported and not state.authorizes("anything")


def test_an_inactive_claim_is_invalidated_whatever_its_history_says():
    state = resolve(verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a"),
                    claim=claim_row(status="superseded"))
    assert state.state == "invalidated" and state.verdict_id is None


def test_a_superseded_or_contested_claim_is_not_current():
    """A decided contradiction, and an undecided one. Neither is verified."""
    verified = verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a")
    assert resolve(verified, superseded_claim_ids=["claim-1"]).state == "superseded"
    assert resolve(verified, contested_claim_ids=["claim-1"]).state == "contested"

    # And both id sets are read from the durable rows rather than assembled.
    assert superseded_claim_ids([{"state": "resolved",
                                  "superseded_claim_ids": ["claim-1"]}]) == {"claim-1"}
    assert superseded_claim_ids([{"state": "unresolved",
                                  "superseded_claim_ids": ["claim-1"]}]) == frozenset()
    assert contested_claim_ids([{"outcome": "unresolved_needs_review",
                                 "claim_ids": ["claim-1"]}]) == {"claim-1"}
    assert contested_claim_ids([{"outcome": "resolved_value_selected",
                                 "claim_ids": ["claim-1"]}]) == frozenset()


def test_a_citation_must_name_the_verdict_that_is_current():
    """Two questions, and passing one is not passing the other."""
    older = verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a")
    newer = verdict_row("verified", at="2026-09-05T10:00:00+00:00", row_id="b")
    state = resolve(older, newer)

    assert state.supported and state.authorizes("b")
    # Still `verified` -- and the older row does not authorize a thing.
    assert not state.authorizes("a")


def test_replayed_verdict_rows_resolve_to_one_state():
    """The idempotent-replay case, which must never look like a re-verification."""
    row = verdict_row("verified", at="2026-09-01T10:00:00+00:00", row_id="a")
    once = resolve(row)
    assert resolve(row, dict(row), dict(row)) == once


def test_every_state_is_named_and_carries_a_safe_message():
    for state in CURRENT_VERDICT_STATES:
        assert CURRENT_VERDICT_REASONS[state]
        assert CurrentVerdict(claim_id="c", state=state).safe_message
    assert set(CURRENT_VERDICT_REASONS) == set(CURRENT_VERDICT_STATES)
    assert SUPPORTED_STATE in CURRENT_VERDICT_STATES


def test_a_resolved_state_read_back_from_a_repository_fails_closed():
    """A durable read is data, not a trusted object."""
    good = CurrentVerdict(claim_id="c", state="supported", verdict_id="v", support_count=1)
    assert parse_current_verdict(good.as_row()) == good
    assert parse_current_verdict(good) is good
    for bad in ({}, {"claim_id": "c"}, {"claim_id": "c", "state": "made-up"},
                {"claim_id": "c", "state": "supported"}, "not a row", None):
        with pytest.raises(CurrentVerdictError):
            parse_current_verdict(bad)


def test_resolving_many_claims_uses_the_same_rule_per_claim():
    claims = [claim_row("claim-1"), claim_row("claim-2")]
    verdicts = [verdict_row("verified", at="2026-09-01T00:00:00+00:00", row_id="a"),
                verdict_row("rejected", at="2026-09-02T00:00:00+00:00", row_id="b",
                            support=0),
                verdict_row("verified", at="2026-09-01T00:00:00+00:00", row_id="c",
                            claim_id="claim-2")]
    resolved = resolve_current_verdicts(claims=claims, verdicts=verdicts)

    assert resolved["claim-1"].state == "rejected"
    assert resolved["claim-2"].state == SUPPORTED_STATE


def test_a_row_with_no_timestamp_never_displaces_one_that_has_one():
    """A verdict that cannot say when it was written is the oldest thing here."""
    timeless = {"id": "a", "claim_id": "claim-1", "verdict": "verified",
                "verification_mode": "deterministic_structured",
                "verifier_contract_version": VERIFIER_CONTRACT_VERSION, "support_count": 1}
    stamped = verdict_row("rejected", at="2026-09-01T00:00:00+00:00", row_id="b", support=0)
    assert current_verdict_row([timeless, stamped])["id"] == "b"


# =============================================================================
# 2. the same resolution, for in-process verdict objects
# =============================================================================

def test_in_process_verdicts_are_indexed_by_the_same_rule():
    """`{v.claim_id: v for v in verdicts}` kept whichever came LAST.

    The product builder, the correction planner and the checkpoint all built
    that map. A stray second verdict for one claim could therefore raise it to
    `verified` by arriving later in a list.
    """
    verified = VerificationVerdict(claim_id="c", verdict="verified",
                                   reason="R4_STRUCTURED_MATCH",
                                   mode="deterministic_structured",
                                   contract_version=VERIFIER_CONTRACT_VERSION,
                                   support=[SupportLink(source_id="s", content_hash="0" * 64,
                                                        fragment_id="f", locator="l")])
    rejected = VerificationVerdict(claim_id="c", verdict="rejected",
                                   reason="R4_VALUE_MISMATCH",
                                   mode="deterministic_structured",
                                   contract_version=VERIFIER_CONTRACT_VERSION)

    assert current_verdict_by_claim([rejected, verified])["c"] is rejected
    assert current_verdict_by_claim([verified, rejected])["c"] is rejected
    # The normal case -- exactly one verdict per claim -- is unchanged.
    assert current_verdict_by_claim([verified]) == {"c": verified}


# =============================================================================
# 3. the in-memory repository mirrors the durable read
# =============================================================================

def test_the_repository_read_resolves_current_truth(repository):
    lease = leased_run(repository)
    _source, fragment, claim = durable_chain(repository, lease)
    accepted = settle(repository, lease, claim, fragment)

    states = repository.claim_current_verdict_states(lease.run_id)
    assert len(states) == 1
    assert states[0]["state"] == SUPPORTED_STATE
    assert states[0]["verdict_id"] == str(accepted["id"])
    assert states[0]["contract_version"] == CURRENT_VERDICT_CONTRACT_VERSION

    # A NEWER verdict rejects the claim. The older `verified` row stays in the
    # table -- that is what append-only means -- and stops being current.
    rejection = settle(repository, lease, claim, fragment, verdict="rejected",
                       key="verdict-2", reason="R4_VALUE_MISMATCH")
    states = repository.claim_current_verdict_states(lease.run_id)
    assert states[0]["state"] == "rejected"
    assert states[0]["verdict_id"] == str(rejection["id"])
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row.get("id"))) == "claim_verdict"]) == 2


def test_the_repository_read_is_scoped_bounded_and_claim_filtered(repository):
    lease = leased_run(repository)
    _source, fragment, claim = durable_chain(repository, lease)
    settle(repository, lease, claim, fragment)
    other = leased_run(repository, worker="worker-2")

    assert repository.claim_current_verdict_states(other.run_id) == []
    assert repository.claim_current_verdict_states(lease.run_id, [str(claim["id"])])
    assert repository.claim_current_verdict_states(lease.run_id, ["11111111-2222-4333-"
                                                                 "8444-555555555555"]) == []
    assert repository.claim_current_verdict_states(lease.run_id, limit=0) == []


def test_an_unknown_claim_resolves_to_no_row_at_all(repository):
    """Not "unverified". A claim that does not exist is not a claim."""
    lease = leased_run(repository)
    assert repository._current_verdict(str(uuid4())) is None


def test_a_replayed_verdict_does_not_become_a_second_current_answer(repository):
    """The durable write is idempotent, so replay changes nothing at all."""
    lease = leased_run(repository)
    _source, fragment, claim = durable_chain(repository, lease)
    first = settle(repository, lease, claim, fragment)
    again = settle(repository, lease, claim, fragment)

    assert str(again["id"]) == str(first["id"])
    assert repository.claim_current_verdict_states(lease.run_id)[0]["verdict_id"] \
        == str(first["id"])


# =============================================================================
# 4. the SQL and the Python state the SAME rule
# =============================================================================

def test_the_migration_states_the_same_ordering_and_the_same_states():
    """Textual pinning, because the two definitions must not drift.

    The executable proof that PostgreSQL applies this rule lives in
    `tests/test_migrations_postgres.py`; this is what fails when someone
    edits one definition and not the other.
    """
    sql = MIGRATION.read_text(encoding="utf-8")

    # The ordering, character for character.
    assert sql.count("order by v.created_at desc, (v.verdict = 'verified') asc, v.id desc") == 2
    # Every state the Python contract can produce is named in the SQL, except
    # the ones the SQL reports by other means.
    for state in CURRENT_VERDICT_STATES:
        assert f"'{state}'" in sql, state
    # The contract version has ONE definition per language and they agree.
    assert f"select '{CURRENT_VERDICT_CONTRACT_VERSION}'::text" in sql
    # The evidence-bearing modes are the same set on both sides.
    modes = re.search(r"v_row\.verification_mode in \(([^)]*)\)", sql).group(1)
    assert {item.strip().strip("'") for item in modes.split(",")} == set(EVIDENCE_BEARING_MODES)
    # And the two durable gates exist, on the two relations that matter.
    assert "catalog_candidate_evidence_links_current_verdict" in sql
    assert "catalog_canonical_field_provenance_current_verdict" in sql


def test_the_pending_promotion_read_no_longer_joins_on_existence():
    """The one-line defect, and its absence from the current definition.

    Scoped to the READ's own body: the ordering rule legitimately mentions
    `verdict = 'verified'`, and the point here is the JOIN the read used to
    make -- "a verified verdict exists for this claim, in this run".
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    start = sql.index("create or replace function public.catalog_run_pending_promotions")
    body = sql[start:sql.index("$$;", start)]

    assert "join lateral public.claim_current_verdict_state(c.id) cv on cv.state = 'supported'" \
        in body
    assert "public.claim_verdicts" not in body
    assert "verdict = 'verified'" not in body
    # The previous definition is still in the tree, and it is what this
    # replaces: if that join ever comes back, it comes back HERE.
    previous = Path("supabase/migrations/"
                    "20260916120000_catalog_field_level_promotion.sql").read_text()
    assert "join public.claim_verdicts v\n        on v.claim_id = c.id and v.run_id = " \
        "p_run_id and v.verdict = 'verified'" in previous


# =============================================================================
# 5. V2 evidence behaviour is unchanged
# =============================================================================

def test_a_v2_evidence_chain_behaves_exactly_as_it_did(repository):
    """One verified verdict, one supported claim, and the same durable rows.

    R5 adds a resolution over verdict history. It changes no evidence write,
    no verdict contract and no vocabulary -- and a chain with one verdict
    resolves to exactly what "a verified verdict exists" used to answer.
    """
    lease = leased_run(repository)
    source, fragment, claim = durable_chain(repository, lease)
    accepted = settle(repository, lease, claim, fragment)

    # The verdict row is what it always was.
    assert accepted["verdict"] == "verified"
    assert accepted["verification_mode"] == evidence_fixtures.VERIFICATION_MODE
    assert accepted["verifier_contract_version"] == VERIFIER_CONTRACT_VERSION
    assert accepted["support"] == [evidence_fixtures.support_link(fragment["id"])]
    # The claim keeps its R3 provenance untouched.
    assert claim["evidence_locator"] == evidence_fixtures.LOCATOR
    assert source["source_version_kind"] == evidence_fixtures.SOURCE_VERSION_KIND
    # And the one verdict is the current one.
    state = repository.claim_current_verdict_states(lease.run_id)[0]
    assert state["state"] == SUPPORTED_STATE and state["support_count"] == 1


def test_the_evidence_board_verdict_write_is_unchanged(repository):
    """The Board still writes exactly the payload it always wrote."""
    lease = leased_run(repository)
    _source, fragment, claim = durable_chain(repository, lease, label="board")
    board = EvidenceBoard(repository, lease)

    row = board.record_verification_verdict(VerificationVerdict(
        claim_id=str(claim["id"]), verdict="verified", reason="R4_STRUCTURED_MATCH",
        mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
        support=[SupportLink(source_id=str(claim["source_id"]),
                             content_hash=fragment["content_hash"],
                             fragment_id=str(fragment["id"]),
                             locator=fragment["locator_key"])]))

    assert row["verdict"] == "verified"
    assert row["support"] == [{"content_hash": fragment["content_hash"],
                               "fragment_id": str(fragment["id"]),
                               "locator_key": fragment["locator_key"]}]
    assert board.record_verification_verdict(VerificationVerdict(
        claim_id=str(claim["id"]), verdict="verified", reason="R4_STRUCTURED_MATCH",
        mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
        support=[SupportLink(source_id=str(claim["source_id"]),
                             content_hash=fragment["content_hash"],
                             fragment_id=str(fragment["id"]),
                             locator=fragment["locator_key"])]))["id"] == row["id"]
