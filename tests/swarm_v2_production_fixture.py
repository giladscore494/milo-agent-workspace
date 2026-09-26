"""PR-X S4: a production-scale, fully offline Swarm V2 Government run.

Test-only fixture builder. It reproduces the SHAPE of the runs that lost their
paid work in production, at production scale, with no network, no provider
and no database:

* a pinned Toyota Government snapshot of 6,374 rows -- the size of the real
  snapshot of run 9e7f0d11 (see tests/test_government_placeholder_rows.py);
* a prepared work queue of 20 batch items, one of which is the placeholder
  row of run 6825eb96 (``11111`` / ``11111111``, excluded at preparation) and
  two of which share ONE register identity (the duplicate-signature pair of
  run 3c72bfbc, which the register answers as ambiguous);
* a Commander plan -- one ``resolve_variant`` task per queued item -- whose
  task ``output_schema`` uses ``enum``, ``description`` and ``minimum`` and
  carries ONE annotation (``format``) that normalization strips.

Everything below the plan is REAL: ToolRegistry + GovernmentVehicleTool over
the in-memory repository, the trusted evidence mapper and the lease-guarded
EvidenceBoard, GenericWorker (its model is a fake gateway that returns a
VALID worker output), RepositoryEvidenceResolver + Verifier, FinalBuilder and
the VehicleCatalogResultAssembler. The repository is wrapped in a spy that
refuses every whole-snapshot read.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import UUID, uuid4

from backend.catalog.government.preparation import GovernmentPreparation, prepare_government_work
from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander, CommanderModelResolver,
                                      EvidenceReference, FinalBuilder, GenericWorker,
                                      PlanLimits, PlanValidator, SwarmV2Engine, Verifier)
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.evidence_mapping import (RegisteredOperationEvidenceSink,
                                                       TrustedEvidenceAcquisition,
                                                       production_evidence_mappers)
from backend.engines.swarm_v2.grounding import RepositoryEvidenceResolver
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import committed_records, seed_prepared_plan, start_batch_run
from backend.tools import ToolContext, ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE,
                                              GovernmentVehicleTool)

#: The pinned Toyota snapshot of run 9e7f0d11 holds this many rows.
SNAPSHOT_ROWS = 6_374
#: The prepared batch: 20 register items.
QUEUE_ITEMS = 20
PLACEHOLDER_ID = "37363"
DUPLICATE_IDS = ("37350", "37439")
DUPLICATE_CODE = "TZNA55L-GKZSZA"

#: The ONE task output schema every task declares: enum, description and
#: minimum are kept and enforced; `format` is an annotation normalization
#: strips (and records by name in `stripped_output_keywords`).
OUTPUT_SCHEMA = {
    "type": "object",
    "description": "What the register answered for this one candidate.",
    "properties": {
        "summary": {"type": "string", "description": "One sentence, no values."},
        "register_answer": {"type": "string", "enum": ["resolved", "ambiguous", "not_found"],
                            "description": "The register's answer, as the tool reported it."},
        "match_count": {"type": "integer", "minimum": 0,
                        "description": "How many register rows matched."},
        "checked_on": {"type": "string", "format": "date",
                       "description": "The day the answer was read."},
    },
    "required": ["summary", "register_answer", "match_count"],
    "additionalProperties": False,
}


def _row(base: dict, record_id: int, *, model: str, code: str, trim: str,
         year: int = 2026) -> dict:
    row = copy.deepcopy(base)
    row.update({"_id": record_id, "kinuy_mishari": model, "degem_nm": code,
                "ramat_gimur": trim, "shnat_yitzur": year})
    return row


def register_rows() -> list[dict]:
    """The 20 batch rows: 17 distinct variants, 1 placeholder, 1 duplicate pair."""
    base = committed_records(1)[0]
    rows = [_row(base, 40_000 + index, model="RAV4", code=f"AXAL52L-P{index:02d}",
                 trim=f"TRIM {index:02d}") for index in range(17)]
    rows.insert(5, _row(base, int(PLACEHOLDER_ID), model="11111", code="11111111", trim="SE"))
    rows += [_row(base, int(record_id), model="4RUNNER", code=DUPLICATE_CODE, trim="LIMITED")
             for record_id in DUPLICATE_IDS]
    assert len(rows) == QUEUE_ITEMS
    return rows


def grow_snapshot(repository: MemoryRepository, rows: int = SNAPSHOT_ROWS) -> dict:
    """Grow the pinned snapshot in memory to `rows` rows of distinct filler.

    Exactly the technique of test_government_placeholder_rows.real_size_snapshot:
    the batch, its binding and its items are untouched.
    """
    (snapshot,) = [row for row in repository.catalog_snapshots.values()
                   if row.get("activated_at")]
    template_record = next(row for row in repository.catalog_raw_records.values()
                           if row["snapshot_id"] == snapshot["id"]
                           and row["upstream_record_id"] != PLACEHOLDER_ID)
    template_candidate = next(row for row in repository.catalog_candidates.values()
                              if row["raw_record_id"] == template_record["id"])
    existing = sum(1 for row in repository.catalog_candidates.values()
                   if row["snapshot_id"] == snapshot["id"])
    for index in range(rows - existing):
        record = copy.deepcopy(template_record)
        record.update({"id": str(uuid4()), "record_key": f"cr1.{index:032x}",
                       "upstream_record_id": str(100_000 + index)})
        record["payload"]["_id"] = 100_000 + index
        candidate = copy.deepcopy(template_candidate)
        candidate.update({"id": str(uuid4()), "candidate_key": f"cc1.{index:032x}",
                          "raw_record_id": record["id"],
                          "commercial_model": f"FILLER{index % 97}",
                          "official_model_code": f"FILL-{index:05d}"})
        repository.catalog_raw_records[(snapshot["id"], record["record_key"])] = record
        repository.catalog_candidates[(snapshot["id"], candidate["candidate_key"])] = candidate
    metadata = snapshot["retrieval_metadata"]
    for name in ("reported_total", "captured_record_count", "normalized_record_count"):
        metadata[name] = rows
    snapshot["stored_record_count"] = snapshot["declared_record_count"] = rows
    return snapshot


class WholeSnapshotSpy:
    """The repository, refusing every whole-snapshot read and counting catalog rows."""

    WHOLE_SNAPSHOT_READS = ("list_catalog_raw_records", "list_catalog_candidates")

    def __init__(self, inner: MemoryRepository) -> None:
        self._inner = inner
        self.catalog_rows_read = 0
        self.whole_snapshot_reads: list[str] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name in self.WHOLE_SNAPSHOT_READS:
            def refused(*_args, **_kwargs):
                self.whole_snapshot_reads.append(name)
                raise AssertionError(f"the run paged the whole snapshot via {name}")
            return refused
        if name.startswith("catalog_") and callable(attribute):
            def counted(*args, **kwargs):
                result = attribute(*args, **kwargs)
                if isinstance(result, list):
                    self.catalog_rows_read += len(result)
                elif result is not None:
                    self.catalog_rows_read += 1
                return result
            return counted
        return attribute


#: The guarded evidence RPCs and the grounding reads. They go to the R3/R5
#: guarded evidence store the proof suites already hold to the migration's
#: contract (lease on every write, idempotency on `evidence_key`); everything
#: else -- the catalog, the runs, the batches -- is the in-memory repository.
EVIDENCE_METHODS = frozenset({
    "create_tool_usage", "create_source", "record_evidence_fragment", "create_claim",
    "create_conflict", "record_claim_verdict", "record_conflict_resolution",
    "patch_run_blackboard_evidence", "list_sources_for_ids",
    "list_evidence_fragments_for_sources", "list_structured_facts_for_sources"})


class RunRepository:
    """The catalog (spied) plus the guarded evidence store, as ONE repository."""

    def __init__(self, catalog: WholeSnapshotSpy, evidence: Any) -> None:
        self.catalog, self.evidence = catalog, evidence

    def __getattr__(self, name: str):
        return getattr(self.evidence if name in EVIDENCE_METHODS else self.catalog, name)


def resolve_call(item: Any) -> dict:
    arguments = {"manufacturer": item.manufacturer, "commercial_model": item.commercial_model,
                 "model_year": item.model_year_start,
                 "official_model_code": item.official_model_code}
    if item.trim:
        arguments["trim"] = item.trim
    return {"call_id": "c1", "name": GOVERNMENT_TOOL_NAME, "operation": "resolve_variant",
            "arguments": arguments, "dependency_bindings": []}


def commander_plan(queue: tuple, *, tasks: int | None = None, max_replans: int = 1) -> dict:
    """One `resolve_variant` task per queued item (optionally the first `tasks`)."""
    items = list(queue)[:tasks] if tasks is not None else list(queue)
    planned = [{
        "task_id": f"t{index:02d}",
        "goal": f"resolve register candidate {index:02d}",
        "scope": "Israeli Government vehicle register, pinned snapshot",
        "dependencies": [], "tools": [resolve_call(item)],
        "output_schema": copy.deepcopy(OUTPUT_SCHEMA),
        "evidence": {"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
        "priority": 50, "recursion_depth": 0, "estimated_cost_units": 10,
        "completion": {"required_outputs": ["summary"], "evidence_satisfied": True,
                       "allow_partial": False},
    } for index, item in enumerate(items, start=1)]
    return {"version": "1", "objective": "resolve the prepared Toyota batch",
            "graph": {"tasks": planned},
            "assignments": [{"task_id": task["task_id"], "worker_role": "register reader",
                             "context_task_ids": []} for task in planned],
            "max_replans": max_replans,
            "estimated_cost_units": sum(task["estimated_cost_units"] for task in planned)}


class WorkerGateway:
    """The worker model: returns ONE valid output per call, read from the tool result."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, **kwargs):
        document = json.dumps(kwargs.get("messages", []))
        self.calls.append(kwargs.get("agent", ""))
        ambiguous = '\\"ambiguous\\": true' in document or '"ambiguous": true' in document
        return {"summary": "the register answered this candidate",
                "register_answer": "ambiguous" if ambiguous else "resolved",
                "match_count": 2 if ambiguous else 1}


