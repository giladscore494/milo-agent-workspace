"""R5: V1's verified facts rest on evidence, not on what a model said.

Two defects are proven closed here, and they are different defects.

THE VERIFIER COULD NOT SEE THE VALUES. `compact_verifier_input` handed
`source_verifier` the NAMES of the fields a record happened to fill in, so the
phase deciding whether a model was `verified` could not distinguish
`power_hp: 218` from `power_hp: 5000`. Section 1 proves it now receives the
values themselves, and that two records differing only in a value are
different verifier inputs.

A `verified` V1 FIELD RESTED ON NOTHING DURABLE. `verification_status:
"verified"` was a string in a JSON document, supported by a list of URLs the
same model wrote down. Sections 2-5 prove that a verified V1 field now carries
the same chain every other verified fact in this repository does --
field/value -> claim -> source -> version + locator -> durable fragment --
built by trusted server code through the existing Evidence Board, and that a
model-reported URL with nothing behind it can never become one.

Offline and deterministic by construction: creating a socket anywhere in this
module is a test failure, and no model is called on any path -- the engine
test drives the real orchestration with the model-calling phases replaced by
scripted results.
"""

from __future__ import annotations

import socket
from uuid import uuid4

import pytest

from backend.engines.swarm_v2.current_verdict import SUPPORTED_STATE
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.evidence_contracts import parse_locator_key
from backend.engines.swarm_v2.fragments import fragment_content_hash
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.engines.vehicle_catalog_v1 import core
from backend.engines.vehicle_catalog_v1.engine import (VehicleCatalogEngine,
                                                       VehicleCatalogRunConfig)
from backend.engines.swarm_v2.fragments import MAX_FRAGMENTS_PER_SOURCE
from backend.engines.vehicle_catalog_v1.evidence_authority import (
    V1_ACCEPTED_REASON, V1_TOOL_OPERATION, V1_VERDICT_REASONS, V1_VERIFICATION_MODE,
    V1EvidenceAuthority, apply_evidence_authority, model_key, observed_records)
from backend.catalog.pipeline import PROMOTABLE_TOOL_OPERATION
from backend.errors import LEASE_FAILURE_CODES, AppError
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs

ISRAELI_URL = "https://toyota.co.il/rav4"
FOREIGN_URL = "https://toyota-usa.com/rav4"


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Creating a socket anywhere in this module is a test failure."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline V1 evidence test attempted a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# =============================================================================
# helpers
# =============================================================================

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


def engines_record(**overrides) -> dict:
    """One `engines_fuel_power_agent` record, as the technical phase returns it."""
    item = {"model": "RAV4", "engine": "2.5L hybrid", "fuel_type": "hybrid",
            "power_hp": 218, "torque_nm": 221, "sources": [ISRAELI_URL],
            "confidence": "high"}
    item.update(overrides)
    return {"engines_fuel_power_agent": {"agent": "engines_fuel_power_agent",
                                         "items": [item], "missing_data": [],
                                         "extra_candidate_models": []}}


def verifier_document(status: str = "verified", model: str = "RAV4") -> dict:
    return {"agent": "source_verifier", "status": "success",
            "verified_models": [{"model": model, "status": status, "confidence": "high",
                                 "issues": []}],
            "rejected_data_points": [], "needs_review": []}


def rows_of(repository: MemoryRepository, kind: str) -> list[dict]:
    return [row for row in repository.tool_rows
            if repository.evidence_kinds.get(str(row.get("id"))) == kind]


def settle(repository: MemoryRepository, lease: WorkerLease, technical: dict, *,
           verifier: dict | None = None, market: str = "Israel",
           board: EvidenceBoard | None = None, reader: object | None = None):
    """One evidence pass. `board` writes; `reader` answers "is it current?".

    The two are separate arguments because they are separate failures: a
    durable write that did not happen and a current-state answer that could
    not be read are different things, and a field must fail closed on either.
    """
    authority = V1EvidenceAuthority(board=board or EvidenceBoard(repository, lease),
                                    repository=repository if reader is None else reader)
    return authority.record(manufacturer="Toyota", market=market, period="2015-2025",
                            technical=technical, verifier=verifier or verifier_document())


def reasons_of(report) -> set[str]:
    return {reason for _model, _field, _verdict, reason in report.fields}


def verdicts_of(report) -> set[str]:
    return {verdict for _model, _field, verdict, _reason in report.fields}


