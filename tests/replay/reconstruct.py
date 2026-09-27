"""How the four RECONSTRUCTED replay fixtures were written -- reproducibly.

    python tests/replay/reconstruct.py            # rewrite the four manifests
    python tests/replay/reconstruct.py --check    # exit 1 if any differs

None of the raw provider outputs of runs 6825eb96, 280fc9e5, c4b8bb54 and
aa63369b is available offline, and this repository reads no production state.
Every artifact below is therefore RECONSTRUCTED, and each manifest says so per
artifact, naming the durable fact it was written from. Nothing here is labelled
"captured": a captured fixture comes only from `scripts/export_replay_capture.py`
over a run executed with MILO_CAPTURE_REPLAY on.

What is durable fact, and what is a stand-in
--------------------------------------------

* The SHAPES that broke production are durable facts recorded in this
  repository (the tests that pinned each fix): 6825eb96's ten-task plan with
  ``completion.required_outputs = ["evidence_record"]`` over a five-property
  output schema; 280fc9e5's task output schemas with an items-less array and an
  open object (11 tasks); c4b8bb54's eleven completed tasks followed by ONE
  replan decision the contract refused; aa63369b's outcome as stated for this
  PR (8 vehicles, 2 ambiguous groups, a get_variants-only ``register_meta``
  task that declared evidence).
* The register rows are STAND-INS. 37350/37439 (LIMITED, TZNA55L-GKZSZA) and
  37309/37345 (TRAILHUNTER, TZNH55L GKVSZA) are real duplicate identities; the
  other 4RUNNER codes and trims are the fixture values of
  tests/replay_6825eb96.py. Every 4RUNNER row is the committed R5 register row
  38683 with its identity fields replaced; the c4b8bb54 / 280fc9e5 rows are real
  RAV4 rows of the committed R5 capture, standing in for rows not available
  offline.
* Every tool result is what the REAL Government tool answers over those rows
  (candidate ids and the activation timestamp normalized), so the replay's
  cross-check holds by construction and a data-layer drift breaks it.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from backend.replay_capture import RECONSTRUCTED, REPLAY_FORMAT  # noqa: E402
from backend.testing.work_scope_seed import committed_records  # noqa: E402
from backend.tools import ToolContext  # noqa: E402
from backend.tools.government_vehicle import GOVERNMENT_TOOL_NAME, GovernmentVehicleTool  # noqa: E402

from replay_harness import MANIFEST_NAME, REPLAY_ROOT, _seed_snapshot  # noqa: E402

TOYOTA = "טויוטה"
MODELS = {"commander": "kimi-k3", "worker": "kimi-k2.6"}
FIXED_ACTIVATED_AT = "2026-09-01T00:00:00+00:00"
TEMPLATE_ROW_ID = 38683
OBJECTIVE = "Resolve the prepared Mapping Plan batch against the Israeli vehicle register."

#: The 4RUNNER 2026 stand-in identities (tests/replay_6825eb96.py fixture values).
FOUR_RUNNER = {
    "37254": ("TRN285L-GKTSKA", "SR5"),
    "37291": ("TRN285L-GKTLKA", "LIMITED"),
    "37293": ("TRN285L-GKTXKA", "TRD PRO"),
    "37096": ("TRN285L-GKTHKA", "TRAILHUNTER"),
    "37098": ("TRN285L-GKTPKA", "PLATINUM"),
    "37425": ("TRN285L-GKTOKA", "TRD OFF-ROAD"),
    "37316": ("TRN285L-GKTMKA", "SR5 PREMIUM"),
    "37417": ("TRN285L-GKTTKA", "TRD SPORT"),
}
#: Real duplicate identities of the production Toyota snapshot.
LIMITED_PAIR = ("37350", "37439", "TZNA55L-GKZSZA", "LIMITED")
TRAILHUNTER_PAIR = ("37309", "37345", "TZNH55L GKVSZA", "TRAILHUNTER")
PLACEHOLDER = ("37363", "11111", "11111111", "SE")

STAND_IN_4RUNNER = ("stand-in register row: committed R5 row 38683 with its identity fields "
                    "(_id, kinuy_mishari, degem_nm, ramat_gimur, shnat_yitzur) replaced; "
                    "the run's own row is not available offline")
REAL_DUPLICATE = ("identity from durable state (a real duplicate register identity of the "
                  "production Toyota snapshot); other fields from committed R5 row 38683")
STAND_IN_RAV4 = ("stand-in register row: a real RAV4 row of the committed R5 capture, "
                 "standing in for a row of this run that is not available offline")
TOOL_SOURCE = ("the real GovernmentVehicleTool's answer over this fixture's snapshot rows "
               "(candidate ids and activated_at normalized); not the run's recorded result")


def _template() -> dict[str, Any]:
    return next(row for row in committed_records() if row["_id"] == TEMPLATE_ROW_ID)


def four_runner_row(record_id: str, code: str, trim: str, *, model: str = "4RUNNER") -> dict:
    row = copy.deepcopy(_template())
    row.update({"_id": int(record_id), "kinuy_mishari": model, "degem_nm": code,
                "ramat_gimur": trim, "shnat_yitzur": 2026})
    row.pop("rank", None)
    return row


def rav4_rows(count: int) -> list[dict]:
    """`count` committed RAV4 rows with pairwise-distinct identities."""
    chosen, seen = [], set()
    for row in committed_records():
        identity = (row["kinuy_mishari"], row["shnat_yitzur"], row["degem_nm"],
                    row["ramat_gimur"])
        if identity in seen or not row.get("ramat_gimur") or not row.get("degem_nm"):
            continue
        seen.add(identity)
        clean = dict(row)
        clean.pop("rank", None)
        chosen.append(clean)
        if len(chosen) == count:
            return chosen
    raise AssertionError("not enough distinct committed rows")


# --- the register, as the real tool reads it ---------------------------------

class Register:
    """The stand-in rows landed through the real ingestion, read by the real tool."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.repository, self.snapshot_key = _seed_snapshot(rows)
        self.tool = GovernmentVehicleTool(self.repository, snapshot_key=self.snapshot_key)
        by_raw = {row["id"]: row for row in self.repository.catalog_raw_records.values()}
        self.candidates = {}
        for candidate in self.repository.catalog_candidates.values():
            record = by_raw[candidate["raw_record_id"]]["upstream_record_id"]
            self.candidates[str(record)] = candidate
        snapshot = next(iter(self.repository.catalog_snapshots.values()))
        self.snapshot = snapshot

    def queue_item(self, record_id: str) -> dict[str, Any]:
        candidate = self.candidates[str(record_id)]
        return {"candidate_key": candidate["candidate_key"],
                "candidate_id": f"cand-{record_id}",
                "manufacturer": candidate["manufacturer"],
                "commercial_model": candidate["commercial_model"],
                "model_year_start": candidate["model_year_start"],
                "model_year_end": candidate["model_year_end"],
                "official_model_code": candidate.get("official_model_code"),
                "trim": candidate.get("trim")}

    def preparation(self, record_ids: list[str]) -> dict[str, Any]:
        queue = [self.queue_item(record_id) for record_id in record_ids]
        return {"snapshot_key": self.snapshot_key,
                "resource_id": self.snapshot["resource_id"],
                "upstream_version": self.snapshot.get("upstream_version") or "",
                "upstream_version_kind": self.snapshot.get("upstream_version_kind") or "",
                "queue": queue, "total_candidates": len(queue), "bounded": False}

    def answer(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(self.tool.execute(ToolContext(), operation, arguments))
        for variant in result.get("variants") or []:
            variant["candidate_id"] = f"cand-{variant['upstream_record_id']}"
        if "provenance" in result:
            result["provenance"]["activated_at"] = FIXED_ACTIVATED_AT
        return result


# --- plan and completion builders ---------------------------------------------

def resolve_arguments(item: dict[str, Any]) -> dict[str, Any]:
    arguments = {"manufacturer": item["manufacturer"],
                 "commercial_model": item["commercial_model"],
                 "model_year": item["model_year_start"]}
    if item.get("trim"):
        arguments["trim"] = item["trim"]
    if item.get("official_model_code"):
        arguments["official_model_code"] = item["official_model_code"]
    return arguments


def call(operation: str, arguments: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"call_id": call_id, "name": GOVERNMENT_TOOL_NAME, "operation": operation,
            "arguments": arguments, "dependency_bindings": []}


def task(task_id: str, goal: str, tools: list[dict], *, output_schema: dict,
         evidence: dict, completion: dict, cost: int = 5) -> dict[str, Any]:
    return {"task_id": task_id, "goal": goal,
            "scope": "Israeli vehicle register, the run's pinned snapshot",
            "dependencies": [], "tools": tools, "output_schema": copy.deepcopy(output_schema),
            "evidence": dict(evidence), "priority": 50, "recursion_depth": 0,
            "estimated_cost_units": cost, "completion": dict(completion)}


def plan(tasks: list[dict], *, objective: str) -> dict[str, Any]:
    return {"version": "1", "objective": objective, "graph": {"tasks": tasks},
            "assignments": [{"task_id": item["task_id"], "worker_role": "register reader",
                             "context_task_ids": []} for item in tasks],
            "max_replans": 1,
            "estimated_cost_units": sum(item["estimated_cost_units"] for item in tasks)}


def text(value: Any) -> str:
    """Inert provider text: the JSON document as the model would have written it."""
    return json.dumps(value, ensure_ascii=False)


def completion(phase: str | None, value: Any, *, raw: bool = False) -> dict[str, Any]:
    entry = {"content": value if raw else text(value), "finish_reason": "stop"}
    if phase is not None:
        entry = {"phase": phase, **entry}
    return entry


REQUEST_VERIFICATION = {"decision": "REQUEST_VERIFICATION", "plan": None,
                        "reason": "every candidate in the batch was answered by the register"}

#: The per-candidate output schema of the post-PR-W plans (c4b8bb54, aa63369b).
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "One sentence, no values."},
        "register_answer": {"type": "string",
                            "enum": ["resolved", "ambiguous", "not_found"]},
        "match_count": {"type": "integer", "minimum": 0},
    },
    "required": ["summary", "register_answer", "match_count"],
    "additionalProperties": False,
}


