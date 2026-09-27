"""Test-only: replay a production Swarm V2 run offline, strictly.

What runs for real, and what is recorded
----------------------------------------

REAL, unmocked: `ModelGateway` (request building, completion classification),
`Commander` + `PlanValidator` (the firewall, with the reviewed production plan
limits), `BoundedTaskExecutor`, `GenericWorker`, the `ToolRegistry` (input and
output schema validation), the trusted Government evidence mapper and the
lease-guarded `EvidenceBoard`, `RepositoryEvidenceResolver` + `Verifier`,
`FinalBuilder` and the `VehicleCatalogResultAssembler`.

RECORDED, replayed strictly:

* the PROVIDER. `ReplayProviderAdapter` stands where `ProviderAdapter` stands
  and answers each call with the next recorded completion for that exact role,
  phase and task. A call the recording does not hold is a
  `ReplayDivergence` (a BaseException, so no `except Exception` in the engine
  can fold it into an ordinary failure) -- it is never answered with anything.
* the GOVERNMENT TOOL. `ReplayGovernmentTool` is the real tool's class and
  descriptor with `execute` answering the recorded result for the call
  identity (operation + resolved arguments, then task_id/call_id checked at the
  sink). An unrecorded call diverges. Each recorded answer is also CROSS-CHECKED
  against the real tool over the recorded snapshot rows: same match count,
  same register rows, same identity projection.

Every catalog read goes through `WholeSnapshotSpy`, which REFUSES the
whole-snapshot projections, so a replay also proves the run never pages a
snapshot.
"""

from __future__ import annotations

import copy
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from uuid import UUID, uuid4

from backend.catalog.government.preparation import GovernmentPreparation, GovernmentWorkItem
from backend.engines.swarm_v2 import (BoundedTaskExecutor, Commander, CommanderModelResolver,
                                      EvidenceReference, FinalBuilder, GenericWorker,
                                      ModelGateway, PlanValidator, RemainingBudget,
                                      SwarmV2Engine, Verifier)
from backend.engines.swarm_v2.commander import CommanderPlanFailure
from backend.engines.swarm_v2.evidence import EvidenceBoard, WorkerLease
from backend.engines.swarm_v2.failures import SwarmExecutionFailure
from backend.engines.swarm_v2.evidence_mapping import (RegisteredOperationEvidenceSink,
                                                       TrustedEvidenceAcquisition,
                                                       production_evidence_mappers)
from backend.engines.swarm_v2.grounding import RepositoryEvidenceResolver
from backend.replay_capture import (PROVENANCE_KINDS, REPLAY_FORMAT, ROLE_PHASES,
                                    sanitization_findings)
from backend.runtime_policy import reviewed_first_run_policy
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import seed_prepared_plan, start_batch_run
from backend.tools import ToolContext, ToolRegistry
from backend.tools.government_vehicle import (GOVERNMENT_TOOL_NAME, GOVERNMENT_TOOL_SCOPE,
                                              GovernmentVehicleTool)

REPLAY_ROOT = Path(__file__).resolve().parent / "replay"
MANIFEST_NAME = "manifest.json"


# =============================================================================
# The format
# =============================================================================

def fixture_dirs() -> list[Path]:
    return sorted(path for path in REPLAY_ROOT.iterdir()
                  if path.is_dir() and (path / MANIFEST_NAME).is_file())


def load_manifest(directory: Path | str) -> dict[str, Any]:
    return json.loads((Path(directory) / MANIFEST_NAME).read_text(encoding="utf-8"))


def artifact_ids(manifest: Mapping[str, Any]) -> list[str]:
    """Every artifact a manifest holds, by the id its provenance is keyed on."""
    ids = ["preparation"]
    ids += [f"commander[{index}]" for index in range(len(manifest.get("commander", [])))]
    for task_id, attempts in sorted((manifest.get("workers") or {}).items()):
        ids += [f"workers.{task_id}[{index}]" for index in range(len(attempts))]
    ids += [f"verifier[{index}]" for index in range(len(manifest.get("verifier", [])))]
    ids += [f"tool_results.{item['task_id']}/{item['call_id']}"
            for item in manifest.get("tool_results", [])]
    ids += [f"snapshot_rows.{row['_id']}" for row in manifest.get("snapshot_rows", [])]
    return ids


