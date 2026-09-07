"""R4: deterministic verification, durable support links, conflict resolution
and ONE bounded correction round.

The primary invariant under test is the regression this release exists to
close: a claim that says 1,798 cc about a source whose durable structured fact
says 1,600 cc is REJECTED by deterministic code, whatever a model answers --
because for a structured fact the model is never asked at all.

Everything here is offline and deterministic: the gateway is a fake, the
resolver reads a fake durable repository shaped exactly like the three bounded
internal reads SupabaseRepository exposes, and no network, provider, browser or
paid call is involved anywhere.
"""

from __future__ import annotations

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from backend.engines.swarm_v2 import (
    CONFLICT_POLICY_VERSION, MAX_CORRECTION_ISSUES, MAX_CORRECTION_ROUNDS,
    MAX_SUPPORT_LINKS_PER_VERDICT, MIN_CORRECTION_MODEL_CALLS,
    STRUCTURED_COMPARISON_VERSION, UNIT_CONVERSIONS, UNIT_RULE_VERSION,
    VERIFICATION_MODES, VERIFIER_CONTRACT_VERSION, BoundedTaskExecutor,
    ConflictResolution, CorrectionAllowance, EvidenceReference, FinalBuilder,
    RemainingBudget, StructuredSourceFact, SupportContractError, SupportLink, SwarmState,
    SwarmV2Engine, VerificationVerdict, Verifier, VerifierContractError,
    compare_structured, compare_values, conflict_groups, correction_allowance,
    correction_issues, correction_summary, field_family, is_authoritative,
    normalize_identity, parse_support, resolve_conflicts, scope_identity,
    validate_support,
)
from backend.engines.swarm_v2.evidence_contracts import record_field_locator
from backend.engines.swarm_v2.fragments import fragment_content_hash
from test_swarm_v2 import plan, task
from test_swarm_v2_grounded_verifier import (RUN_ID, TASK, DurableRepository,
                                             GroundingJudgeGateway, fact_row, fragment_row,
                                             ref, resolver_for, source_row, verifier_with)
from test_swarm_v2_stage1_e2e import DECLINE_CORRECTION, Plans, Worker, commander

# The known regression. A source record that states 1,600 cc, and a claim that
# says 1,798 cc about it. Before R4 a model completion decided this; now
# deterministic code does, and the model is not consulted.
CLAIMED_CC = 1798
SOURCE_CC = 1600
FIELD = "engine_displacement_cc"

# A source type this repository's policy calls authoritative for homologated
# technical data, and one it calls authoritative for nothing.
AUTHORITATIVE = "government_registry"
UNRANKED = "primary"


# --- offline fixtures --------------------------------------------------------

def structured_source(source_id: str = "source-0001", *, source_type: str = UNRANKED,
                      version: str | None = "2026.08.1", **overrides) -> dict:
    """A durable source row that records the version it was read at."""
    row = source_row(source_id, source_type=source_type, **overrides)
    if version is not None:
        row.update(source_version_kind="dataset_version", source_version_id=version)
    return row


def projection(source_id: str, *, field: str = FIELD, value: object = SOURCE_CC,
               index: int = 0, run_id: str = RUN_ID, task_key: str = TASK) -> dict:
    """The focused fragment a located structured fact rests on.

    R3 guarantees a fact's locator is also one of its source's own fragment
    locators, so this is exactly the row an accepted deterministic verdict must
    be able to name.
    """
    text = f"{field}={value}"
    return {**fragment_row(source_id, text, index=index, run_id=run_id, task_key=task_key),
            "fragment_type": "structured_projection",
            "locator_key": record_field_locator(source_id, (field,)).locator_key}


def structured_setup(*, claim_value: object = CLAIMED_CC, claim_unit: str | None = "cc",
                     fact_value: object = SOURCE_CC, fact_unit: str | None = "cc",
                     source_type: str = UNRANKED, source_version: str | None = "2026.08.1",
                     claim_source_version: str | None = "dataset_version:2026.08.1",
                     claim_identity: dict | None = None, fact_identity: dict | None = None,
                     claim_field: str = FIELD, fact_field: str = FIELD,
                     claim_scope: dict | None = None, fact_scope: dict | None = None,
                     ):
    """One claim, one versioned structured source, one located fact."""
    scope = claim_scope or {}
    reference = ref("claim-0001", field=claim_field, value=claim_value,
                    entity="vehicle:corolla", **scope)
    reference = reference.model_copy(update={
        "unit": claim_unit, "source_version": claim_source_version,
        "identity": claim_identity or {}})
    facts = [fact_row("source-0001", field=fact_field, value=fact_value, unit=fact_unit,
                      entity="vehicle:corolla", identity=fact_identity,
                      **(fact_scope or {}))]
    resolver = resolver_for([structured_source(source_type=source_type,
                                               version=source_version)],
                            [projection("source-0001", field=fact_field, value=fact_value)],
                            facts)
    return reference, resolver


def only(verdicts) -> VerificationVerdict:
    assert len(verdicts) == 1
    return verdicts[0]


# --- A. the closed structured comparison contract ----------------------------

def test_a_structured_mismatch_is_rejected_whatever_the_model_says():
    """THE regression this release exists to close.

    The gateway below answers `verified` for anything it is asked. It is never
    asked: the claim's own source records 1,600 cc at an exact location, the
    comparison is code, and 1,798 is not 1,600.
    """
    reference, resolver = structured_setup()

    def always_verified(document):
        return {"verdicts": [{"claim_id": claim["claim_id"], "verdict": "verified",
                              "supporting_fragment_hashes":
                                  [document["sources"][0]["fragments"][0]["content_hash"]]}
                             for claim in document["claims"]]}

    verifier, gateway = verifier_with(resolver, GroundingJudgeGateway(always_verified))
    verdict = only(verifier.verify([reference]))
    assert (verdict.verdict, verdict.reason) == ("rejected", "R4_VALUE_MISMATCH")
    assert verdict.mode == "deterministic_structured"
    assert gateway.model_calls == 0          # the model was never consulted
    assert not verdict.support               # a rejection cites nothing


def test_an_exact_structured_match_verifies_with_no_model_call():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    verifier, gateway = verifier_with(resolver)
    verdict = only(verifier.verify([reference]))
    assert (verdict.verdict, verdict.reason) == ("verified", "R4_STRUCTURED_MATCH")
    assert verdict.mode == "deterministic_structured"
    assert gateway.model_calls == 0
    assert verdict.is_r4_grounded