def answer_output(result: dict[str, Any]) -> dict[str, Any]:
    kind = "resolved" if result["resolved"] else (
        "ambiguous" if result["ambiguous"] else "not_found")
    return {"summary": "the register answered this candidate", "register_answer": kind,
            "match_count": result["match_count"]}


def tool_result(task_id: str, operation: str, arguments: dict, result: dict,
                call_id: str = "c1") -> dict[str, Any]:
    return {"task_id": task_id, "call_id": call_id, "tool": GOVERNMENT_TOOL_NAME,
            "operation": operation, "arguments": arguments, "result": result}


def row_provenance(rows: list[dict], sources: dict[str, str]) -> dict[str, dict]:
    return {f"snapshot_rows.{row['_id']}": {"kind": RECONSTRUCTED,
                                            "source": sources[str(row["_id"])]}
            for row in rows}


def manifest(*, run_id: str, description: str, register: Register, preparation: dict,
             commander: list[dict], workers: dict[str, list[dict]], tool_results: list[dict],
             provenance: dict[str, dict], expected: dict[str, Any]) -> dict[str, Any]:
    referenced = {str(variant["upstream_record_id"])
                  for item in tool_results for variant in item["result"].get("variants", [])}
    rows = sorted((row for row in register.rows if str(row["_id"]) in referenced),
                  key=lambda row: row["_id"])
    return {"format": REPLAY_FORMAT, "run_id": run_id, "description": description,
            "models": dict(MODELS), "objective": OBJECTIVE, "preparation": preparation,
            "commander": commander, "workers": workers, "verifier": [],
            "tool_results": tool_results, "snapshot_rows": rows,
            "provenance": dict(sorted(provenance.items())), "expected": expected}


