"""PR-L1: deterministic catalog variants, offline.

The mapper's every rule on REAL committed register rows (record 37425 and the
production 4RUNNER rows, and the 29eb076c replay rows); the category table;
the bounded, idempotent build and its mapper-version rule; the coverage ledger
at the two new levels; retention; the backfill entrypoint; the read API
behind its flag. The database side is tests/test_catalog_variants_postgres.py.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.catalog import coverage as catalog_coverage
from backend.catalog.register import browser
from backend.catalog.register import variants as mapper
from backend.dependencies import get_repository
from backend.main import app
from backend.testing.memory_repository import MemoryRepository
from tests.test_register_capture import (USER, api_env, captured_world, no_sockets,  # noqa: F401
                                         snapshot_by_key)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260930000100_catalog_variants.sql"


def fixture_rows() -> list[dict[str, Any]]:
    """Every distinct real register row the repository commits (16)."""
    rows: dict[int, dict[str, Any]] = {}
    for row in json.loads((ROOT / "tests/fixtures/production_4runner_2026_rows.json").read_text())["rows"]:
        rows[row["_id"]] = row
    for run in ("29eb076c", "29eb076c-ev"):
        manifest = json.loads((ROOT / f"tests/replay/{run}/manifest.json").read_text())
        for row in manifest["snapshot_rows"]:
            rows.setdefault(row["_id"], row)
    return [copy.deepcopy(rows[key]) for key in sorted(rows)]


def record(upstream: int) -> dict[str, Any]:
    (row,) = [r for r in fixture_rows() if r["_id"] == upstream]
    return row


# =============================================================================
# 1. the mapper (L1-1), on real rows
# =============================================================================

def test_record_37425_maps_level_1_verbatim_and_level_1_5_typed():
    payload = record(37425)
    row = mapper.map_record(payload)
    for column, field, _kind in mapper.LEVEL_1_FIELDS:
        assert row[column] == payload[field], column
    assert row["upstream_record_id"] == "37425"
    assert (row["mishkal_kolel"], row["nefah_manoa"], row["koah_sus"]) == (3100, payload["nefah_manoa"],
                                                                          payload["koah_sus"])
    assert row["kamut_co2_city"] == payload["kamut_CO2_city"]
    assert row["kamut_co2_hway"] == payload["kamut_CO2_hway"]
    assert row["co2_wltp"] is None and payload["CO2_WLTP"] is None
    assert row["bakarat_yatzivut_ind"] == payload["bakarat_yatzivut_ind"] == 1
    assert row["parse_issues"] == []
    # PR-V's normalization, in its own columns; the source columns unchanged.
    assert (row["norm_fuel_type"], row["norm_body_style"], row["norm_drivetrain"]) == ("petrol", "suv", "awd")
    assert row["delek_nm"] == "בנזין" and row["merkav"] == "פנאי-שטח"


def test_sug_tkina_is_the_homologation_standard_never_the_transmission():
    standards = set()
    for payload in fixture_rows():
        row = mapper.map_record(payload)
        standards.add(row["sug_tkina_nm"])
        assert row["automatic_ind"] == payload["automatic_ind"]
        assert not any("transmission" in key or "gearbox" in key for key in row)
        # Changing the homologation standard changes nothing but itself.
        other = mapper.map_record({**payload, "sug_tkina_cd": 9, "sug_tkina_nm": "אחרת", "automatic_ind": 0})
        assert other["automatic_ind"] == 0 and other["sug_tkina_nm"] == "אחרת"
        assert {k: v for k, v in other.items() if not k.startswith(("sug_tkina", "automatic"))} == \
            {k: v for k, v in row.items() if not k.startswith(("sug_tkina", "automatic"))}
    assert standards == {"אמריקאית", "אירופאית"}


def test_sug_mamir_code_0_is_null():
    for payload in fixture_rows():
        assert (payload["sug_mamir_cd"], payload["sug_mamir_nm"]) == (0, "לא ידוע קוד 0")
        row = mapper.map_record(payload)
        assert row["sug_mamir_cd"] is None and row["sug_mamir_nm"] is None
    stated = mapper.map_record({**record(37425), "sug_mamir_cd": 3, "sug_mamir_nm": "ממיר"})
    assert (stated["sug_mamir_cd"], stated["sug_mamir_nm"]) == (3, "ממיר")


def test_no_fuel_consumption_and_nothing_computed_from_co2():
    for payload in fixture_rows():
        row = mapper.map_record(payload)
        assert not any(word in key for key in row for word in ("consumption", "tzricha", "km_per", "l_per"))
        assert row["kamut_co2_city"] == payload["kamut_CO2_city"]
        assert row["kamut_co2_hway"] == payload["kamut_CO2_hway"]
        assert row["co2_wltp"] == payload["CO2_WLTP"]
    columns = {c for c, _f, _k in mapper.LEVEL_1_FIELDS + mapper.LEVEL_1_5_FIELDS}
    assert not any("consumption" in c for c in columns)


def test_empty_is_null_and_a_value_that_does_not_parse_is_null_with_an_issue():
    payload = {**record(37425), "merkav": "", "ramat_gimur": "   ", "koah_sus": None, "tozeret_eretz_nm": None}
    row = mapper.map_record(payload)
    assert (row["merkav"], row["ramat_gimur"], row["koah_sus"], row["tozeret_eretz_nm"]) == (None,) * 4
    assert row["parse_issues"] == []
    bad = mapper.map_record({**record(37425), "mishkal_kolel": "abc", "nefah_manoa": "2393.5",
                             "abs_ind": 7, "madad_yarok": "331.5", "mispar_dlatot": "5",
                             "tozeret_nm": 413, "zihuy_holchey_regel_ind": "yes"})
    assert (bad["mishkal_kolel"], bad["nefah_manoa"], bad["abs_ind"], bad["tozeret_nm"]) == (None,) * 4
    assert bad["madad_yarok"] == 331.5 and bad["mispar_dlatot"] == 5
    assert "zihuy_holchey_regel_ind" not in bad["equipment"]
    assert bad["parse_issues"] == [
        {"field": "tozeret_nm", "reason": "not_text"},
        {"field": "nefah_manoa", "reason": "not_a_whole_number"},
        {"field": "mishkal_kolel", "reason": "not_a_number"},
        {"field": "abs_ind", "reason": "not_an_indicator"},
        {"field": "zihuy_holchey_regel_ind", "reason": "not_an_indicator"}]
    assert {issue["reason"] for issue in bad["parse_issues"]} <= set(mapper.PARSE_REASONS)


@pytest.mark.parametrize("value,reason", [
    ("1e400", "not_a_number"), ("1E3", "not_a_number"), (float("inf"), "not_a_number"),
    (float("nan"), "not_a_number"), ("1_000", "not_a_number"), ("١٢٣", "not_a_number"),
    ("12,5", "not_a_number"), ("1234567890123456", "out_of_range"), (10 ** 16, "out_of_range"),
])
def test_a_number_the_register_did_not_state_plainly_is_an_issue_never_a_failure(value, reason):
    row = mapper.map_record({**record(37425), "madad_yarok": value})
    assert row["madad_yarok"] is None
    assert {"field": "madad_yarok", "reason": reason} in row["parse_issues"]
    json.dumps(row, allow_nan=False)      # the batch is always valid JSON
    assert mapper.map_record({**record(37425), "madad_yarok": " 331.5 "})["madad_yarok"] == 331.5
    assert mapper.map_record({**record(37425), "mishkal_kolel": "-3"})["mishkal_kolel"] == -3


def test_the_equipment_document_has_a_closed_key_list():
    for payload in fixture_rows():
        row = mapper.map_record({**payload, "some_new_register_field_ind": 1})
        assert set(row["equipment"]) <= set(mapper.EQUIPMENT_FIELDS)
        assert "some_new_register_field_ind" not in row["equipment"]
    full = mapper.map_record(record(37425))["equipment"]
    assert full["zihuy_holchey_regel_makor_hatkana"] == "יצרן" and full["matzlemat_reverse_ind"] == 1
    with pytest.raises(mapper.VariantMappingError) as refused:
        mapper.check_equipment({"mazgan_ind": 1})
    assert refused.value.reason_code == "CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN"
    with pytest.raises(mapper.VariantMappingError):
        mapper.check_equipment({"zihuy_holchey_regel_ind": 2})
    # The database's CHECK names exactly the same list.
    sql = MIGRATION.read_text()
    listed = re.search(r"e\.key <> all \(array\[(.*?)\]::text\[\]\)", sql, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", listed)) == set(mapper.EQUIPMENT_FIELDS)


def test_the_database_pins_the_mapper_version_and_the_parse_reasons():
    sql = MIGRATION.read_text()
    assert f"select '{mapper.MAPPER_VERSION}'::text" in sql
    reasons = re.search(r"\(i->>'reason'\) not in \((.*?)\)\)", sql, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", reasons)) == set(mapper.PARSE_REASONS)
    for column, _field, _kind in mapper.LEVEL_1_FIELDS + mapper.LEVEL_1_5_FIELDS:
        assert re.search(rf"^  {column} (text|integer|numeric|smallint)", sql, re.M), column
    for column in mapper.NORMALISED_COLUMNS:
        assert re.search(rf"^  {column} text", sql, re.M), column


def test_a_row_without_an_id_is_refused():
    with pytest.raises(mapper.VariantMappingError):
        mapper.map_record({"tozar": "טויוטה"})


# =============================================================================
# 2. the category (L1-2)
# =============================================================================

def test_every_sug_degem_in_the_fixtures_maps_and_unknown_is_unknown():
    counts: dict[str, int] = {}
    for payload in fixture_rows():
        segment = mapper.map_record(payload)["vehicle_segment"]
        assert segment != mapper.UNKNOWN_SEGMENT, payload["sug_degem"]
        counts[segment] = counts.get(segment, 0) + 1
    assert counts == {"private": 16}
    assert mapper.vehicle_segment("P") == "private" and mapper.vehicle_segment("M") == "commercial"
    for unknown in ("X", "p", "", None, 5, "PM"):
        assert mapper.vehicle_segment(unknown) == "unknown"
    assert set(mapper.SEGMENT_BY_SUG_DEGEM.values()) | {"unknown"} == set(mapper.SEGMENTS)
    assert "check (vehicle_segment in ('private', 'commercial', 'unknown'))" in MIGRATION.read_text()


# =============================================================================
# 3. the build (L1-4) and the ledger (L1-5), on the memory mirror
# =============================================================================

def built_world():
    """A captured Toyota unit of the 16 real rows; the capture job built it."""
    repo, w, _version, report, _writer = captured_world(fixture_rows())
    snapshot = snapshot_by_key(repo, report.units[0].snapshot_key)
    return repo, w, report, snapshot


def variant_rows(repo: MemoryRepository, version: str = mapper.MAPPER_VERSION) -> dict[str, dict[str, Any]]:
    return {u: dict(v) for (_s, u, mv), v in repo._variants_state()["rows"].items() if mv == version}


def test_the_capture_job_builds_the_captured_snapshot_and_reports_it():
    repo, _w, report, snapshot = built_world()
    unit = report.units[0]
    assert unit.status == "captured"
    assert unit.variants == {"status": "built", "built_rows": 16, "expected_rows": 16}
    assert unit.as_document()["variants"]["status"] == "built"
    rows = variant_rows(repo)
    assert set(rows) == {str(r["_id"]) for r in fixture_rows()}
    row = rows["37425"]
    assert row["snapshot_key"] == snapshot["snapshot_key"] and row["archive_line"] >= 1
    assert re.fullmatch(r"[0-9a-f]{64}", row["variant_identity_key"])


def test_a_rebuild_is_a_no_op():
    repo, _w, _report, snapshot = built_world()
    before = variant_rows(repo)
    ledger = copy.deepcopy(repo.catalog_variant_coverage)
    reads: list[int] = []
    original = repo.list_catalog_raw_records
    repo.list_catalog_raw_records = lambda *a, **k: reads.append(1) or original(*a, **k)
    answer = mapper.build_snapshot_variants(repo, snapshot["id"])
    assert answer["status"] == "unchanged" and answer["inserted"] == 0 and reads == []
    assert variant_rows(repo) == before and repo.catalog_variant_coverage == ledger


def test_the_build_is_bounded_batches_and_completes_a_partial_build():
    repo, _w, _report, snapshot = built_world()
    before = variant_rows(repo)
    repo._variants = None          # as if nothing were built yet
    limits: list[int] = []
    original = repo.list_catalog_raw_records

    def spy(snapshot_id, *, limit, offset=0):
        limits.append(limit)
        return original(snapshot_id, limit=limit, offset=offset)

    repo.list_catalog_raw_records = spy
    answer = mapper.build_snapshot_variants(repo, snapshot["id"], batch_rows=5)
    assert answer["status"] == "built" and answer["batches"] == 4 and answer["built_rows"] == 16
    assert set(limits) == {5}
    assert {u: {k: v for k, v in r.items() if k not in ("id", "created_at")} for u, r in variant_rows(repo).items()} \
        == {u: {k: v for k, v in r.items() if k not in ("id", "created_at")} for u, r in before.items()}
    assert mapper.build_snapshot_variants(repo, snapshot["id"], batch_rows=10_000)["status"] == "unchanged"


def test_a_new_mapper_version_writes_new_rows_and_never_mutates_old_ones(monkeypatch):
    repo, _w, _report, snapshot = built_world()
    old = copy.deepcopy(variant_rows(repo))
    monkeypatch.setattr(mapper, "MAPPER_VERSION", "gov.wltp.variant-mapper.2")
    answer = mapper.build_snapshot_variants(repo, snapshot["id"])
    assert answer["status"] == "built" and answer["inserted"] == 16
    assert variant_rows(repo, "gov.wltp.variant-mapper.1") == old
    assert len(variant_rows(repo, "gov.wltp.variant-mapper.2")) == 16


def test_the_ledger_records_both_new_levels_and_leaves_register_rows_alone():
    repo, _w, _report, _snapshot = built_world()
    keys = {r["variant_identity_key"] for r in variant_rows(repo).values()}
    levels = {(k, lvl) for (k, lvl) in repo.catalog_variant_coverage}
    assert levels == {(k, lvl) for k in keys for lvl in catalog_coverage.VARIANT_BUILD_LEVELS}
    for (key, _level), row in repo.catalog_variant_coverage.items():
        assert row["status"] == "enriched" and row["reason_code"] is None
        assert row["content_sha256"] in {r["content_sha256"] for r in variant_rows(repo).values()
                                          if r["variant_identity_key"] == key}
    # A `register` row of the same variant is untouched by a build.
    key = next(iter(keys))
    register_row = {"id": str(uuid4()), "variant_identity_key": key, "level": "register",
                    "status": "pending", "last_run_id": str(uuid4()), "snapshot_key": "cs1.other",
                    "content_sha256": "0" * 64, "vocabulary_version": "gov.wltp.vocabulary.2",
                    "reason_code": None}
    repo.catalog_variant_coverage[(key, "register")] = dict(register_row)
    repo._variants = None
    mapper.build_snapshot_variants(repo, _snapshot["id"])
    assert repo.catalog_variant_coverage[(key, "register")] == register_row
    # The batch surface never accepts the new levels.
    assert catalog_coverage.COVERAGE_LEVELS == ("register",)


def test_a_changed_content_updates_the_ledger_and_a_collision_fails_the_key():
    repo, _w, _report, snapshot = built_world()
    rows = variant_rows(repo)
    key = rows["37425"]["variant_identity_key"]
    assert repo.catalog_variant_coverage[(key, "identity")]["status"] == "enriched"
    before = repo.catalog_variant_coverage[(key, "identity")]["content_sha256"]
    # A second row of the snapshot states the same key with other content.
    state = repo._variants_state()
    peer = dict(rows["37439"], variant_identity_key=key, content_sha256="f" * 64)
    state["rows"][(snapshot["id"], "37439", mapper.MAPPER_VERSION)] = peer
    built = [v for (s, _u, mv), v in state["rows"].items() if s == snapshot["id"]]
    repo._refresh_variant_ledger(snapshot, built, {key})
    collided = repo.catalog_variant_coverage[(key, "government_fields")]
    assert collided["status"] == "failed" and collided["reason_code"] == catalog_coverage.KEY_COLLISION
    assert collided["content_sha256"] == catalog_coverage.collision_content_sha256({before, "f" * 64})


def test_a_failed_build_leaves_the_unit_captured():
    repo, w, _version, report, _writer = captured_world(fixture_rows())
    broken = MemoryRepository.record_catalog_variants

    def refuse(self, *args, **kwargs):
        raise mapper.VariantMappingError("CATALOG_VARIANT_ROWS_INVALID")

    MemoryRepository.record_catalog_variants = refuse
    try:
        assert mapper.build_after_capture(repo, snapshot_by_key(repo, report.units[0].snapshot_key)["id"]) \
            == {"status": "unchanged", "built_rows": 16, "expected_rows": 16}
        repo._variants = None
        assert mapper.build_after_capture(repo, snapshot_by_key(repo, report.units[0].snapshot_key)["id"]) \
            == {"status": "failed", "code": "CATALOG_VARIANT_ROWS_INVALID"}
    finally:
        MemoryRepository.record_catalog_variants = broken


def test_an_empty_snapshot_still_records_its_build():
    calls = []

    class Empty:
        def catalog_variant_build_state(self, *_args):
            return None

        def list_catalog_raw_records(self, *_args, **_kwargs):
            return []

        def record_catalog_variants(self, snapshot_id, version, rows):
            calls.append(rows)
            return {"snapshot_key": "cs1.x", "built_rows": 0, "expected_rows": 0, "complete": True,
                    "inserted": 0}

    assert mapper.build_snapshot_variants(Empty(), "s")["status"] == "built"
    assert calls == [[]]


def test_the_newest_capture_of_a_snapshot_decides_its_count_verification():
    repo, _w, _report, snapshot = built_world()
    units = repo._register_state()["units"]
    (unit,) = units.values()
    units[("old", "x")] = {**unit, "id": "u-old", "count_verified": False, "updated_at": "2000-01-01"}
    repo._variants = None
    assert mapper.build_snapshot_variants(repo, snapshot["id"])["status"] == "built"
    units[("new", "x")] = {**unit, "id": "u-new", "count_verified": False, "updated_at": "2999-01-01"}
    repo._variants = None
    with pytest.raises(Exception) as refused:
        mapper.build_snapshot_variants(repo, snapshot["id"])
    assert getattr(refused.value, "code", "") == "CATALOG_VARIANT_SNAPSHOT_INELIGIBLE"


def test_retention_keeps_a_snapshot_with_variants():
    repo, _w, _report, snapshot = built_world()
    kept = {row["snapshot_id"] for row in repo.prunable_register_snapshots()}
    assert snapshot["id"] not in kept


def test_an_ineligible_snapshot_is_refused():
    repo, _w, _report, snapshot = built_world()
    repo._variants = None
    del snapshot["retrieval_metadata"]["capture_scope"]
    with pytest.raises(Exception) as refused:
        mapper.build_snapshot_variants(repo, snapshot["id"])
    assert getattr(refused.value, "code", "") == "CATALOG_VARIANT_SNAPSHOT_INELIGIBLE"


# =============================================================================
# 4. the operator backfill entrypoint
# =============================================================================

def test_the_backfill_builds_one_snapshot_key_and_says_so(capsys):
    repo, _w, _report, snapshot = built_world()
    assert mapper.main(["--snapshot-key", snapshot["snapshot_key"]], repository=repo) == mapper.EXIT_OK
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"BUILT snapshot_key={snapshot['snapshot_key']} status=unchanged rows=16/16")
    repo._variants = None
    assert mapper.main(["--snapshot-key", snapshot["snapshot_key"]], repository=repo) == mapper.EXIT_OK
    assert "status=built rows=16/16 inserted=16 batches=1" in capsys.readouterr().out
    assert mapper.main(["--snapshot-key", "cs1.nope"], repository=repo) == mapper.EXIT_REFUSED
    assert capsys.readouterr().out.startswith("REFUSED CATALOG_VARIANT_SNAPSHOT_UNKNOWN")
    assert mapper.main(["--snapshot-key", "bad key"], repository=repo) == mapper.EXIT_REFUSED
    assert "טויוטה" not in capsys.readouterr().out


# =============================================================================
# 5. the read API (L1-3)
# =============================================================================

@pytest.fixture
def browsing(monkeypatch):
    repo, w, _report, snapshot = built_world()
    monkeypatch.setenv(browser.BROWSER_FLAG, "true")
    app.dependency_overrides[get_repository] = lambda: repo
    client = TestClient(app)
    base = f"/projects/{w['project']}/catalog/browser"
    headers = {"x-milo-auth-user-id": str(USER)}
    yield client, base, headers, repo


def test_the_tree_pages_down_to_variants(browsing):
    client, base, headers, _repo = browsing
    makers = client.get(f"{base}/manufacturers", headers=headers)
    assert makers.status_code == 200, makers.text
    assert makers.json() == {"total": 1, "limit": 50, "offset": 0, "items": [{"tozar": "טויוטה", "variants": 16}]}
    models = client.get(f"{base}/models", params={"tozar": "טויוטה"}, headers=headers).json()
    assert [(m["kinuy_mishari"], m["variants"], m["year_min"], m["year_max"]) for m in models["items"]] == \
        [("4RUNNER", 5, 2026, 2026), ("RAV4", 11, 2022, 2026)]
    years = client.get(f"{base}/years", params={"tozar": "טויוטה", "kinuy_mishari": "RAV4"}, headers=headers).json()
    assert [y["shnat_yitzur"] for y in years["items"]] == [2026, 2025, 2024, 2023, 2022]
    first = client.get(f"{base}/variants", params={"tozar": "טויוטה", "kinuy_mishari": "4RUNNER",
                                                  "shnat_yitzur": 2026, "limit": 2}, headers=headers).json()
    second = client.get(f"{base}/variants", params={"tozar": "טויוטה", "kinuy_mishari": "4RUNNER",
                                                   "shnat_yitzur": 2026, "limit": 2, "offset": 2},
                        headers=headers).json()
    assert first["total"] == second["total"] == 5 and len(first["items"]) == len(second["items"]) == 2
    ids = [i["upstream_record_id"] for i in first["items"] + second["items"]]
    assert len(set(ids)) == 4
    item = first["items"][0]
    assert item["coverage"]["identity"] == {"status": "enriched", "reason_code": None, "current": True}
    assert set(item["coverage"]) == {"identity", "government_fields"}
    assert "snapshot_id" not in item and item["snapshot_key"].startswith("cs1.")


def test_filters_and_facets(browsing):
    client, base, headers, _repo = browsing
    facets = client.get(f"{base}/facets", headers=headers).json()
    assert facets["segments"] == [{"value": "private", "variants": 16}]
    assert facets["year_min"] == 2022 and facets["year_max"] == 2026
    assert {b["merkav"] for b in facets["bodies"]} == {"פנאי-שטח"}
    filtered = client.get(f"{base}/models", params={"tozar": "טויוטה", "year_from": 2025}, headers=headers).json()
    assert [(m["kinuy_mishari"], m["variants"]) for m in filtered["items"]] == [("4RUNNER", 5), ("RAV4", 5)]
    none = client.get(f"{base}/manufacturers", params={"segment": "commercial"}, headers=headers).json()
    assert none == {"total": 0, "limit": 50, "offset": 0, "items": []}


@pytest.mark.parametrize("path,params", [
    ("manufacturers", {"limit": 0}), ("manufacturers", {"limit": 101}), ("manufacturers", {"segment": "truck"}),
    ("manufacturers", {"year_from": 2026, "year_to": 2020}), ("models", {}),
    ("variants", {"tozar": "טויוטה", "kinuy_mishari": "RAV4"}), ("manufacturers", {"offset": -1}),
    ("manufacturers", {"year_from": "²"}), ("manufacturers", {"delek_cd": "--5"}),
    ("manufacturers", {"year_from": "٢٠٢٠"}), ("manufacturers", {"limit": "1e2"}),
])
def test_an_invalid_query_is_422(browsing, path, params):
    client, base, headers, _repo = browsing
    answer = client.get(f"{base}/{path}", params=params, headers=headers)
    assert answer.status_code == 422 and answer.json()["error"]["code"] == "CATALOG_BROWSER_QUERY_INVALID"


def test_flag_off_is_404_and_unknown_levels_do_not_exist(browsing, monkeypatch):
    client, base, headers, _repo = browsing
    assert client.get(f"{base}/everything", headers=headers).status_code == 404
    monkeypatch.delenv(browser.BROWSER_FLAG)
    for level in (*browser.LEVELS, "everything"):
        answer = client.get(f"{base}/{level}", headers=headers)
        assert answer.status_code == 404 and answer.json()["error"]["code"] == "CATALOG_BROWSER_DISABLED"
    assert client.post(f"{base}/manufacturers", headers=headers).status_code in (403, 405)


def test_another_project_is_not_browsable(browsing):
    client, _base, headers, _repo = browsing
    answer = client.get(f"/projects/{uuid4()}/catalog/browser/manufacturers", headers=headers)
    assert answer.status_code in (403, 404)


def test_the_flag_is_a_stage_a_flag_pinned_off_and_restorable():
    from backend.production_config import EXECUTION_FLAGS

    assert browser.BROWSER_FLAG in EXECUTION_FLAGS
    contract = (ROOT / "scripts/deploy/deployment-contract.sh").read_text()
    stage_a = contract.split("MILO_STAGE_A_EXECUTION_FLAGS=(", 1)[1].split(")", 1)[0]
    assert "MILO_ENABLE_CATALOG_BROWSER=false" in stage_a
    assert "MILO_CATALOG_BROWSER_API_ENABLE_FLAGS=(\n  MILO_ENABLE_CATALOG_BROWSER\n)" in contract
    activate = (ROOT / "scripts/deploy/website-execution-activate.sh").read_text()
    assert '--apply-catalog-browser) MODE="apply-catalog-browser"' in activate
    for path in ("scripts/ops/website-stage.sh", "scripts/ops/deploy.sh", ".github/workflows/deploy.yml",
                 ".github/workflows/website-stage.yml"):
        assert "catalog-browser" in (ROOT / path).read_text(), path
