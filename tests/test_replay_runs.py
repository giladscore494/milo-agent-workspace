"""PR-Y Y3: every recorded production run replays offline through the real engine.

Each fixture under tests/replay/ is replayed with the provider and the
Government tool answering ONLY from the recording (see tests/replay_harness.py)
and everything else real. The outcome must be exactly the fixture's `expected`
on the current code, no call may be unrecorded unless `expected` says where
the current code leaves the recording, and no whole-snapshot read may happen.

When a replay stops matching, the code changed how it treats a shape a model
already produced in production: that is the point of this suite. Fix the code,
or -- when the new behaviour is intended -- update `expected` in the manifest
(and, for a reconstructed fixture, in tests/replay/reconstruct.py) in the same
PR, saying why.
"""

from __future__ import annotations

import copy
import json

import pytest

from replay_harness import ReplayDivergence, fixture_dirs, load_manifest, replay

FIXTURES = {path.name: path for path in fixture_dirs()}


def _expected(manifest: dict) -> dict:
    return {key: value for key, value in manifest["expected"].items() if key != "note"}


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_recorded_run_replays_to_its_expected_outcome(name):
    manifest = load_manifest(FIXTURES[name])
    report = replay(manifest)
    assert report.outcome() == _expected(manifest)
    # The spy refused nothing because nothing asked for a whole snapshot.
    assert report.whole_snapshot_reads == []
    # Every recorded tool answer went to the task/call that recorded it, and
    # was re-read from the recorded rows by the real tool.
    assert report.identity_mismatches == []
    if report.terminal == "result":
        assert report.cross_checked == len(manifest["tool_results"])
        assert report.unconsumed_completions == {}
        assert report.unconsumed_tool_results == []


def test_the_original_aa63369b_plan_is_rejected_at_plan_time():
    """PR-V V4: register_meta (get_variants only) declares evidence, so the
    recorded plan is refused and the Commander is asked for its ONE repair --
    which production never produced. The replay stops at that call: nothing
    past the plan is paid for, and this is not a completed (or failed) run."""
    report = replay(load_manifest(FIXTURES["aa63369b"]))
    assert report.terminal == "unrecorded_call"
    assert report.result is None
    assert report.model_calls == 1
    assert report.retry_reasons == [["commander", "planning",
                                     "EVIDENCE_REQUIRES_EVIDENCE_TOOL"]]
    assert report.divergence == {"role": "commander", "phase": "planning",
                                 "task_id": None, "index": 1}
    assert [item["role"] for item in report.served] == ["commander"]
    assert report.catalog_reads == []


def test_the_v4_plan_differs_from_the_original_by_one_reversible_substitution():
    from replay.reconstruct import (V4_PROVENANCE, derive_v4_plan_text,
                                    reverse_v4_plan_text)

    original = load_manifest(FIXTURES["aa63369b"])
    derived = load_manifest(FIXTURES["aa63369b-v4"])
    text, position = derive_v4_plan_text(original["commander"][0]["content"])
    assert derived["commander"][0]["content"] == text
    # Reversing the one substitution restores the recorded text byte for byte.
    assert reverse_v4_plan_text(text, position).encode("utf-8") == \
        original["commander"][0]["content"].encode("utf-8")
    before, after = (json.loads(item["commander"][0]["content"])
                     for item in (original, derived))
    changed = [(a["task_id"], a["evidence"], b["evidence"])
               for a, b in zip(before["graph"]["tasks"], after["graph"]["tasks"]) if a != b]
    assert changed == [("register_meta",
                        {"minimum_sources": 1, "required_fields": [], "min_confidence": 0.5},
                        {"minimum_sources": 0, "required_fields": [], "min_confidence": 0.5})]
    assert {key for key in before if before[key] != after[key]} == {"graph"}
    # Every other recorded artifact, and its provenance, is aa63369b's.
    for key in ("preparation", "workers", "verifier", "tool_results", "snapshot_rows",
                "models", "objective"):
        assert derived[key] == original[key], key
    assert derived["commander"][1:] == original["commander"][1:]
    assert {name: entry for name, entry in derived["provenance"].items()
            if name != "commander[0]"} == {name: entry for name, entry
                                           in original["provenance"].items()
                                           if name != "commander[0]"}
    assert derived["provenance"]["commander[0]"]["source"].startswith(V4_PROVENANCE)


def test_the_v4_plan_is_accepted_and_the_run_completes_partially():
    """The ENGINE's result for the one-field-changed plan, asserted directly."""
    report = replay(load_manifest(FIXTURES["aa63369b-v4"]))
    result = report.result
    assert report.terminal == "result"
    assert result["status"] == "partial_success"
    assert len(result["vehicles"]) == 8 and len(result["unresolved_groups"]) == 2
    assert report.model_calls == 13
    codes = sorted(item["code"] for item in result["needs_review"])
    assert codes == ["CANDIDATE_UNRESOLVED_AMBIGUOUS", "CANDIDATE_UNRESOLVED_AMBIGUOUS"]
    assert "EVIDENCE_REQUIREMENTS_UNMET" not in codes
    assert report.unconsumed_completions == {} and report.unconsumed_tool_results == []
    # Each vehicle carries exactly its own register row.
    rows = sorted(vehicle["vehicle_key"] for vehicle in result["vehicles"])
    assert rows == sorted(["37254", "37291", "37293", "37096", "37098", "37425", "37316",
                           "37417"])