def _resolve_tasks(register: Register, record_ids: list[str], task_ids: list[str], *,
                   output_schema: dict, evidence: dict, completion_spec: dict,
                   goal: str, worker_output) -> tuple[list[dict], list[dict], dict]:
    tasks, results, workers = [], [], {}
    for task_id, record_id in zip(task_ids, record_ids):
        item = register.queue_item(record_id)
        arguments = resolve_arguments(item)
        answer = register.answer("resolve_variant", arguments)
        tasks.append(task(task_id, goal.format(task_id=task_id),
                          [call("resolve_variant", arguments)], output_schema=output_schema,
                          evidence=evidence, completion=completion_spec))
        results.append(tool_result(task_id, "resolve_variant", arguments, answer))
        workers[task_id] = [completion(None, worker_output(answer, item))]
    return tasks, results, workers


# =============================================================================
# 6825eb96
# =============================================================================

#: The output schema every 6825eb96 task declared
#: (tests/test_swarm_v2_plan_required_outputs.py), with the `outcome` enum of the
#: run's plan (tests/test_swarm_v2_output_schema_nested.py).
SCHEMA_6825EB96 = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string",
                    "enum": ["resolved", "unresolved_ambiguous", "unresolved_not_found"]},
        "resolved": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "match_count": {"type": "integer"},
        "candidate_id": {"type": "string"},
    },
    "required": ["outcome", "resolved", "ambiguous", "match_count", "candidate_id"],
    "additionalProperties": False,
}


