"""PR-Y Y4: the opt-in replay capture and its export.

* OFF (the default and every deployed posture): no recorder is constructed,
  nothing is wrapped, and no checkpoint carries a capture -- zero writes and
  zero overhead.
* ON: the REAL worker records each completion's answer content and
  finish_reason (never reasoning_content) and each Registry-validated tool
  result with its resolved arguments, bounded, on the run's own checkpoints;
  a run that fails at planning keeps its capture in one capture checkpoint.
* The export turns a capture into a replay/1 fixture that replays offline to
  the same outcome the worker reached.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from uuid import UUID

import pytest

import backend.replay_capture as capture_module
import backend.worker.main as worker_main
from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                       GOVERNMENT_READ_FLAG)
from backend.replay_capture import (CAPTURE_ARTIFACT_KEY, CAPTURE_FLAG, CAPTURE_PHASE,
                                    MAX_CAPTURE_BYTES, CapturingAuthority,
                                    CapturingTools, ReplayCaptureRecorder,
                                    capture_enabled, capturing_result_sink,
                                    sanitization_findings)
from backend.testing.work_scope_seed import seed_prepared_plan, start_batch_run
from backend.tools.government_vehicle import GOVERNMENT_TOOL_NAME
from test_government_placeholder_rows import register_rows
from test_swarm_v2_smoke_offline import (USER, FakeKimiCompletions, build_repo, kimi_response,
                                         patch_client, swarm_env)



def _export_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "export_replay_capture.py"
    spec = importlib.util.spec_from_file_location("export_replay_capture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =============================================================================
# the flag
# =============================================================================

@pytest.mark.parametrize("value,expected", [(None, False), ("", False), ("false", False),
                                            ("0", False), ("enabled", False),
                                            ("true", True), ("1", True), ("ON", True)])
def test_capture_is_off_unless_explicitly_on(value, expected):
    env = {} if value is None else {CAPTURE_FLAG: value}
    assert capture_enabled(env) is expected


# =============================================================================
# the recorder
# =============================================================================

def _response(content, finish="stop", reasoning="THINKING-NEVER-CAPTURED"):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=1), id="chatcmpl-1")


def test_the_recorder_keeps_content_and_finish_reason_only():
    recorder = ReplayCaptureRecorder()
    recorder.record_completion(agent="commander", phase="planning", response=_response("{}"))
    recorder.record_completion(agent="worker:t01", phase="execute",
                               response=_response('{"a"', "length"))
    recorder.record_completion(agent="worker:t01", phase="execute", response=_response("{}"))
    artifact = recorder.artifact()
    assert artifact["completions"] == [
        {"role": "commander", "phase": "planning", "content": "{}", "finish_reason": "stop",
         "attempt": 1},
        {"role": "worker", "phase": "execute", "content": '{"a"', "finish_reason": "length",
         "task_id": "t01", "attempt": 1},
        {"role": "worker", "phase": "execute", "content": "{}", "finish_reason": "stop",
         "task_id": "t01", "attempt": 2}]
    assert "THINKING" not in json.dumps(artifact) and "chatcmpl" not in json.dumps(artifact)
    assert artifact["truncated"] is False


def test_the_recorder_is_bounded_and_says_so():
    recorder = ReplayCaptureRecorder()
    big = "x" * 90_000
    count = MAX_CAPTURE_BYTES // 90_000 + 5
    for _ in range(count):
        recorder.record_completion(agent="worker:t01", phase="execute", response=_response(big))
    artifact = recorder.artifact()
    assert artifact["truncated"] is True and artifact["dropped"] > 0
    assert len(json.dumps(artifact)) <= MAX_CAPTURE_BYTES + 10_000


def test_tool_results_are_paired_with_their_own_arguments_per_thread():
    recorder = ReplayCaptureRecorder()

    class Registry:
        def execute(self, name, operation, context, payload):
            return {"echo": payload["n"]}

    tools = CapturingTools(Registry(), recorder)
    sink = capturing_result_sink(None, recorder)

    def one(n):
        result = tools.execute(GOVERNMENT_TOOL_NAME, "resolve_variant", None, {"n": n})
        sink(SimpleNamespace(task_id=f"t{n}", call_id="c1", tool=GOVERNMENT_TOOL_NAME,
                             operation="resolve_variant", result=result))

    threads = [threading.Thread(target=one, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    calls = recorder.artifact()["tool_calls"]
    assert len(calls) == 20
    assert all(item["arguments"] == {"n": item["result"]["echo"]} and
               item["task_id"] == f"t{item['result']['echo']}" for item in calls)


def test_the_capturing_adapter_returns_exactly_what_the_authority_returned():
    recorder = ReplayCaptureRecorder()
    response = _response("{}")

    class Authority:
        def chat(self, request, **kwargs):
            return response

    assert CapturingAuthority(Authority(), recorder).chat(
        {}, agent="commander", phase="planning") is response
    assert len(recorder.artifact()["completions"]) == 1


# =============================================================================
# the real worker
# =============================================================================

ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
                 "required": ["answer"], "additionalProperties": False}


def _government_plan(item: dict) -> dict:
    arguments = {"manufacturer": item["manufacturer"],
                 "commercial_model": item["commercial_model"],
                 "model_year": item["model_year_start"],
                 "official_model_code": item["official_model_code"], "trim": item["trim"]}
    task = {"task_id": "t01", "goal": "resolve one register candidate", "scope": "register",
            "dependencies": [],
            "tools": [{"call_id": "c1", "name": GOVERNMENT_TOOL_NAME,
                       "operation": "resolve_variant", "arguments": arguments,
                       "dependency_bindings": []}],
            "output_schema": ANSWER_SCHEMA,
            "evidence": {"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
            "priority": 1, "recursion_depth": 0, "estimated_cost_units": 1,
            "completion": {"required_outputs": ["answer"], "evidence_satisfied": True,
                           "allow_partial": False}}
    return {"version": "1", "objective": "o", "graph": {"tasks": [task]},
            "assignments": [{"task_id": "t01", "worker_role": "reader",
                             "context_task_ids": []}],
            "max_replans": 1, "estimated_cost_units": 1}


class ReasoningCompletions(FakeKimiCompletions):
    """The smoke fake, with reasoning text on every message (never captured)."""

    def create(self, **kwargs):
        response = super().create(**kwargs)
        response.choices[0].message.reasoning_content = "REASONING-SENTINEL"
        return response


def _run(monkeypatch, *, capture: bool, plan_body=None):
    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false",
                              CAPTURE_FLAG: "true" if capture else None})
    repository, conversation_id = build_repo()
    rows = [row for row in register_rows() if row["_id"] == 37254]
    plan = seed_prepared_plan(repository, user_id=USER, conversation_id=conversation_id,
                              units=("toyota",), max_items=5, batch_size=5, records=rows)
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])
    item = repository.work_scope_batch_for_run(run_id)["items"][0]
    completions = ReasoningCompletions(
        plan_body=plan_body if plan_body is not None else json.dumps(_government_plan(item)),
        decision_body=json.dumps({"decision": "REQUEST_VERIFICATION", "plan": None,
                                  "reason": "done"}))
    patch_client(monkeypatch, completions)
    assert worker_main.execute_run(run_id, repository) == 0
    checkpoints = [row for row in repository.checkpoints if str(row["run_id"]) == str(run_id)]
    return repository, run_id, checkpoints, completions


def test_off_constructs_nothing_and_writes_no_capture(monkeypatch):
    def refused(*_args, **_kwargs):
        raise AssertionError("a capture object was constructed with the flag off")

    for name in ("ReplayCaptureRecorder", "CapturingAuthority", "CapturingTools",
                 "capturing_result_sink", "with_capture"):
        monkeypatch.setattr(capture_module, name, refused)
    repository, run_id, checkpoints, _ = _run(monkeypatch, capture=False)
    assert checkpoints
    assert all(CAPTURE_ARTIFACT_KEY not in (row.get("artifacts") or {}) for row in checkpoints)
    assert all(row.get("phase") != CAPTURE_PHASE for row in checkpoints)
    assert repository.runs[str(run_id)]["status"] in {"completed", "partial_success"}


def test_on_the_worker_captures_completions_and_tool_results(monkeypatch):
    repository, run_id, checkpoints, completions = _run(monkeypatch, capture=True)
    capture = checkpoints[-1]["artifacts"][CAPTURE_ARTIFACT_KEY]
    roles = [(item["role"], item["phase"]) for item in capture["completions"]]
    assert roles == [("commander", "planning"), ("worker", "execute"),
                     ("commander", "replanning")]
    assert len(capture["completions"]) == len(completions.calls)
    (tool_call,) = capture["tool_calls"]
    assert (tool_call["task_id"], tool_call["call_id"], tool_call["operation"]) == \
        ("t01", "c1", "resolve_variant")
    assert tool_call["arguments"]["trim"] == "SR5"
    assert tool_call["result"]["resolved"] is True
    assert capture["models"] == {"commander": "kimi-k2.6", "worker": "kimi-k2.6"}
    assert "REASONING-SENTINEL" not in json.dumps(capture)
    # Provider text never reaches the shadow observer's durable writes.
    assert "REASONING-SENTINEL" not in json.dumps(repository.run_events, default=str)


def test_a_run_refused_at_planning_keeps_its_capture(monkeypatch):
    _repository, _run_id, checkpoints, _ = _run(monkeypatch, capture=True,
                                                plan_body="not json at all")
    (row,) = [row for row in checkpoints if row["phase"] == CAPTURE_PHASE]
    completions = row["artifacts"][CAPTURE_ARTIFACT_KEY]["completions"]
    # The refused plan and its one repair, exactly as the provider returned them.
    assert [item["content"] for item in completions] == ["not json at all", "not json at all"]


def test_the_export_replays_to_the_outcome_the_worker_reached(monkeypatch, tmp_path):
    repository, run_id, _checkpoints, _ = _run(monkeypatch, capture=True)
    export = _export_module()
    manifest = export.build_manifest(repository, str(run_id), name="capture-test",
                                     today="2026-09-26")
    assert sanitization_findings(manifest) == []
    assert {entry["kind"] for entry in manifest["provenance"].values()} == {"captured"}
    assert manifest["objective"] == "(objective not exported)"
    target = export.write_manifest(manifest, tmp_path / "capture-test", write_expected=True)
    written = json.loads(target.read_text(encoding="utf-8"))
    from replay_harness import fixture_findings, replay

    assert fixture_findings(written) == []
    report = replay(written)
    assert report.outcome() == written["expected"]
    assert written["expected"]["terminal"] == "result"
    # Every call the worker made is answered by the export, and nothing is left.
    assert report.unconsumed_completions == {} and report.unconsumed_tool_results == []
    assert [(item["role"], item["phase"]) for item in report.served] == [
        ("commander", "planning"), ("worker", "execute"), ("commander", "replanning")]
    # The same register row became the same vehicle. (The in-memory product
    # repository has no structured-fact read, so the WORKER's verification
    # degrades there; the replay's guarded evidence store has it. Status is
    # therefore compared through the replay's own expected outcome above.)
    output = repository.runs[str(run_id)]["output"]
    assert [item["vehicle_key"] for item in report.result["vehicles"]] == \
        [item["vehicle_key"] for item in output["vehicles"]] == ["37254"]


def test_the_export_refuses_a_missing_or_truncated_capture(monkeypatch):
    export = _export_module()
    repository, run_id, checkpoints, _ = _run(monkeypatch, capture=False)
    with pytest.raises(export.ExportRefused, match="REPLAY_CAPTURE_MISSING"):
        export.build_manifest(repository, str(run_id), name="x")
    checkpoints[-1]["artifacts"][CAPTURE_ARTIFACT_KEY] = {
        "format": "replay-capture/1", "completions": [], "tool_calls": [],
        "truncated": True, "dropped": 3}
    with pytest.raises(export.ExportRefused, match="REPLAY_CAPTURE_TRUNCATED"):
        export.build_manifest(repository, str(run_id), name="x")


def test_the_export_refuses_unsanitary_material(monkeypatch, tmp_path):
    export = _export_module()
    repository, run_id, _checkpoints, _ = _run(monkeypatch, capture=True)
    manifest = export.build_manifest(repository, str(run_id), name="x")
    manifest["workers"]["t01"][0]["content"] = json.dumps(
        {"answer": "mail " + "someone" + "@" + "example.com"})
    with pytest.raises(export.ExportRefused, match="EMAIL"):
        export.write_manifest(manifest, tmp_path / "x", write_expected=False)
    assert not (tmp_path / "x").exists()


def test_the_worker_run_with_capture_on_reaches_the_same_outcome_as_off(monkeypatch):
    off_repo, off_id, _, _ = _run(monkeypatch, capture=False)
    on_repo, on_id, _, _ = _run(monkeypatch, capture=True)
    off, on = off_repo.runs[str(off_id)], on_repo.runs[str(on_id)]
    assert off["status"] == on["status"]
    assert (off.get("output") or {}).get("status") == (on.get("output") or {}).get("status")


def test_kimi_response_shape_is_what_the_recorder_reads():
    recorder = ReplayCaptureRecorder()
    recorder.record_completion(agent="commander", phase="planning",
                               response=kimi_response('{"x": 1}'))
    assert recorder.artifact()["completions"][0]["content"] == '{"x": 1}'


# =============================================================================
# pinned off everywhere, enforced by scripts/check_unsafe_defaults.py
# =============================================================================

def _unsafe_defaults():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "check_unsafe_defaults.py"
    spec = importlib.util.spec_from_file_location("check_unsafe_defaults", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_deploy_script_pins_the_capture_off():
    assert _unsafe_defaults().replay_capture_pin_problems() == []


def test_the_contract_array_sources_to_false():
    import subprocess
    from pathlib import Path

    contract = Path(__file__).resolve().parents[1] / "scripts/deploy/deployment-contract.sh"
    out = subprocess.run(["bash", "-c", f'source "{contract}"; '
                          'printf "%s\\n" "${MILO_REPLAY_CAPTURE_PINNED_OFF[@]}"; '
                          'printf "%s" "$MILO_REPLAY_CAPTURE_FLAG_NAME"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == f"{CAPTURE_FLAG}=false\n{CAPTURE_FLAG}"


@pytest.mark.parametrize("rel", ["scripts/deploy/cloud-run.sh",
                                 "scripts/deploy/website-execution-activate.sh",
                                 "scripts/catalog/government-production-capture.sh",
                                 "scripts/deploy/staging-cloud-run.sh",
                                 "scripts/release/generate-deployment-plan.sh",
                                 "scripts/deploy/kill-switch.sh"])
def test_a_script_that_stops_pinning_the_capture_fails_the_scan(tmp_path, rel):
    import shutil
    from pathlib import Path

    module = _unsafe_defaults()
    root = Path(__file__).resolve().parents[1]
    for name in [*module.REPLAY_CAPTURE_PINS, module.REPLAY_CAPTURE_KILL_SWITCH]:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / name, tmp_path / name)
    assert module.replay_capture_pin_problems(tmp_path) == []
    target = tmp_path / rel
    text = target.read_text()
    text = (text.replace('"${MILO_REPLAY_CAPTURE_PINNED_OFF[@]}"', '"X=1"', 1)
                .replace('"${MILO_REPLAY_CAPTURE_PINNED_OFF[*]}"', '"X"', 1)
                .replace('"$MILO_REPLAY_CAPTURE_FLAG_NAME"', '"X"', 1))
    target.write_text(text)
    problems = module.replay_capture_pin_problems(tmp_path)
    assert any(item.startswith(rel) for item in problems), problems


def test_a_committed_enabled_capture_is_an_unsafe_default():
    module = _unsafe_defaults()
    pattern = module.ENABLE_RE[CAPTURE_FLAG]
    assert pattern.search(CAPTURE_FLAG + "=" + "true")
    assert not pattern.search(CAPTURE_FLAG + "=false")


def test_the_offline_ci_job_runs_the_replay_suite():
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    job = workflow.split("offline-checks:", 1)[1].split("\n  frontend", 1)[0]
    command = job.split("Backend offline tests", 1)[1].split("- name:", 1)[0]
    assert "pytest -q -rs tests" in command
    assert "replay" not in command  # nothing about the replay suite is ignored