class RepositoryView:
    """The repository, with ONE read replaced. Everything else is the real one."""

    def __init__(self, inner, current):
        self._inner = inner
        self._current = current

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def claim_current_verdict_states(self, run_id, claim_ids=None, *, limit: int = 200):
        return self._current(run_id, claim_ids, limit)


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


# =============================================================================
# 1. the verifier is asked about VALUES, not about which fields exist
# =============================================================================

def test_the_verifier_input_carries_the_actual_values_it_is_asked_to_verify():
    """The defect, stated as an assertion: `fields` is values, not names."""
    normalized = {"canonical_models": [{"canonical_model_name": "RAV4",
                                        "model_name_he": "ראב4", "sources": [ISRAELI_URL]}]}
    compact = core.compact_verifier_input(normalized, engines_record())
    stated = compact["technical_summaries"]["engines_fuel_power_agent"]["items"][0]["fields"]

    assert stated == {"engine": "2.5L hybrid", "fuel_type": "hybrid",
                      "power_hp": 218, "torque_nm": 221}
    # A number stays a number: rendering it as text would make the verifier
    # compare strings instead of quantities.
    assert isinstance(stated["power_hp"], int)


def test_two_records_that_differ_only_in_a_value_are_different_verifier_inputs():
    """The old input could not tell 218 hp from 5000 hp. This one can."""
    normalized = {"canonical_models": [{"canonical_model_name": "RAV4"}]}
    honest = core.compact_verifier_input(normalized, engines_record())
    absurd = core.compact_verifier_input(normalized, engines_record(power_hp=5000))

    assert honest != absurd
    # And the OLD shape -- the sorted field names -- is identical for both,
    # which is exactly why it could not verify anything about a value.
    def names(payload):
        return sorted(payload["technical_summaries"]["engines_fuel_power_agent"]
                      ["items"][0]["fields"])
    assert names(honest) == names(absurd)


def test_the_verifier_input_is_bounded_and_scoped_to_its_own_chunk():
    """Showing values is paid for by showing only this chunk's models."""
    technical = {"engines_fuel_power_agent": {
        "agent": "engines_fuel_power_agent",
        "items": [{"model": "RAV4", "power_hp": 218, "sources": [ISRAELI_URL]},
                  {"model": "Corolla", "power_hp": 140, "sources": [ISRAELI_URL]}]}}
    compact = core.compact_verifier_input({"canonical_models": [
        {"canonical_model_name": "RAV4"}]}, technical)
    items = compact["technical_summaries"]["engines_fuel_power_agent"]["items"]

    assert [item["model"] for item in items] == ["RAV4"]
    # One value is bounded, and visibly bounded: a truncated value ends in an
    # ellipsis so the verifier reads a cut statement as a cut one.
    long_value = core.compact_verifier_value("x" * 200)
    assert len(long_value) == core.MAX_VERIFIER_VALUE_CHARS and long_value.endswith("…")
    many = core.compact_verifier_fields({f"f{index}": index for index in range(20)})
    assert len(many) == core.MAX_VERIFIER_FIELDS_PER_ITEM


# =============================================================================
# 2. a verified V1 field carries the whole durable chain
# =============================================================================

def test_a_verified_v1_field_carries_the_whole_durable_chain(repository):
    """field/value -> claim -> source -> version + locator -> durable fragment.

    Every link is asserted from the durable rows, not from the report: this is
    the claim the whole module makes, so nothing here may be taken on trust.
    """
    lease = leased_run(repository)
    report = settle(repository, lease, engines_record())

    assert report.verified("RAV4")
    assert {field for _model, field, verdict, _reason in report.fields
            if verdict == "verified"} == {"engine", "fuel_type", "power_hp", "torque_nm"}

    source = rows_of(repository, "source")[0]
    # THE SOURCE states which version of the record was read, and it is a
    # content hash the SERVER computed -- never a string a model supplied.
    assert source["source_version_kind"] == "content_sha256"
    assert len(source["source_version_id"]) == 64
    assert source["tool_operation"] == V1_TOOL_OPERATION

    claims = {row["field_key"]: row for row in rows_of(repository, "claim")}
    fragments = {row["locator_key"]: row for row in rows_of(repository, "evidence_fragment")}
    verdicts = {str(row["claim_id"]): row for row in rows_of(repository, "claim_verdict")}

    power = claims["power_hp"]
    # THE CLAIM states the value, its unit and the exact place it was read.
    assert power["value"] == 218 and power["unit"] == "hp"
    assert str(power["source_id"]) == str(source["id"])
    locator = parse_locator_key(power["evidence_locator"])
    assert locator.kind == "record_field" and locator.field_path == ("power_hp",)

    # THE FRAGMENT sits at that exact locator, and its text is a deterministic
    # projection of the record -- never a quote a model wrote.
    fragment = fragments[power["evidence_locator"]]
    assert fragment["fragment_type"] == "structured_projection"
    assert fragment["fragment_text"] == "model=RAV4; power_hp=218"
    assert fragment["content_hash"] == fragment_content_hash(fragment["fragment_text"])

    # THE VERDICT cites that fragment, by id and by content hash.
    verdict = verdicts[str(power["id"])]
    assert verdict["verdict"] == "verified" and verdict["reason"] == V1_ACCEPTED_REASON
    assert verdict["verification_mode"] == V1_VERIFICATION_MODE
    assert verdict["verifier_contract_version"] == VERIFIER_CONTRACT_VERSION
    assert verdict["support"] == [{"fragment_id": str(fragment["id"]),
                                   "content_hash": fragment["content_hash"],
                                   "locator_key": power["evidence_locator"]}]

    # And the CURRENT state of every one of those claims is supported.
    states = repository.claim_current_verdict_states(lease.run_id)
    assert {row["state"] for row in states} == {SUPPORTED_STATE}


