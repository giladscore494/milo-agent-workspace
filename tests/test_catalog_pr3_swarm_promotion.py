"""Catalog PR3: the whole path, end to end, and every gate that refuses it.

    Commander plan
      -> PlanValidator (the deterministic firewall)
      -> GenericWorker -> ToolRegistry -> GovernmentVehicleTool
      -> validated, bounded tool result
      -> TrustedEvidenceAcquisition (the ONE registered Government mapper)
      -> versioned source + focused fragments + located claims
      -> verified verdicts
      -> catalog_candidate_evidence_links
      -> field-level canonical promotion
      -> the canonical read model

Offline and deterministic. Every byte is a committed R5 fixture re-hashed
against the R5 manifest, an autouse fixture makes creating a socket an error,
and no model is called anywhere: the worker runs with a trusted deterministic
task-output strategy, so this whole proof costs zero provider calls.

`tests/test_migrations_postgres.py` proves the durable half against real
PostgreSQL -- the promotion transaction, both triggers and the ACLs. What lives
here is everything above the database: the plan firewall, the tool bounds, the
mapping, the reconciliation, the promotion assembly, the refresh and the
refusals each of them owns.
"""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.catalog.contracts import CANONICAL_VARIANT_FIELDS
from backend.catalog.government import source as src
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.evidence import (GOVERNMENT_FIELD_SOURCES,
                                                 GovernmentVariantEvidenceMapper,
                                                 government_entity_key, government_record_id)
from backend.catalog.government.ingest import GovernmentCatalogIngestor
from backend.catalog.government.query import GovernmentCatalogQuery
from backend.catalog.government.reconcile import (AliasRule, CatalogReconciliationError,
                                                  ReconcilableVariant, normalize_identity_text,
                                                  reconcile_catalog,
                                                  variants_from_candidate_rows)
from backend.catalog.diff import MAX_DIFF_ITEMS, diff_rows, is_count_row
from backend.catalog.government.refresh import (GovernmentCatalogRefresh,
                                                diff_candidate_sets)
from backend.catalog.keys import CatalogKeyError
from backend.catalog.pipeline import (MAX_PROMOTIONS_PER_RUN, PROMOTABLE_TOOL_OPERATION,
                                      CatalogPromotionPipeline)
from backend.catalog.promotion import (LEASE_FAILURE_CODES, CanonicalPromotion,
                                       CatalogPromotionError, build_promotion_plan,
                                       field_evidence_for)
from backend.engines.swarm_v2 import PlanLimits, PlanValidator
from backend.engines.swarm_v2.contracts import SupportLink, VerificationVerdict
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.evidence_mapping import (NO_EVIDENCE,
                                                       PRODUCTION_EVIDENCE_MAPPER_OPERATIONS,
                                                       EvidenceMappingError,
                                                       RegisteredOperationEvidenceSink,
                                                       TrustedEvidenceAcquisition,
                                                       production_evidence_mappers)
from backend.engines.swarm_v2.evidence_contracts import (SourceVersion,
                                                         record_field_locator,
                                                         structured_projection)
from backend.engines.swarm_v2.support import VERIFIER_CONTRACT_VERSION
from backend.engines.swarm_v2.tool_calls import ToolCallRecord
from backend.engines.swarm_v2.validation import (PlanValidationError, SOURCE_FIRST_TOOL_POLICY,
                                                 provider_plan_policy)
from backend.engines.swarm_v2.worker import GenericWorker
from backend.errors import AppError
from backend.runtime import CancellationRequested
from backend.testing.government_capture import (FixtureTransport, PINNED_PAGE_LIMIT,
                                                PINNED_QUERY)
from backend.testing.memory_repository import MemoryRepository
from backend.tools import ToolContext, ToolError, ToolMode, ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE,
                                              MAX_TOOL_PAGE_ITEMS, GovernmentVehicleTool)

#: The pinned capture's own identity text, so a test names the same vehicle the
#: committed R5 fixtures state rather than one invented for the test.
TOYOTA = "טויוטה"
RAV4 = "RAV4"
PINNED_YEAR = 2022


# =============================================================================
# 0. the module is offline by construction
# =============================================================================

@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Creating a socket anywhere in this module is a test failure."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("an offline catalog test attempted a network connection")

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
    message = repository.create_user_message(conversation["id"], "go", {})
    run = repository.create_queued_run(conversation["id"], message["id"], "go", {},
                                       requested_by=user)
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def lease_kwargs(lease: WorkerLease) -> dict:
    return {"worker_id": lease.worker_id, "attempt": lease.attempt,
            "lease_token": lease.lease_token}


def ingest(repository: MemoryRepository, lease: WorkerLease, **kwargs):
    client = DataGovClient(kwargs.pop("transport", None) or FixtureTransport(),
                           page_limit=PINNED_PAGE_LIMIT, sleep_fn=lambda _seconds: None,
                           **kwargs)
    return GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
        src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))


@pytest.fixture
def repository() -> MemoryRepository:
    return MemoryRepository()


@pytest.fixture
def landed(repository):
    """One leased run with the committed Government capture already durable."""
    lease = leased_run(repository)
    report = ingest(repository, lease)
    return lease, report


def registry(repository, **kwargs) -> ToolRegistry:
    return ToolRegistry([GovernmentVehicleTool(repository, **kwargs)])


GRANTED = ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}))


def resolve(repository, *, code: str | None = None, year: int = PINNED_YEAR,
            context: ToolContext = GRANTED) -> dict:
    """One `resolve_variant` call through the real Registry."""
    payload = {"manufacturer": TOYOTA, "commercial_model": RAV4, "model_year": year}
    if code is not None:
        payload["official_model_code"] = code
    return registry(repository).execute(GOVERNMENT_TOOL_NAME, "resolve_variant",
                                        context, payload)


def one_code(repository, year: int = PINNED_YEAR) -> str:
    page = registry(repository).execute(GOVERNMENT_TOOL_NAME, "get_variants", GRANTED,
                                        {"manufacturer": TOYOTA, "commercial_model": RAV4,
                                         "model_year": year, "limit": 1})
    return page["variants"][0]["official_model_code"]


def acquire(repository, lease, result, *, call_id="call-1", operation="resolve_variant"):
    board = EvidenceBoard(repository, lease)
    acquisition = TrustedEvidenceAcquisition(board=board, mappers=production_evidence_mappers())
    return board, acquisition.acquire(ToolCallRecord(
        task_id="task-1", call_id=call_id, tool=GOVERNMENT_TOOL_NAME,
        operation=operation, result=result))


def mark_ready(repository, lease, candidate_id: str) -> dict:
    """Move ONE candidate to `ready_for_review` through the guarded write.

    The reviewed status transition a reconciliation round produces. Promotion
    refuses anything else, so this is a deliberate, recorded step rather than
    something a promotion could do to a candidate on its way past.
    """
    candidate = next(row for row in repository.catalog_candidates.values()
                     if row["id"] == candidate_id)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    record = next(row for row in repository.catalog_raw_records.values()
                  if row["id"] == candidate["raw_record_id"])
    repository.record_catalog_candidate(lease.run_id, {
        "snapshot_key": snapshot["snapshot_key"], "record_key": record["record_key"],
        "snapshot_id": candidate["snapshot_id"], "raw_record_id": candidate["raw_record_id"],
        "manufacturer": candidate["manufacturer"],
        "commercial_model": candidate["commercial_model"],
        "model_year_start": candidate["model_year_start"],
        "model_year_end": candidate["model_year_end"],
        "official_model_code": candidate["official_model_code"], "trim": candidate["trim"],
        "identity_dimensions": candidate["identity_dimensions"],
        "status": "ready_for_review"}, **lease_kwargs(lease))
    return next(row for row in repository.catalog_candidates.values()
                if row["id"] == candidate_id)


def verify_and_link(repository, lease, board, acquired, candidate, *,
                    verdict: str = "verified", reason: str = "R4_STRUCTURED_MATCH"):
    """A verified verdict per acquired claim, and the evidence link that cites it."""
    fragments = {item["locator_key"]: item for item in acquired.fragments}
    links, claims, verdicts = [], {}, {}
    for claim in acquired.claims:
        fragment = fragments[claim["evidence_locator"]]
        decision = board.record_verification_verdict(VerificationVerdict(
            claim_id=str(claim["id"]), verdict=verdict, reason=reason,
            mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
            support=[SupportLink(source_id=str(claim["source_id"]),
                                 content_hash=fragment["content_hash"],
                                 fragment_id=str(fragment["id"]),
                                 locator=fragment["locator_key"])]))
        links.append(repository.link_catalog_candidate_evidence(lease.run_id, {
            "candidate_key": candidate["candidate_key"], "candidate_id": candidate["id"],
            "source_id": claim["source_id"], "claim_id": claim["id"],
            "verdict_id": decision["id"]}, **lease_kwargs(lease)))
        claims[str(claim["id"])] = claim
        verdicts[str(decision["id"])] = decision
    return links, claims, verdicts


