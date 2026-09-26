"""PR-Y Y1: the replay/1 fixture format and its sanitizer.

A replay fixture is committed to the repository, so it must never carry a
secret, a user id, an e-mail address or a URL outside data.gov.il. The ONE
sanitizer (`backend.replay_capture.sanitization_findings`) is run over every
committed fixture here and by the export script before it writes anything.

Planted values are assembled at run time so that this file itself never
contains credential-shaped text for the repository secret scan to find.
"""

from __future__ import annotations

import copy
import json

import pytest

from backend.replay_capture import (CAPTURED, PROVENANCE_KINDS, RECONSTRUCTED, REPLAY_FORMAT,
                                    sanitization_findings)
from replay_harness import (artifact_ids, fixture_dirs, fixture_findings, load_manifest,
                            manifest_problems, provenance_summary, referenced_record_ids)

RUNS = {"6825eb96", "280fc9e5", "c4b8bb54", "aa63369b"}


def _clean() -> dict:
    return load_manifest(next(path for path in fixture_dirs() if path.name == "aa63369b"))


def test_the_four_production_runs_have_fixtures():
    assert RUNS <= {path.name for path in fixture_dirs()}


@pytest.mark.parametrize("name", sorted(RUNS))
def test_every_committed_fixture_is_clean_and_well_formed(name):
    manifest = load_manifest(next(path for path in fixture_dirs() if path.name == name))
    assert manifest["format"] == REPLAY_FORMAT
    assert fixture_findings(manifest) == []


@pytest.mark.parametrize("name", sorted(RUNS))
def test_each_manifest_lists_captured_vs_reconstructed_per_artifact(name):
    """STOP CHECK Y2: every artifact carries its provenance, and none of these
    four claims to be captured -- no raw provider output of them exists offline."""
    manifest = load_manifest(next(path for path in fixture_dirs() if path.name == name))
    provenance = manifest["provenance"]
    assert set(provenance) == set(artifact_ids(manifest))
    assert all(entry["kind"] in PROVENANCE_KINDS and entry["source"].strip()
               for entry in provenance.values())
    assert provenance_summary(manifest)[CAPTURED] == 0
    assert provenance_summary(manifest)[RECONSTRUCTED] == len(provenance)


@pytest.mark.parametrize("name", sorted(RUNS))
def test_only_the_rows_the_tool_results_reference_are_carried(name):
    manifest = load_manifest(next(path for path in fixture_dirs() if path.name == name))
    assert {str(row["_id"]) for row in manifest["snapshot_rows"]} == \
        referenced_record_ids(manifest)


def test_every_completion_is_inert_text_before_validation():
    for path in fixture_dirs():
        manifest = load_manifest(path)
        entries = [*manifest["commander"], *manifest["verifier"],
                   *[item for attempts in manifest["workers"].values() for item in attempts]]
        assert all(isinstance(item["content"], str) for item in entries)


def test_the_reconstructed_fixtures_are_reproducible():
    """Nothing in a reconstructed fixture was edited by hand."""
    from replay.reconstruct import main

    assert main(["--check"]) == 0


# --- the sanitizer: STOP CHECK Y1 --------------------------------------------

def _provider_key() -> str:
    return "sk-" + "live" + "A1b2C3d4" * 3


def _jwt() -> str:
    return "ey" + "JhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".sig"


PLANTED = {
    "provider key in a worker completion": (
        lambda doc: doc["workers"]["t01"][0].update(
            content=json.dumps({"summary": "key " + _provider_key()})),
        "SECRET_PROVIDER_KEY"),
    "JWT in the plan text": (
        lambda doc: doc["commander"][0].update(
            content=doc["commander"][0]["content"].replace(
                "resolve register candidate t01", "resolve " + _jwt())),
        "SECRET_JWT"),
    "e-mail address": (
        lambda doc: doc.update(description="ask " + "operator" + "@" + "example.com"),
        "EMAIL"),
    "non data.gov.il URL": (
        lambda doc: doc.update(description="see https://" + "evil.example.test/x"),
        "URL_NOT_DATA_GOV_IL"),
    "user id key": (
        lambda doc: doc["expected"].update(user_id="someone"), "FORBIDDEN_KEY"),
    "lease token key inside decoded JSON": (
        lambda doc: doc["commander"][1].update(
            content=json.dumps({"decision": "FINISH", "plan": None, "reason": "r",
                                "lease_token": "x"})),
        "FORBIDDEN_KEY"),
    "unexplained UUID": (
        lambda doc: doc.update(description="owner 1f0e2d3c-4b5a-4968-8776-655443322110"),
        "UNEXPLAINED_UUID"),
    "secret assignment": (
        lambda doc: doc.update(description="pass" + "word=" + "hunter2"),
        "SECRET_ASSIGNMENT"),
}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_a_planted_secret_fails_the_sanitizer(name):
    plant, code = PLANTED[name]
    document = _clean()
    assert sanitization_findings(document) == []
    plant(document)
    findings = sanitization_findings(document)
    assert any(item.startswith(code + " at ") for item in findings), findings
    # The report names a code and a path, never the planted value.
    assert _provider_key() not in json.dumps(findings)
    assert "hunter2" not in json.dumps(findings)


def test_clean_data_passes_and_data_gov_il_urls_are_allowed():
    document = _clean()
    document["description"] = ("register at https://data.gov.il/dataset/degem-rechev-wltp "
                               "and https://www.data.gov.il/api/3/action/datastore_search")
    assert sanitization_findings(document) == []


def test_data_identifiers_are_uuids_the_sanitizer_explains():
    document = _clean()
    uuid = "1f0e2d3c-4b5a-4968-8776-655443322110"
    document["preparation"]["queue"][0]["candidate_id"] = uuid
    document["description"] = f"see candidate {uuid}"
    assert sanitization_findings(document) == []


def test_manifest_problems_name_a_missing_provenance_entry():
    document = _clean()
    document["provenance"].pop("commander[0]")
    assert "PROVENANCE_MISSING commander[0]" in manifest_problems(document)


def test_manifest_problems_refuse_a_row_no_tool_result_references():
    document = _clean()
    extra = copy.deepcopy(document["snapshot_rows"][0])
    extra["_id"] = 99_999
    document["snapshot_rows"].append(extra)
    document["provenance"]["snapshot_rows.99999"] = {"kind": RECONSTRUCTED, "source": "x"}
    assert "SNAPSHOT_ROWS_NOT_EXACTLY_THOSE_REFERENCED" in manifest_problems(document)


def test_manifest_problems_refuse_a_parsed_completion():
    document = _clean()
    document["workers"]["t01"][0]["content"] = {"summary": "already parsed"}
    assert "COMPLETION_CONTENT_NOT_INERT_TEXT" in manifest_problems(document)