def test_an_allowlisted_unit_rule_converts_exactly_and_preserves_the_original():
    """1.6 l IS 1600 cc under the versioned allowlist -- and the claim keeps
    saying 1.6 l, because a comparison never rewrites the evidence."""
    reference, resolver = structured_setup(claim_value=1.6, claim_unit="l")
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    assert (verdict.verdict, verdict.reason) == ("verified", "R4_STRUCTURED_MATCH")
    assert (reference.value, reference.unit) == (1.6, "l")


@pytest.mark.parametrize("kwargs,expected", [
    # wrong model year / market / geography: the same variant, a different scope
    ({"claim_scope": {"time_scope": {"year": 2021}}}, "R4_SCOPE_MISMATCH"),
    ({"claim_scope": {"market": "US"}}, "R4_SCOPE_MISMATCH"),
    ({"claim_scope": {"geography": "United States"}}, "R4_SCOPE_MISMATCH"),
    # wrong variant: make + commercial model + an overlapping year is NOT identity
    ({"claim_identity": {"generation": "e210"}, "fact_identity": {"generation": "e170"}},
     "R4_IDENTITY_MISMATCH"),
    ({"claim_identity": {"engine": "2zr-fae"}, "fact_identity": {"engine": "1nz-fxe"}},
     "R4_IDENTITY_MISMATCH"),
    ({"claim_identity": {"transmission": "cvt"}, "fact_identity": {"transmission": "mt6"}},
     "R4_IDENTITY_MISMATCH"),
    ({"claim_identity": {"model_code": "zre210l"}, "fact_identity": {"model_code": "nre180l"}},
     "R4_IDENTITY_MISMATCH"),
    # a claim that states NO identity can never match a fact that states one
    ({"fact_identity": {"generation": "e210"}}, "R4_IDENTITY_MISMATCH"),
    # wrong field: the source is structured and simply does not state it
    ({"claim_field": "gross_weight_kg"}, "R4_FIELD_NOT_IN_SOURCE"),
    # wrong source version: evidence read at one version validates no other
    ({"claim_source_version": "dataset_version:2025.01.1"}, "R4_SOURCE_VERSION_MISMATCH"),
    # units: absent, and present but not relatable under the closed allowlist
    ({"claim_unit": None}, "R4_UNIT_MISSING"),
    ({"claim_unit": "hp"}, "R4_UNIT_NOT_CONVERTIBLE"),
    ({"claim_value": "1600cc"}, "R4_VALUE_NOT_COMPARABLE"),
])
def test_a_claim_that_is_not_the_same_statement_never_verifies(kwargs, expected):
    reference, resolver = structured_setup(**{"claim_value": SOURCE_CC, **kwargs})
    verifier, gateway = verifier_with(resolver)
    verdict = only(verifier.verify([reference]))
    assert (verdict.verdict, verdict.reason) == ("rejected", expected)
    assert verdict.mode == "deterministic_structured"
    assert gateway.model_calls == 0


def test_a_source_that_contradicts_itself_is_needs_review_not_a_guess():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    resolver.repository.facts.append(
        fact_row("source-0001", claim_id="fact-second", value=1800, unit="cc",
                 entity="vehicle:corolla"))
    resolver.repository.fragments.append(projection("source-0001", value=1800, index=1))
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    assert (verdict.verdict, verdict.reason) == ("needs_review", "R4_AMBIGUOUS_SOURCE_FACT")


def test_free_text_evidence_still_uses_the_grounded_model_verifier():
    """Deterministic code does not pretend to understand prose.

    A source with no structured fact is exactly the pre-R4 situation, and it
    still travels the grounded model path -- one model call, a cited hash, and
    the backend's own reason.
    """
    reference = ref("claim-0001", value=1798)
    resolver = resolver_for(
        [source_row("source-0001")],
        [fragment_row("source-0001",
                      "Toyota Corolla vehicle spec sheet, geography Israel, IL market, "
                      "model year 2020: engine displacement cc 1798.")])
    verifier, gateway = verifier_with(resolver)
    verdict = only(verifier.verify([reference]))
    assert verdict.verdict == "verified"
    assert verdict.mode == "grounded_model"
    assert gateway.model_calls == 1
    assert [link.content_hash for link in verdict.support] == \
        [fragment_content_hash(gateway.documents[0]["sources"][0]["fragments"][0]["text"])]


def test_the_comparison_contract_is_closed_versioned_and_exact():
    assert STRUCTURED_COMPARISON_VERSION == "r4.structured.1"
    assert UNIT_RULE_VERSION == "r4.units.1"
    # Exact rational arithmetic, never float tolerance.
    assert compare_values(1.6, "l", 1600, "cc") == "R4_STRUCTURED_MATCH"
    assert compare_values(0.1, "l", 100, "cc") == "R4_STRUCTURED_MATCH"
    assert compare_values(1599.9999, "cc", 1600, "cc") == "R4_VALUE_MISMATCH"
    # Case and surrounding space are formatting; equivalence is the allowlist.
    assert compare_values(1600, " CC ", 1600, "cm3") == "R4_STRUCTURED_MATCH"
    # Lossy and reciprocal conversions a person might expect are refused.
    for unit in ("hp", "ps", "bhp", "mpg", "l_100km", "c", "f", "lbft"):
        assert unit not in UNIT_CONVERSIONS
        assert compare_values(100, unit, 100, "kw") == "R4_UNIT_NOT_CONVERTIBLE"
    # Cross-family is never a conversion either.
    assert compare_values(1000, "kg", 1000, "l") == "R4_UNIT_NOT_CONVERTIBLE"
    # Text is compared by canonical identity, never by similarity.
    assert compare_values("Hybrid", None, "hybrid", None) == "R4_STRUCTURED_MATCH"
    assert compare_values("Hybrid", None, "Plug-in Hybrid", None) == "R4_VALUE_MISMATCH"
    assert compare_values(True, None, 1, None) == "R4_VALUE_NOT_COMPARABLE"


def test_identity_normalization_is_closed_and_bounded():
    assert normalize_identity({"generation": " E210 ", "unknown": "x"}) == (("generation", "e210"),)
    assert normalize_identity({"generation": "x" * 200}) == ()
    assert normalize_identity(None) == () and normalize_identity({"engine": None}) == ()
    # Ordering never changes identity.
    assert scope_identity(entity="a", field="f", identity={"engine": "b", "generation": "c"}) == \
        scope_identity(entity="a", field="f", identity={"generation": "c", "engine": "b"})