def test_a_model_reported_url_alone_never_becomes_durable_evidence(repository):
    """A record that states a source and no value states nothing.

    This is the "provenance string" case: the model named a perfectly credible
    Israeli source and filled in no field. Nothing is written, so there is
    nothing a `verified` could ever attach to.
    """
    lease = leased_run(repository)
    urls_only = {"engines_fuel_power_agent": {
        "agent": "engines_fuel_power_agent",
        "items": [{"model": "RAV4", "sources": [ISRAELI_URL], "confidence": "high"}]}}

    assert observed_records(manufacturer="Toyota", technical=urls_only) == ()
    report = settle(repository, lease, urls_only)

    assert report.fields == () and report.durable_claims == 0
    assert not report.verified("RAV4")
    for kind in ("source", "evidence_fragment", "claim", "claim_verdict"):
        assert rows_of(repository, kind) == []

    # And the model's own `verified` is taken down to `needs_review`.
    document = verifier_document()
    apply_evidence_authority(document, report)
    assert document["verified_models"][0]["status"] == "needs_review"


def test_a_field_with_no_durable_row_is_never_verified(repository):
    """A durable write that fails costs the verification, not the run.

    The evidence write is the thing that makes a V1 field verifiable, so a
    board that cannot write one leaves every field `needs_review` -- and the
    run itself carries on and returns its research.
    """
    lease = leased_run(repository)

    class RefusingBoard(EvidenceBoard):
        def record_evidence_bundle(self, bundle, *, task_key):
            raise RuntimeError("durable evidence write failed")

    report = settle(repository, lease, engines_record(),
                    board=RefusingBoard(repository, lease))

    assert report.durable_claims == 0 and report.recorded is False
    assert {verdict for _m, _f, verdict, _r in report.fields} == {"needs_review"}
    assert {reason for _m, _f, _v, reason in report.fields} == {"V1_EVIDENCE_UNSUPPORTED"}
    assert not report.verified("RAV4")


# =============================================================================
# 2b. `verified` requires DURABLE, CURRENT, EXACT support -- or it is demoted
# =============================================================================
#
# Two fail-open paths used to survive here, and they are different ones.
#
# THE VERDICT WRITE COULD FAIL AND THE FIELD STAYED VERIFIED. The evidence and
# the claim were durable, the verdict was not, and the locally decided
# `verified` was what the report carried -- a field reported verified by a row
# that does not exist.
#
# "WE COULD NOT TELL" WAS READ AS "STILL VERIFIED". When the authoritative
# current-state read was unavailable or failed, the local decision stood. That
# is the one answer it may never give: an absence of an answer is not an
# answer, and another worker may have rejected the claim in between.
#
# A `verified` V1 field now requires all four: the verdict and its support
# persisted, the current-state read SUCCEEDED, the state is `supported`, and
# the current verdict id is EXACTLY the row just settled.