class VerifierModelNotExpected(AssertionError):
    """Register claims verify deterministically: no verifier model call exists."""


class NoVerifierModel:
    def __init__(self) -> None:
        self.calls = 0

    def call(self, **_kwargs):
        self.calls += 1
        raise VerifierModelNotExpected("the verifier model was called")


class ScriptedCommander:
    """The Commander client: the compiled plan, then scripted replan decisions."""

    def __init__(self, initial: dict, decisions: list[Any]) -> None:
        self.initial, self._decisions = initial, list(decisions)
        self.replans = 0

    def create_plan(self, **_kwargs):
        return self.initial

    def create_replan(self, **_kwargs):
        self.replans += 1
        if self._decisions:
            decision = self._decisions.pop(0)
            if isinstance(decision, BaseException):
                raise decision
            return decision
        return {"decision": "FINISH", "plan": None, "reason": "no further work"}


REQUEST_VERIFICATION = {"decision": "REQUEST_VERIFICATION", "plan": None,
                        "reason": "every candidate was answered"}


@dataclass
class ProductionRun:
    repository: MemoryRepository
    evidence: Any
    spy: WholeSnapshotSpy
    run_id: str
    lease: WorkerLease
    preparation: GovernmentPreparation
    plan: dict
    commander: ScriptedCommander
    worker_gateway: WorkerGateway
    verifier_gateway: NoVerifierModel
    engine: SwarmV2Engine
    checkpoints: list = field(default_factory=list)
    events: list = field(default_factory=list)
    ledger: list = field(default_factory=list)

    def run(self, *, checkpoint: dict | None = None) -> dict:
        payload = {"id": self.run_id, "input": {"objective": "resolve the batch",
                                                 "commander_model": "fake"}}
        if checkpoint is not None:
            payload["checkpoint"] = checkpoint
        return self.engine.run(payload)

    def claims(self) -> list[dict]:
        return list(self.evidence.claims.values())