def referenced_record_ids(manifest: Mapping[str, Any]) -> set[str]:
    """The register rows the recorded tool results name."""
    found: set[str] = set()
    for item in manifest.get("tool_results", []):
        result = item.get("result") or {}
        for variant in result.get("variants") or []:
            found.add(str(variant["upstream_record_id"]))
        record = result.get("source_record")
        if isinstance(record, Mapping):
            found.add(str(record["upstream_record_id"]))
    return found


def manifest_problems(manifest: Mapping[str, Any]) -> list[str]:
    """Static reasons a manifest is not a valid replay/1 fixture."""
    problems: list[str] = []
    if manifest.get("format") != REPLAY_FORMAT:
        problems.append("FORMAT_NOT_REPLAY_1")
    provenance = manifest.get("provenance") or {}
    for artifact in artifact_ids(manifest):
        entry = provenance.get(artifact)
        if not isinstance(entry, Mapping) or entry.get("kind") not in PROVENANCE_KINDS \
                or not str(entry.get("source") or "").strip():
            problems.append(f"PROVENANCE_MISSING {artifact}")
    for artifact in provenance:
        if artifact not in artifact_ids(manifest):
            problems.append(f"PROVENANCE_ORPHAN {artifact}")
    for index, entry in enumerate(manifest.get("commander", [])):
        if entry.get("phase") not in ROLE_PHASES["commander"]:
            problems.append(f"COMMANDER_PHASE_INVALID commander[{index}]")
    for entry in manifest.get("verifier", []):
        if entry.get("phase") not in ROLE_PHASES["verifier"]:
            problems.append("VERIFIER_PHASE_INVALID")
    completions = [*manifest.get("commander", []), *manifest.get("verifier", []),
                   *[item for attempts in (manifest.get("workers") or {}).values()
                     for item in attempts]]
    for entry in completions:
        if not isinstance(entry.get("content"), str):
            problems.append("COMPLETION_CONTENT_NOT_INERT_TEXT")
    rows = {str(row["_id"]) for row in manifest.get("snapshot_rows", [])}
    if rows != referenced_record_ids(manifest):
        problems.append("SNAPSHOT_ROWS_NOT_EXACTLY_THOSE_REFERENCED")
    seen = set()
    for item in manifest.get("tool_results", []):
        identity = (item.get("task_id"), item.get("call_id"))
        if identity in seen:
            problems.append(f"TOOL_RESULT_DUPLICATE {identity}")
        seen.add(identity)
    expected = manifest.get("expected") or {}
    if expected.get("terminal") not in {"result", "unrecorded_call", "failed"}:
        problems.append("EXPECTED_TERMINAL_INVALID")
    return problems


def provenance_summary(manifest: Mapping[str, Any]) -> dict[str, int]:
    kinds = [entry["kind"] for entry in (manifest.get("provenance") or {}).values()]
    return {kind: kinds.count(kind) for kind in PROVENANCE_KINDS}


# =============================================================================
# The strict replay seams
# =============================================================================

class ReplayDivergence(BaseException):
    """The engine did something the recording cannot answer.

    A BaseException ON PURPOSE: the worker, the Registry and the Commander all
    fold `Exception` into their own static failure codes, and a divergence
    folded into TASK_FAILED would look like an ordinary outcome.
    """

    def __init__(self, code: str, **detail: Any) -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code} {json.dumps(detail, sort_keys=True, default=str)}")


def _completion(entry: Mapping[str, Any]) -> Any:
    """An OpenAI-compatible completion object, so the REAL classifier reads it."""
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=entry["content"]),
        finish_reason=entry.get("finish_reason") or "stop")])