def test_a_verdict_that_could_not_be_persisted_leaves_the_field_unverified(repository):
    """The evidence IS durable. The verdict is not. So nothing is verified."""
    lease = leased_run(repository)

    class VerdictRefusingBoard(EvidenceBoard):
        def record_verification_verdict(self, verdict):
            raise RuntimeError("durable verdict write failed")

    report = settle(repository, lease, engines_record(),
                    board=VerdictRefusingBoard(repository, lease))

    # The claims and their fragments landed -- this is NOT the unsupported
    # case, it is the case where only the decision failed to persist.
    assert report.durable_claims == 4 and report.recorded is True
    assert len(rows_of(repository, "claim")) == 4
    assert rows_of(repository, "claim_verdict") == []

    assert report.durable_verdicts == 0
    assert verdicts_of(report) == {"needs_review"}
    assert reasons_of(report) == {"V1_EVIDENCE_VERDICT_NOT_DURABLE"}
    assert not report.verified("RAV4")

    document = verifier_document()
    apply_evidence_authority(document, report)
    assert document["verified_models"][0]["status"] == "needs_review"


def test_a_current_state_read_that_failed_never_verifies_a_field(repository):
    """The verdict persisted. Whether it is CURRENT could not be read.

    The durable row says `verified` -- and the report does not, because
    nothing confirmed that row is the current one. Reporting the local
    decision here is exactly the fail-open this test exists to prevent.
    """
    lease = leased_run(repository)

    def refuse(_run_id, _claim_ids, _limit):
        raise AppError("REPOSITORY_ERROR", "bounded catalog read failed", 502)

    report = settle(repository, lease, engines_record(),
                    reader=RepositoryView(repository, refuse))

    assert report.durable_verdicts == 4
    assert {row["verdict"] for row in rows_of(repository, "claim_verdict")} == {"verified"}
    assert verdicts_of(report) == {"needs_review"}
    assert reasons_of(report) == {"V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE"}
    assert not report.verified("RAV4")


def test_a_backend_with_no_current_state_read_never_verifies_a_field(repository):
    """Unavailable is the same answer as failed: not an answer."""
    lease = leased_run(repository)

    class NoSuchRead:
        """A repository that does not offer the authoritative read at all."""

    report = settle(repository, lease, engines_record(), reader=NoSuchRead())

    assert report.durable_verdicts == 4
    assert reasons_of(report) == {"V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE"}
    assert not report.verified("RAV4")

    # And with no board at all -- the strictest case -- nothing is even
    # durable, so the field fails the FIRST requirement.
    bare = V1EvidenceAuthority().record(manufacturer="Toyota", market="Israel",
                                        period="2015-2025", technical=engines_record(),
                                        verifier=verifier_document())
    assert reasons_of(bare) == {"V1_EVIDENCE_UNSUPPORTED"} and not bare.verified("RAV4")


def test_a_supported_state_naming_another_verdict_never_verifies_this_field(repository):
    """Current-state DRIFT. Still `supported`, and still not this row.

    The fact being written rests on the verdict this pass settled and on
    nothing else, so a supported state naming a different row is history --
    exactly the distinction `CurrentVerdict.authorizes` exists to make.
    """
    lease = leased_run(repository)
    other_verdict = str(uuid4())

    def drift(run_id, claim_ids, limit):
        return [{**row, "verdict_id": other_verdict}
                for row in repository.claim_current_verdict_states(run_id, claim_ids,
                                                                   limit=limit)]

    report = settle(repository, lease, engines_record(),
                    reader=RepositoryView(repository, drift))

    assert report.durable_verdicts == 4
    assert verdicts_of(report) == {"needs_review"}
    assert reasons_of(report) == {"V1_EVIDENCE_CURRENT_STATE_DRIFT"}
    assert not report.verified("RAV4")


def test_a_state_that_is_not_supported_never_verifies_this_field(repository):
    """The other half of the same gate: `supported`, or nothing."""
    lease = leased_run(repository)

    def rejected(run_id, claim_ids, limit):
        return [{**row, "state": "rejected", "verdict": "rejected", "support_count": 0}
                for row in repository.claim_current_verdict_states(run_id, claim_ids,
                                                                   limit=limit)]

    report = settle(repository, lease, engines_record(),
                    reader=RepositoryView(repository, rejected))

    assert reasons_of(report) == {"V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED"}
    assert not report.verified("RAV4")