def run_6825eb96() -> dict[str, Any]:
    rows = [four_runner_row(PLACEHOLDER[0], PLACEHOLDER[2], PLACEHOLDER[3],
                            model=PLACEHOLDER[1])]
    rows += [four_runner_row(record_id, *FOUR_RUNNER[record_id])
             for record_id in ("37254", "37291", "37293", "37096", "37098", "37425", "37316")]
    rows += [four_runner_row(record_id, LIMITED_PAIR[2], LIMITED_PAIR[3])
             for record_id in LIMITED_PAIR[:2]]
    register = Register(rows)
    order = ["37363", "37254", "37291", "37350", "37439", "37293", "37096", "37098",
             "37425", "37316"]
    task_ids = [f"t{index:02d}" for index in range(1, 11)]

    def output(result, item):
        return {"outcome": "resolved" if result["resolved"] else "unresolved_ambiguous",
                "resolved": result["resolved"], "ambiguous": result["ambiguous"],
                "match_count": result["match_count"], "candidate_id": item["candidate_id"]}

    tasks, results, workers = _resolve_tasks(
        register, order, task_ids, output_schema=SCHEMA_6825EB96,
        evidence={"minimum_sources": 1, "required_fields": ["outcome"], "min_confidence": 0.5},
        completion_spec={"required_outputs": ["evidence_record"], "evidence_satisfied": True,
                         "allow_partial": True},
        goal="verify register candidate {task_id}", worker_output=output)
    commander = [completion("planning", plan(tasks, objective=OBJECTIVE)),
                 completion("replanning", REQUEST_VERIFICATION)]
    provenance = {
        "preparation": {"kind": RECONSTRUCTED, "source": (
            "the run's ten queued register rows in task order, including placeholder 37363 "
            "(prepared before PR-U); candidate keys from the stand-in rows")},
        "commander[0]": {"kind": RECONSTRUCTED, "source": (
            "the approved revision-1 plan as durable state describes it: ten resolve_variant "
            "tasks, output_schema outcome/resolved/ambiguous/match_count/candidate_id, "
            "completion.required_outputs ['evidence_record'] on every task "
            "(tests/test_swarm_v2_plan_required_outputs.py)")},
        "commander[1]": {"kind": RECONSTRUCTED, "source": (
            "the run went on to verification and partial_success, so its replan decision is "
            "written as REQUEST_VERIFICATION; the decision text is not durable")},
    }
    for task_id, record_id in zip(task_ids, order):
        provenance[f"workers.{task_id}[0]"] = {"kind": RECONSTRUCTED, "source": (
            "the completed task output persisted in the run's checkpoint shape, rebuilt from "
            "the register answer; the raw completion is not durable")}
        provenance[f"tool_results.{task_id}/c1"] = {"kind": RECONSTRUCTED, "source": TOOL_SOURCE}
    sources = {"37363": ("identity from durable state (the placeholder row the run queued: "
                         "kinuy_mishari 11111, degem_nm 11111111, SE, 2026); other fields "
                         "from committed R5 row 38683"),
               **{record_id: STAND_IN_4RUNNER for record_id in FOUR_RUNNER},
               **{record_id: REAL_DUPLICATE for record_id in LIMITED_PAIR[:2]}}
    provenance.update(row_provenance(rows, sources))
    expected = {
        "terminal": "unrecorded_call", "model_calls": 1,
        "retry_reasons": [["commander", "planning", "REQUIRED_OUTPUT_NOT_IN_SCHEMA"]],
        "unconsumed": {"completions": 11, "tool_results": 10},
        "unrecorded_call": {"role": "commander", "phase": "planning", "task_id": None,
                            "index": 1},
        "note": ("On current main the recorded plan is refused at plan validation "
                 "(REQUIRED_OUTPUT_NOT_IN_SCHEMA) and the Commander's ONE repair is requested; "
                 "production never produced that repair, so the replay stops there, before "
                 "any tool call or worker completion is paid for."),
    }
    return manifest(run_id="6825eb96-1550-4464-8e8a-ecb1eca3e967", description=(
        "Gate 0, partial_success in production: ten register tasks, 8 resolved rows "
        "(one the placeholder 37363), one ambiguous identity asked twice (t04, t05)."),
        register=register, preparation=register.preparation(order), commander=commander,
        workers=workers, tool_results=results, provenance=provenance, expected=expected)