class RecordingRepository:
    """A repository that remembers WHICH methods something used.

    The wrapper is the whole point of the resume proofs: "no tool call and no
    model call was repeated" is checked by the set of repository methods the
    resumed worker actually reached, not by a comment saying it did not.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value):
            return value

        def recorded(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return value(*args, **kwargs)
        return recorded


#: Every repository method that WRITES evidence. A repeated Government tool
#: call or a repeated model call can only reach durable state through one of
#: these, so a resume that touches none of them repeated neither.
EVIDENCE_WRITE_METHODS = frozenset({
    "create_source", "create_claim", "record_evidence_fragment", "record_claim_verdict",
    "create_conflict", "record_conflict_resolution",
    "record_catalog_snapshot", "record_catalog_raw_record", "activate_catalog_snapshot"})


def run_government_task(repository, lease, *, year: int = PINNED_YEAR,
                        operation: str = "resolve_variant", call_id: str = "call-1"):
    """What the ENGINE's tool loop does: one resolution, into the trusted sink.

    Exactly the seam `backend/worker/main.py` wires. Nothing here links
    evidence, moves a status or promotes -- it stops where a crash would leave
    a run whose R3 evidence is durable and whose verdicts are not.
    """
    board = EvidenceBoard(repository, lease)
    sink = RegisteredOperationEvidenceSink(
        TrustedEvidenceAcquisition(board=board, mappers=production_evidence_mappers()))
    result = resolve(repository, code=one_code(repository, year), year=year)
    sink(ToolCallRecord(task_id="task-1", call_id=call_id, tool=GOVERNMENT_TOOL_NAME,
                        operation=operation, result=result))
    return board


def settle_verdicts(repository, lease, *, verdict: str = "verified"):
    """What the VERIFIER does: one settled verdict per unverified claim.

    Idempotent by omission, so a resumed worker that re-verifies writes the
    same rows the crashed one did rather than a second set.
    """
    board = EvidenceBoard(repository, lease)
    fragments = {row["locator_key"]: row for row in board_fragments(repository, lease)}
    settled = {str(row["claim_id"]) for row in repository.tool_rows
               if repository.evidence_kinds.get(str(row.get("id"))) == "claim_verdict"}
    for claim in claims_of(repository, lease):
        fragment = fragments.get(claim["evidence_locator"])
        if fragment is None or str(claim["id"]) in settled:
            continue
        board.record_verification_verdict(VerificationVerdict(
            claim_id=str(claim["id"]), verdict=verdict, reason="R4_STRUCTURED_MATCH",
            mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
            support=[SupportLink(source_id=str(claim["source_id"]),
                                 content_hash=fragment["content_hash"],
                                 fragment_id=str(fragment["id"]),
                                 locator=fragment["locator_key"])]))
    return board


def gather_evidence(repository, lease, *, year: int = PINNED_YEAR,
                    verdict: str = "verified", operation: str = "resolve_variant",
                    call_id: str = "call-1"):
    """Everything the ENGINE does, and nothing the promotion path does.

    Stops exactly where a crash would leave a run whose evidence, verdicts and
    checkpoint are all durable and whose canonical catalog is still empty --
    the window that used to lose the promotion entirely.
    """
    run_government_task(repository, lease, year=year, operation=operation, call_id=call_id)
    return settle_verdicts(repository, lease, verdict=verdict)


def production_path(repository, lease, *, year: int = PINNED_YEAR,
                    verdict: str = "verified", operation: str = "resolve_variant"):
    """The PRODUCTION orchestration, driven exactly as `backend/worker/main.py` does.

    No step is assembled by hand: the engine's half runs, and then
    `CatalogPromotionPipeline.promote` does the durable read, the linking, the
    reviewed status transition, the planning and the guarded promotion itself.

    That is the whole point of this helper: a test that performed those steps
    itself would prove the steps work and prove nothing about whether anything
    in production ever performs them.
    """
    gather_evidence(repository, lease, year=year, verdict=verdict, operation=operation)
    return CatalogPromotionPipeline(repository, lease).promote()


def restarted_lease(repository, lease, *, worker: str = "worker-restarted") -> WorkerLease:
    """The lease a REPLACEMENT worker holds after the first one died.

    The dead worker's lease is expired -- which is exactly what happens when a
    process stops: nothing renews it -- and the run is re-claimed. The attempt
    advances, as production's single-statement CAS advances it, so the
    replacement writes under its own lease and the dead worker can write
    nothing at all.
    """
    repository.runs[str(lease.run_id)]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    claimed = repository.claim_run(lease.run_id, worker)
    return WorkerLease(lease.run_id, worker, int(claimed["attempt"]),
                       claimed["lease_token"])


def canonical_counts(repository) -> tuple[int, int, int, int]:
    """Every durable row a promotion can create, counted."""
    return (len(repository.catalog_models), len(repository.catalog_model_variants),
            len(repository.catalog_canonical_field_provenance),
            len(repository.catalog_evidence_links))


#: Every WRONG-SCOPE refusal the promotion gate states, with the one thing each
#: case changes about an otherwise complete, verified chain. The SQL trigger in
#: `20260916120000_catalog_field_level_promotion.sql` raises exactly these
#: sentences; `tests/test_catalog_migration_static.py` pins the two copies
#: together, so this table is parity rather than a second opinion.
PROMOTION_SCOPE_REFUSALS = (
    ("wrong-vehicle", "claim is about another vehicle", {"entity": "cm1." + "a" * 32 + ":2024"}),
    ("wrong-year", "scoped to another model year", {"time_scope": {"model_year": 1999}}),
    ("no-year", "states no model year scope", {"time_scope": {"as_of": "2026-08"}}),
    ("no-market", "states no market scope", {"market": None}),
    ("wrong-identity", "scoped to another vehicle identity", {"identity": {"trim": "LIMITED"}}),
    ("wrong-locator", "read from another source record", {"record_id": "cs1." + "b" * 32 + ":1"}),
)


def forged_chain(repository, lease, board, candidate, *, label, field_key, value,
                 entity, time_scope, market="IL", identity=None, record_id=None,
                 register_field="shnat_yitzur"):
    """A COMPLETE, valid R3/R4 chain that states the wrong thing on purpose.

    Every rule below the scope gate is satisfied -- a real versioned source, a
    real focused fragment at the locator, a real claim citing it, a real
    `verified` verdict supported by that fragment, and a real evidence link for
    this candidate. So when the promotion is refused, the ONLY thing that can
    have refused it is the gate the case is named after.
    """
    from backend.schemas import ClaimCreate, SourceCreate

    record_id = record_id or government_record_id(
        next(row["snapshot_key"] for row in repository.catalog_snapshots.values()
             if row["id"] == candidate["snapshot_id"]),
        next(row["upstream_record_id"] for row in repository.catalog_raw_records.values()
             if row["id"] == candidate["raw_record_id"]))
    locator = record_field_locator(record_id, (register_field,))
    projection = structured_projection(record={register_field: value}, fields=(register_field,),
                                       locator=locator, fragment_index=0)
    source = board.record_source(
        SourceCreate(agent="catalog.government",
                     url=f"https://{src.DATA_GOV_HOST}/dataset/x/resource/{label}",
                     title=label, domain=src.DATA_GOV_HOST, source_type="government_register",
                     source_strength="strong", query=record_id,
                     tool_operation="catalog.government_vehicle.resolve_variant"),
        task_key="task-1",
        version=SourceVersion(kind="dataset_version", identifier="2026.09.1"))
    fragment = board.record_focused_fragment(source["id"], projection, task_key="task-1")
    claim = board.record_claim(
        ClaimCreate(entity_key=entity, field_key=field_key, value=value,
                    unit="year" if field_key.startswith("model_year") else None,
                    time_scope=dict(time_scope), geography=market, market=market,
                    source_id=uuid.UUID(str(source["id"])), source_strength="strong",
                    confidence=0.95, agent="catalog.government"),
        task_key="task-1", evidence_locator=locator.locator_key, identity=identity)
    verdict = board.record_verification_verdict(VerificationVerdict(
        claim_id=str(claim["id"]), verdict="verified", reason="R4_STRUCTURED_MATCH",
        mode="deterministic_structured", contract_version=VERIFIER_CONTRACT_VERSION,
        support=[SupportLink(source_id=str(source["id"]),
                             content_hash=fragment["content_hash"],
                             fragment_id=str(fragment["id"]),
                             locator=projection.locator.locator_key)]))
    return repository.link_catalog_candidate_evidence(lease.run_id, {
        "candidate_key": candidate["candidate_key"], "candidate_id": candidate["id"],
        "source_id": source["id"], "claim_id": claim["id"],
        "verdict_id": verdict["id"]}, **lease_kwargs(lease))


def claims_of(repository, lease):
    """The durable claim rows this run wrote, in write order."""
    return [row for row in repository.tool_rows
            if repository.evidence_kinds.get(str(row.get("id"))) == "claim"
            and str(row.get("run_id")) == str(lease.run_id)]


def board_fragments(repository, lease):
    """The durable focused-fragment rows this run wrote."""
    return [row for row in repository.tool_rows
            if repository.evidence_kinds.get(str(row.get("id"))) == "evidence_fragment"
            and str(row.get("run_id")) == str(lease.run_id)]


def promoted(repository, lease, *, year: int = PINNED_YEAR):
    """The WHOLE path, once: tool -> evidence -> verdict -> link -> promotion."""
    code = one_code(repository, year)
    result = resolve(repository, code=code, year=year)
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    links, claims, verdicts = verify_and_link(repository, lease, board, acquired, candidate)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    evidence = field_evidence_for(candidate=candidate, links=links, claims=claims,
                                  verdicts=verdicts)
    plan = build_promotion_plan(candidate=candidate, snapshot=snapshot, evidence=evidence)
    promotion = CanonicalPromotion(repository, lease)
    return promotion, plan, promotion.promote(plan), acquired, candidate, links, claims, verdicts


# =============================================================================
# 1. the plan firewall sees the tool, and the source policy travels with it
# =============================================================================

def plan_json(operation: str, arguments: dict, *, tool: str = GOVERNMENT_TOOL_NAME) -> dict:
    """ONE plan a Commander could legitimately produce, in the real contract."""
    return {
        "version": "1",
        "objective": "Which RAV4 model years does the Israeli register state?",
        "max_replans": 0, "estimated_cost_units": 10,
        "graph": {"tasks": [{
            "task_id": "t1", "goal": "read the Israeli register",
            "scope": "Israeli vehicle register, one model year",
            "dependencies": [], "priority": 50, "recursion_depth": 0,
            "estimated_cost_units": 10,
            "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}},
                              "required": ["answer"], "additionalProperties": False},
            "tools": [{"call_id": "c1", "name": tool, "operation": operation,
                       "arguments": arguments, "dependency_bindings": []}],
            "evidence": {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5},
            "completion": {"required_outputs": ["answer"], "evidence_satisfied": True,
                           "allow_partial": False}}]},
        "assignments": [{"task_id": "t1", "worker_role": "generic researcher",
                         "context_task_ids": []}]}


def validator(repository) -> PlanValidator:
    return PlanValidator(allowed_tools=registry(repository).descriptors(),
                         limits=PlanLimits(max_tasks=4, max_tool_calls=4, max_replans=0))


def test_a_plan_may_name_the_government_tool_and_only_its_registered_operations(repository):
    """The firewall's tool authority is the REGISTERED descriptors, nothing else."""
    approved = validator(repository).validate(plan_json(
        "resolve_variant", {"manufacturer": TOYOTA, "commercial_model": RAV4,
                            "model_year": PINNED_YEAR}))
    assert approved.graph.tasks[0].tools[0].name == GOVERNMENT_TOOL_NAME
    with pytest.raises(PlanValidationError) as failure:
        validator(repository).validate(plan_json("dump_everything", {}))
    assert failure.value.reason == "TOOL_OPERATION_UNKNOWN"
    with pytest.raises(PlanValidationError) as failure:
        validator(repository).validate(plan_json(
            "resolve_variant", {"manufacturer": TOYOTA}))
    assert failure.value.reason == "TOOL_ARGUMENTS_INVALID"
    # A tool nobody registered is not addressable, whatever it is called.
    with pytest.raises(PlanValidationError) as failure:
        validator(repository).validate(plan_json("search", {"query": "rav4"},
                                                 tool="yeda.catalog"))
    assert failure.value.reason == "TOOL_NOT_ALLOWLISTED"