def test_only_the_exact_current_supported_verdict_verifies_a_field(repository):
    """The positive half, asserted against the durable rows themselves.

    Every field the report calls verified has a durable verdict row, and the
    authoritative current state for that claim is `supported` and names THAT
    row -- not merely some verified row of the claim's history.
    """
    lease = leased_run(repository)
    report = settle(repository, lease, engines_record())

    assert report.verified("RAV4")
    assert verdicts_of(report) == {"verified"}
    assert reasons_of(report) == {V1_ACCEPTED_REASON}
    assert report.durable_verdicts == 4

    states = {row["claim_id"]: row
              for row in repository.claim_current_verdict_states(lease.run_id)}
    settled = {str(row["claim_id"]): row for row in rows_of(repository, "claim_verdict")}
    assert len(states) == 4 and len(settled) == 4
    for claim in rows_of(repository, "claim"):
        state = states[str(claim["id"])]
        assert state["state"] == SUPPORTED_STATE
        assert state["verdict_id"] == str(settled[str(claim["id"])]["id"])
        assert state["support_count"] == 1


def test_the_authoritative_read_is_asked_once_for_the_whole_run(repository):
    """Confirming the evidence must not cost more than gathering it.

    A V1 run states a handful of fields for every model it found, so a
    round trip per FIELD would be hundreds of reads. Every verdict is settled
    first and the whole run is asked about in one bounded, chunked read --
    which changes no semantics, because a claim's state is still read after
    its own verdict was settled.
    """
    lease = leased_run(repository)
    asked: list[list[str]] = []

    def counting(run_id, claim_ids, limit):
        asked.append(list(claim_ids or []))
        return repository.claim_current_verdict_states(run_id, claim_ids, limit=limit)

    technical = engines_record()
    technical["transmission_drivetrain_performance_agent"] = {
        "agent": "transmission_drivetrain_performance_agent",
        "items": [{"model": "RAV4", "drivetrain": "AWD", "transmission": "eCVT",
                   "sources": [ISRAELI_URL]}]}
    report = settle(repository, lease, technical,
                    reader=RepositoryView(repository, counting))

    assert report.verified("RAV4") and report.durable_verdicts == 6
    assert len(asked) == 1 and len(asked[0]) == 6
    assert len(rows_of(repository, "claim_verdict")) == 6


def test_every_demotion_reason_is_a_static_bounded_code():
    """A refusal names a PROPERTY, from the closed vocabulary, and nothing else."""
    for code in ("V1_EVIDENCE_VERDICT_NOT_DURABLE", "V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE",
                 "V1_EVIDENCE_CURRENT_STATE_DRIFT", "V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED"):
        assert code in V1_VERDICT_REASONS
        assert 1 <= len(V1_VERDICT_REASONS[code]) <= 120


def test_a_lost_lease_escapes_instead_of_becoming_an_answer(repository):
    """A stale worker is infrastructure, never "this field has no evidence".

    Every other durable-write failure is absorbed so a research run still
    returns its research. A LOST LEASE is the one that must not be: it says
    this worker is no longer the run's writer, and it has to reach the
    worker's own lease handling exactly as every other guarded write's does.
    """
    lease = leased_run(repository)

    class StaleBoard(EvidenceBoard):
        def record_evidence_bundle(self, bundle, *, task_key):
            raise AppError("RUN_LEASE_LOST", "the run lease is no longer held", 409)

    with pytest.raises(AppError) as failure:
        settle(repository, lease, engines_record(), board=StaleBoard(repository, lease))
    assert failure.value.code in LEASE_FAILURE_CODES

    # The VERDICT write is the same: a stale worker may not have its lost
    # lease reported as "this field could not be made durable".
    class StaleVerdictBoard(EvidenceBoard):
        def record_verification_verdict(self, verdict):
            raise AppError("RUN_TRANSITION_CONFLICT", "the run lease moved on", 409)

    with pytest.raises(AppError) as verdict_failure:
        settle(repository, leased_run(repository, "worker-stale-verdict"),
               engines_record(), board=StaleVerdictBoard(repository, lease))
    assert verdict_failure.value.code in LEASE_FAILURE_CODES

    # And so is the authoritative READ: it must reach the worker's lease
    # handling rather than be absorbed as "the state is unavailable".
    def stale_read(_run_id, _claim_ids, _limit):
        raise AppError("RUN_LEASE_LOST", "the run lease is no longer held", 409)

    with pytest.raises(AppError) as read_failure:
        settle(repository, leased_run(repository, "worker-stale-read"), engines_record(),
               reader=RepositoryView(repository, stale_read))
    assert read_failure.value.code in LEASE_FAILURE_CODES


# =============================================================================
# 3. the deterministic rules, over evidence
# =============================================================================