def test_c4b8bb54_keeps_its_work_after_the_rejected_replan():
    report = replay(load_manifest(FIXTURES["c4b8bb54"]))
    assert report.result["status"] == "partial_success"
    assert report.result["needs_review"] == [
        {"task_id": "commander_replan", "code": "COMMANDER_REPLAN_REJECTED"}]
    assert len(report.result["vehicles"]) == 11
    # The Commander was asked for a replan exactly once.
    assert [item["phase"] for item in report.served if item["role"] == "commander"] == \
        ["planning", "replanning"]


@pytest.mark.parametrize("name,reason", [("280fc9e5", "OUTPUT_SCHEMA_NESTED_INVALID"),
                                         ("6825eb96", "REQUIRED_OUTPUT_NOT_IN_SCHEMA")])
def test_the_broken_plans_now_stop_at_plan_validation(name, reason):
    report = replay(load_manifest(FIXTURES[name]))
    assert report.terminal == "unrecorded_call"
    assert report.retry_reasons == [["commander", "planning", reason]]
    # Nothing was paid for past the plan: no worker completion, no tool call.
    assert [item["role"] for item in report.served] == ["commander"]
    assert report.catalog_reads == []


def test_the_original_29eb076c_plan_is_rejected_at_plan_time():
    """PR-EV: every T task requires resolve_variant OUTPUT keys as evidence
    fields, so the recorded plan is refused and the Commander is asked for its
    ONE repair -- which production never produced. Nothing past the plan is
    paid for."""
    report = replay(load_manifest(FIXTURES["29eb076c"]))
    assert report.terminal == "unrecorded_call"
    assert report.result is None
    assert report.model_calls == 1
    assert report.retry_reasons == [["commander", "planning",
                                     "EVIDENCE_FIELD_NOT_PRODUCIBLE"]]
    assert report.divergence == {"role": "commander", "phase": "planning",
                                 "task_id": None, "index": 1}
    assert [item["role"] for item in report.served] == ["commander"]
    assert report.catalog_reads == []


def test_without_the_pr_ev_rule_the_t_recording_reproduces_production(monkeypatch):
    """The control: with ONLY the new firewall check switched off, the same
    recording ends as production did -- 11 resolved vehicles and yet
    partial_success, one EVIDENCE_REQUIREMENTS_UNMET per task. The engine's
    evidence rule itself is unchanged (EV-4)."""
    from backend.engines.swarm_v2.validation import PlanValidator

    monkeypatch.setattr(PlanValidator, "_validate_evidence_fields", lambda self, task: None)
    report = replay(load_manifest(FIXTURES["29eb076c"]))
    result = report.result
    assert report.terminal == "result" and report.retry_reasons == []
    assert result["status"] == "partial_success"
    assert len(result["vehicles"]) == 11
    assert sorted(item["code"] for item in result["needs_review"]) == \
        ["EVIDENCE_REQUIREMENTS_UNMET"] * 11


def test_the_ev_repair_differs_from_the_original_by_one_reversible_substitution():
    from replay.reconstruct import (EV_ORIGINAL, EV_PROVENANCE, EV_REPAIRED,
                                    derive_ev_plan_text, reverse_ev_plan_text)

    original = load_manifest(FIXTURES["29eb076c"])
    derived = load_manifest(FIXTURES["29eb076c-ev"])
    recorded = original["commander"][0]["content"]
    # The refused plan is kept as recorded, then the ONE repair, then the
    # recorded replan decision.
    assert derived["commander"] == [original["commander"][0],
                                    {"phase": "planning",
                                     "content": derive_ev_plan_text(recorded),
                                     "finish_reason": "stop"},
                                    original["commander"][1]]
    repair = derived["commander"][1]["content"]
    assert EV_ORIGINAL not in repair
    assert reverse_ev_plan_text(repair).encode("utf-8") == recorded.encode("utf-8")
    before, after = (json.loads(text) for text in (recorded, repair))
    assert {key for key in before if before[key] != after[key]} == {"graph"}
    for a, b in zip(before["graph"]["tasks"], after["graph"]["tasks"]):
        assert a["evidence"]["required_fields"] == ["resolved", "ambiguous", "match_count"]
        assert b["evidence"] == {**a["evidence"],
                                 "required_fields": ["trim", "official_model_code"]}
        assert {key: value for key, value in a.items() if key != "evidence"} == \
            {key: value for key, value in b.items() if key != "evidence"}
    assert repair.count(EV_REPAIRED) == len(after["graph"]["tasks"]) == 11
    for key in ("preparation", "workers", "verifier", "tool_results", "snapshot_rows",
                "models", "objective"):
        assert derived[key] == original[key], key
    assert derived["provenance"]["commander[1]"]["source"].startswith(EV_PROVENANCE)