# =============================================================================
# 280fc9e5
# =============================================================================

#: The t02 output_schema of run 280fc9e5, revision 1
#: (tests/test_swarm_v2_output_schema_nested.py). 11/11 tasks failed on it.
SCHEMA_280FC9E5 = {
    "type": "object",
    "properties": {
        "variants": {"type": "array"},
        "provenance": {"type": "object"},
        "candidate_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["variants", "provenance", "candidate_ids"],
    "additionalProperties": False,
}


def run_280fc9e5() -> dict[str, Any]:
    rows = rav4_rows(11)
    register = Register(rows)
    order = [str(row["_id"]) for row in rows]
    task_ids = [f"t{index:02d}" for index in range(1, 12)]
    tasks = [task(task_id, f"resolve register variants {task_id}",
                  [call("resolve_variant", resolve_arguments(register.queue_item(record)))],
                  output_schema=SCHEMA_280FC9E5,
                  evidence={"minimum_sources": 0, "required_fields": [],
                            "min_confidence": 0.5},
                  completion={"required_outputs": ["candidate_ids"],
                              "evidence_satisfied": True, "allow_partial": True})
             for task_id, record in zip(task_ids, order)]
    commander = [completion("planning", plan(tasks, objective=OBJECTIVE))]
    provenance = {
        "preparation": {"kind": RECONSTRUCTED, "source": (
            "eleven queued items (the run had 11 tasks); identities are stand-ins from real "
            "RAV4 rows of the committed R5 capture")},
        "commander[0]": {"kind": RECONSTRUCTED, "source": (
            "the approved revision-1 plan as durable state describes it: 11 tasks whose "
            "output_schema declares 'variants': {'type': 'array'} (no items) and "
            "'provenance': {'type': 'object'} (tests/test_swarm_v2_output_schema_nested.py)")},
    }
    expected = {
        "terminal": "unrecorded_call", "model_calls": 1,
        "retry_reasons": [["commander", "planning", "OUTPUT_SCHEMA_NESTED_INVALID"]],
        "unconsumed": {"completions": 0, "tool_results": 0},
        "unrecorded_call": {"role": "commander", "phase": "planning", "task_id": None,
                            "index": 1},
        "note": ("On current main the recorded plan fails at plan validation "
                 "(OUTPUT_SCHEMA_NESTED_INVALID) instead of failing 11/11 tasks at run time; "
                 "the one repair it asks for was never produced in production. No worker "
                 "completion or tool result is recorded: the run's failed tasks left none."),
    }
    return manifest(run_id="280fc9e5", description=(
        "11/11 tasks failed TASK_FAILED in production after successful model calls: the "
        "runtime validator raised on an items-less array schema the firewall had approved."),
        register=register, preparation=register.preparation(order), commander=commander,
        workers={}, tool_results=[], provenance=provenance, expected=expected)


# =============================================================================
# c4b8bb54
# =============================================================================

#: A decision the CommanderDecision contract refuses (an extra top-level field).
#: The refused decision itself was never durable; any refused decision
#: reproduces the recorded status, COMMANDER_DECISION_INVALID.
INVALID_DECISION = {"decision": "FINISH", "plan": None,
                    "reason": "all eleven candidates were answered by the register",
                    "next_steps": "none"}


def run_c4b8bb54() -> dict[str, Any]:
    rows = rav4_rows(11)
    register = Register(rows)
    order = [str(row["_id"]) for row in rows]
    task_ids = [f"t{index:02d}" for index in range(1, 12)]
    tasks, results, workers = _resolve_tasks(
        register, order, task_ids, output_schema=ANSWER_SCHEMA,
        evidence={"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
        completion_spec={"required_outputs": ["summary"], "evidence_satisfied": True,
                         "allow_partial": False},
        goal="resolve register candidate {task_id}",
        worker_output=lambda result, _item: answer_output(result))
    commander = [completion("planning", plan(tasks, objective=OBJECTIVE)),
                 completion("replanning", INVALID_DECISION)]
    provenance = {
        "preparation": {"kind": RECONSTRUCTED, "source": (
            "eleven queued items (the run completed 11/11 tasks); identities are stand-ins "
            "from real RAV4 rows of the committed R5 capture")},
        "commander[0]": {"kind": RECONSTRUCTED, "source": (
            "an eleven-task resolve_variant plan the current firewall approves; the run's "
            "own plan text is not durable")},
        "commander[1]": {"kind": RECONSTRUCTED, "source": (
            "the run failed COMMANDER_DECISION_INVALID on its one post-execution replan; the "
            "refused text was never durable, so a decision the contract refuses (an extra "
            "top-level field) stands in for it")},
    }
    for task_id in task_ids:
        provenance[f"workers.{task_id}[0]"] = {"kind": RECONSTRUCTED, "source": (
            "a completed task output rebuilt from the register answer; the raw completion "
            "is not durable")}
        provenance[f"tool_results.{task_id}/c1"] = {"kind": RECONSTRUCTED, "source": TOOL_SOURCE}
    provenance.update(row_provenance(rows, {record: STAND_IN_RAV4 for record in order}))
    expected = {
        "terminal": "result", "model_calls": 13, "retry_reasons": [],
        "status": "partial_success", "result_kind": "partial_result",
        "vehicles": 11, "unresolved_groups": 0,
        "needs_review": [{"task_id": "commander_replan", "code": "COMMANDER_REPLAN_REJECTED"}],
        "summary": {"unresolved_ambiguous": 0, "unresolved_not_found": 0,
                    "vehicles_resolved": 11, "vehicles_with_review": 0},
        "unconsumed": {"completions": 0, "tool_results": 0},
    }
    return manifest(run_id="c4b8bb54", description=(
        "Completed 11/11 tasks with evidence in production, then failed the whole run on "
        "COMMANDER_DECISION_INVALID from the post-execution replan (fixed by PR-X)."),
        register=register, preparation=register.preparation(order), commander=commander,
        workers=workers, tool_results=results, provenance=provenance, expected=expected)


# =============================================================================
# aa63369b
# =============================================================================

META_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "variant_count": {"type": "integer"}},
    "required": ["summary", "variant_count"],
    "additionalProperties": False,
}