def test_two_records_that_disagree_verify_nothing(repository):
    """The merge picked the higher confidence. A contradiction is not a ranking.

    Both records are the same agent's, about the same model, in the same
    identity scope, and they state different power. Neither side is verified,
    and the model is taken down to `rejected` rather than to `needs_review`:
    this run's own evidence contradicted it.
    """
    lease = leased_run(repository)
    technical = {"engines_fuel_power_agent": {
        "agent": "engines_fuel_power_agent",
        "items": [{"model": "RAV4", "power_hp": 218, "sources": [ISRAELI_URL],
                   "confidence": "high"},
                  {"model": "RAV4", "power_hp": 197, "sources": [ISRAELI_URL],
                   "confidence": "low"}]}}
    report = settle(repository, lease, technical)

    assert {verdict for _m, _f, verdict, _r in report.fields} == {"rejected"}
    assert report.rejected("RAV4") and not report.verified("RAV4")
    # The durable verdicts say so too -- this is not an in-process opinion.
    assert {row["verdict"] for row in rows_of(repository, "claim_verdict")} == {"rejected"}

    document = verifier_document()
    apply_evidence_authority(document, report)
    assert document["verified_models"][0]["status"] == "rejected"


def test_the_same_value_stated_twice_is_not_a_contradiction(repository):
    """Agreement is agreement, including across units the contract relates."""
    lease = leased_run(repository)
    technical = {"dimensions_safety_equipment_agent": {
        "agent": "dimensions_safety_equipment_agent",
        "items": [{"model": "RAV4", "trunk_liters": 580, "sources": [ISRAELI_URL]},
                  {"model": "RAV4", "trunk_liters": 580, "sources": [ISRAELI_URL]}]}}
    report = settle(repository, lease, technical)

    assert {verdict for _m, _f, verdict, _r in report.fields} == {"verified"}
    assert report.verified("RAV4")


def test_foreign_only_evidence_never_verifies_an_israeli_market_field(repository):
    """The deterministic source policy gates the VERDICT, not only a note."""
    lease = leased_run(repository)
    report = settle(repository, lease, engines_record(sources=[FOREIGN_URL]))

    assert {reason for _m, _f, _v, reason in report.fields} == {
        "V1_EVIDENCE_NO_ISRAEL_MARKET_SOURCE"}
    assert not report.verified("RAV4")
    # The same records in a market with no Israeli requirement verify fine.
    other = leased_run(repository, worker="worker-2")
    assert settle(repository, other, engines_record(sources=[FOREIGN_URL]),
                  market="Germany").verified("RAV4")


def test_the_model_can_only_take_a_field_down(repository):
    """A classification is a proposal. It refuses, and it never accepts."""
    lease = leased_run(repository)
    flagged = settle(repository, lease, engines_record(),
                     verifier=verifier_document(status="needs_review"))

    assert not flagged.verified("RAV4")
    assert {reason for _m, _f, _v, reason in flagged.fields} == {"V1_EVIDENCE_MODEL_FLAGGED"}

    # And the reverse: a model that says `verified` about a model with no
    # evidence at all does not make it verified.
    document = verifier_document()
    apply_evidence_authority(document, settle(repository, leased_run(repository, "worker-3"),
                                              {}, verifier=document))
    assert document["verified_models"][0]["status"] == "needs_review"


def test_one_unevidenced_field_keeps_the_whole_model_for_review(repository):
    """A model is not "verified except for the engine"; it has an open question."""
    lease = leased_run(repository)
    technical = engines_record()
    # A second agent states one field whose only source is foreign, so that
    # field is refused while the four engine fields are accepted.
    technical["transmission_drivetrain_performance_agent"] = {
        "agent": "transmission_drivetrain_performance_agent",
        "items": [{"model": "RAV4", "drivetrain": "AWD", "sources": [FOREIGN_URL]}]}
    report = settle(repository, lease, technical)

    verdicts = {field: verdict for _m, field, verdict, _r in report.fields}
    assert verdicts["power_hp"] == "verified" and verdicts["drivetrain"] == "needs_review"
    assert not report.verified("RAV4")


# =============================================================================
# 4. durability across a checkpoint and a resume
# =============================================================================