def test_a_structured_fact_needs_a_locator_to_be_a_comparison_authority():
    """A claim with no locator is a statement ABOUT a source, not a fact read
    FROM one, and never decides a comparison."""
    reference = ref("claim-0001", value=CLAIMED_CC).model_copy(update={"unit": "cc"})
    unlocated = StructuredSourceFact(fact_id="f1", source_id="source-0001", task_id=TASK,
                                     field=FIELD, entity="vehicle:corolla", value=SOURCE_CC,
                                     unit="cc", locator=None)
    result = compare_structured(reference, [unlocated])
    assert (result.outcome, result.reason) == ("not_comparable", "R4_NO_STRUCTURED_FACT")
    assert not result.is_decisive


# --- B. durable support links ------------------------------------------------

def test_a_deterministic_match_names_the_exact_durable_fragment_rows():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    fragment = resolver.repository.fragments[0]
    assert [(link.source_id, link.fragment_id, link.content_hash, link.locator)
            for link in verdict.support] == [
        ("source-0001", fragment["id"], fragment["content_hash"], fragment["locator_key"])]
    assert verdict.contract_version == VERIFIER_CONTRACT_VERSION


def test_support_links_are_validated_against_the_claims_own_source_context():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    context = resolver.resolve([reference])["source-0001"]
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    assert validate_support(verdict.support, source=context,
                            claim_source_version=reference.source_version) == \
        tuple(verdict.support)


@pytest.mark.parametrize("mutation,expected", [
    ({"source_id": "source-9999"}, "SUPPORT_LINK_FOREIGN_SOURCE"),
    ({"content_hash": "b" * 64}, "SUPPORT_LINK_UNKNOWN_EVIDENCE"),
    ({"locator": record_field_locator("source-0001", ("forged",)).locator_key},
     "SUPPORT_LINK_UNKNOWN_EVIDENCE"),
])
def test_a_forged_or_cross_source_support_link_fails_closed(mutation, expected):
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    context = resolver.resolve([reference])["source-0001"]
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    forged = verdict.support[0].model_copy(update=mutation)
    with pytest.raises(SupportContractError) as excinfo:
        validate_support([forged], source=context)
    assert excinfo.value.reason_code == expected


def test_support_from_another_source_version_fails_closed():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    context = resolver.resolve([reference])["source-0001"]
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    with pytest.raises(SupportContractError) as excinfo:
        validate_support(verdict.support, source=context,
                         claim_source_version="dataset_version:2025.01.1")
    assert excinfo.value.reason_code == "SUPPORT_LINK_VERSION_MISMATCH"


def test_a_stored_support_link_from_another_source_fails_a_resume_closed():
    reference, resolver = structured_setup(claim_value=SOURCE_CC)
    verdict = only(verifier_with(resolver)[0].verify([reference]))
    stored = verdict.model_dump(mode="json")
    stored["support"][0]["source_id"] = "source-9999"
    with pytest.raises(VerifierContractError) as excinfo:
        verifier_with(resolver)[0].verify([reference],
                                          existing_verdicts={"claim-0001": stored})
    assert excinfo.value.reason_code == "VERIFIER_STATE_SUPPORT_INVALID"


@pytest.mark.parametrize("raw", ["not-a-list", [{"source_id": "s"}], [{"unknown": 1}],
                                 [{"source_id": "s", "content_hash": "zz"}]])
def test_a_malformed_stored_support_link_is_never_rebuilt(raw):
    with pytest.raises(SupportContractError) as excinfo:
        parse_support(raw)
    assert excinfo.value.reason_code == "SUPPORT_LINK_INVALID"


def test_a_verified_verdict_can_never_be_stored_without_its_evidence():
    """Support links are provenance, so the contract refuses a verdict that
    claims them without saying which contract produced them."""
    link = SupportLink(source_id="s", content_hash="a" * 64)
    with pytest.raises(ValueError):
        VerificationVerdict(claim_id="c", verdict="verified", reason="R4_STRUCTURED_MATCH",
                            support=[link])
    with pytest.raises(ValueError):
        VerificationVerdict(claim_id="c", verdict="verified", reason="r",
                            mode="deterministic_local", contract_version="v", support=[link])
    with pytest.raises(ValueError):  # two sources can never support one claim
        VerificationVerdict(claim_id="c", verdict="verified", reason="r",
                            mode="grounded_model", contract_version="v",
                            support=[link, link.model_copy(update={"source_id": "other"})])


def test_the_verifier_contract_version_and_mode_are_durable_and_bounded():
    assert VERIFICATION_MODES == ("deterministic_local", "deterministic_structured",
                                  "grounded_model")
    assert len(VERIFIER_CONTRACT_VERSION) <= 120
    assert STRUCTURED_COMPARISON_VERSION in VERIFIER_CONTRACT_VERSION
    assert UNIT_RULE_VERSION in VERIFIER_CONTRACT_VERSION
    assert MAX_SUPPORT_LINKS_PER_VERDICT == 4
    with pytest.raises(ValueError):
        VerificationVerdict(claim_id="c", verdict="verified", reason="r",
                            mode="invented_mode", contract_version="v")
    with pytest.raises(ValueError):
        VerificationVerdict(claim_id="c", verdict="verified", reason="r",
                            mode="grounded_model", contract_version="x" * 121)


def test_a_legacy_verdict_is_readable_but_is_not_r4_grounded():
    legacy = VerificationVerdict.model_validate(
        {"claim_id": "claim-0001", "verdict": "verified", "reason": "ok"})
    assert (legacy.mode, legacy.contract_version, legacy.support) == (None, None, [])
    assert not legacy.is_r4_grounded
    grounded = VerificationVerdict(claim_id="c", verdict="needs_review", reason="r",
                                   mode="deterministic_local",
                                   contract_version=VERIFIER_CONTRACT_VERSION)
    assert grounded.is_r4_grounded


# --- C. explicit conflict resolution -----------------------------------------

