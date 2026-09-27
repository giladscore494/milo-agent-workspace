"""PR-V V2 + V3: official model code comparison, and duplicate identities.

V2. The register writes one code two ways across DIFFERENT rows:
`TZNA55L-GKZSZA` and `TZNA55L GKZSZA`. `resolve_variant` matches the code
EXACTLY first; only when the exact spelling matches nothing does it retry ONCE
with a key that ignores "-" vs " " and repeated spaces. It reports which
(`match_mode`), never widens an exact unique match into an ambiguity, and
returns every stored value exactly as the source wrote it.

V3. Candidates of one snapshot that state an identical identity (the real
LIMITED / TZNA55L-GKZSZA pair 37350 + 37439 and TRAILHUNTER / TZNH55L GKVSZA
pair 37309 + 37345) are annotated at preparation with
`duplicate_identity_record_ids`, through bounded reads only, and the
Commander context carries the one rule that goes with it.

PR-Z2. "An identical identity" is the variant identity key (which carries the
Government's registration identifiers `tozeret_cd`, `degem_cd`, `sug_degem`)
AND identical content minus `_id`. The REAL rows 37350 / 37439 and 37309 /
37345 are different vehicles -- different `degem_cd` -- and are never told to
the Commander as duplicates; the stand-ins above are identical copies and
still are.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import backend.catalog.government.query as query_module
import backend.catalog.government.preparation as preparation_module
from backend.catalog.government.preparation import (DUPLICATE_IDENTITY_RULE,
                                                    GovernmentPreparationError,
                                                    GovernmentWorkItem,
                                                    prepare_government_work)
from backend.catalog.government.query import (MATCH_EXACT, MATCH_SEPARATOR_INSENSITIVE,
                                              separator_key)
from backend.tools import ToolContext
from backend.tools.government_vehicle import GovernmentVehicleTool
from backend.testing.work_scope_seed import committed_records
from swarm_v2_production_fixture import WholeSnapshotSpy
from test_government_placeholder_rows import seeded_batch

TOYOTA = "טויוטה"


def _row(record_id: int, code: str, trim: str, *, model: str = "4RUNNER",
         year: int = 2026, **changes) -> dict:
    row = copy.deepcopy(committed_records(1)[0])
    row.update({"_id": record_id, "kinuy_mishari": model, "degem_nm": code,
                "ramat_gimur": trim, "shnat_yitzur": year, **changes})
    return row


def _tool(rows):
    repository, run = seeded_batch(rows)
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row.get("activated_at"))
    return repository, run, GovernmentVehicleTool(repository,
                                                  snapshot_key=snapshot["snapshot_key"])


def _resolve(tool, code, trim="LIMITED"):
    return tool.execute(ToolContext(), "resolve_variant", {
        "manufacturer": TOYOTA, "commercial_model": "4RUNNER", "model_year": 2026,
        "trim": trim, "official_model_code": code})


def _rows_of(result):
    return sorted(item["upstream_record_id"] for item in result["variants"])


# =============================================================================
# V2
# =============================================================================

@pytest.mark.parametrize("code,key", [
    ("TZNA55L-GKZSZA", "TZNA55L GKZSZA"), ("TZNA55L GKZSZA", "TZNA55L GKZSZA"),
    ("TZNA55L  -  GKZSZA", "TZNA55L GKZSZA"), (" TZNH55L GKVSZA ", "TZNH55L GKVSZA"),
    ("tzna55l-gkzsza", "tzna55l gkzsza"), ("", None), ("--", None)])
def test_the_separator_key(code, key):
    assert separator_key(code) == key


def test_exact_requests_stay_exact_and_unique_for_the_real_pair():
    """STOP CHECK V2: both spellings are DIFFERENT rows; each exact request
    resolves its own row alone -- never widened into an ambiguity."""
    rows = [_row(37350, "TZNA55L-GKZSZA", "LIMITED"), _row(37501, "TZNA55L GKZSZA", "LIMITED")]
    _repository, _run, tool = _tool(rows)
    dash, space = _resolve(tool, "TZNA55L-GKZSZA"), _resolve(tool, "TZNA55L GKZSZA")
    assert (dash["resolved"], dash["match_mode"], _rows_of(dash)) == (True, MATCH_EXACT,
                                                                       ["37350"])
    assert (space["resolved"], space["match_mode"], _rows_of(space)) == (True, MATCH_EXACT,
                                                                         ["37501"])
    assert dash["source_record"]["degem_nm"] == "TZNA55L-GKZSZA"
    assert space["source_record"]["degem_nm"] == "TZNA55L GKZSZA"


def test_a_spelling_no_row_states_retries_once_and_reports_it():
    _repository, _run, tool = _tool([_row(37350, "TZNA55L-GKZSZA", "LIMITED")])
    result = _resolve(tool, "TZNA55L GKZSZA")
    assert (result["resolved"], result["match_count"], result["match_mode"]) == (
        True, 1, MATCH_SEPARATOR_INSENSITIVE)
    # The stored value is returned exactly as the register wrote it.
    assert result["variants"][0]["official_model_code"] == "TZNA55L-GKZSZA"
    assert result["source_record"]["degem_nm"] == "TZNA55L-GKZSZA"


def test_the_retry_that_finds_both_spellings_reports_the_ambiguity():
    rows = [_row(37350, "TZNA55L-GKZSZA", "LIMITED"), _row(37501, "TZNA55L GKZSZA", "LIMITED")]
    _repository, _run, tool = _tool(rows)
    result = _resolve(tool, "TZNA55L   GKZSZA")
    assert (result["resolved"], result["ambiguous"], result["match_mode"]) == (
        False, True, MATCH_SEPARATOR_INSENSITIVE)
    assert _rows_of(result) == ["37350", "37501"]
    assert "source_record" not in result


def test_nothing_else_is_ignored_by_the_retry():
    _repository, _run, tool = _tool([_row(37350, "TZNA55L-GKZSZA", "LIMITED")])
    for code in ("TZNA55L_GKZSZA", "TZNA55LGKZSZA", "tzna55l-gkzsza", "TZNA55L-GKZSZB"):
        result = _resolve(tool, code)
        assert (result["resolved"], result["match_count"], result["match_mode"]) == (
            False, 0, MATCH_EXACT), code
    # The other stated filters still apply to the retry.
    result = _resolve(tool, "TZNA55L GKZSZA", trim="SR5")
    assert (result["match_count"], result["match_mode"]) == (0, MATCH_EXACT)


def test_an_exact_ambiguity_is_never_retried():
    rows = [_row(37350, "TZNA55L-GKZSZA", "LIMITED"), _row(37439, "TZNA55L-GKZSZA", "LIMITED"),
            _row(37501, "TZNA55L GKZSZA", "LIMITED")]
    _repository, _run, tool = _tool(rows)
    result = _resolve(tool, "TZNA55L-GKZSZA")
    assert (result["match_count"], result["match_mode"], _rows_of(result)) == (
        2, MATCH_EXACT, ["37350", "37439"])


def test_the_retry_reads_one_bounded_page_and_never_a_partial_answer(monkeypatch):
    rows = [_row(37350, "TZNA55L-GKZSZA", "LIMITED"), _row(37351, "OTHER-1", "LIMITED"),
            _row(37352, "OTHER-2", "LIMITED")]
    _repository, _run, tool = _tool(rows)
    monkeypatch.setattr(query_module, "MAX_SEPARATOR_SCAN_ROWS", 2)
    result = _resolve(tool, "TZNA55L GKZSZA")
    # Three rows share the model year's filters, the page holds two: no guess.
    assert (result["match_count"], result["match_mode"]) == (0, MATCH_EXACT)


# =============================================================================
# V3
# =============================================================================

def duplicate_rows() -> list[dict]:
    return [_row(37350, "TZNA55L-GKZSZA", "LIMITED"), _row(37439, "TZNA55L-GKZSZA", "LIMITED"),
            _row(37309, "TZNH55L GKVSZA", "TRAILHUNTER"),
            _row(37345, "TZNH55L GKVSZA", "TRAILHUNTER"),
            _row(37254, "TRN285L-GKTSKA", "SR5"),
            # Same model, code and trim as 37254 but another drivetrain: a
            # DIFFERENT identity, never a duplicate.
            _row(37255, "TRN285L-GKTSKA", "SR5", hanaa_cd=1, hanaa_nm="4X2")]


def _by_code_trim(preparation):
    return {(item.official_model_code, item.trim, item.candidate_id):
            item.duplicate_identity_record_ids for item in preparation.queue}


def test_duplicate_identities_are_annotated_with_their_sorted_register_rows():
    repository, run = seeded_batch(duplicate_rows())
    preparation = prepare_government_work(repository, run_id=run)
    groups = {}
    for item in preparation.queue:
        groups.setdefault((item.official_model_code, item.trim), set()).add(
            item.duplicate_identity_record_ids)
    assert groups[("TZNA55L-GKZSZA", "LIMITED")] == {("37350", "37439")}
    assert groups[("TZNH55L GKVSZA", "TRAILHUNTER")] == {("37309", "37345")}
    assert groups[("TRN285L-GKTSKA", "SR5")] == {()}


def test_the_commander_context_gets_the_annotation_and_the_one_rule():
    repository, run = seeded_batch(duplicate_rows())
    context = prepare_government_work(repository, run_id=run).work_context({})
    assert context["duplicate_identity_rule"] == DUPLICATE_IDENTITY_RULE
    assert "ONE resolve_variant call" in DUPLICATE_IDENTITY_RULE
    assert "valid outcome" in DUPLICATE_IDENTITY_RULE
    annotated = [item for item in context["items"] if "duplicate_identity_record_ids" in item]
    assert sorted(tuple(item["duplicate_identity_record_ids"]) for item in annotated) == [
        ("37309", "37345"), ("37309", "37345"), ("37350", "37439"), ("37350", "37439")]


def test_a_queue_without_duplicates_is_unchanged():
    rows = [_row(37254, "TRN285L-GKTSKA", "SR5"), _row(37291, "TRN285L-GKTLKA", "LIMITED")]
    repository, run = seeded_batch(rows)
    preparation = prepare_government_work(repository, run_id=run)
    assert all(item.duplicate_identity_record_ids == () for item in preparation.queue)
    assert "duplicate_identity_rule" not in preparation.work_context({})
    assert all("duplicate_identity_record_ids" not in item
               for item in preparation.as_artifact()["queue"])


def test_the_annotation_survives_a_resume_and_an_old_record_still_resumes():
    repository, run = seeded_batch(duplicate_rows())
    first = prepare_government_work(repository, run_id=run)
    checkpoint = {"artifacts": {"government": first.as_artifact()}}
    resumed = prepare_government_work(repository, run_id=run, checkpoint=checkpoint)
    assert resumed.resumed and resumed.queue == first.queue
    old = copy.deepcopy(first.as_artifact())
    for item in old["queue"]:
        item.pop("duplicate_identity_record_ids", None)
    before_pr_v = prepare_government_work(repository, run_id=run,
                                          checkpoint={"artifacts": {"government": old}})
    assert all(item.duplicate_identity_record_ids == () for item in before_pr_v.queue)


@pytest.mark.parametrize("bad", ["37350", [1], [""], {"a": 1}])
def test_a_malformed_annotation_in_a_record_is_refused(bad):
    record = {"candidate_key": "k", "candidate_id": "c", "manufacturer": TOYOTA,
              "commercial_model": "4RUNNER", "duplicate_identity_record_ids": bad}
    with pytest.raises(GovernmentPreparationError):
        GovernmentWorkItem.from_record(record)


def test_preparation_never_reads_the_whole_snapshot():
    """STOP CHECK V3: the spy refuses both whole-snapshot projections."""
    repository, run = seeded_batch(duplicate_rows())
    spy = WholeSnapshotSpy(repository)
    preparation = prepare_government_work(spy, run_id=run)
    assert spy.whole_snapshot_reads == []
    assert any(item.duplicate_identity_record_ids for item in preparation.queue)
    # Bounded: one page per DISTINCT stated identity (4 here), each at most
    # MAX_DUPLICATE_SCAN_ROWS rows.
    assert spy.catalog_rows_read < 100


# =============================================================================
# PR-Z2: a duplicate is the same key AND identical content
# =============================================================================

REAL = json.loads((Path(__file__).resolve().parent / "fixtures"
                   / "production_4runner_2026_rows.json").read_text())
REAL_ROWS = {str(row["_id"]): row for row in REAL["rows"]}


def _groups(preparation) -> dict[str, tuple[str, ...]]:
    records = {}
    for item in preparation.queue:
        records[item.candidate_id] = item.duplicate_identity_record_ids
    return records


def test_the_real_production_pairs_are_different_vehicles_and_never_annotated():
    rows = [copy.deepcopy(row) for row in REAL["rows"]]
    repository, run = seeded_batch(rows)
    preparation = prepare_government_work(repository, run_id=run)
    assert len(preparation.queue) == 5
    # PR-V annotated 37350 + 37439 and 37309 + 37345 by their stated identity;
    # the register gives each its own degem_cd, so none is anyone's duplicate.
    assert all(item.duplicate_identity_record_ids == () for item in preparation.queue)
    assert "duplicate_identity_rule" not in preparation.work_context({})
    # The register still cannot tell each pair apart by the identity a task
    # states: resolving it stays ambiguous -- an honest answer, not a group.
    snapshot = next(row for row in repository.catalog_snapshots.values()
                    if row.get("activated_at"))
    tool = GovernmentVehicleTool(repository, snapshot_key=snapshot["snapshot_key"])
    assert _rows_of(_resolve(tool, "TZNA55L-GKZSZA")) == ["37350", "37439"]


def test_only_an_identical_copy_is_a_duplicate():
    real = REAL_ROWS["37350"]
    copy_of = {**copy.deepcopy(real), "_id": 90001}                    # identical minus _id
    other_model_code = {**copy.deepcopy(real), "_id": 90002, "degem_cd": 999}   # another key
    same_key_other_content = {**copy.deepcopy(real), "_id": 90003,
                              "koah_sus": int(real["koah_sus"]) + 1}    # same key, collision
    rows = [copy.deepcopy(real), copy_of, other_model_code, same_key_other_content]
    repository, run = seeded_batch(rows)
    preparation = prepare_government_work(repository, run_id=run)
    by_record = {}
    records = {row["id"]: row["upstream_record_id"]
               for row in repository.catalog_raw_records.values()}
    for candidate in repository.catalog_candidates.values():
        by_record[str(records[candidate["raw_record_id"]])] = candidate["id"]
    groups = _groups(preparation)
    assert groups[by_record["37350"]] == groups[by_record["90001"]] == ("37350", "90001")
    assert groups[by_record["90002"]] == ()
    assert groups[by_record["90003"]] == ()


def test_a_would_be_group_larger_than_the_read_bound_is_left_unannotated(monkeypatch):
    monkeypatch.setattr(preparation_module, "MAX_DUPLICATE_GROUP_READS", 1)
    repository, run = seeded_batch(duplicate_rows())
    preparation = prepare_government_work(repository, run_id=run)
    assert all(item.duplicate_identity_record_ids == () for item in preparation.queue)


def test_the_proof_reads_one_register_row_per_group_member_and_never_a_snapshot():
    repository, run = seeded_batch(duplicate_rows())
    reads: list[str] = []
    real = repository.catalog_raw_record_by_upstream_id

    def counted(snapshot_id, upstream_record_id, **kwargs):
        reads.append(str(upstream_record_id))
        return real(snapshot_id, upstream_record_id, **kwargs)

    repository.catalog_raw_record_by_upstream_id = counted
    spy = WholeSnapshotSpy(repository)
    preparation = prepare_government_work(spy, run_id=run)
    assert spy.whole_snapshot_reads == []
    assert any(item.duplicate_identity_record_ids for item in preparation.queue)
    # Each member of the two would-be groups is read once, cached per preparation.
    assert sorted(set(reads)) == ["37309", "37345", "37350", "37439"]
    assert len(reads) == 4