def test_the_source_first_policy_appears_exactly_when_the_tool_is_registered(repository):
    """Server-owned policy text, keyed by the REGISTERED tool name.

    A rule naming a capability the Commander cannot call would be an
    instruction to do something impossible, so the policy is derived from the
    same allowlist the firewall enforces.
    """
    with_tool = provider_plan_policy(PlanLimits(), [GOVERNMENT_TOOL_NAME])
    assert with_tool["source_policy"] == list(SOURCE_FIRST_TOOL_POLICY[GOVERNMENT_TOOL_NAME])
    assert provider_plan_policy(PlanLimits(), [])["source_policy"] == []
    assert provider_plan_policy(PlanLimits(), ["mock.search"])["source_policy"] == []
    # It says which source answers which question. It does NOT hard-code a
    # workflow: no fixed sequence, no fixed categories, no task decomposition.
    text = " ".join(with_tool["source_policy"]).lower()
    assert "before planning broad web research" in text
    assert "not authority for reliability" in text
    for forbidden in ("step 1", "then call", "workflow", "always create a task"):
        assert forbidden not in text


# =============================================================================
# 2. the end-to-end path
# =============================================================================

def test_the_production_path_promotes_without_anyone_assembling_the_steps(repository, landed):
    """Catalog PR3's actual deliverable: the connection, exercised end to end.

    Nothing here links evidence, moves a status, builds a plan or calls the
    promotion RPC. A Government resolution goes into the trusted tool-result
    sink and verdicts go into the trusted verdict sink -- the two seams
    `backend/worker/main.py` wires -- and `CatalogPromotionPipeline.promote`
    does the rest. If any arrow of the production path were missing, the
    canonical catalog would still be empty at the end of this test.
    """
    lease, _report = landed
    assert not repository.catalog_model_variants

    gather_evidence(repository, lease)
    # The DURABLE read found ONE candidate, derived from rows the server wrote
    # -- not from anything a process was holding.
    pipeline = CatalogPromotionPipeline(repository, lease)
    pending = pipeline.pending()
    assert len(pending) == 1
    observation = pending[0]
    assert observation.candidate["manufacturer"] == TOYOTA
    assert observation.claims and all(row["source_id"] for row in observation.claims)

    attempts = pipeline.promote()
    assert len(attempts) == 1 and attempts[0].promoted
    outcome = attempts[0].outcome

    # 1. The REVIEWED status transition happened, durably, and is what the
    #    promotion required -- it was not a status the test wrote.
    candidate = next(row for row in repository.catalog_candidates.values()
                     if row["id"] == observation.candidate_id)
    assert candidate["status"] == "ready_for_review"
    # The attempt names the DURABLE candidate key, which the tool result never
    # carried: the pipeline read the row back rather than trusting the payload.
    assert attempts[0].candidate_key == candidate["candidate_key"]

    # 2. The evidence LINKS exist, one per verified claim, each citing the
    #    claim's own source and a verified verdict.
    links = [row for row in repository.catalog_evidence_links.values()
             if row["candidate_id"] == observation.candidate_id]
    assert len(links) == len(observation.claims)
    assert all(row["verdict_id"] for row in links)

    # 3. The canonical row exists, and every fact on it is traceable.
    current = CanonicalPromotion(repository, lease).current_canonical(
        outcome.variant["canonical_key"])
    assert current["manufacturer"] == TOYOTA and current["commercial_model"] == RAV4
    assert current["model_year_start"] == PINNED_YEAR
    assert current["official_model_code"] == candidate["official_model_code"]
    assert current["trim"] == candidate["trim"]
    provenance = repository.list_canonical_field_provenance(outcome.variant["id"])
    assert {row["field_key"] for row in provenance} == set(outcome.promoted_fields)
    assert all(row["run_id"] == str(lease.run_id) for row in provenance)

    # 4. The event a run would carry is bounded, static and browser-safe.
    event = attempts[0].as_event()
    assert event["promoted"] is True and event["canonical_key"] == current["canonical_key"]
    assert set(event) == {"candidate_key", "promoted", "canonical_key",
                          "promoted_fields", "unsupported_fields", "replayed"}


def test_the_production_path_promotes_nothing_without_a_verified_verdict(repository, landed):
    """A `needs_review` verdict is a real answer, and it is an answer against.

    Same orchestration, one thing changed: the Verifier did not verify. The
    pipeline links nothing, transitions no status, and the canonical catalog
    stays empty -- and it says WHY, with a static code.
    """
    lease, _report = landed
    attempts = production_path(repository, lease, verdict="needs_review")
    # The durable read joins on a VERIFIED verdict, so an unverified run owes
    # nothing at all -- there is no candidate to refuse, and nothing was moved.
    assert attempts == ()
    assert not repository.catalog_model_variants and not repository.catalog_models
    # The candidate was NOT moved to `ready_for_review` on the way past.
    assert all(row["status"] != "ready_for_review"
               for row in repository.catalog_candidates.values())
    assert not repository.catalog_evidence_links


def test_the_production_path_replays_onto_the_same_canonical_row(repository, landed):
    """Running the whole connection twice writes one canonical row, not two."""
    lease, _report = landed
    first = production_path(repository, lease)
    counts = canonical_counts(repository)
    second = CatalogPromotionPipeline(repository, lease).promote()
    assert first[0].outcome.variant["id"] == second[0].outcome.variant["id"]
    assert canonical_counts(repository) == counts
    assert second[0].outcome.replayed


def test_the_production_path_is_bounded_and_promotion_is_not_a_tool(repository, landed):
    """Two properties that must hold however many vehicles a run resolves."""
    lease, _report = landed
    for year in (PINNED_YEAR, PINNED_YEAR + 1):
        gather_evidence(repository, lease, year=year, call_id=f"call-{year}")
    # The durable read is BOUNDED, so the end-of-run promotion can never become
    # an unbounded write loop however many vehicles a run resolved -- and the
    # bound is applied in `candidate_key` order, so the same set comes back
    # every time it is asked.
    unbounded = CatalogPromotionPipeline(repository, lease).pending()
    bounded = CatalogPromotionPipeline(repository, lease, limit=1).pending()
    assert len(bounded) == 1
    assert bounded[0].candidate_key == min(item.candidate_key for item in unbounded)
    assert CatalogPromotionPipeline(repository, lease, limit=1).pending()[0].candidate_key \
        == bounded[0].candidate_key
    assert MAX_PROMOTIONS_PER_RUN == 25
    # The durable read is scoped to the ONE operation the registered mapper
    # actually records on every source it writes -- pinned against a real
    # source row rather than against the constant beside it.
    sources = [row for row in repository.tool_rows
               if repository.evidence_kinds.get(str(row["id"])) == "source"]
    assert sources and {row["tool_operation"] for row in sources} == {PROMOTABLE_TOOL_OPERATION}
    # Promotion is not a registered capability: no operation of the one
    # registered tool writes anything, so a plan cannot ask for one.
    operations = {operation.name for operation
                  in registry(repository).descriptors()[0].operations}
    assert not any("promote" in name or "write" in name for name in operations)


def test_the_whole_path_ends_in_a_canonical_row_backed_by_one_fact_per_field(repository, landed):
    """Catalog PR3's deliverable, proven in one test.

    Everything the canonical row states is traceable on its own: the field, the
    value, the revision, the candidate, the evidence link, the snapshot, the
    source, the claim, the VERIFIED verdict, the run and worker lease, the
    source version and the exact locator.
    """
    lease, _report = landed
    promotion, plan, outcome, acquired, candidate, links, _claims, _verdicts = promoted(
        repository, lease)

    assert outcome.promoted_fields == tuple(sorted(item.field_key for item in plan.fields))
    assert set(outcome.promoted_fields) <= set(CANONICAL_VARIANT_FIELDS)
    current = promotion.current_canonical(outcome.variant["canonical_key"])
    assert current["manufacturer"] == TOYOTA and current["commercial_model"] == RAV4
    assert current["model_year_start"] == PINNED_YEAR
    assert current["official_model_code"] == candidate["official_model_code"]
    assert current["trim"] == candidate["trim"]
    assert current["identity_dimensions"] == {"fuel_type": "petrol"}

    provenance = promotion.field_provenance(outcome.variant["id"])
    assert len(provenance) == len(outcome.promoted_fields)
    by_field = {row["field_key"]: row for row in provenance}
    for item in plan.fields:
        row = by_field[item.field_key]
        assert row["field_value"] == item.value
        assert row["revision"] == 1
        assert row["candidate_id"] == candidate["id"]
        assert row["evidence_link_id"] == item.evidence_link_id
        assert row["claim_id"] == item.claim_id and row["verdict_id"] == item.verdict_id
        assert row["run_id"] == str(lease.run_id) and row["worker_id"] == lease.worker_id
        # The locator and the version are DERIVED from the cited evidence.
        link = next(entry for entry in links if entry["id"] == item.evidence_link_id)
        assert row["record_locator"] == link["record_locator"]
        assert (row["source_version"], row["source_version_kind"]) == \
            (link["source_version"], link["source_version_kind"])
    # And the dimensions the register states but this PR cannot promote are
    # REPORTED, never silently dropped.
    assert set(plan.unsupported_fields) == {
        "identity_dimensions.body_style", "identity_dimensions.drivetrain",
        "identity_dimensions.propulsion_technology"}


def test_the_worker_runs_the_planned_call_and_the_sink_records_the_evidence(repository, landed):
    """The trusted seam, driven by the REAL worker with no model call at all."""
    lease, _report = landed
    board = EvidenceBoard(repository, lease)
    sink = RegisteredOperationEvidenceSink(
        TrustedEvidenceAcquisition(board=board, mappers=production_evidence_mappers()))
    approved = validator(repository).validate(plan_json(
        "resolve_variant", {"manufacturer": TOYOTA, "commercial_model": RAV4,
                            "model_year": PINNED_YEAR,
                            "official_model_code": one_code(repository)}))

    class PoisonGateway:
        def call(self, **_kwargs):
            raise AssertionError("this proof pays for no model call")

    worker = GenericWorker(gateway=PoisonGateway(), tools=registry(repository), model="unused",
                           tool_context=GRANTED, tool_result_sink=sink,
                           task_output_strategy=lambda **_: {"answer": "read"})
    result = worker.execute(approved.graph.tasks[0], {})
    assert result.status == "completed"
    claims = [row for row in repository.tool_rows
              if repository.evidence_kinds.get(str(row["id"])) == "claim"]
    assert {claim["field_key"] for claim in claims} == {
        field_key for field_key, *_rest in GOVERNMENT_FIELD_SOURCES}
    # Every claim is scoped to the VEHICLE, so a Web claim about the same car
    # would meet it in one conflict scope rather than passing beside it.
    assert {claim["entity_key"] for claim in claims} == {
        government_entity_key(TOYOTA, RAV4, PINNED_YEAR)}