def test_durable_v1_evidence_survives_a_checkpoint_and_resume(repository):
    """A replacement worker replays onto the rows the first one wrote.

    The whole evidence pass runs twice against the same durable state, exactly
    as a resumed run re-runs its phases. Every write is idempotent on an
    identity derived from the evidence's own content, so the second pass adds
    no row, changes no verdict, and resolves to the same current truth.
    """
    lease = leased_run(repository)
    first = settle(repository, lease, engines_record())
    before = {kind: [row["id"] for row in rows_of(repository, kind)]
              for kind in ("source", "evidence_fragment", "claim", "claim_verdict")}
    states = repository.claim_current_verdict_states(lease.run_id)

    second = settle(repository, lease, engines_record())

    assert second.fields == first.fields
    assert {kind: [row["id"] for row in rows_of(repository, kind)]
            for kind in before} == before
    assert repository.claim_current_verdict_states(lease.run_id) == states
    assert all(row["state"] == SUPPORTED_STATE for row in states)


def test_an_idempotent_replay_cannot_create_contradictory_current_truth(repository):
    """Replay is the ONE case that must never look like a re-verification."""
    lease = leased_run(repository)
    settle(repository, lease, engines_record())
    settle(repository, lease, engines_record())
    settle(repository, lease, engines_record())

    verdicts = rows_of(repository, "claim_verdict")
    by_claim: dict[str, set[str]] = {}
    for row in verdicts:
        by_claim.setdefault(str(row["claim_id"]), set()).add(row["verdict"])

    # One verdict row per claim, and therefore one current state per claim.
    assert all(len(values) == 1 for values in by_claim.values())
    assert len(verdicts) == len(by_claim)
    assert {row["state"] for row in repository.claim_current_verdict_states(lease.run_id)} \
        == {SUPPORTED_STATE}


# =============================================================================
# 5. V1 evidence is not a promotion path
# =============================================================================

def test_v1_evidence_can_never_reach_the_canonical_promotion_path(repository):
    """Not a flag -- a property of the data the promotion read matches on."""
    lease = leased_run(repository)
    settle(repository, lease, engines_record())

    assert V1_TOOL_OPERATION != PROMOTABLE_TOOL_OPERATION
    assert repository.catalog_run_pending_promotions(
        lease.run_id, PROMOTABLE_TOOL_OPERATION) == []
    # Even asked for its own operation, there is no catalog snapshot, raw
    # record or candidate behind a V1 source for one to be derived from.
    assert repository.catalog_run_pending_promotions(lease.run_id, V1_TOOL_OPERATION) == []


def test_one_record_of_many_fields_becomes_several_bounded_sources(repository):
    """A record wider than the durable fragment bound is not silently trimmed."""
    lease = leased_run(repository)
    technical = {"dimensions_safety_equipment_agent": {
        "agent": "dimensions_safety_equipment_agent",
        "items": [{"model": "RAV4", "body_type": "SUV", "seats": 5, "trunk_liters": 580,
                   "length_mm": 4600, "width_mm": 1855, "height_mm": 1685,
                   "sources": [ISRAELI_URL]}]}}
    report = settle(repository, lease, technical)

    assert len({field for _m, field, _v, _r in report.fields}) == 6
    assert report.verified("RAV4")
    # Six fields cannot fit one source's fragment bound, so the record was
    # read as two sources -- each naming, in its own query, the fields it was
    # read for -- rather than losing the fields past the bound.
    sources = rows_of(repository, "source")
    fragments = rows_of(repository, "evidence_fragment")
    assert len(sources) == 2 and len(fragments) == 6
    per_source = [len([row for row in fragments
                       if str(row["source_id"]) == str(source["id"])])
                  for source in sources]
    assert sorted(per_source) == [2, 4] and max(per_source) <= MAX_FRAGMENTS_PER_SOURCE


# =============================================================================
# 6. the engine holds its own final document to the evidence
# =============================================================================