def test_the_repaired_t_batch_completes_with_no_unmet_evidence():
    """EV-4/EV-5: the SAME 11 resolved candidates, now requiring fields their
    evidence carries, end completed with no EVIDENCE_REQUIREMENTS_UNMET."""
    report = replay(load_manifest(FIXTURES["29eb076c-ev"]))
    result = report.result
    assert report.terminal == "result"
    assert report.retry_reasons == [["commander", "planning",
                                     "EVIDENCE_FIELD_NOT_PRODUCIBLE"]]
    assert result["status"] == "complete"
    assert result["result_kind"] == "usable_result"
    assert len(result["vehicles"]) == 11 and result["unresolved_groups"] == []
    assert result["needs_review"] == []
    assert report.model_calls == 14
    assert report.unconsumed_completions == {} and report.unconsumed_tool_results == []


# --- the replay is strict ------------------------------------------------------

def _aa() -> dict:
    """The strictness tests need an ACCEPTED plan: the derived V4 fixture."""
    return copy.deepcopy(load_manifest(FIXTURES["aa63369b-v4"]))


def test_an_unrecorded_worker_call_diverges_instead_of_being_answered():
    manifest = _aa()
    manifest["workers"].pop("t03")
    report = replay(manifest)
    assert report.terminal == "unrecorded_call"
    assert report.divergence == {"role": "worker", "phase": "execute", "task_id": "t03",
                                 "index": 0}


def test_an_unrecorded_commander_call_diverges():
    manifest = _aa()
    manifest["commander"].pop()
    report = replay(manifest)
    assert report.divergence == {"role": "commander", "phase": "replanning",
                                 "task_id": None, "index": 0}


def test_an_unrecorded_tool_call_diverges():
    manifest = _aa()
    entry = next(item for item in manifest["tool_results"] if item["task_id"] == "t02")
    entry["arguments"]["trim"] = "NOT THE PLANNED TRIM"
    report = replay(manifest)
    assert report.terminal == "divergence"
    assert report.divergence["code"] == "UNRECORDED_TOOL_CALL"


def test_a_recorded_result_the_rows_do_not_support_fails_the_cross_check():
    manifest = _aa()
    entry = next(item for item in manifest["tool_results"] if item["task_id"] == "t09")
    entry["result"]["match_count"] = 3
    report = replay(manifest)
    assert report.divergence["code"] == "TOOL_RESULT_CROSS_CHECK_FAILED"


def test_a_result_recorded_under_another_task_is_refused():
    manifest = _aa()
    first, second = (next(item for item in manifest["tool_results"] if item["task_id"] == task)
                     for task in ("t01", "t02"))
    first["task_id"], second["task_id"] = "t02", "t01"
    report = replay(manifest)
    assert report.divergence["code"] == "TOOL_CALL_IDENTITY_MISMATCH"


def test_a_leftover_recorded_completion_is_reported():
    manifest = _aa()
    manifest["workers"]["t01"].append(copy.deepcopy(manifest["workers"]["t01"][0]))
    report = replay(manifest)
    assert report.outcome()["unconsumed"] == {"completions": 1, "tool_results": 0}
    assert report.outcome() != _expected(manifest)


def test_a_whole_snapshot_read_is_refused(monkeypatch):
    from backend.catalog.government import query

    original = query.GovernmentCatalogQuery.resolve_variant

    def paging(self, *args, **kwargs):
        self._repository.list_catalog_candidates(self._active()["id"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(query.GovernmentCatalogQuery, "resolve_variant", paging)
    report = replay(_aa())
    assert report.divergence["code"] == "WHOLE_SNAPSHOT_READ"
    assert report.whole_snapshot_reads == ["list_catalog_candidates"]


def test_divergence_is_not_foldable_into_an_ordinary_failure():
    assert not issubclass(ReplayDivergence, Exception)


@pytest.mark.parametrize("mode,diverges", [("exact", False), ("separator_insensitive", True)])
def test_a_recorded_match_mode_must_match_the_real_tool(mode, diverges):
    """Recordings made before PR-V state no match_mode; one that states it is
    held to it by the cross-check."""
    manifest = _aa()
    for item in manifest["tool_results"]:
        if item["operation"] == "resolve_variant":
            item["result"]["match_mode"] = mode
    report = replay(manifest)
    if diverges:
        assert report.divergence["code"] == "TOOL_RESULT_CROSS_CHECK_FAILED"
    else:
        assert report.outcome() == _expected(manifest)