def test_an_unmapped_operation_records_nothing_and_never_becomes_text_evidence(repository, landed):
    """The routing, both halves: nothing recorded, and no generic fallback."""
    lease, _report = landed
    board = EvidenceBoard(repository, lease)
    mappers = production_evidence_mappers()
    sink = RegisteredOperationEvidenceSink(
        TrustedEvidenceAcquisition(board=board, mappers=mappers))
    meta = registry(repository).execute(GOVERNMENT_TOOL_NAME, "dataset_meta", GRANTED, {})
    before = len(repository.tool_rows)
    sink(ToolCallRecord(task_id="t", call_id="c", tool=GOVERNMENT_TOOL_NAME,
                        operation="dataset_meta", result=meta))
    assert len(repository.tool_rows) == before
    # The strict path still REFUSES it, so a caller that requires evidence is
    # never handed silence instead.
    with pytest.raises(EvidenceMappingError) as failure:
        mappers.map(ToolCallRecord(task_id="t", call_id="c", tool=GOVERNMENT_TOOL_NAME,
                                   operation="dataset_meta", result=meta))
    assert failure.value.reason_code == "EVIDENCE_MAPPER_NOT_REGISTERED"
    assert mappers.registered == PRODUCTION_EVIDENCE_MAPPER_OPERATIONS
    # And nothing that is not a validated tool-call record can enter at all.
    with pytest.raises(EvidenceMappingError) as failure:
        sink({"tool": GOVERNMENT_TOOL_NAME, "operation": "resolve_variant", "result": {}})
    assert failure.value.reason_code == "EVIDENCE_SOURCE_NOT_TRUSTED"


# =============================================================================
# 3. the negative proofs -- every gate, one case each
# =============================================================================

def test_without_the_government_scope_no_operation_runs_at_all(repository, landed):
    """Scope is server-owned. A plan may REQUEST; only wiring may grant."""
    for operation, payload in (("dataset_meta", {}),
                               ("list_manufacturers", {}),
                               ("resolve_variant", {"manufacturer": TOYOTA,
                                                    "commercial_model": RAV4,
                                                    "model_year": PINNED_YEAR})):
        with pytest.raises(ToolError) as failure:
            registry(repository).execute(GOVERNMENT_TOOL_NAME, operation,
                                         ToolContext(scopes=frozenset()), payload)
        assert failure.value.code == "TOOL_SCOPE_REQUIRED"
    # A scope that merely LOOKS like it is not it either.
    with pytest.raises(ToolError) as failure:
        registry(repository).execute(
            GOVERNMENT_TOOL_NAME, "dataset_meta",
            ToolContext(scopes=frozenset({"catalog:government:write", "catalog:read"})), {})
    assert failure.value.code == "TOOL_SCOPE_REQUIRED"
    # And nothing was read: a refused call never reached the repository.
    assert not any(row.get("kind") == "catalog" for row in repository.tool_rows)


def test_the_government_tool_is_read_only_and_has_no_write_operation(repository):
    """No promotion capability exists to authorize, so none can be granted.

    Promotion is a lease-guarded repository RPC that trusted server code calls.
    It is deliberately NOT a Tool, which is why `write_approved` and a
    `tool:write:<name>` capability never enter this path at all.
    """
    tool = GovernmentVehicleTool(repository)
    assert tool.mode is ToolMode.READ
    assert not tool.required_scope.endswith(":write")
    for name in tool.operations:
        assert not any(word in name for word in ("write", "promote", "publish", "update",
                                                 "delete", "insert", "sync", "refresh"))
    # Registering it grants nothing: the descriptor catalog carries names,
    # modes, scopes and schemas, and no authorization of any kind.
    payload = registry(repository).descriptor_payload()
    assert [item["mode"] for item in payload] == ["read"]
    serialized = json.dumps(payload)
    for leaked in ("write_approved", "capabilities", "lease_token", "service_role",
                   "api_key", "SUPABASE"):
        assert leaked not in serialized


def test_no_operation_can_return_a_complete_raw_resource(repository, landed):
    """Bounded in every direction, and only ONE row is ever quoted.

    A caller asking for more than the server bound gets the bound, an
    unfiltered listing is still one page of one snapshot, and the only
    operation that returns register FIELDS returns the reviewed identity
    projection of exactly one row -- and only when the filters resolved to one.
    """
    page = registry(repository).execute(GOVERNMENT_TOOL_NAME, "get_variants", GRANTED,
                                        {"manufacturer": TOYOTA, "commercial_model": RAV4,
                                         "limit": 10_000})
    assert page["limit"] == MAX_TOOL_PAGE_ITEMS
    assert len(page["variants"]) <= MAX_TOOL_PAGE_ITEMS
    assert page["total"] > len(page["variants"]) or not page["has_more"]
    # No returned variant carries a payload, a raw record or a whole row.
    for variant in page["variants"]:
        assert set(variant) <= {"candidate_id", "status", "manufacturer", "commercial_model",
                                "model_year_start", "model_year_end", "official_model_code",
                                "trim", "identity_dimensions", "upstream_record_id",
                                "resource_id", "payload_sha256"}
    # An AMBIGUOUS resolution quotes nothing at all.
    ambiguous = resolve(repository)
    assert ambiguous["ambiguous"] and "source_record" not in ambiguous
    # A unique resolution quotes exactly the reviewed identity fields of ONE
    # row -- never the register row itself.
    unique = resolve(repository, code=one_code(repository))
    record = unique["source_record"]
    assert set(record) <= {"upstream_record_id", "tozar", "kinuy_mishari", "shnat_yitzur",
                           "degem_nm", "ramat_gimur", "delek_cd", "delek_nm"}
    assert "koah_sus" not in record and "mishkal_kolel" not in record


def test_an_unusable_snapshot_answers_nothing_and_promotes_nothing(repository):
    """A capture with a stated, counted reading gap is not a smaller truth."""
    lease = leased_run(repository)
    # A capture whose rows the reviewed vocabulary cannot read: one row's code
    # and label contradict each other, which PR2 counts as a durable issue.
    from backend.testing.government_capture import encode, page_document

    document = page_document(0)
    document["result"]["records"][0]["delek_nm"] = "לא תואם"
    transport = FixtureTransport(bodies={0: encode(document)})
    report = ingest(repository, lease, transport=transport)
    assert report.normalization_issue_count == 1
    with pytest.raises(ToolError) as failure:
        registry(repository).execute(GOVERNMENT_TOOL_NAME, "dataset_meta", GRANTED, {})
    assert failure.value.code == "GOV_PROJECTION_SNAPSHOT_INCOMPLETE"
    # The acknowledgement is TRUSTED WIRING, not a tool argument: a model
    # cannot acknowledge its own gap.
    assert "allow_incomplete" not in json.dumps(registry(repository).descriptor_payload())
    acknowledged = registry(repository, allow_incomplete=True).execute(
        GOVERNMENT_TOOL_NAME, "dataset_meta", GRANTED, {})
    # And the gap travels on the answer, so a later reader cannot mistake it
    # for a complete one.
    assert acknowledged["provenance"]["normalization_issue_count"] == 1


def test_an_ambiguous_variant_is_returned_whole_and_never_resolved(repository, landed):
    """Ambiguity is an ANSWER. Nothing picks a first row, at any layer."""
    lease, _report = landed
    result = resolve(repository)
    assert result["ambiguous"] and not result["resolved"]
    assert result["match_count"] > 1 and len(result["variants"]) > 1
    # The mapper DECLINES it: there is no single row whose fields it could
    # quote, so it invents none and fails no task.
    assert GovernmentVariantEvidenceMapper().map(ToolCallRecord(
        task_id="t", call_id="c", tool=GOVERNMENT_TOOL_NAME,
        operation="resolve_variant", result=result)) is NO_EVIDENCE
    board = EvidenceBoard(repository, lease)
    acquisition = TrustedEvidenceAcquisition(board=board,
                                             mappers=production_evidence_mappers())
    before = len(repository.tool_rows)
    assert acquisition.acquire(ToolCallRecord(
        task_id="t", call_id="c", tool=GOVERNMENT_TOOL_NAME, operation="resolve_variant",
        result=result)) is None
    assert len(repository.tool_rows) == before
    # An AMBIGUOUS candidate is not promotable either, even with evidence of
    # its own: the status gate refuses it before any field is looked at.
    candidate = next(row for row in repository.catalog_candidates.values()
                     if row["status"] != "ready_for_review")
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    with pytest.raises(CatalogPromotionError) as failure:
        build_promotion_plan(candidate=candidate, snapshot=snapshot, evidence=())
    assert failure.value.reason_code == "CATALOG_PROMOTION_CANDIDATE_NOT_READY"


def test_a_field_the_register_does_not_define_is_never_evidence_or_canonical(repository, landed):
    """Government authority is field-specific, by construction.

    There is no branch in the mapper that could emit reliability, price or a
    `koah_sus`-to-horsepower conversion, and the promotable vocabulary does not
    contain one either -- so this is not a policy that could be relaxed by a
    caller, it is a shape that does not exist.
    """
    lease, _report = landed
    _board, acquired = acquire(repository, lease, resolve(repository, code=one_code(repository)))
    emitted = {claim["field_key"] for claim in acquired.claims}
    assert emitted == {field_key for field_key, *_rest in GOVERNMENT_FIELD_SOURCES}
    for never in ("koah_sus", "horsepower", "reliability", "price", "market_value",
                  "dg_metach_solela", "mishkal_kolel", "automatic_ind", "sug_degem"):
        assert never not in emitted
        assert never not in CANONICAL_VARIANT_FIELDS
    # And the source states its own strength rather than a blanket authority.
    source = acquired.source
    assert source["source_type"] == "government_register"
    assert source["domain"] == src.DATA_GOV_HOST
    mapper_source = Path("backend/catalog/government/evidence.py").read_text(encoding="utf-8")
    assert "koah_sus" in mapper_source  # named ONLY to say it is never read
    assert "GOVERNMENT_FIELD_SOURCES" in mapper_source


def test_an_unverified_or_rejected_verdict_can_never_back_a_canonical_fact(repository, landed):
    """`verified`, exactly. Not `needs_review`, and not `rejected`."""
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    for verdict in ("rejected", "needs_review"):
        with pytest.raises(AppError) as failure:
            verify_and_link(repository, lease, board, acquired, candidate,
                            verdict=verdict, reason="R4_VALUE_MISMATCH")
        assert "verdict is not verified" in failure.value.message
    # A link with NO verdict is legitimate discovery evidence -- and is still
    # not promotable, because a promoted fact needs a verified one.
    claim = acquired.claims[0]
    link = repository.link_catalog_candidate_evidence(lease.run_id, {
        "candidate_key": candidate["candidate_key"], "candidate_id": candidate["id"],
        "source_id": claim["source_id"], "claim_id": claim["id"]}, **lease_kwargs(lease))
    with pytest.raises(CatalogPromotionError) as promotion_failure:
        field_evidence_for(candidate=candidate, links=[link],
                           claims={str(claim["id"]): claim}, verdicts={})
    assert promotion_failure.value.reason_code == "CATALOG_PROMOTION_LINK_UNVERIFIED"