class ReplayProviderAdapter:
    """Answers each provider call with its recorded completion, or diverges."""

    def __init__(self, manifest: Mapping[str, Any]) -> None:
        self._lock = threading.Lock()
        self._commander = deque(copy.deepcopy(manifest.get("commander", [])))
        self._verifier = deque(copy.deepcopy(manifest.get("verifier", [])))
        self._workers = {task_id: deque(copy.deepcopy(attempts))
                         for task_id, attempts in (manifest.get("workers") or {}).items()}
        self.served: list[dict[str, Any]] = []
        self._counts: dict[str, int] = {}

    def chat(self, request: Mapping[str, Any], *, client: Any = None, agent: str = "",
             phase: str = "", **_kwargs: Any) -> Any:
        role, _, task_id = str(agent).partition(":")
        with self._lock:
            key = f"{role}:{task_id}:{phase}"
            index = self._counts.get(key, 0)
            self._counts[key] = index + 1
            if role == "commander":
                queue = self._commander
            elif role == "verifier":
                queue = self._verifier
            elif role == "worker":
                queue = self._workers.get(task_id, deque())
            else:
                queue = deque()
            # The Commander's completions are ONE ordered list across its two
            # phases: a planning call when the recording's next Commander
            # completion is a replan decision is a call production never made.
            if not queue or queue[0].get("phase", phase) != phase:
                raise ReplayDivergence("UNRECORDED_MODEL_CALL", role=role, phase=phase,
                                       task_id=task_id or None, index=index)
            entry = queue.popleft()
            self.served.append({"role": role, "phase": phase, "task_id": task_id or None})
            return _completion(entry)

    def unconsumed(self) -> dict[str, int]:
        left = {"commander": len(self._commander), "verifier": len(self._verifier)}
        for task_id, queue in sorted(self._workers.items()):
            if queue:
                left[f"worker:{task_id}"] = len(queue)
        return {key: value for key, value in left.items() if value}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ReplayGovernmentTool(GovernmentVehicleTool):
    """The real tool's class and descriptor; `execute` answers from the recording."""

    def __init__(self, recorded: list[Mapping[str, Any]], real: GovernmentVehicleTool | None,
                 **kwargs: Any) -> None:
        super().__init__(None, **kwargs)
        self._recorded = [dict(item) for item in recorded]
        self._consumed: set[int] = set()
        self._real = real
        self._lock = threading.Lock()
        self.pending = threading.local()
        self.cross_checked = 0

    def execute(self, context: ToolContext, operation: str,
                payload: Mapping[str, Any]) -> Mapping[str, Any]:
        wanted = _canonical(dict(payload))
        with self._lock:
            index = next((position for position, item in enumerate(self._recorded)
                          if position not in self._consumed
                          and item["operation"] == operation
                          and _canonical(item["arguments"]) == wanted), None)
            if index is None:
                raise ReplayDivergence("UNRECORDED_TOOL_CALL", operation=operation,
                                       arguments=dict(payload))
            self._consumed.add(index)
        entry = self._recorded[index]
        self.pending.entry = entry
        if self._real is not None:
            self._cross_check(context, operation, payload, entry["result"])
        return copy.deepcopy(entry["result"])

    def _cross_check(self, context: ToolContext, operation: str,
                     payload: Mapping[str, Any], recorded: Mapping[str, Any]) -> None:
        """The recorded answer must be what the real tool reads from the recorded rows."""
        real = self._real.execute(context, operation, payload)
        ids = lambda result: sorted(str(item["upstream_record_id"])
                                    for item in result.get("variants") or [])
        if operation == "resolve_variant":
            summary = lambda result: {"resolved": result["resolved"],
                                      "ambiguous": result["ambiguous"],
                                      "match_count": result["match_count"],
                                      "rows": ids(result),
                                      "source_record": result.get("source_record")}
            if summary(real) != summary(recorded):
                raise ReplayDivergence("TOOL_RESULT_CROSS_CHECK_FAILED", operation=operation,
                                       recorded=summary(recorded), real=summary(real))
        elif "variants" in recorded:
            if not set(ids(recorded)) <= set(ids(real)):
                raise ReplayDivergence("TOOL_RESULT_CROSS_CHECK_FAILED", operation=operation,
                                       recorded=ids(recorded), real=ids(real))
        self.cross_checked += 1

    def unconsumed(self) -> list[str]:
        return [f"{item['task_id']}/{item['call_id']}"
                for position, item in enumerate(self._recorded)
                if position not in self._consumed]