def build_production_run(*, tasks: int | None = None, decisions: list[Any] | None = None,
                         max_replans: int = 1,
                         engine_kwargs: Callable[[dict], dict] | None = None,
                         snapshot_rows: int = SNAPSHOT_ROWS) -> ProductionRun:
    """The whole offline production-scale run, wired as backend/worker/main.py wires it."""
    repository = MemoryRepository()
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "gov-scale", "Gov scale", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))["id"]
    prepared = seed_prepared_plan(repository, user_id=user, conversation_id=conversation,
                                  units=("toyota",), max_items=QUEUE_ITEMS,
                                  batch_size=QUEUE_ITEMS, records=register_rows())
    run_id = start_batch_run(repository, prepared)["run"]["id"]
    snapshot = grow_snapshot(repository, snapshot_rows)
    spy = WholeSnapshotSpy(repository)
    preparation = prepare_government_work(spy, run_id=run_id)
    claimed = repository.claim_run(UUID(run_id), "worker-scale")
    lease = WorkerLease(UUID(run_id), "worker-scale", int(claimed["attempt"]),
                        claimed["lease_token"])

    plan = commander_plan(preparation.queue, tasks=tasks, max_replans=max_replans)
    from test_swarm_v2_r5_vehicle_proof import ProofRepository

    evidence_store = ProofRepository(lease)
    combined = RunRepository(spy, evidence_store)
    tools = ToolRegistry([GovernmentVehicleTool(combined,
                                                snapshot_key=snapshot["snapshot_key"])])
    board = EvidenceBoard(combined, lease)
    sink = RegisteredOperationEvidenceSink(
        TrustedEvidenceAcquisition(board=board, mappers=production_evidence_mappers()))
    client = ScriptedCommander(plan, list(decisions or [REQUEST_VERIFICATION]))
    commander = Commander(client=client, resolver=CommanderModelResolver(("fake",), {"fake"}),
                          validator=PlanValidator(allowed_tools=tools.descriptors(),
                                                  limits=PlanLimits(max_tasks=23,
                                                                    max_tool_calls=24,
                                                                    max_replans=1)))
    worker_gateway, verifier_gateway = WorkerGateway(), NoVerifierModel()
    context = ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}))
    holder: dict = {}
    checkpoints: list = []
    events: list = []
    ledger: list = []

    def evidence_loader(results):
        completed = {str(task_id) for task_id, result in dict(results).items()
                     if getattr(result, "status", None) == "completed"}
        return [EvidenceReference.model_validate(item)
                for item in board.references(task_ids=completed)]

    kwargs = dict(
        commander=commander,
        executor=BoundedTaskExecutor(worker_factory=lambda: GenericWorker(
            gateway=worker_gateway, tools=tools, model="kimi-k2.6", tool_context=context,
            tool_result_sink=sink), max_active_workers=1),
        verifier=Verifier(gateway=verifier_gateway, model="kimi-k2.6",
                          resolver=RepositoryEvidenceResolver(combined,
                                                             run_id=UUID(run_id))),
        builder=FinalBuilder(), evidence_loader=evidence_loader,
        checkpoint_sink=lambda _phase, value: checkpoints.append(copy.deepcopy(value)),
        event_sink=lambda kind, payload: events.append((kind, payload)),
        ledger_sink=ledger.append,
        verdict_sink=board.record_verification_verdict,
        resolution_sink=board.record_conflict_resolution)
    if engine_kwargs is not None:
        kwargs.update(engine_kwargs(kwargs))
    holder["engine"] = SwarmV2Engine(**kwargs)
    return ProductionRun(repository=repository, evidence=evidence_store, spy=spy,
                         run_id=run_id, lease=lease,
                         preparation=preparation, plan=plan, commander=client,
                         worker_gateway=worker_gateway, verifier_gateway=verifier_gateway,
                         engine=holder["engine"], checkpoints=checkpoints, events=events,
                         ledger=ledger)