def test_the_legacy_reference_can_never_verify_or_promote_anything(repository):
    """The product decision, at three independent layers.

    The trust state is pinned to the family by a CHECK constraint, the link
    path refuses a verdict on such a snapshot, and the promotion assembly
    refuses the source family outright.
    """
    from backend.catalog.contracts import is_evidence_family, trust_state_for

    assert trust_state_for("legacy_reference") == "unverified"
    assert not is_evidence_family("legacy_reference")
    lease = leased_run(repository)
    snapshot = repository.record_catalog_snapshot(lease.run_id, {
        "source_family": "legacy_reference", "resource_id": "reliabilityAIModelsR2",
        "upstream_version": "2026.09.1", "upstream_version_kind": "dataset_version",
        "content_sha256": "0" * 64, "retrieved_at": "2026-09-14T16:11:13.272Z",
        "declared_record_count": 0}, **lease_kwargs(lease))
    assert snapshot["trust_state"] == "unverified"
    with pytest.raises(CatalogPromotionError) as failure:
        build_promotion_plan(
            candidate={"id": "x", "candidate_key": "cc1." + "0" * 32, "status": "ready_for_review",
                       "manufacturer": "Toyota", "commercial_model": "RAV4",
                       "model_year_start": 2021, "model_year_end": 2021,
                       "official_model_code": None, "trim": None, "identity_dimensions": {}},
            snapshot=snapshot, evidence=())
    assert failure.value.reason_code == "CATALOG_PROMOTION_SOURCE_NOT_EVIDENCE"
    # And nothing in this repository imports, reads or parses the aggregated
    # JSON: the legacy side is bounded catalog material or nothing.
    for module in sorted(Path("backend").rglob("*.py")):
        text = module.read_text(encoding="utf-8")
        assert "model_technical_catalog_il" not in text, module


def test_a_link_may_not_state_a_locator_or_version_the_evidence_does_not(repository, landed):
    """Provenance is DERIVED from the cited claim and source, never supplied."""
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    claim = acquired.claims[0]
    base = {"candidate_key": candidate["candidate_key"], "candidate_id": candidate["id"],
            "source_id": claim["source_id"], "claim_id": claim["id"]}
    for field, value, message in (
            ("record_locator", '["record_field","other",["trim"],null,null,null]',
             "record locator does not match"),
            ("source_version", "2099.12.31", "source version does not match"),
            ("source_version_kind", "git_commit", "source version does not match")):
        with pytest.raises(AppError) as failure:
            repository.link_catalog_candidate_evidence(
                lease.run_id, {**base, field: value}, **lease_kwargs(lease))
        assert message in failure.value.message
    # Stating the RIGHT ones is accepted, and is an assertion that was checked.
    source = next(row for row in repository.tool_rows if str(row["id"]) == str(claim["source_id"]))
    link = repository.link_catalog_candidate_evidence(lease.run_id, {
        **base, "record_locator": claim["evidence_locator"],
        "source_version": source["source_version_id"],
        "source_version_kind": source["source_version_kind"]}, **lease_kwargs(lease))
    assert link["record_locator"] == claim["evidence_locator"]


def test_two_verified_sources_that_disagree_are_never_promoted(repository, landed):
    """An unresolved conflict is a wait, not a value to pick."""
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    links, claims, verdicts = verify_and_link(repository, lease, board, acquired, candidate)
    # A SECOND verified link for the same field, at a different value.
    trim_claim = next(claim for claim in acquired.claims if claim["field_key"] == "trim")
    rival = dict(trim_claim, id=str(uuid4()), value="LIMITED")
    rival_link = dict(links[0], id=str(uuid4()), claim_id=rival["id"])
    with pytest.raises(CatalogPromotionError) as failure:
        field_evidence_for(candidate=candidate,
                           links=[*links, rival_link],
                           claims={**claims, str(rival["id"]): rival}, verdicts=verdicts)
    assert failure.value.reason_code == "CATALOG_PROMOTION_CONFLICT_UNRESOLVED"


def test_a_stale_worker_promotes_nothing(repository, landed):
    """The lease is the first thing every durable catalog write checks."""
    lease, _report = landed
    promotion, plan, _outcome, *_rest = promoted(repository, lease)
    stale = WorkerLease(lease.run_id, "ghost", lease.attempt, "not-the-token")
    with pytest.raises(AppError) as failure:
        CanonicalPromotion(repository, stale).promote(plan)
    # A lost lease ESCAPES as infrastructure rather than becoming a promotion
    # refusal: the worker's own lease handling has to see it.
    assert failure.value.code in LEASE_FAILURE_CODES
    assert not isinstance(failure.value, CatalogPromotionError)


def test_cancellation_stops_the_tool_between_reads(repository, landed):
    """A cancelled run stops between pages, not after the last one."""
    lease, _report = landed
    calls: list[int] = []

    def cancelled() -> bool:
        calls.append(1)
        return len(calls) > 2

    with pytest.raises(CancellationRequested):
        registry(repository).execute(
            GOVERNMENT_TOOL_NAME, "get_variants",
            ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}),
                        cancellation_checker=cancelled),
            {"manufacturer": TOYOTA, "commercial_model": RAV4})
    # Cancellation is not a tool failure: it must reach the worker's own
    # terminal handling rather than being reported as a task error.
    assert calls


def test_an_exact_replay_writes_nothing_and_a_conflicting_one_fails_closed(repository, landed):
    """Idempotency, both directions, at the promotion boundary."""
    lease, _report = landed
    promotion, plan, outcome, _acquired, candidate, _links, _claims, _verdicts = promoted(
        repository, lease)
    before = len(promotion.field_provenance(outcome.variant["id"]))
    assert promotion.promote(plan).variant["id"] == outcome.variant["id"]
    assert len(promotion.field_provenance(outcome.variant["id"])) == before
    # A CONFLICTING replay cannot even be built through the repository: the
    # promotion key is DERIVED from the candidate, the variant and the exact
    # field values, so a payload that changed any of them derives a different
    # key and a payload that keeps the old key disagrees with its own content.
    from dataclasses import replace

    conflicting = replace(plan, fields=plan.fields[:-1], trim=None)
    payload = conflicting.as_payload()
    payload["promotion_key"] = next(
        row["promotion_key"] for row in promotion.field_provenance(outcome.variant["id"]))
    with pytest.raises(CatalogKeyError) as failure:
        repository.promote_catalog_variant(lease.run_id, payload, **lease_kwargs(lease))
    assert "does not match its trusted derivation" in str(failure.value)
    # A DIRECT RPC caller can still present a hand-picked key, which is why the
    # database checks the stored field set against the requested one too. That
    # refusal is proven against real PostgreSQL in
    # `tests/test_migrations_postgres.py::test_the_promotion_refusal_matrix`.
    assert len(promotion.field_provenance(outcome.variant["id"])) == before


# =============================================================================
# 4. reconciliation -- structured work items, never a merge
# =============================================================================

def variant(make="Toyota", model="RAV4", years=(2022, 2022), code=None, trim=None,
            dimensions=None, identifier=None) -> ReconcilableVariant:
    return ReconcilableVariant(
        candidate_id=identifier or str(uuid4()), candidate_key="cc1." + "0" * 32,
        manufacturer=make, commercial_model=model,
        model_year_start=years[0], model_year_end=years[1], official_model_code=code,
        trim=trim, identity_dimensions=dimensions or {})


def test_reconciliation_matches_by_exact_identity_and_never_by_similarity():
    """Four tiers, in confidence order, and no fifth."""
    government = [variant(code="AXAA54L-ANZVB", trim="ADVENTURE",
                          dimensions={"fuel_type": "petrol"}, identifier="g1")]
    # Tier 1: the official model code both sides state.
    report = reconcile_catalog(government, [variant(code="AXAA54L-ANZVB", identifier="l1")])
    assert [(item.state, item.method) for item in report.matches] == \
        [("under_enriched", "official_model_code")]
    assert report.matches[0].government_candidate_ids == ("g1",)
    assert report.matches[0].legacy_candidate_ids == ("l1",)
    assert set(report.matches[0].detail) == {"trim", "fuel_type"}
    # Tier 2: exact normalized identity plus an overlapping year, reached only
    # when the code tier finds nothing. Case folding is the only normalization
    # applied, and it is applied to both sides.
    report = reconcile_catalog(government, [variant(make="toyota", code=None,
                                                    trim="ADVENTURE",
                                                    dimensions={"fuel_type": "petrol"})])
    assert [(item.state, item.method) for item in report.matches] == \
        [("under_enriched", "normalized_identity_and_year")]
    # The register states a code the legacy row does not: a gap to fill, named.
    assert report.matches[0].detail == ("official_model_code",)
    # NOT a match: a similar name is not the same name. Both sides are then
    # reported -- the legacy row as `legacy_only` and the register row as
    # `government_only` -- which is exactly right: two unrelated identities.
    for near_miss in ("RAV4 HEV", "NEW RAV4", "RAV-4", "RAV41"):
        report = reconcile_catalog(government, [variant(model=near_miss)])
        assert sorted(item.state for item in report.gaps) == ["government_only", "legacy_only"]
        assert not report.matches
    # Nor is a non-overlapping year: the MODEL is known to both sides, so the
    # register's own year comes back as a targeted `missing_years` gap.
    report = reconcile_catalog(government, [variant(years=(2019, 2019))])
    assert sorted(item.state for item in report.gaps) == ["government_only", "legacy_only"]


def test_a_reviewed_alias_rule_is_the_only_way_two_names_become_one_vehicle():
    """Tier 3 is explicit, reviewed and two-sided -- and ships empty."""
    from backend.catalog.government.reconcile import REVIEWED_ALIAS_RULES

    assert REVIEWED_ALIAS_RULES == ()
    government = [variant(model="RAV4", identifier="g1")]
    legacy = [variant(model="RAV 4 HYBRID", identifier="l1")]
    assert [item.state for item in reconcile_catalog(government, legacy).gaps] == \
        ["legacy_only", "government_only"]
    rule = AliasRule(legacy_manufacturer="Toyota", legacy_commercial_model="RAV 4 HYBRID",
                     government_manufacturer="Toyota", government_commercial_model="RAV4",
                     reviewed_reason="reviewed by hand for this test")
    report = reconcile_catalog(government, legacy, alias_rules=[rule])
    assert [(item.state, item.method) for item in report.matches] == [("matched", "reviewed_alias")]
    # Two rules pointing one legacy identity at two government identities is a
    # contradiction in the RULES, and there is no conservative side to pick.
    with pytest.raises(CatalogReconciliationError):
        reconcile_catalog(government, legacy, alias_rules=[
            rule, AliasRule(legacy_manufacturer="Toyota",
                            legacy_commercial_model="RAV 4 HYBRID",
                            government_manufacturer="Toyota",
                            government_commercial_model="COROLLA")])