def run_aa63369b() -> dict[str, Any]:
    resolved = list(FOUR_RUNNER)
    rows = [four_runner_row(record_id, *FOUR_RUNNER[record_id]) for record_id in resolved]
    rows += [four_runner_row(record_id, LIMITED_PAIR[2], LIMITED_PAIR[3])
             for record_id in LIMITED_PAIR[:2]]
    rows += [four_runner_row(record_id, TRAILHUNTER_PAIR[2], TRAILHUNTER_PAIR[3])
             for record_id in TRAILHUNTER_PAIR[:2]]
    register = Register(rows)
    queue = [*resolved, *LIMITED_PAIR[:2], *TRAILHUNTER_PAIR[:2]]
    # ONE resolve_variant task per distinct identity: eight rows, two duplicate groups.
    asked = [*resolved, LIMITED_PAIR[0], TRAILHUNTER_PAIR[0]]
    task_ids = [f"t{index:02d}" for index in range(1, 11)]
    tasks, results, workers = _resolve_tasks(
        register, asked, task_ids, output_schema=ANSWER_SCHEMA,
        evidence={"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
        completion_spec={"required_outputs": ["summary"], "evidence_satisfied": True,
                         "allow_partial": False},
        goal="resolve register candidate {task_id}",
        worker_output=lambda result, _item: answer_output(result))
    meta_arguments = {"manufacturer": TOYOTA, "commercial_model": "4RUNNER", "model_year": 2026}
    meta_answer = register.answer("get_variants", meta_arguments)
    tasks.append(task("register_meta", "state how many 4RUNNER 2026 variants the register holds",
                      [call("get_variants", meta_arguments)], output_schema=META_SCHEMA,
                      evidence={"minimum_sources": 1, "required_fields": [],
                                "min_confidence": 0.5},
                      completion={"required_outputs": ["variant_count"],
                                  "evidence_satisfied": True, "allow_partial": False}))
    results.append(tool_result("register_meta", "get_variants", meta_arguments, meta_answer))
    workers["register_meta"] = [completion(None, {
        "summary": "the register lists the model year's variants",
        "variant_count": meta_answer["total"]})]
    commander = [completion("planning", plan(tasks, objective=OBJECTIVE)),
                 completion("replanning", REQUEST_VERIFICATION)]
    provenance = {
        "preparation": {"kind": RECONSTRUCTED, "source": (
            "twelve queued rows: eight resolvable stand-ins plus the two real duplicate "
            "identities (37350/37439, 37309/37345)")},
        "commander[0]": {"kind": RECONSTRUCTED, "source": (
            "a plan of the shape the stated outcome requires: one resolve_variant task per "
            "distinct identity (8 resolved, 2 ambiguous groups) and a get_variants-only task "
            "register_meta that declares evidence.minimum_sources 1; the run's own plan text "
            "is not available offline")},
        "commander[1]": {"kind": RECONSTRUCTED, "source": (
            "the run reached verification and partial_success, so its replan decision is "
            "written as REQUEST_VERIFICATION")},
    }
    for task_id in [*task_ids, "register_meta"]:
        provenance[f"workers.{task_id}[0]"] = {"kind": RECONSTRUCTED, "source": (
            "a completed task output rebuilt from the register answer; the raw completion "
            "is not available offline")}
        provenance[f"tool_results.{task_id}/c1"] = {"kind": RECONSTRUCTED, "source": TOOL_SOURCE}
    sources = {**{record: STAND_IN_4RUNNER for record in resolved},
               **{record: REAL_DUPLICATE for record in (*LIMITED_PAIR[:2],
                                                        *TRAILHUNTER_PAIR[:2])}}
    provenance.update(row_provenance(rows, sources))
    expected = {
        "terminal": "result", "model_calls": 13, "retry_reasons": [],
        "status": "partial_success", "result_kind": "partial_result",
        "vehicles": 8, "unresolved_groups": 2,
        "needs_review": [
            {"task_id": "t09", "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"},
            {"task_id": "t10", "code": "CANDIDATE_UNRESOLVED_AMBIGUOUS"},
            {"task_id": "register_meta", "code": "EVIDENCE_REQUIREMENTS_UNMET"},
        ],
        "summary": {"unresolved_ambiguous": 2, "unresolved_not_found": 0,
                    "vehicles_resolved": 8, "vehicles_with_review": 0},
        "unconsumed": {"completions": 0, "tool_results": 0},
    }
    return manifest(run_id="aa63369b", description=(
        "partial_success in production: 8 vehicles, 2 ambiguous groups, and a "
        "get_variants-only register_meta task that declared evidence it could never get "
        "(EVIDENCE_REQUIREMENTS_UNMET)."),
        register=register, preparation=register.preparation(queue), commander=commander,
        workers=workers, tool_results=results, provenance=provenance, expected=expected)


BUILDERS = {"6825eb96": run_6825eb96, "280fc9e5": run_280fc9e5,
            "c4b8bb54": run_c4b8bb54, "aa63369b": run_aa63369b}


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=1, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv
    differ = []
    for name, build in BUILDERS.items():
        target = REPLAY_ROOT / name / MANIFEST_NAME
        rendered = render(build())
        if check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                differ.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target.relative_to(ROOT)}")
    if differ:
        print("reconstructed fixtures differ from reconstruct.py: " + ", ".join(differ))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