def scripted_engine(monkeypatch, repository, lease, *, technical: dict,
                    verifier_status: str = "verified"
                    ) -> tuple[VehicleCatalogEngine, list[dict]]:
    """The real engine, with only the MODEL-calling phases replaced.

    The orchestration, the checkpointing, the evidence authority, the
    deterministic final merge and the Israel source policy are all the real
    ones; nothing here calls a provider.
    """
    canonical = [{"canonical_model_name": "RAV4", "model_name_he": "ראב4",
                  "sources": [ISRAELI_URL], "confidence": "high"}]
    monkeypatch.setattr(core, "run_discovery_phase", lambda *a, **k: [
        {"agent": "current_official_lineup_agent", "status": "success",
         "parsed": {"models": canonical}, "input_tokens": 1, "output_tokens": 1}])
    monkeypatch.setattr(core, "run_normalizer_phase", lambda *a, **k: {
        "agent": "normalizer_deduper", "status": "success",
        "parsed": {"canonical_models": canonical}, "input_tokens": 1, "output_tokens": 1})
    monkeypatch.setattr(core, "run_technical_enrichment_phase", lambda *a, **k: [
        {"agent": agent, "status": "success", "parsed": parsed,
         "input_tokens": 1, "output_tokens": 1}
        for agent, parsed in technical.items()])
    monkeypatch.setattr(core, "run_verification_phase", lambda *a, **k: {
        "agent": "source_verifier", "status": "success", "phase": "verification",
        "parsed": {"agent": "source_verifier",
                   "verified_models": [{"model": "RAV4", "status": verifier_status,
                                        "confidence": "high", "issues": []}],
                   "rejected_data_points": [], "needs_review": []},
        "input_tokens": 1, "output_tokens": 1})
    monkeypatch.setattr(core, "run_hebrew_summary_phase", lambda *a, **k: {
        "agent": "hebrew_summary", "status": "success", "parsed": {"summary": "סיכום"},
        "input_tokens": 1, "output_tokens": 1})
    checkpoints: list[dict] = []
    return VehicleCatalogEngine(
        evidence_authority=V1EvidenceAuthority(board=EvidenceBoard(repository, lease),
                                               repository=repository),
        checkpoint_sink=lambda phase, payload: checkpoints.append(payload)), checkpoints


def test_the_evidence_pass_adds_no_new_run_event_type(monkeypatch, repository):
    """The browser contract is closed, and this work does not widen it.

    `EVENT_TYPES` is what the API accepts from a worker and what the browser
    projects (`frontend/lib/eventVocabulary.ts` mirrors it). The evidence
    summary travels in the run's ARTIFACTS instead, so nothing here needs a
    new type -- and if a later change emits one, this fails rather than the
    run failing in production at the API boundary.
    """
    from backend.runtime import EVENT_TYPES

    lease = leased_run(repository)
    emitted: list[str] = []
    engine, _checkpoints = scripted_engine(monkeypatch, repository, lease,
                                           technical=engines_record())
    engine.event_sink = lambda event_type, _payload: emitted.append(event_type)
    engine.run(VehicleCatalogRunConfig(api_key="", manufacturer="Toyota", market="Israel",
                                       period="2015-2025"))

    assert emitted and set(emitted) <= set(EVENT_TYPES)


def test_the_engine_holds_its_final_document_to_the_evidence(monkeypatch, repository):
    """The product the run returns says `verified` only where evidence does."""
    lease = leased_run(repository)
    engine, checkpoints = scripted_engine(monkeypatch, repository, lease,
                                          technical=engines_record())
    result = engine.run(VehicleCatalogRunConfig(api_key="", manufacturer="Toyota",
                                                market="Israel", period="2015-2025"))

    model = result["result"]["models"][0]
    assert model["verification_status"] == "verified"
    assert rows_of(repository, "claim_verdict")
    # The evidence summary travels in the run's own checkpoint, so a resumed
    # worker restores a document that has already been held to the evidence.
    verification = next(item for item in checkpoints if item["phase"] == "verification")
    assert verification["artifacts"]["evidence_authority"]["durable_verdicts"] == 4


def test_the_engine_downgrades_a_model_the_evidence_does_not_support(monkeypatch,
                                                                     repository):
    """A model-asserted `verified` with a model-reported URL and no value."""
    lease = leased_run(repository)
    urls_only = {"engines_fuel_power_agent": {
        "agent": "engines_fuel_power_agent",
        "items": [{"model": "RAV4", "sources": [ISRAELI_URL], "confidence": "high"}]}}
    engine, _checkpoints = scripted_engine(monkeypatch, repository, lease,
                                           technical=urls_only)
    result = engine.run(VehicleCatalogRunConfig(api_key="", manufacturer="Toyota",
                                                market="Israel", period="2015-2025"))

    model = result["result"]["models"][0]
    assert model["verification_status"] == "needs_review"
    assert model in result["result"]["needs_review"]
    assert rows_of(repository, "claim_verdict") == []
    # A run whose models are all unsettled is not a complete one.
    assert result["status"] == "partial_success"


def test_model_identity_folding_is_the_shared_normalization():
    """One model identity, folded the way every other scope in the repo is."""
    assert model_key(" RAV4  Hybrid ") == model_key("rav4 hybrid")
    assert model_key("") == ""