def test_reconciliation_reports_every_required_state_and_merges_nothing():
    """matched, ambiguous, government_only, legacy_only, missing_years, under_enriched."""
    government = [variant(code="A", trim="BASE", identifier="g-2022"),
                  variant(code="B", trim="LIMITED", identifier="g-2022b"),
                  variant(years=(2023, 2023), code="C", identifier="g-2023"),
                  variant(model="COROLLA", code="D", identifier="g-corolla")]
    legacy = [variant(identifier="l-2022"), variant(model="YARIS", identifier="l-yaris")]
    report = reconcile_catalog(government, legacy)
    states = {item.state for item in report.results}
    assert states == {"ambiguous", "missing_years", "government_only", "legacy_only"}
    ambiguous = report.by_state("ambiguous")[0]
    # EVERY match is returned; nothing picks a first row.
    assert set(ambiguous.government_candidate_ids) == {"g-2022", "g-2022b"}
    assert ambiguous.method == "normalized_identity_and_year"
    missing = report.by_state("missing_years")[0]
    assert missing.model_years == (2023,) and missing.government_candidate_ids == ("g-2023",)
    assert {item.commercial_model for item in report.by_state("government_only")} == {"COROLLA"}
    assert {item.commercial_model for item in report.by_state("legacy_only")} == {"YARIS"}
    # Every result states its method and the exact candidates it is about.
    for item in report.results:
        assert item.method in ("official_model_code", "normalized_identity_and_year",
                               "reviewed_alias", "none")
        assert item.government_candidate_ids or item.legacy_candidate_ids


def test_reconciliation_reads_only_bounded_catalog_material(repository, landed):
    """The legacy side is candidate rows, and the comparison is bounded."""
    lease, _report = landed
    snapshot = next(iter(repository.catalog_snapshots.values()))
    rows = repository.list_catalog_candidates(snapshot["id"], limit=5)
    variants = variants_from_candidate_rows(rows)
    assert len(variants) == 5 and all(item.manufacturer == TOYOTA for item in variants)
    assert normalize_identity_text("  RAV4   HEV ") == "rav4 hev"
    with pytest.raises(CatalogReconciliationError):
        reconcile_catalog([variant()] * 5_001, [])


# =============================================================================
# 5. refresh and diff -- deterministic, and NOT scheduled
# =============================================================================

def refresh(repository, lease, **kwargs) -> GovernmentCatalogRefresh:
    client = DataGovClient(kwargs.pop("transport", None) or FixtureTransport(),
                           page_limit=PINNED_PAGE_LIMIT, sleep_fn=lambda _seconds: None)
    return GovernmentCatalogRefresh(repository, lease, client=client, **kwargs)


def test_an_unchanged_source_creates_no_snapshot_and_requests_no_research(repository, landed):
    """The property a schedulable refresh has to have: a quiet source is quiet."""
    lease, report = landed
    before = dict(repository.catalog_snapshots)
    outcome = refresh(repository, lease).sync_if_changed(query=dict(PINNED_QUERY))
    assert not outcome.changed and outcome.no_op and not outcome.research_required
    assert outcome.active_snapshot_key == report.snapshot_key
    assert repository.catalog_snapshots == before
    assert outcome.diff is None


def test_a_changed_source_lands_a_new_snapshot_and_a_bounded_diff(repository, landed):
    """A new immutable snapshot, and the difference as a focused work item."""
    from backend.testing.government_capture import encode, page_document

    lease, first = landed
    document = page_document(0)
    document["result"]["records"][0]["ramat_gimur"] = "ADVENTURE PLUS"
    package = json.loads(FixtureTransport().get(
        "https://data.gov.il/api/3/action/package_show", params={"id": src.CKAN_PACKAGE_ID},
        connect_timeout=1, read_timeout=1, max_bytes=10 ** 7).body)
    for resource in package["result"]["resources"]:
        if resource["id"] == src.WLTP_RESOURCE_ID:
            resource["last_modified"] = "2026-10-01T00:00:00.000000"
            resource.pop("revision_id", None)
    transport = FixtureTransport(bodies={0: encode(document),
                                         "package": encode(package)})
    outcome = refresh(repository, lease, transport=transport).sync_if_changed(
        query=dict(PINNED_QUERY))
    assert outcome.changed and outcome.research_required
    assert outcome.report.snapshot_key != first.snapshot_key
    # The PREVIOUS snapshot is untouched: an active snapshot is immutable, and
    # a failed refresh could never have replaced it.
    assert first.snapshot_key in repository.catalog_snapshots
    assert repository.catalog_snapshots[first.snapshot_key]["activated_at"] is not None
    diff = outcome.diff
    assert diff.added_count == 1 and diff.added[0].trim == "ADVENTURE PLUS"
    assert diff.previous_snapshot_key == first.snapshot_key
    # The REMOVED side is whatever identity the register stopped stating. It
    # publishes several rows sharing one code and trim that differ only in
    # their coded dimensions, so a trim change removes an identity only when no
    # other row still states it.
    assert diff.removed_count in (0, 1)
    assert diff.changed_count == 0  # nothing's READING changed, only one trim
    # Rollback is READING the older snapshot again, by name. Nothing is
    # deactivated and no raw history is deleted.
    pinned = GovernmentCatalogQuery(repository, snapshot_key=first.snapshot_key)
    assert pinned.dataset_metadata().snapshot_key == first.snapshot_key


def test_the_diff_states_exact_counts_and_drops_an_oversized_list_whole():
    """A truncated list would be a diff claiming completeness it does not have."""
    previous = [{"manufacturer": "Toyota", "commercial_model": "RAV4",
                 "model_year_start": 2000 + index, "model_year_end": 2000 + index,
                 "official_model_code": None, "trim": None, "identity_dimensions": {},
                 "status": "candidate", "upstream_record_id": str(index)}
                for index in range(120)]
    diff = diff_candidate_sets(previous, [], max_items=10)
    assert diff.removed_count == 120 and diff.bounded
    assert diff.removed == () and diff.added == () and diff.changed == ()
    # A change in the READING of an identity both sides state is `changed`,
    # never an add plus a remove.
    changed = diff_candidate_sets(previous[:1], [{**previous[0], "status": "ready_for_review"}])
    assert (changed.added_count, changed.changed_count, changed.removed_count) == (0, 1, 0)
    assert changed.changed[0].changed_fields == ("status",)


def test_a_diff_of_a_whole_resource_states_exact_counts_without_reading_it():
    """The COUNTS are exact for every row; only the LIST is bounded.

    `diff_rows` is the one comparison rule -- mirrored by
    `public.catalog_snapshot_candidate_diff`, which is what production actually
    calls -- so this is where "far more rows than any page" is proven. Ten
    thousand identities disappear, the count says ten thousand, and the list is
    dropped WHOLE rather than truncated, because a truncated list is a diff
    claiming a completeness it does not have.
    """
    previous = [{"manufacturer": "Toyota", "commercial_model": "RAV4",
                 "model_year_start": 2000, "model_year_end": 2000,
                 "official_model_code": f"CODE-{index:06d}", "trim": None,
                 "identity_dimensions": {}, "status": "candidate",
                 "upstream_record_id": str(index)}
                for index in range(10_000)]
    rows = diff_rows(previous, [], limit=MAX_DIFF_ITEMS)
    assert len(rows) == 1 and is_count_row(rows[0])
    assert rows[0]["removed_count"] == 10_000
    assert (rows[0]["added_count"], rows[0]["changed_count"]) == (0, 0)
    # Exactly at the bound the list IS reported, and in the deterministic order
    # the SQL function applies.
    rows = diff_rows(previous[:MAX_DIFF_ITEMS], [], limit=MAX_DIFF_ITEMS)
    assert len(rows) == MAX_DIFF_ITEMS and not any(is_count_row(row) for row in rows)
    assert all(row["removed_count"] == MAX_DIFF_ITEMS for row in rows)
    # Ordered by the identity itself -- here the official model code -- and
    # only then by the record id, exactly as the SQL function orders.
    assert [row["official_model_code"] for row in rows] \
        == [f"CODE-{index:06d}" for index in range(MAX_DIFF_ITEMS)]
    # One more than the bound, and the whole list goes.
    rows = diff_rows(previous[:MAX_DIFF_ITEMS + 1], [], limit=MAX_DIFF_ITEMS)
    assert len(rows) == 1 and rows[0]["removed_count"] == MAX_DIFF_ITEMS + 1


def test_a_comparison_that_cannot_run_still_lands_the_capture_and_says_so(repository, landed,
                                                                         monkeypatch):
    """A failure of the COMPARISON never undoes the CAPTURE, and never guesses.

    By the time the diff is asked for, an immutable snapshot has already landed
    and is already readable. Answering from an EMPTY set would report every row
    of the other side as added or removed -- a fabricated diff, which is worse
    than no diff at all -- so the refusal is reported and the snapshot stays.
    """
    from backend.testing.government_capture import encode, page_document

    lease, first = landed

    def unavailable(*args, **kwargs):
        raise AppError("CATALOG_QUERY_UNAVAILABLE", "diff is unavailable", 503)

    document = page_document(0)
    document["result"]["records"][0]["ramat_gimur"] = "ADVENTURE PLUS"
    package = json.loads(FixtureTransport().get(
        "https://data.gov.il/api/3/action/package_show", params={"id": src.CKAN_PACKAGE_ID},
        connect_timeout=1, read_timeout=1, max_bytes=10 ** 7).body)
    for resource in package["result"]["resources"]:
        if resource["id"] == src.WLTP_RESOURCE_ID:
            resource["last_modified"] = "2026-10-01T00:00:00.000000"
            resource.pop("revision_id", None)
    monkeypatch.setattr(repository, "catalog_snapshot_candidate_diff", unavailable)
    outcome = refresh(repository, lease,
                      transport=FixtureTransport(bodies={0: encode(document),
                                                         "package": encode(package)})
                      ).sync_if_changed(query=dict(PINNED_QUERY))
    assert outcome.changed and outcome.diff is None and outcome.diff_unavailable
    assert not outcome.no_op and not outcome.research_required
    assert "GOV_QUERY_UNAVAILABLE" in outcome.detail[0]
    # The snapshot landed anyway, and is readable.
    monkeypatch.undo()
    assert outcome.report.snapshot_key != first.snapshot_key
    assert GovernmentCatalogQuery(
        repository, snapshot_key=outcome.report.snapshot_key
    ).dataset_metadata().snapshot_key == outcome.report.snapshot_key


def test_no_production_entrypoint_schedules_this_refresh():
    """The operation exists and is deliberately not activated."""
    # The module and the package's own re-export are not entrypoints. Nothing
    # ELSE in `backend/` names the operation -- so no worker, no adapter, no
    # API route and no job can start one.
    owned = {"backend/catalog/government/refresh.py", "backend/catalog/government/__init__.py"}
    for path in sorted(Path("backend").rglob("*.py")):
        if str(path) in owned:
            continue
        text = path.read_text(encoding="utf-8")
        assert "GovernmentCatalogRefresh" not in text, path
        assert "sync_if_changed" not in text, path
    for path in sorted(Path("scripts").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".sh", ".yaml", ".yml"):
            assert "sync_if_changed" not in path.read_text(encoding="utf-8", errors="ignore"), path
    for path in sorted(Path(".github/workflows").glob("*.yml")):
        assert "catalog" not in path.read_text(encoding="utf-8").lower() or \
            "sync_if_changed" not in path.read_text(encoding="utf-8")