def conflicting(*, decisive_type: str = AUTHORITATIVE, decisive_field: str = FIELD,
                decisive_value: object = SOURCE_CC):
    """Two claims contradicting each other in one identity, and a third source
    that states the answer structurally."""
    contested = [
        ref("claim-0001", field=decisive_field, value=CLAIMED_CC,
            entity="vehicle:corolla").model_copy(update={"unit": "cc"}),
        ref("claim-0002", source_id="source-0002", field=decisive_field,
            value=decisive_value, entity="vehicle:corolla").model_copy(update={"unit": "cc"}),
    ]
    sources = [source_row("source-0001", source_type=UNRANKED),
               structured_source("source-0002", source_type=decisive_type)]
    fragments = [fragment_row("source-0001", "prose about the corolla"),
                 projection("source-0002", field=decisive_field, value=decisive_value)]
    facts = [fact_row("source-0002", field=decisive_field, value=decisive_value, unit="cc",
                      entity="vehicle:corolla")]
    return contested, resolver_for(sources, fragments, facts)


def test_a_decisive_source_closes_the_conflict_and_changes_the_active_result():
    contested, resolver = conflicting()
    verifier, gateway = verifier_with(resolver)
    plan = verifier.prepare(contested, conflict_claim_ids={"claim-0001", "claim-0002"})
    verdicts = {item.claim_id: item for item in verifier.verify_prepared(plan)}
    assert gateway.model_calls == 0
    resolution = only(plan.resolutions)
    assert (resolution.state, resolution.reason, resolution.winning_claim_id) == (
        "resolved", "R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE", "claim-0002")
    assert resolution.superseded_claim_ids == ["claim-0001"]
    assert resolution.policy_version == CONFLICT_POLICY_VERSION
    # The active result changes: the winner is verified, the loser is not.
    assert verdicts["claim-0002"].verdict == "verified"
    assert (verdicts["claim-0001"].verdict, verdicts["claim-0001"].reason) == (
        "rejected", "R4_SUPERSEDED_BY_DECISIVE_SOURCE")
    # And the losing claim is still entirely present in the run's history.
    assert resolution.state_of("claim-0001") == "superseded"
    assert set(resolution.claim_ids) == {"claim-0001", "claim-0002"}


def test_source_authority_is_field_specific():
    """Government presence and an official model code prove regulatory identity
    and homologated technical data. They prove nothing about a market price or
    a reliability statistic."""
    assert is_authoritative(AUTHORITATIVE, FIELD)
    assert is_authoritative(AUTHORITATIVE, "model_code")
    assert not is_authoritative(AUTHORITATIVE, "list_price")
    assert not is_authoritative(AUTHORITATIVE, "reliability_score")
    assert is_authoritative("importer_price_list", "list_price")
    assert not is_authoritative("importer_price_list", FIELD)
    # Unknown source type and unknown field are both authoritative for nothing.
    assert not is_authoritative("a_confident_blog", FIELD)
    assert not is_authoritative(AUTHORITATIVE, "vibes")
    assert field_family("vibes") is None


def test_a_government_source_cannot_settle_a_price_conflict():
    contested, resolver = conflicting(decisive_field="list_price", decisive_value=99_000)
    verifier, gateway = verifier_with(resolver)
    plan = verifier.prepare(contested, conflict_claim_ids={"claim-0001", "claim-0002"})
    resolution = only(plan.resolutions)
    assert (resolution.state, resolution.reason) == (
        "unresolved", "R4_CONFLICT_UNRESOLVED_NO_DECISIVE_SOURCE")
    assert {v.verdict for v in verifier.verify_prepared(plan)} == {"needs_review"}
    assert gateway.model_calls == 0


def test_two_decisive_sources_that_disagree_stay_unresolved():
    contested, resolver = conflicting()
    # Make the FIRST source decisive too, stating the other value.
    resolver.repository.sources[0] = structured_source("source-0001",
                                                       source_type=AUTHORITATIVE)
    resolver.repository.fragments[0] = projection("source-0001", value=CLAIMED_CC)
    resolver.repository.facts.append(
        fact_row("source-0001", value=CLAIMED_CC, unit="cc", entity="vehicle:corolla"))
    contested[0] = contested[0].model_copy(
        update={"source_version": "dataset_version:2026.08.1"})
    verifier, _ = verifier_with(resolver)
    plan = verifier.prepare(contested, conflict_claim_ids={"claim-0001", "claim-0002"})
    resolution = only(plan.resolutions)
    assert (resolution.state, resolution.reason) == (
        "unresolved", "R4_CONFLICT_UNRESOLVED_AMBIGUOUS")
    assert resolution.winning_claim_id is None and resolution.superseded_claim_ids == []
    assert {v.reason for v in verifier.verify_prepared(plan)} == {"unresolved conflict"}


def test_an_identity_difference_is_never_a_contradiction():
    """Make + commercial model + an overlapping year does not identify a
    variant, so two generations stating different values do not conflict."""
    base = ref("claim-0001", value=1798, entity="toyota corolla")
    other = ref("claim-0002", source_id="source-0002", value=1600, entity="toyota corolla")
    assert conflict_groups([base, other])          # same identity: a conflict
    distinct = other.model_copy(update={"identity": {"generation": "e170"}})
    assert conflict_groups([base, distinct]) == {}  # different variant: not one


def test_a_resolution_can_never_delete_or_half_supersede_a_claim():
    common = {"scope_hash": "a" * 64, "entity": "e", "field": "f",
              "policy_version": CONFLICT_POLICY_VERSION,
              "claim_ids": ["c1", "c2", "c3"]}
    with pytest.raises(ValueError):  # a resolved conflict must close something
        ConflictResolution(state="resolved", reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                           winning_claim_id="c1", superseded_claim_ids=[], **common)
    with pytest.raises(ValueError):  # the winner is never one of the losers
        ConflictResolution(state="resolved", reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                           winning_claim_id="c1", superseded_claim_ids=["c1", "c2"], **common)
    with pytest.raises(ValueError):  # an unresolved conflict supersedes nothing
        ConflictResolution(state="unresolved",
                           reason="R4_CONFLICT_UNRESOLVED_AMBIGUOUS",
                           superseded_claim_ids=["c2"], **common)
    with pytest.raises(ValueError):  # a winner outside the conflict is impossible
        ConflictResolution(state="resolved", reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
                           winning_claim_id="c9", superseded_claim_ids=["c1", "c2", "c3"],
                           **common)