class WholeSnapshotSpy:
    """The catalog repository, refusing every whole-snapshot read."""

    WHOLE_SNAPSHOT_READS = ("list_catalog_raw_records", "list_catalog_candidates")

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.whole_snapshot_reads: list[str] = []
        self.catalog_reads: list[str] = []

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name in self.WHOLE_SNAPSHOT_READS:
            def refused(*_args, **_kwargs):
                self.whole_snapshot_reads.append(name)
                raise ReplayDivergence("WHOLE_SNAPSHOT_READ", method=name)
            return refused
        if name.startswith("catalog_") and callable(attribute):
            def counted(*args, **kwargs):
                self.catalog_reads.append(name)
                return attribute(*args, **kwargs)
            return counted
        return attribute


#: The guarded evidence RPCs and grounding reads (see
#: tests/swarm_v2_production_fixture.py): they go to the R3/R5 guarded store.
EVIDENCE_METHODS = frozenset({
    "create_tool_usage", "create_source", "record_evidence_fragment", "create_claim",
    "create_conflict", "record_claim_verdict", "record_conflict_resolution",
    "patch_run_blackboard_evidence", "list_sources_for_ids",
    "list_evidence_fragments_for_sources", "list_structured_facts_for_sources"})


class RunRepository:
    def __init__(self, catalog: Any, evidence: Any) -> None:
        self.catalog, self.evidence = catalog, evidence

    def __getattr__(self, name: str):
        return getattr(self.evidence if name in EVIDENCE_METHODS else self.catalog, name)


class ReplayBudget:
    """The worker's `remaining()` over the reviewed first-run envelope."""

    def __init__(self, limits: Any) -> None:
        self._limits = limits
        self._budget = reviewed_first_run_policy().budget_config()
        self.model_calls = self.agent_steps = self.tool_calls = self.retries = 0
        self.tasks = 0
        self.retry_reasons: list[list[str]] = []
        self._lock = threading.Lock()

    def agent_step(self, _agent: str, _phase: str) -> None:
        with self._lock:
            self.agent_steps += 1

    def tool_call(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def retry(self, agent: str, phase: str, reason: str) -> None:
        with self._lock:
            self.retries += 1
            self.retry_reasons.append([agent, phase, reason])

    def consumed(self, kind: str) -> None:
        if kind in {"task_completed", "task_failed"}:
            with self._lock:
                self.tasks += 1

    def remaining(self) -> RemainingBudget:
        return RemainingBudget(
            cost_units=self._limits.max_cost_units,
            tool_calls=max(0, self._limits.max_tool_calls - self.tool_calls),
            tasks=max(0, self._limits.max_tasks - self.tasks),
            model_calls=max(0, self._budget.max_model_calls_per_run - self.model_calls),
            retries=max(0, self._budget.max_retries - self.retries),
            agent_steps=max(0, self._budget.max_agent_steps - self.agent_steps))


# =============================================================================
# The run
# =============================================================================

@dataclass
class ReplayReport:
    terminal: str
    result: dict[str, Any] | None
    divergence: dict[str, Any] | None
    model_calls: int
    served: list[dict[str, Any]]
    retry_reasons: list[list[str]]
    unconsumed_completions: dict[str, int]
    unconsumed_tool_results: list[str]
    whole_snapshot_reads: list[str]
    catalog_reads: list[str]
    cross_checked: int
    identity_mismatches: list[str] = field(default_factory=list)

    def outcome(self) -> dict[str, Any]:
        """The comparable outcome, in the shape a manifest's `expected` states."""
        out: dict[str, Any] = {"terminal": self.terminal, "model_calls": self.model_calls,
                               "retry_reasons": self.retry_reasons,
                               "unconsumed": {
                                   "completions": sum(self.unconsumed_completions.values()),
                                   "tool_results": len(self.unconsumed_tool_results)}}
        if self.result is not None:
            summary = self.result.get("summary") or {}
            out.update({
                "status": self.result.get("status"),
                "result_kind": self.result.get("result_kind"),
                "vehicles": len(self.result.get("vehicles") or []),
                "unresolved_groups": len(self.result.get("unresolved_groups") or []),
                "needs_review": sorted(
                    ({"task_id": item.get("task_id"), "code": item.get("code")}
                     for item in self.result.get("needs_review") or []),
                    key=lambda item: (str(item["code"]), str(item["task_id"]))),
                "summary": {key: summary[key] for key in sorted(summary)},
            })
        if self.divergence is not None:
            out["failure" if self.terminal == "failed" else "unrecorded_call"] = self.divergence
        return out


def _seed_snapshot(rows: list[Mapping[str, Any]]) -> tuple[MemoryRepository, str | None]:
    """The recorded register rows, landed through the REAL ingestion path."""
    repository = MemoryRepository()
    if not rows:
        return repository, None
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "replay", "Replay", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))["id"]
    plan = seed_prepared_plan(repository, user_id=user, conversation_id=conversation,
                              units=("toyota",), max_items=max(1, len(rows)),
                              batch_size=max(1, len(rows)),
                              records=[dict(row) for row in rows])
    return repository, plan["snapshot_key"]