# =============================================================================
# 6. resume: the same work twice produces the same durable state
# =============================================================================

def test_a_replacement_worker_promotes_what_the_crashed_one_never_did(repository, landed):
    """THE crash window this path used to lose, closed and proven closed.

    The first attempt stops where a worker dies: Swarm V2 has durably persisted
    its evidence and its verified verdicts, and the canonical catalog is still
    empty. A REPLACEMENT worker is then constructed -- new worker id, new
    attempt, new lease, new pipeline, nothing carried over from the process
    that gathered any of it.

    It does not replay the tool call, and the test does not replay it either.
    It cannot: the resumed worker restored completed tasks from the checkpoint,
    so there is nothing left to re-execute. Everything it needs it derives from
    durable rows, and the proof that it did is the SET OF REPOSITORY METHODS it
    reached -- no source, no fragment, no claim, no verdict and no capture.

    This is the property the earlier round claimed and did not have: the
    association between a claim and the candidate it is evidence for lived in
    the crashed worker's memory, so the replacement found nothing to promote
    and the canonical catalog stayed empty with nothing saying why.
    """
    lease, _report = landed

    # --- attempt 1: the engine's half, then the process dies ----------------
    gather_evidence(repository, lease)
    assert canonical_counts(repository) == (0, 0, 0, 0)
    claims = len(claims_of(repository, lease))
    assert claims and all(row["source_id"] for row in claims_of(repository, lease))
    sources_before = len([row for row in repository.tool_rows
                          if repository.evidence_kinds.get(str(row["id"])) == "source"])

    # --- attempt 2: a replacement worker, with nothing carried over ---------
    resumed = restarted_lease(repository, lease)
    assert resumed.worker_id != lease.worker_id and resumed.attempt > lease.attempt
    recording = RecordingRepository(repository)
    attempts = CatalogPromotionPipeline(recording, resumed).promote()

    assert len(attempts) == 1 and attempts[0].promoted
    assert not attempts[0].outcome.replayed
    # NOTHING was re-gathered: no tool call, no model call, no capture, no
    # source request could have happened without one of these writes.
    assert not (set(recording.calls) & EVIDENCE_WRITE_METHODS), sorted(set(recording.calls))
    assert "catalog_run_pending_promotions" in recording.calls
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row["id"])) == "source"]) == sources_before
    assert len(claims_of(repository, lease)) == claims

    # The canonical variant exists, and its provenance names the RESUMED
    # worker's own lease -- the dead worker wrote none of it.
    variant = repository.get_canonical_catalog_variant(
        attempts[0].outcome.variant["canonical_key"])
    assert variant is not None and variant["manufacturer"] == TOYOTA
    provenance = repository.list_canonical_field_provenance(variant["variant_id"])
    assert provenance and all(row["worker_id"] == resumed.worker_id
                              and row["attempt"] == resumed.attempt
                              and row["run_id"] == str(lease.run_id) for row in provenance)

    # --- attempt 3: a SECOND resume writes nothing at all --------------------
    counts = canonical_counts(repository)
    again = CatalogPromotionPipeline(repository, restarted_lease(
        repository, resumed, worker="worker-restarted-again")).promote()
    assert len(again) == 1 and again[0].promoted and again[0].outcome.replayed
    assert canonical_counts(repository) == counts
    assert again[0].outcome.variant["id"] == attempts[0].outcome.variant["id"]


def test_every_crash_window_of_the_promotion_resumes_to_one_canonical_row(repository, landed):
    """One run, crashed at every window the path has, ending in ONE row.

    A crash is represented the only honest way: a step raises something the
    pipeline does not catch, so `promote` does not return -- and then a
    replacement worker starts from durable state. After all six the canonical
    catalog holds exactly one model, one variant and one provenance row per
    promoted field, because every write on the way is idempotent on a derived
    key.
    """
    lease, _report = landed

    # 1. AFTER EVIDENCE, BEFORE VERDICTS. Nothing is promotable: the durable
    #    read joins on a VERIFIED verdict, so the run owes nothing yet.
    run_government_task(repository, lease)
    resumed = restarted_lease(repository, lease, worker="w-1")
    assert CatalogPromotionPipeline(repository, resumed).pending() == ()
    assert CatalogPromotionPipeline(repository, resumed).promote() == ()
    assert canonical_counts(repository) == (0, 0, 0, 0)

    # 2. AFTER VERIFIED VERDICTS AND CHECKPOINT, BEFORE PROMOTION. The window
    #    that used to lose everything. The replacement worker finds the work.
    settle_verdicts(repository, resumed)
    resumed = restarted_lease(repository, resumed, worker="w-2")
    assert len(CatalogPromotionPipeline(repository, resumed).pending()) == 1
    assert canonical_counts(repository) == (0, 0, 0, 0)

    # 3. AFTER THE LINKS WERE CREATED. The reviewed transition dies.
    def dying_review(*_args, **_kwargs):
        raise RuntimeError("worker died after linking")

    crashing = RecordingRepository(repository)
    crashing.record_catalog_candidate = dying_review
    with pytest.raises(RuntimeError):
        CatalogPromotionPipeline(crashing, resumed).promote()
    links_after_crash = len(repository.catalog_evidence_links)
    assert links_after_crash > 0
    assert all(row["status"] != "ready_for_review"
               for row in repository.catalog_candidates.values())
    assert canonical_counts(repository)[:3] == (0, 0, 0)

    # 4. AFTER `ready_for_review` WAS WRITTEN, BEFORE THE CANONICAL PROMOTION.
    resumed = restarted_lease(repository, resumed, worker="w-3")

    def dying_promotion(*_args, **_kwargs):
        raise RuntimeError("worker died after the reviewed transition")

    crashing = RecordingRepository(repository)
    crashing.promote_catalog_variant = dying_promotion
    with pytest.raises(RuntimeError):
        CatalogPromotionPipeline(crashing, resumed).promote()
    assert any(row["status"] == "ready_for_review"
               for row in repository.catalog_candidates.values())
    assert canonical_counts(repository)[:3] == (0, 0, 0)
    # Re-linking created no second link: the key is derived from the evidence.
    assert len(repository.catalog_evidence_links) == links_after_crash

    # 5. THE PROMOTION ITSELF, on the next replacement.
    resumed = restarted_lease(repository, resumed, worker="w-4")
    attempts = CatalogPromotionPipeline(repository, resumed).promote()
    assert len(attempts) == 1 and attempts[0].promoted and not attempts[0].outcome.replayed
    counts = canonical_counts(repository)
    assert counts[0] == 1 and counts[1] == 1
    assert counts[2] == len(attempts[0].outcome.promoted_fields)
    assert counts[3] == links_after_crash

    # 6. AFTER PROMOTION, BEFORE THE RUN WAS FINALIZED. Everything replays.
    resumed = restarted_lease(repository, resumed, worker="w-5")
    replayed = CatalogPromotionPipeline(repository, resumed).promote()
    assert len(replayed) == 1 and replayed[0].outcome.replayed
    assert canonical_counts(repository) == counts
    # No duplicate revision either: every field is still at revision 1.
    provenance = repository.list_canonical_field_provenance(
        attempts[0].outcome.variant["id"])
    assert provenance and all(row["revision"] == 1 for row in provenance)


def test_a_partly_promoted_run_resumes_the_candidates_it_did_not_reach(repository, landed):
    """Some promoted, others not -- and the replacement finishes the rest.

    The crash lands BETWEEN two candidates, which is the window where a naive
    resume would either skip the unpromoted one or write the promoted one
    twice. It does neither: the first comes back a replay, the second is
    promoted, and the canonical catalog ends with exactly two variants.
    """
    lease, _report = landed
    for year in (PINNED_YEAR, PINNED_YEAR + 1):
        gather_evidence(repository, lease, year=year, call_id=f"call-{year}")
    pending = CatalogPromotionPipeline(repository, lease).pending()
    assert len(pending) == 2

    # The worker dies after the FIRST canonical promotion lands.
    real_promote = repository.promote_catalog_variant
    promoted_now: list[str] = []

    def dies_after_the_first(run_id, promotion, **kwargs):
        if promoted_now:
            raise RuntimeError("worker died between candidates")
        row = real_promote(run_id, promotion, **kwargs)
        promoted_now.append(str(row["id"]))
        return row

    crashing = RecordingRepository(repository)
    crashing.promote_catalog_variant = dies_after_the_first
    with pytest.raises(RuntimeError):
        CatalogPromotionPipeline(crashing, lease).promote()
    assert len(repository.catalog_model_variants) == 1

    # The replacement worker finishes the job.
    resumed = restarted_lease(repository, lease)
    recording = RecordingRepository(repository)
    attempts = CatalogPromotionPipeline(recording, resumed).promote()
    assert len(attempts) == 2 and all(item.promoted for item in attempts)
    # Exactly one replay: the candidate the crashed worker reached. The other
    # was promoted for the first time.
    assert sum(1 for item in attempts if item.outcome.replayed) == 1
    assert len(repository.catalog_model_variants) == 2
    assert len({row["id"] for row in repository.catalog_model_variants}) == 2
    # Still nothing re-gathered.
    assert not (set(recording.calls) & EVIDENCE_WRITE_METHODS)

    # And a third pass writes nothing.
    counts = canonical_counts(repository)
    final = CatalogPromotionPipeline(repository, restarted_lease(
        repository, resumed, worker="worker-final")).promote()
    assert len(final) == 2 and all(item.outcome.replayed for item in final)
    assert canonical_counts(repository) == counts


def test_every_guarded_write_replays_onto_the_row_it_already_wrote(repository, landed):
    """At-least-once delivery onto idempotent writes, write by write.

    This is NOT the resume proof -- `test_a_replacement_worker_promotes_what_
    the_crashed_one_never_did` is, and it drives the real restart path. This
    one replays each guarded write BY HAND, which is the only way to show that
    every individual one is idempotent rather than that the sequence happens to
    be.
    """
    lease, _report = landed
    promotion, plan, outcome, acquired, candidate, links, claims, verdicts = promoted(
        repository, lease)
    snapshot_state = json.dumps(repository.catalog_snapshots, sort_keys=True, default=str)
    sources = len([row for row in repository.tool_rows
                   if repository.evidence_kinds.get(str(row["id"])) == "source"])
    fragments = len([row for row in repository.tool_rows
                     if repository.evidence_kinds.get(str(row["id"])) == "evidence_fragment"])
    claim_count = len([row for row in repository.tool_rows
                       if repository.evidence_kinds.get(str(row["id"])) == "claim"])
    provenance = len(promotion.field_provenance(outcome.variant["id"]))

    # The resume: every step again, from the top.
    result = resolve(repository, code=candidate["official_model_code"])
    board, replayed = acquire(repository, lease, result, call_id="call-1")
    assert [row["id"] for row in replayed.claims] == [row["id"] for row in acquired.claims]
    replayed_links, _c, _v = verify_and_link(repository, lease, board, replayed, candidate)
    assert sorted(row["id"] for row in replayed_links) == sorted(row["id"] for row in links)
    assert promotion.promote(plan).variant["id"] == outcome.variant["id"]

    assert json.dumps(repository.catalog_snapshots, sort_keys=True, default=str) == snapshot_state
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row["id"])) == "source"]) == sources
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row["id"])) == "evidence_fragment"]) == fragments
    assert len([row for row in repository.tool_rows
                if repository.evidence_kinds.get(str(row["id"])) == "claim"]) == claim_count
    assert len(promotion.field_provenance(outcome.variant["id"])) == provenance
    assert len(repository.catalog_models) == 1 and len(repository.catalog_model_variants) == 1