def test_corroborating_claims_are_not_superseded_losers():
    agreeing = [ref(f"claim-000{index}", source_id=f"source-000{index}", value=value,
                    entity="vehicle:corolla")
                for index, value in ((1, SOURCE_CC), (2, SOURCE_CC), (3, CLAIMED_CC))]
    groups = conflict_groups(agreeing)
    resolution = only(resolve_conflicts(groups, decisive_claim_ids={"claim-0001"}))
    assert resolution.winning_claim_id == "claim-0001"
    assert resolution.superseded_claim_ids == ["claim-0003"]
    assert resolution.state_of("claim-0002") == "corroborating"  # it agrees; it never lost


def test_a_decisive_source_changes_the_ACTIVE_final_result_of_a_whole_run():
    """The acceptance criterion, end to end through the engine.

    Before R4 both contradicting claims were `needs_review / unresolved
    conflict` forever and `fields` stayed empty however good a source arrived.
    Now the decisive value is the run's answer and the loser is preserved in
    history rather than presented as a result.
    """
    refs, resolver = conflicting()
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = [FIELD]
    client = Plans(plan([planned]),
                   [{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "v"},
                    DECLINE_CORRECTION], final=None)
    checkpoints = []
    result = engine_for(client, resolver, refs, checkpoints=checkpoints).run(
        {"id": RUN_ID, "input": {"objective": "conflict", "commander_model": "fake"}})
    # The ACTIVE result is the decisive value, and only it.
    assert [entry["value"] for entry in result["fields"][FIELD]] == [SOURCE_CC]
    assert only(result["fields"][FIELD])["provenance"]["claim_id"] == "claim-0002"
    assert CLAIMED_CC not in [entry["value"] for entry in result["fields"][FIELD]]
    # The losing claim is preserved in full: its evidence reference, its
    # verdict and the decision that superseded it are all durable.
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert {item["claim_id"] for item in state["evidence_references"]} == {
        "claim-0001", "claim-0002"}
    assert state["verifier_state"]["claim-0001"]["reason"] == \
        "R4_SUPERSEDED_BY_DECISIVE_SOURCE"
    resolution = only(state["conflict_resolutions"])
    assert (resolution["state"], resolution["winning_claim_id"],
            resolution["superseded_claim_ids"]) == ("resolved", "claim-0002", ["claim-0001"])


# --- D. exactly one bounded correction round ---------------------------------

def correction_plan(*, max_replans: int = 2, field: str = "answer") -> dict:
    """The plan a Commander returns for the ONE bounded correction round.

    The research task carries no evidence requirement of its own: the fixture
    worker produces an answer, not evidence, so demanding evidence from it
    would fail the run for a reason that has nothing to do with R4.
    """
    research = task("fix", "acquire the missing evidence")
    research["completion"]["evidence_satisfied"] = False
    research["evidence"]["required_fields"] = []
    research["evidence"]["minimum_sources"] = 0
    first = task("a", "a")
    first["evidence"]["required_fields"] = [field]
    return plan([first, research], max_replans=max_replans)


def engine_for(client, resolver, refs, *, checkpoints=None, events=None,
               budget: RemainingBudget | None = None, verdicts=None, resolutions=None):
    return SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=GroundingJudgeGateway(), model="fake", resolver=resolver),
        builder=FinalBuilder(), evidence_loader=lambda _: list(refs),
        checkpoint_sink=(None if checkpoints is None
                         else lambda phase, value: checkpoints.append(deepcopy(value))),
        event_sink=(None if events is None
                    else lambda kind, payload: events.append((kind, payload))),
        remaining_budget=(None if budget is None else lambda: budget),
        verdict_sink=(None if verdicts is None else verdicts.append),
        resolution_sink=(None if resolutions is None else resolutions.append))


def gap_run(*, decisions, budget: RemainingBudget | None = None, checkpoints=None,
            events=None, verdicts=None, resolutions=None, checkpoint=None,
            max_replans: int = 2):
    """One run whose final verification discovers a missing-evidence gap."""
    refs = [ref("claim-0001", field="answer", value=1798)]
    resolver = resolver_for([source_row("source-0001")], [])   # source exists, no evidence
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["answer"]
    client = Plans(plan([planned], max_replans=max_replans), decisions, final=None)
    engine = engine_for(client, resolver, refs, checkpoints=checkpoints, events=events,
                        budget=budget, verdicts=verdicts, resolutions=resolutions)
    payload = {"id": RUN_ID, "input": {"objective": "gap", "commander_model": "fake"}}
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint
    return client, engine.run(payload)