def _work_context(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The Commander context production built, from the recorded preparation."""
    record = manifest["preparation"]
    preparation = GovernmentPreparation(
        snapshot_key=record["snapshot_key"], snapshot_id=str(record.get("snapshot_id") or ""),
        resource_id=record["resource_id"], upstream_version=record["upstream_version"],
        upstream_version_kind=record["upstream_version_kind"],
        queue=tuple(GovernmentWorkItem.from_record(item) for item in record["queue"]),
        total_candidates=int(record["total_candidates"]), bounded=bool(record["bounded"]),
        resumed=False)
    return preparation.work_context({})


def replay(manifest: Mapping[str, Any]) -> ReplayReport:
    """Run the whole engine against one recorded run. Never raises on divergence."""
    models = manifest["models"]
    catalog, snapshot_key = _seed_snapshot(list(manifest.get("snapshot_rows", [])))
    spy = WholeSnapshotSpy(catalog)
    run_id = str(uuid4())
    lease = WorkerLease(UUID(run_id), "replay-worker", 1, "replay-lease")
    from test_swarm_v2_r5_vehicle_proof import ProofRepository

    evidence_store = ProofRepository(lease)
    combined = RunRepository(spy, evidence_store)

    real_tool = (GovernmentVehicleTool(spy, snapshot_key=snapshot_key)
                 if snapshot_key is not None else None)
    replay_tool = ReplayGovernmentTool(manifest.get("tool_results", []), real_tool)
    tools = ToolRegistry([replay_tool])
    limits = reviewed_first_run_policy().plan_limits()
    budget = ReplayBudget(limits)
    provider = ReplayProviderAdapter(manifest)

    class CountingProvider:
        def chat(self, request, **kwargs):
            budget.model_calls += 1
            return provider.chat(request, **kwargs)

    gateway = ModelGateway(guarded_client_factory=lambda _key, _url: None,
                           adapter=CountingProvider(), api_key="", base_url="",
                           tool_descriptors=tools.descriptors(),
                           agent_step_callback=budget.agent_step, plan_limits=limits)
    commander = Commander(client=gateway,
                          resolver=CommanderModelResolver((models["commander"],),
                                                          {models["commander"]}),
                          validator=PlanValidator(allowed_tools=tools.descriptors(),
                                                  limits=limits),
                          retry_callback=budget.retry)
    board = EvidenceBoard(combined, lease)
    evidence_sink = RegisteredOperationEvidenceSink(
        TrustedEvidenceAcquisition(board=board, mappers=production_evidence_mappers()))
    identity_mismatches: list[str] = []

    def checked_sink(record: Any) -> None:
        # The recorded entry the tool answered with must be THIS call's own.
        entry = getattr(replay_tool.pending, "entry", None)
        replay_tool.pending.entry = None
        if entry is None or (entry["task_id"], entry["call_id"]) != (record.task_id,
                                                                      record.call_id):
            identity_mismatches.append(f"{record.task_id}/{record.call_id}")
            raise ReplayDivergence("TOOL_CALL_IDENTITY_MISMATCH", task_id=record.task_id,
                                   call_id=record.call_id)
        evidence_sink(record)

    context = ToolContext(scopes=frozenset({GOVERNMENT_TOOL_SCOPE}))

    def evidence_loader(results):
        completed = {str(task_id) for task_id, result in dict(results).items()
                     if getattr(result, "status", None) == "completed"}
        return [EvidenceReference.model_validate(item)
                for item in board.references(task_ids=completed)]

    engine = SwarmV2Engine(
        commander=commander,
        executor=BoundedTaskExecutor(worker_factory=lambda: GenericWorker(
            gateway=gateway, tools=tools, model=models["worker"], tool_context=context,
            retry_callback=budget.retry, tool_result_sink=checked_sink,
            tool_call_callback=budget.tool_call),
            # ONE logical worker: the tool recording is matched in recorded
            # order, and the run's outcome does not depend on interleaving.
            max_active_workers=1),
        verifier=Verifier(gateway=gateway, model=models["commander"],
                          resolver=RepositoryEvidenceResolver(combined, run_id=UUID(run_id)),
                          retry_callback=budget.retry),
        builder=FinalBuilder(), evidence_loader=evidence_loader,
        remaining_budget=budget.remaining, ledger_sink=budget.consumed,
        verdict_sink=board.record_verification_verdict,
        resolution_sink=board.record_conflict_resolution)
    payload = {"id": run_id, "input": {
        "objective": manifest.get("objective", ""), "commander_model": models["commander"],
        "context": {"government_work": _work_context(manifest)}}}
    result: dict[str, Any] | None = None
    divergence: dict[str, Any] | None = None
    try:
        result = engine.run(payload)
        terminal = "result"
    except (CommanderPlanFailure, SwarmExecutionFailure) as exc:
        # The engine's own static, terminal refusal of the run: a recorded
        # run that FAILED replays to that failure, named by its code.
        terminal = "failed"
        divergence = {"code": exc.code}
    except ReplayDivergence as exc:
        terminal = "unrecorded_call" if exc.code == "UNRECORDED_MODEL_CALL" else "divergence"
        divergence = {"code": exc.code, **exc.detail} if exc.code != "UNRECORDED_MODEL_CALL" \
            else dict(exc.detail)
    return ReplayReport(
        terminal=terminal, result=result, divergence=divergence,
        model_calls=len(provider.served), served=list(provider.served),
        retry_reasons=list(budget.retry_reasons),
        unconsumed_completions=provider.unconsumed(),
        unconsumed_tool_results=replay_tool.unconsumed(),
        whole_snapshot_reads=list(spy.whole_snapshot_reads),
        catalog_reads=list(spy.catalog_reads), cross_checked=replay_tool.cross_checked,
        identity_mismatches=identity_mismatches)


def fixture_findings(manifest: Mapping[str, Any]) -> list[str]:
    """Format problems plus sanitization findings: empty for a committable fixture."""
    return [*manifest_problems(manifest), *sanitization_findings(manifest)]


__all__ = ["GOVERNMENT_TOOL_NAME", "MANIFEST_NAME", "REPLAY_ROOT", "ReplayDivergence",
           "ReplayGovernmentTool", "ReplayProviderAdapter", "ReplayReport",
           "WholeSnapshotSpy", "artifact_ids", "fixture_dirs", "fixture_findings",
           "load_manifest", "manifest_problems", "provenance_summary",
           "referenced_record_ids", "replay"]