# =============================================================================
# 7. the gates would have FAILED on the Catalog PR2 base
# =============================================================================

PR2_MIGRATIONS = ("supabase/migrations/20260914200000_catalog_evidence_foundation.sql",
                  "supabase/migrations/20260915120000_catalog_integrity_corrections.sql",
                  "supabase/migrations/20260915180000_catalog_raw_record_source_locator.sql")
PR3_MIGRATIONS = ("supabase/migrations/20260916090000_catalog_bounded_candidate_queries.sql",
                  "supabase/migrations/20260916120000_catalog_field_level_promotion.sql")


def pr2_sql() -> str:
    return "\n".join(Path(name).read_text(encoding="utf-8") for name in PR2_MIGRATIONS)


def pr3_sql() -> str:
    return "\n".join(Path(name).read_text(encoding="utf-8") for name in PR3_MIGRATIONS)


def test_every_gate_this_pr_adds_is_absent_from_the_pr2_schema():
    """The gates are NEW, and the PR2 base could not have applied them.

    A test that only exercises the finished implementation cannot tell a gate
    that was added from one that was always there. Each name below is a
    mechanism this PR introduces; none of them exists in the three migrations
    that were on `main` before it, which is what makes "this would have failed
    on the PR2 base" checkable rather than asserted.
    """
    before, after = pr2_sql(), pr3_sql()
    for mechanism in ("catalog_canonical_field_provenance",
                      "catalog_check_field_provenance",
                      "catalog_require_field_provenance",
                      "catalog_canonical_stated_fields",
                      "catalog_canonical_variant_current",
                      "promote_catalog_variant_guarded",
                      "catalog_candidate_variant_page",
                      "catalog_readable_snapshot"):
        assert mechanism not in before, mechanism
        assert mechanism in after, mechanism
    # And the PR2 base could not have promoted anything at all: the canonical
    # pair was SELECT-only for every role, which is the state this PR replaces
    # with INSERT plus two triggers.
    assert "grant select on table %s to service_role" in before
    assert "revoke insert, update, delete on table %s from service_role" in before
    assert "grant select, insert on table %s to service_role" in after


def test_the_pr2_promotion_shape_is_refused_by_the_gate_this_pr_adds():
    """The exact shape PR1's corrective round said was not sufficient.

    A canonical row carrying ONE row-level `promoted_from_verdict_id` for a row
    of several independent facts. It is refused now for the reason that note
    gave: a verdict that confirmed one field says nothing about the field
    beside it. The executable proof against real PostgreSQL is
    `test_a_canonical_row_cannot_commit_without_provenance_for_every_field`.
    """
    # The requirement, read out of the migration that wrote it down. Its
    # comment wraps across lines, so the prefixes are stripped before matching
    # -- the sentence is the assertion, not its formatting.
    correction = " ".join(
        line.lstrip().removeprefix("--").strip() for line
        in Path(PR2_MIGRATIONS[1]).read_text(encoding="utf-8").splitlines())
    assert "PR3 MUST add FIELD-LEVEL, append-only revision provenance -- one " \
           "provenance row per fact, not one per canonical row -- before any insert " \
           "is enabled; a row-level FK is not sufficient" in correction
    # And that is exactly what this PR added: one provenance row per FACT,
    # required for every field the canonical row states.
    promotion = pr3_sql()
    assert "one provenance row per promoted FACT" in promotion.replace("\n", " ") or \
        "ONE row per promoted canonical FACT" in promotion
    assert "requires verified provenance for every field it states" in promotion


@pytest.mark.parametrize("label,expected,override", PROMOTION_SCOPE_REFUSALS,
                         ids=[case[0] for case in PROMOTION_SCOPE_REFUSALS])
def test_evidence_about_another_vehicle_scope_or_record_is_never_promoted(repository, landed,
                                                                          label, expected,
                                                                          override):
    """Sound evidence about the WRONG THING is still refused.

    Everything above the scope gate is satisfied in each case -- a versioned
    government source, a focused fragment at the locator, a claim citing it,
    a `verified` verdict supported by that fragment, an evidence link for this
    exact candidate, and a field/value that matches the canonical row exactly.
    The ONE thing each case changes is what the claim is ABOUT.

    Without these gates a verified fact about a 1999 Corolla, or about another
    market, or read out of a different register row, could become a canonical
    fact about this vehicle -- which is the worst failure the canonical catalog
    has, and the only one its own provenance could not later reveal.
    """
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    links, claims, verdicts = verify_and_link(repository, lease, board, acquired, candidate)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    plan = build_promotion_plan(
        candidate=candidate, snapshot=snapshot,
        evidence=field_evidence_for(candidate=candidate, links=links, claims=claims,
                                    verdicts=verdicts))

    # The honest chain for `model_year_start`, rebuilt with ONE thing wrong.
    scope = {"entity": government_entity_key(candidate["manufacturer"],
                                             candidate["commercial_model"],
                                             candidate["model_year_start"]),
             "time_scope": {"model_year": candidate["model_year_start"]},
             "market": src.GOVERNMENT_DATASET_MARKET,
             # The identity the real mapper emits for this row, from the same
             # builder, so only the case's own override is ever wrong.
             "identity": GovernmentVariantEvidenceMapper._identity(candidate)}
    forged = forged_chain(repository, lease, board, candidate, label=label,
                          field_key="model_year_start", value=candidate["model_year_start"],
                          **{**scope, **override})
    payload = plan.as_payload()
    payload["fields"] = [{**entry, "evidence_link_id": forged["id"]}
                         if entry["field_key"] == "model_year_start" else entry
                         for entry in payload["fields"]]
    with pytest.raises(AppError) as failure:
        repository.promote_catalog_variant(lease.run_id, payload, **lease_kwargs(lease))
    assert expected in str(failure.value)
    # A refused promotion leaves the canonical catalog exactly as it was.
    assert not repository.catalog_model_variants and not repository.catalog_models
    assert not repository.catalog_canonical_field_provenance


def test_every_scope_refusal_is_spelled_the_same_way_in_sql_and_in_memory():
    """Parity as a STRING comparison, because a mirror that drifts is not one.

    The in-memory repository is a mirror of the database's rules, so a refusal
    it raises must be the refusal PostgreSQL raises -- not a paraphrase a
    reviewer would have to translate. Every sentence the table above asserts
    offline is asserted to exist in the migration and in the mirror.
    """
    migration = Path("supabase/migrations/"
                     "20260916120000_catalog_field_level_promotion.sql").read_text(encoding="utf-8")
    memory = Path("backend/testing/memory_repository.py").read_text(encoding="utf-8")
    for _label, sentence, _override in PROMOTION_SCOPE_REFUSALS:
        assert sentence in migration, sentence
        assert sentence in memory, sentence
    # The run-consistency and identity refusals, same rule.
    for sentence in ("catalog promotion states an identity its candidate does not",
                     "catalog canonical variant identity conflict",
                     "was not promoted by its linking run",
                     "support chain spans more than one run",
                     "catalog promotion is not one act of one run",
                     "scope disagrees with this variant"):
        assert sentence in migration, sentence
        assert sentence in memory, sentence


def test_a_promotion_may_not_cite_another_runs_evidence(repository, landed):
    """One promotion is one leased act, all the way down to the stored fact.

    The link, the source, the claim, the verdict and the provenance row must
    all name the run that holds the lease. A run that reused an earlier run's
    verified evidence would be attributing a promotion to a lease that never
    covered it.
    """
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    links, claims, verdicts = verify_and_link(repository, lease, board, acquired, candidate)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    plan = build_promotion_plan(
        candidate=candidate, snapshot=snapshot,
        evidence=field_evidence_for(candidate=candidate, links=links, claims=claims,
                                    verdicts=verdicts))
    # A SECOND run, with its own lease, presenting the first run's links.
    other = leased_run(repository, worker="worker-2")
    with pytest.raises(AppError) as failure:
        repository.promote_catalog_variant(other.run_id, plan.as_payload(),
                                           **lease_kwargs(other))
    assert "not promoted by its linking run" in str(failure.value)
    assert not repository.catalog_canonical_field_provenance


def test_the_memory_repository_applies_the_same_promotion_invariants(repository, landed):
    """Offline tests are only meaningful if they test the database's rule.

    Stated exactly, because overclaiming parity is worse than not claiming it:
    the memory repository mirrors the RULES -- the lease, the derived keys, the
    support chain, the field/value gate, the coverage check and the replay
    conflict. It does NOT reproduce PostgreSQL. Two protections are
    DATABASE-ONLY and are documented as such rather than asserted here: the
    DEFERRED timing of the coverage trigger (this implementation checks
    coverage before it appends anything, so there is no window at all rather
    than one that closes at COMMIT), and the concurrency semantics of the
    unique indexes under simultaneous writers.
    """
    source = Path("backend/testing/memory_repository.py").read_text(encoding="utf-8")
    assert "WHAT THIS IS NOT" in source
    assert "DATABASE-ONLY" in source
    lease, _report = landed
    result = resolve(repository, code=one_code(repository))
    board, acquired = acquire(repository, lease, result)
    candidate = mark_ready(repository, lease, result["variants"][0]["candidate_id"])
    links, claims, verdicts = verify_and_link(repository, lease, board, acquired, candidate)
    evidence = field_evidence_for(candidate=candidate, links=links, claims=claims,
                                  verdicts=verdicts)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row["id"] == candidate["snapshot_id"])
    plan = build_promotion_plan(candidate=candidate, snapshot=snapshot, evidence=evidence)
    # A payload whose fields do not match the row it would write is refused by
    # the repository too, not only by the assembly above it.
    payload = plan.as_payload()
    payload["fields"] = payload["fields"][:-1]
    with pytest.raises(Exception) as failure:
        repository.promote_catalog_variant(lease.run_id, payload, **lease_kwargs(lease))
    assert "do not match the canonical row" in str(failure.value)
    assert not repository.catalog_model_variants