def test_a_verifier_discovered_gap_creates_exactly_one_correction_round():
    correction = correction_plan()
    events, checkpoints = [], []
    client, result = gap_run(
        decisions=[{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"},
                   {"decision": "ADD_TASKS", "plan": correction, "reason": "research the gap"},
                   {"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "still open"}],
        events=events, checkpoints=checkpoints)
    kinds = [kind for kind, _ in events]
    assert kinds.count("correction_round_started") == 1
    assert client.replans == 3          # verify, the ONE correction, then finish
    states = [c["artifacts"]["swarm_state"] for c in checkpoints]
    assert states[-1]["correction_rounds"] == MAX_CORRECTION_ROUNDS == 1
    # The run still finalizes under the R1 outcome contract.
    assert (result["status"], result["result_kind"]) == ("partial_success", "no_usable_result")


def test_the_correction_summary_is_compact_structured_and_backend_authored():
    correction = correction_plan()
    client, _ = gap_run(
        decisions=[{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"},
                   {"decision": "ADD_TASKS", "plan": correction, "reason": "research the gap"},
                   {"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "still open"}])
    summaries = [ctx for ctx in client.contexts if isinstance(ctx, dict)
                 and "verification_findings" in ctx]
    findings = only(summaries)["verification_findings"]
    issue = only(findings["issues"])
    assert issue["code"] == "R4_ISSUE_MISSING_EVIDENCE" and issue["missing_evidence"]
    assert (issue["entity"], issue["field"]) == ("vehicle:corolla", "answer")
    assert issue["scope"] == {"geography": "Israel", "market": "IL",
                              "time_scope": {"year": 2020}}
    assert only(issue["claims"])["claim_id"] == "claim-0001"
    assert findings["correction_rounds_remaining"] == MAX_CORRECTION_ROUNDS
    # Bounded, and every string is an identifier or a module constant.
    assert len(json.dumps(findings).encode()) <= 8192
    assert len(findings["issues"]) <= MAX_CORRECTION_ISSUES


@pytest.mark.parametrize("budget,expected", [
    (RemainingBudget(cost_units=100, tool_calls=10, tasks=0, model_calls=50),
     "R4_CORRECTION_NO_TASK_BUDGET"),
    (RemainingBudget(cost_units=100, tool_calls=0, tasks=5, model_calls=50),
     "R4_CORRECTION_NO_TOOL_CALL_BUDGET"),
    (RemainingBudget(cost_units=100, tool_calls=10, tasks=5,
                     model_calls=MIN_CORRECTION_MODEL_CALLS - 1),
     "R4_CORRECTION_NO_MODEL_CALL_BUDGET"),
    (RemainingBudget(cost_units=100, tool_calls=10, tasks=5, model_calls=50, retries=0),
     "R4_CORRECTION_NO_RETRY_BUDGET"),
    (RemainingBudget(cost_units=0, tool_calls=10, tasks=5, model_calls=50),
     "R4_CORRECTION_NO_COST_BUDGET"),
])
def test_an_unavailable_budget_blocks_the_correction_round(budget, expected):
    assert correction_allowance(rounds_used=0, remaining=budget, replans_used=0,
                                max_replans=2) == CorrectionAllowance(False, expected)


def test_a_plan_with_no_replan_allowance_gets_no_correction_round():
    generous = RemainingBudget(cost_units=100, tool_calls=10, tasks=5, model_calls=50)
    assert correction_allowance(rounds_used=0, remaining=generous, replans_used=2,
                                max_replans=2).reason == "R4_CORRECTION_NO_REPLAN_BUDGET"


def test_an_exhausted_allowance_creates_no_second_round():
    generous = RemainingBudget(cost_units=100, tool_calls=10, tasks=5, model_calls=50)
    assert correction_allowance(rounds_used=MAX_CORRECTION_ROUNDS, remaining=generous,
                                replans_used=0, max_replans=9).reason == \
        "R4_CORRECTION_ALLOWANCE_SPENT"


@pytest.mark.parametrize("budget,max_replans,expected", [
    # A run that can START but has no semantic retry allowance left: the
    # correction round is refused without touching the plan's feasibility.
    (RemainingBudget(cost_units=10_000, tool_calls=50, tasks=32, model_calls=50, retries=0),
     2, "R4_CORRECTION_NO_RETRY_BUDGET"),
    # A plan that permits no replan at all never gets an extra round through
    # this door either: a correction round IS a replan and is charged as one.
    (None, 0, "R4_CORRECTION_NO_REPLAN_BUDGET"),
])
def test_an_unavailable_budget_creates_no_additional_round_in_a_real_run(
        budget, max_replans, expected):
    events = []
    _, result = gap_run(
        decisions=[{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"}],
        budget=budget, events=events, max_replans=max_replans)
    blocked = [payload for kind, payload in events if kind == "correction_round_blocked"]
    assert only(blocked)["reason"] == expected
    assert not [kind for kind, _ in events if kind == "correction_round_started"]
    assert result["status"] == "partial_success"


def test_a_commander_that_declines_the_round_finalizes_the_run():
    events = []
    _, result = gap_run(
        decisions=[{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"},
                   DECLINE_CORRECTION],
        events=events)
    kinds = [kind for kind, _ in events]
    assert kinds.count("correction_round_started") == 1
    assert kinds.count("correction_round_declined") == 1
    assert result["status"] == "partial_success"


def test_a_run_with_nothing_to_correct_never_asks_for_a_round():
    """A fully verified run has no verifier-discovered gap, so no correction
    decision is requested at all -- the allowance is not spent by default."""
    refs = [ref("claim-0001", field="answer", value=1798)]
    resolver = resolver_for(
        [source_row("source-0001")],
        [fragment_row("source-0001",
                      "Toyota Corolla vehicle spec sheet, geography Israel, IL market, "
                      "model year 2020: answer 1798.")])
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = ["answer"]
    client = Plans(plan([planned]),
                   [{"decision": "FINISH", "plan": None, "reason": "done"}], final=None)
    events = []
    result = engine_for(client, resolver, refs, events=events).run(
        {"id": RUN_ID, "input": {"objective": "clean", "commander_model": "fake"}})
    assert client.replans == 1
    assert not [kind for kind, _ in events if kind.startswith("correction_round")]
    assert result["status"] == "complete"
    assert correction_issues(refs, [VerificationVerdict(
        claim_id="claim-0001", verdict="verified", reason="ok",
        mode="grounded_model", contract_version=VERIFIER_CONTRACT_VERSION)]) == []


def test_the_correction_summary_drops_issues_rather_than_truncating_a_structure():
    issues = correction_issues(
        [ref(f"claim-{index:04d}", entity=f"vehicle-{index}", field="answer", value=index)
         for index in range(40)],
        [VerificationVerdict(claim_id=f"claim-{index:04d}", verdict="needs_review",
                             reason="SOURCE_CONTEXT_UNAVAILABLE", mode="deterministic_local",
                             contract_version=VERIFIER_CONTRACT_VERSION)
         for index in range(40)])
    assert len(issues) == MAX_CORRECTION_ISSUES
    summary = correction_summary(issues)
    assert len(json.dumps(summary).encode()) <= 8192
    assert all(set(item) == set(issues[0]) for item in summary["issues"])


def test_a_correction_round_can_acquire_the_decisive_source_and_close_a_conflict():
    """The whole R4 acceptance path, end to end.

    Verification finds an unresolved contradiction, the ONE bounded correction
    round researches it, the new source is field-authoritative and states the
    value structurally, and the run's ACTIVE result changes -- instead of the
    conflict staying open forever with one more task next to it.
    """
    contested = [
        ref("claim-0001", field=FIELD, value=CLAIMED_CC,
            entity="vehicle:corolla").model_copy(update={"unit": "cc"}),
        ref("claim-0002", source_id="source-0002", field=FIELD, value=SOURCE_CC,
            entity="vehicle:corolla").model_copy(update={"unit": "cc"}),
    ]
    # The decisive claim arrives only once the correction task has run, from a
    # source that source-authority policy trusts for THIS field.
    decisive = ref("claim-0003", source_id="source-0003", field=FIELD, value=SOURCE_CC,
                   entity="vehicle:corolla", task_id="fix").model_copy(
        update={"unit": "cc", "source_version": "dataset_version:2026.08.1"})
    resolver = resolver_for(
        [source_row("source-0001"), source_row("source-0002"),
         structured_source("source-0003", source_type=AUTHORITATIVE, task_key="fix")],
        [fragment_row("source-0001", "prose about the corolla"),
         fragment_row("source-0002", "different prose about the corolla"),
         projection("source-0003", task_key="fix")],
        [fact_row("source-0003", task_key="fix", value=SOURCE_CC, unit="cc",
                  entity="vehicle:corolla")])

    def evidence_for(results):
        return [*contested, *([decisive] if "fix" in results else [])]

    correction = correction_plan(field=FIELD)
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = [FIELD]
    client = Plans(plan([planned]),
                   [{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"},
                    {"decision": "ADD_TASKS", "plan": correction,
                     "reason": "research the contradiction"},
                    {"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "done"}],
                   final=None)
    events, checkpoints, resolutions = [], [], []
    engine = SwarmV2Engine(
        commander=commander(client),
        executor=BoundedTaskExecutor(worker_factory=lambda: Worker([]), max_active_workers=1),
        verifier=Verifier(gateway=GroundingJudgeGateway(), model="fake", resolver=resolver),
        builder=FinalBuilder(), evidence_loader=evidence_for,
        checkpoint_sink=lambda phase, value: checkpoints.append(deepcopy(value)),
        event_sink=lambda kind, payload: events.append((kind, payload)),
        resolution_sink=resolutions.append)
    result = engine.run({"id": RUN_ID,
                         "input": {"objective": "conflict", "commander_model": "fake"}})

    final_state = checkpoints[-1]["artifacts"]["swarm_state"]
    final_verdicts = final_state["verifier_state"]
    kinds = [kind for kind, _ in events]
    assert kinds.count("correction_round_started") == 1     # exactly one, never two
    # The FIRST pass left the contradiction open; the second closed it.
    assert [payload["state"] for kind, payload in events
            if kind == "conflict_resolution_recorded"] == ["unresolved", "resolved"]
    assert [item.state for item in resolutions] == ["unresolved", "resolved"]
    # The active result changed: the decisive value is the answer, and both
    # losing and corroborating claims are still in the run's history.
    assert {entry["value"] for entry in result["fields"][FIELD]} == {SOURCE_CC}
    # Only the decisive claim is an ANSWER. claim-0002 corroborated the
    # decision -- it stated the winning value, so it never lost and was never
    # superseded -- but its own source captured nothing that supports it, so it
    # still has to earn its own verdict and does not become a result.
    assert {entry["provenance"]["claim_id"] for entry in result["fields"][FIELD]} == {
        "claim-0003"}
    assert final_verdicts["claim-0002"]["verdict"] == "needs_review"
    assert final_state["correction_rounds"] == 1
    assert final_verdicts["claim-0001"]["reason"] == "R4_SUPERSEDED_BY_DECISIVE_SOURCE"
    assert final_verdicts["claim-0003"]["mode"] == "deterministic_structured"
    assert {item["claim_id"] for item in final_state["evidence_references"]} == {
        "claim-0001", "claim-0002", "claim-0003"}


# --- E. resume duplicates nothing --------------------------------------------

def test_resume_at_each_checkpoint_duplicates_no_call_verdict_link_or_task():
    correction = correction_plan()
    decisions = [{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "verify"},
                 {"decision": "ADD_TASKS", "plan": correction, "reason": "research the gap"},
                 {"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "still open"}]
    checkpoints, verdicts, resolutions = [], [], []
    _, expected = gap_run(decisions=decisions, checkpoints=checkpoints, verdicts=verdicts,
                          resolutions=resolutions)
    assert checkpoints, "the run must checkpoint before anything can be resumed"

    for index, saved in enumerate(checkpoints):
        resumed_verdicts, resumed_checkpoints = [], []
        _, result = gap_run(decisions=deepcopy(decisions), verdicts=resumed_verdicts,
                            checkpoints=resumed_checkpoints, checkpoint=deepcopy(saved))
        assert result == expected, f"resume from checkpoint {index} changed the outcome"
        state = resumed_checkpoints[-1]["artifacts"]["swarm_state"]
        # No duplicate task, evidence reference, verdict, support link or round.
        assert len(state["completed_task_ids"]) == len(set(state["completed_task_ids"]))
        claims = [item["claim_id"] for item in state["evidence_references"]]
        assert len(claims) == len(set(claims))
        assert state["correction_rounds"] <= MAX_CORRECTION_ROUNDS
        keys = [(item.claim_id, item.verdict, item.reason) for item in resumed_verdicts]
        # Every verdict handed to the durable sink is idempotent by identity:
        # the same claim never receives two DIFFERENT durable verdicts.
        assert len({claim for claim, _, _ in keys}) == len({(c, v, r) for c, v, r in keys})


def test_a_resumed_checkpoint_keeps_its_verdict_provenance():
    checkpoints = []
    gap_run(decisions=[{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "v"},
                       DECLINE_CORRECTION], checkpoints=checkpoints)
    state = checkpoints[-1]["artifacts"]["swarm_state"]
    assert state["verifier_contract_version"] == VERIFIER_CONTRACT_VERSION
    stored = state["verifier_state"]["claim-0001"]
    assert stored["mode"] == "deterministic_local"
    assert stored["contract_version"] == VERIFIER_CONTRACT_VERSION
    resumed = SwarmState.resume(state, run_id=RUN_ID)
    assert resumed.correction_rounds == 0 and resumed.conflict_resolutions == []


# --- F. the browser boundary stays exactly where it was ----------------------

def test_internal_support_metadata_never_reaches_the_public_product_output():
    refs, resolver = conflicting()
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = [FIELD]
    client = Plans(plan([planned]),
                   [{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "v"},
                    DECLINE_CORRECTION], final=None)
    events, checkpoints = [], []
    result = engine_for(client, resolver, refs, events=events, checkpoints=checkpoints).run(
        {"id": RUN_ID, "input": {"objective": "conflict", "commander_model": "fake"}})
    encoded = json.dumps(result)
    assert "content_hash" not in encoded and "fragment_id" not in encoded
    assert "support" not in encoded and VERIFIER_CONTRACT_VERSION not in encoded
    # Nor through a run event: those carry identifiers and static codes only.
    for _, payload in events:
        assert not {"content_hash", "fragment_id", "support"} & set(payload)
    # The service-only checkpoint is where the provenance actually lives.
    assert "content_hash" in json.dumps(checkpoints)


# --- G. the Evidence Board is the ONE durable write path ---------------------

class VerificationRepository:
    """A faithful stand-in for the two lease-guarded R4 write RPCs.

    Like the guarded RPCs it validates provenance rather than trusting the
    payload, and it is idempotent on `evidence_key`, so a replay lands on the
    same durable row instead of appending a second one.
    """

    def __init__(self, lease, *, fragments=(), claims=()):
        self.lease = lease
        self.fragments = {row["id"]: row for row in fragments}
        self.claims = {row["id"]: row for row in claims}
        self.verdicts: dict[str, dict] = {}
        self.supports: list[dict] = []
        self.resolutions: dict[str, dict] = {}

    def _lease(self, kwargs):
        assert kwargs == {"worker_id": self.lease.worker_id, "attempt": self.lease.attempt,
                          "lease_token": self.lease.lease_token}

    def record_claim_verdict(self, run_id, payload, **kwargs):
        self._lease(kwargs)
        claim = self.claims.get(str(payload["claim_id"]))
        if claim is None:
            raise ValueError("invalid claim verdict claim")
        for link in payload["support"]:
            fragment = self.fragments.get(str(link["fragment_id"]))
            if fragment is None:
                raise ValueError("verdict support link does not name durable evidence")
            if fragment["source_id"] != claim["source_id"]:
                raise ValueError("verdict support link belongs to another source")
            if fragment["content_hash"] != link["content_hash"]:
                raise ValueError("verdict support link content hash mismatch")
        key = payload["evidence_key"]
        row = self.verdicts.setdefault(key, {"id": str(uuid4()), "run_id": str(run_id),
                                             **payload})
        if row["verdict"] != payload["verdict"] or row["reason"] != payload["reason"]:
            raise ValueError("claim verdict idempotency conflict")
        self.supports = [item for item in self.supports if item["verdict_id"] != row["id"]]
        self.supports.extend({"verdict_id": row["id"], **link} for link in payload["support"])
        return row

    def record_conflict_resolution(self, run_id, payload, **kwargs):
        self._lease(kwargs)
        key = payload["evidence_key"]
        return self.resolutions.setdefault(key, {"id": str(uuid4()), "run_id": str(run_id),
                                                 **payload})


def board_fixture():
    from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
    lease = WorkerLease(uuid4(), "worker-1", 2, "lease-token")
    fragment = {"id": str(uuid4()), "source_id": "source-0001",
                "content_hash": fragment_content_hash("engine_displacement_cc=1600")}
    claim = {"id": "claim-0001", "source_id": "source-0001"}
    repository = VerificationRepository(lease, fragments=[fragment], claims=[claim])
    return EvidenceBoard(repository, lease), repository, fragment


def test_the_board_persists_a_verdict_with_its_support_and_replays_onto_one_row():
    board, repository, fragment = board_fixture()
    verdict = VerificationVerdict(
        claim_id="claim-0001", verdict="verified", reason="R4_STRUCTURED_MATCH",
        mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
        support=[SupportLink(source_id="source-0001", fragment_id=fragment["id"],
                             content_hash=fragment["content_hash"],
                             locator=record_field_locator("source-0001", (FIELD,)).locator_key)])
    stored = board.record_verification_verdict(verdict)
    assert stored["verification_mode"] == "deterministic_structured"
    assert stored["verifier_contract_version"] == VERIFIER_CONTRACT_VERSION
    assert [link["fragment_id"] for link in repository.supports] == [fragment["id"]]
    # Replay -- a resume, a re-verification after the correction round, a
    # retried batch -- is idempotent by the verdict's own content.
    assert board.record_verification_verdict(verdict)["id"] == stored["id"]
    assert len(repository.verdicts) == 1 and len(repository.supports) == 1


def test_the_board_refuses_a_verdict_with_no_provenance_or_forged_support():
    board, _, fragment = board_fixture()
    from backend.engines.swarm_v2.evidence import EvidenceValidationError
    with pytest.raises(EvidenceValidationError):
        board.record_verification_verdict(VerificationVerdict(
            claim_id="claim-0001", verdict="verified", reason="ok"))   # no mode/contract
    with pytest.raises(EvidenceValidationError):
        board.record_verification_verdict({"claim_id": "claim-0001"})  # not a contract
    forged = VerificationVerdict(
        claim_id="claim-0001", verdict="verified", reason="R4_STRUCTURED_MATCH",
        mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
        support=[SupportLink(source_id="source-0001", fragment_id=str(uuid4()),
                             content_hash=fragment["content_hash"])])
    with pytest.raises(ValueError, match="does not name durable evidence"):
        board.record_verification_verdict(forged)


def test_the_board_persists_a_conflict_resolution_idempotently():
    board, repository, _ = board_fixture()
    resolution = ConflictResolution(
        scope_hash="a" * 64, entity="vehicle:corolla", field=FIELD, state="resolved",
        reason="R4_CONFLICT_RESOLVED_BY_DECISIVE_SOURCE",
        policy_version=CONFLICT_POLICY_VERSION, claim_ids=["claim-0001", "claim-0002"],
        winning_claim_id="claim-0002", superseded_claim_ids=["claim-0001"])
    stored = board.record_conflict_resolution(resolution)
    assert stored["state"] == "resolved" and stored["winning_claim_id"] == "claim-0002"
    assert board.record_conflict_resolution(resolution)["id"] == stored["id"]
    assert len(repository.resolutions) == 1
    from backend.engines.swarm_v2.evidence import EvidenceValidationError
    with pytest.raises(EvidenceValidationError):
        board.record_conflict_resolution({"state": "resolved"})


def test_the_engine_hands_every_verdict_and_decision_to_the_durable_sinks():
    refs, resolver = conflicting()
    planned = task("a", "a")
    planned["evidence"]["required_fields"] = [FIELD]
    client = Plans(plan([planned]),
                   [{"decision": "REQUEST_VERIFICATION", "plan": None, "reason": "v"},
                    DECLINE_CORRECTION], final=None)
    verdicts, resolutions = [], []
    engine_for(client, resolver, refs, verdicts=verdicts, resolutions=resolutions).run(
        {"id": RUN_ID, "input": {"objective": "conflict", "commander_model": "fake"}})
    assert {item.claim_id for item in verdicts} == {"claim-0001", "claim-0002"}
    assert all(item.mode in VERIFICATION_MODES and
               item.contract_version == VERIFIER_CONTRACT_VERSION for item in verdicts)
    assert [item.state for item in resolutions] == ["resolved"]
    assert only(resolutions).policy_version == CONFLICT_POLICY_VERSION
