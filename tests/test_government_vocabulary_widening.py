"""PR-V V1 + V5: the reviewed register vocabulary, widened from production evidence.

Production Toyota snapshot cs1.e335295707fa8d6935b113bb6a0165f4 (6,374 rows)
read only fuel 1/7, propulsion 1/2 (+ uncoded "regular drive"), drivetrain 1/3
and ONE body name, so Toyota 2018+ was refused `vocabulary_insufficient`
(readable 1,645 vs ambiguous 3,023). PR-V widens the closed tables with the
codes and names that snapshot states, and nothing else:

* every mapping stays a closed, reviewed table; an unknown value stays
  unresolved, never guessed; a source value is never changed;
* a row that was READABLE before keeps exactly its reading and its candidate
  key; old snapshots are never re-read;
* R5's pinned subsets are untouched.

The measurement runs over `toyota_vocabulary_rows()`
(tests/swarm_v2_production_fixture.py): the snapshot's exact marginals and
fuel x propulsion joint, at production scale.
"""

from __future__ import annotations

import copy
from collections import Counter
from uuid import UUID, uuid4

import pytest

from backend.catalog import keys
from backend.catalog.government import normalize, vocabulary as vocab
from backend.catalog.government.normalize import GovernmentNormalizationError, read_wltp_record
from backend.catalog.government.projection import snapshot_usability
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import committed_records
from swarm_v2_production_fixture import (SNAPSHOT_ROWS, TOYOTA_2018_AMBIGUOUS_BEFORE,
                                         TOYOTA_2018_READABLE_BEFORE, toyota_vocabulary_rows)

#: The reviewed tables exactly as they stood before PR-V.
PRE_PR_V = {
    "FUEL_BY_CODE": {1: ("petrol", "בנזין"), 7: ("plug_in_hybrid", "חשמל/בנזין")},
    "PROPULSION_BY_CODE": {1: ("hybrid", "היברידי רגיל"), 2: ("plug_in", "PLUG IN")},
    "DRIVETRAIN_BY_CODE": {1: ("two_wheel_drive", "4X2"), 3: ("awd", "4X4")},
    "BODY_STYLE_BY_MERKAV": {"פנאי-שטח": "suv"},
    "CONSISTENT_FUEL_PROPULSION": frozenset({("petrol", "hybrid"), ("petrol", "conventional"),
                                             ("plug_in_hybrid", "plug_in")}),
}

#: Every English label PR-V adds (owner-approved).
PR_V_LABELS = {"diesel", "electric", "battery_electric", "sedan", "mpv", "hatchback",
               "wagon", "coupe", "convertible", "pickup_double_cab", "pickup_single_cab"}


@pytest.fixture()
def pre_pr_v(monkeypatch):
    """Read rows through the pre-PR-V tables (the normalizer holds the coded
    tables by reference, so its dimension map is swapped too)."""
    def apply():
        for name, table in PRE_PR_V.items():
            monkeypatch.setattr(vocab, name, table)
        monkeypatch.setattr(normalize, "_CODED_DIMENSIONS", {
            "fuel_type": ("delek_cd", "delek_nm", PRE_PR_V["FUEL_BY_CODE"], None),
            "propulsion_technology": ("technologiat_hanaa_cd", "technologiat_hanaa_nm",
                                      PRE_PR_V["PROPULSION_BY_CODE"],
                                      vocab.PROPULSION_BY_UNCODED_NAME),
            "drivetrain": ("hanaa_cd", "hanaa_nm", PRE_PR_V["DRIVETRAIN_BY_CODE"], None),
        })
    return apply


def _row(**changes) -> dict:
    row = copy.deepcopy(committed_records(1)[0])
    row.update(changes)
    return row


def _outcome(row) -> str:
    try:
        return read_wltp_record(row).status
    except GovernmentNormalizationError as refusal:
        return refusal.reason_code


def _key(reading) -> str:
    return keys.candidate_key(
        record_key=keys.raw_record_key(snapshot_key="cs1.test",
                                       upstream_record_id=reading.upstream_record_id),
        manufacturer=reading.manufacturer, commercial_model=reading.commercial_model,
        model_year_start=reading.model_year_start, model_year_end=reading.model_year_end,
        official_model_code=reading.official_model_code, trim=reading.trim,
        identity_dimensions=dict(reading.identity_dimensions))


# =============================================================================
# V1: the tables
# =============================================================================

def test_the_widened_tables_are_exactly_the_reviewed_additions():
    assert dict(vocab.FUEL_BY_CODE) == {**PRE_PR_V["FUEL_BY_CODE"],
                                        2: ("diesel", "דיזל"), 4: ("electric", "חשמל")}
    assert dict(vocab.PROPULSION_BY_CODE) == {**PRE_PR_V["PROPULSION_BY_CODE"],
                                              3: ("battery_electric", "רכב חשמלי")}
    assert dict(vocab.DRIVETRAIN_BY_CODE) == PRE_PR_V["DRIVETRAIN_BY_CODE"]
    assert vocab.CONSISTENT_FUEL_PROPULSION == PRE_PR_V["CONSISTENT_FUEL_PROPULSION"] | {
        ("diesel", "conventional"), ("electric", "battery_electric")}
    assert ("diesel", "hybrid") not in vocab.CONSISTENT_FUEL_PROPULSION
    assert dict(vocab.BODY_STYLE_BY_MERKAV) == {
        "פנאי-שטח": "suv", "סדאן": "sedan", "MPV": "mpv", "הצ'בק": "hatchback",
        "סטיישן": "wagon", "קופה": "coupe", "קבריולט": "convertible",
        "תא כפול": "pickup_double_cab", "תא בודד": "pickup_single_cab"}


def test_every_new_english_label_is_the_approved_list():
    labels = ({value for value, _name in vocab.FUEL_BY_CODE.values()}
              | {value for value, _name in vocab.PROPULSION_BY_CODE.values()}
              | set(vocab.BODY_STYLE_BY_MERKAV.values()))
    before = ({value for value, _name in PRE_PR_V["FUEL_BY_CODE"].values()}
              | {value for value, _name in PRE_PR_V["PROPULSION_BY_CODE"].values()}
              | set(PRE_PR_V["BODY_STYLE_BY_MERKAV"].values()))
    assert labels - before == PR_V_LABELS


@pytest.mark.parametrize("changes,dimensions", [
    ({"delek_cd": 2, "delek_nm": "דיזל", "technologiat_hanaa_cd": None,
      "technologiat_hanaa_nm": "הנעה רגילה"},
     {"fuel_type": "diesel", "propulsion_technology": "conventional"}),
    ({"delek_cd": 4, "delek_nm": "חשמל", "technologiat_hanaa_cd": 3,
      "technologiat_hanaa_nm": "רכב חשמלי"},
     {"fuel_type": "electric", "propulsion_technology": "battery_electric"}),
])
def test_the_new_fuel_and_propulsion_codes_read_completely(changes, dimensions):
    reading = read_wltp_record(_row(**changes))
    assert reading.status == "candidate"
    assert {name: reading.identity_dimensions[name] for name in dimensions} == dimensions


@pytest.mark.parametrize("merkav,body", sorted(
    (name, style) for name, style in vocab.BODY_STYLE_BY_MERKAV.items()))
def test_each_mapped_body_name_reads(merkav, body):
    reading = read_wltp_record(_row(merkav=merkav))
    assert reading.identity_dimensions["body_style"] == body
    assert reading.status == "candidate"


@pytest.mark.parametrize("merkav", ["משא אחוד", "קומבי", "שדה", "ואן/נוסעים"])
def test_the_names_left_unmapped_stay_unresolved_with_a_written_reason(merkav):
    reading = read_wltp_record(_row(merkav=merkav))
    assert reading.status == "ambiguous"
    assert reading.unresolved_dimensions == ("body_style",)
    assert "body_style" not in reading.identity_dimensions
    assert vocab.UNMAPPED_MERKAV_REASONS[merkav].strip()


def test_an_empty_body_name_states_nothing_and_is_unchanged():
    reading = read_wltp_record(_row(merkav=""))
    assert "body_style" not in reading.identity_dimensions
    assert "body_style" not in reading.unresolved_dimensions
    assert vocab.UNMAPPED_MERKAV_REASONS[""].strip()


def test_diesel_with_a_hybrid_propulsion_stays_inconsistent_and_is_unresolved():
    """(diesel, hybrid) is NOT a consistent pair. Each code matches its own
    label, so the row is not refused: neither value is settled, both
    dimensions are unresolved, and the row is an ambiguous candidate."""
    assert ("diesel", "hybrid") not in vocab.CONSISTENT_FUEL_PROPULSION
    reading = read_wltp_record(_row(delek_cd=2, delek_nm="דיזל", technologiat_hanaa_cd=1,
                                    technologiat_hanaa_nm="היברידי רגיל"))
    assert reading.status == "ambiguous"
    assert {"fuel_type", "propulsion_technology"} <= set(reading.unresolved_dimensions)
    assert not {"fuel_type", "propulsion_technology"} & set(reading.identity_dimensions)
    # The other dimensions the row states are still read.
    assert reading.identity_dimensions["drivetrain"] == "awd"
    assert reading.identity_dimensions["body_style"] == "suv"


@pytest.mark.parametrize("changes", [
    {"delek_cd": 1, "delek_nm": "בנזין", "technologiat_hanaa_cd": 3,
     "technologiat_hanaa_nm": "רכב חשמלי"},
    {"delek_cd": 4, "delek_nm": "חשמל", "technologiat_hanaa_cd": 1,
     "technologiat_hanaa_nm": "היברידי רגיל"},
    {"delek_cd": 7, "delek_nm": "חשמל/בנזין", "technologiat_hanaa_cd": None,
     "technologiat_hanaa_nm": "הנעה רגילה"},
])
def test_every_inconsistent_pair_is_unresolved_never_a_special_case(changes):
    reading = read_wltp_record(_row(**changes))
    assert reading.status == "ambiguous"
    assert {"fuel_type", "propulsion_technology"} <= set(reading.unresolved_dimensions)


@pytest.mark.parametrize("changes", [
    {"delek_cd": 2, "delek_nm": "בנזין"}, {"delek_cd": 4, "delek_nm": "דיזל"},
    {"technologiat_hanaa_cd": 3, "technologiat_hanaa_nm": "PLUG IN"}])
def test_a_drifted_pairing_of_a_new_code_is_refused_never_guessed(changes):
    assert _outcome(_row(**changes)) == "GOV_NORM_LABEL_CONTRADICTION"


def test_absent_fuel_and_drivetrain_codes_are_unchanged():
    reading = read_wltp_record(_row(delek_cd=None, delek_nm=None, hanaa_cd=None,
                                    hanaa_nm=None, technologiat_hanaa_cd=None,
                                    technologiat_hanaa_nm="הנעה רגילה"))
    assert reading.status == "candidate"
    assert "fuel_type" not in reading.identity_dimensions
    assert "drivetrain" not in reading.identity_dimensions


def test_source_values_are_never_changed():
    row = _row(merkav="סדאן", delek_cd=2, delek_nm="דיזל", technologiat_hanaa_cd=None,
               technologiat_hanaa_nm="הנעה רגילה", degem_nm="TZNA55L GKZSZA")
    before = copy.deepcopy(row)
    reading = read_wltp_record(row)
    assert row == before
    assert reading.official_model_code == "TZNA55L GKZSZA"


def test_r5_keeps_its_pinned_subsets():
    from backend.testing.r5_proof import government as r5

    assert set(r5.FUEL_BY_CODE) == {1, 7}
    assert set(r5.PROPULSION_BY_CODE) == {1, 2}
    assert set(r5.DRIVETRAIN_BY_CODE) == {1, 3}
    assert set(r5.BODY_STYLE_BY_MERKAV) == {"פנאי-שטח"}
    # The shared consistency table only gained pairs R5 can never decode.
    decodable = {value for value, _ in r5.FUEL_BY_CODE.values()}
    assert all(fuel in decodable for fuel, _ in PRE_PR_V["CONSISTENT_FUEL_PROPULSION"])
    assert not {fuel for fuel, _ in vocab.CONSISTENT_FUEL_PROPULSION
                - PRE_PR_V["CONSISTENT_FUEL_PROPULSION"]} & decodable


# =============================================================================
# candidate keys of previously readable rows never change
# =============================================================================

def test_no_previously_readable_row_changes_its_reading_or_candidate_key(pre_pr_v):
    rows = toyota_vocabulary_rows()
    after = {}
    for row in rows:
        try:
            after[row["_id"]] = read_wltp_record(row)
        except GovernmentNormalizationError:
            after[row["_id"]] = None
    pre_pr_v()
    readable_before = 0
    for row in rows:
        try:
            reading = read_wltp_record(row)
        except GovernmentNormalizationError:
            continue
        if reading.status != "candidate":
            continue
        readable_before += 1
        assert after[row["_id"]] == reading
        assert _key(after[row["_id"]]) == _key(reading)
    assert readable_before > 0


# =============================================================================
# V5: the measurement, at production scale
# =============================================================================

def _count(rows) -> Counter:
    return Counter(_outcome(row) for row in rows)


def test_readable_and_ambiguous_counts_before_and_after(pre_pr_v):
    rows = toyota_vocabulary_rows()
    recent = [row for row in rows if row["shnat_yitzur"] >= 2018]
    after_all, after_recent = _count(rows), _count(recent)
    pre_pr_v()
    before_all, before_recent = _count(rows), _count(recent)

    assert len(rows) == SNAPSHOT_ROWS
    # Before: the counts production reported for the 2018+ scope.
    assert (before_recent["candidate"], before_recent["ambiguous"]) == (
        TOYOTA_2018_READABLE_BEFORE, TOYOTA_2018_AMBIGUOUS_BEFORE)
    # (The whole-snapshot split depends on the fixture's interleave of body
    # names across fuel cells, which the snapshot's published marginals do
    # not fix; the 2018+ split above is the one production reported.)
    assert before_all == Counter({"candidate": 2_141, "ambiguous": 4_233})
    # After: the four unmapped body names stay unresolved (188 rows) and the
    # 2 diesel+hybrid rows are unresolved in fuel and propulsion: 6,184 +
    # 190 = 6,374, nothing refused.
    assert after_all == Counter({"candidate": 6_184, "ambiguous": 190})
    assert sum(after_all.values()) == SNAPSHOT_ROWS
    # (In this fixture both diesel+hybrid rows fall before 2018.)
    assert after_recent == Counter({"candidate": 4_517, "ambiguous": 151})


def _reading_without_the_pair_rule(row, monkeypatch):
    """The reading the CONSISTENCY rule starts from (every pair allowed)."""
    with monkeypatch.context() as patch:
        patch.setattr(vocab, "CONSISTENT_FUEL_PROPULSION", _EveryPair())
        return read_wltp_record(row)


class _EveryPair(frozenset):
    def __contains__(self, _item) -> bool:
        return True


def test_only_the_two_diesel_hybrid_readings_change(monkeypatch):
    """Against the previous rule (an inconsistent pair REFUSED the row):
    exactly the two rows stating diesel with a hybrid propulsion change --
    from a refusal to an ambiguous candidate -- and every other reading is
    identical. The two rows are found by what they STATE, not by record id."""
    rows = toyota_vocabulary_rows()
    stating = {row["_id"] for row in rows
               if (row["delek_cd"], row["technologiat_hanaa_cd"]) == (2, 1)}
    assert len(stating) == 2
    changed = set()
    for row in rows:
        base = _reading_without_the_pair_rule(row, monkeypatch)
        pair = (base.identity_dimensions.get("fuel_type"),
                base.identity_dimensions.get("propulsion_technology"))
        previously_refused = None not in pair and pair not in vocab.CONSISTENT_FUEL_PROPULSION
        now = read_wltp_record(row)
        if previously_refused:
            changed.add(row["_id"])
            assert now.status == "ambiguous"
            assert {name: value for name, value in now.identity_dimensions.items()} == {
                name: value for name, value in base.identity_dimensions.items()
                if name not in ("fuel_type", "propulsion_technology")}
        else:
            assert now == base
    assert changed == stating


# --- the vocabulary gate, through the real repository gate ---------------------

def _land(rows, *, model_year_from=2018):
    """A plan whose Toyota snapshot holds exactly `rows`' real readings, prepared
    through the repository's own vocabulary gate."""
    from backend.catalog.government import source as src
    from backend.catalog.government.capture_scope import CaptureScope
    from backend.catalog.government.client import DataGovClient
    from backend.catalog.government.ingest import GovernmentCatalogIngestor
    from backend.catalog.scope import contract as wsc
    from backend.testing import work_scope_seed as seed

    repository = MemoryRepository()
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "vocab", "Vocab", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))["id"]
    scope = wsc.scope_from_fields({"units": ["toyota"], "model_year_from": model_year_from,
                                   "model_year_to": None, "max_items": 25, "batch_size": 10})
    plan = repository.create_work_scope(
        UUID(conversation), UUID(user),
        {"scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
         "notes": []})["work_scope"]
    lease = seed._capture_lease(repository, conversation, user)
    client = DataGovClient(seed.FixtureTransport(bodies={0: seed.scoped_page(
        committed_records(1))}), page_limit=seed.SCOPED_PAGE_LIMIT,
        sleep_fn=lambda _seconds: None)
    report = GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
        src.WLTP_RESOURCE_ID, capture_scope=CaptureScope.for_register_marque(seed.TOYOTA_REGISTER_MARQUE))
    snapshot = repository.find_active_catalog_snapshot(
        src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, report.snapshot_key)
    # Grow the snapshot to `rows`, each candidate carrying its REAL reading.
    (template,) = [row for row in repository.catalog_candidates.values()
                   if row["snapshot_id"] == snapshot["id"]]
    raw = next(row for row in repository.catalog_raw_records.values()
               if row["snapshot_id"] == snapshot["id"])
    repository.catalog_candidates.clear()
    repository.catalog_raw_records.clear()
    issues: Counter = Counter()
    for index, row in enumerate(rows):
        try:
            reading = read_wltp_record(row)
        except GovernmentNormalizationError as refusal:
            issues[refusal.reason_code] += 1
            continue
        record = copy.deepcopy(raw)
        record.update({"id": str(uuid4()), "record_key": f"cr1.{index:032x}",
                       "upstream_record_id": reading.upstream_record_id, "payload": dict(row)})
        candidate = copy.deepcopy(template)
        candidate.update({"id": str(uuid4()), "candidate_key": f"cc1.{index:032x}",
                          "raw_record_id": record["id"], "status": reading.status,
                          "commercial_model": reading.commercial_model,
                          "model_year_start": reading.model_year_start,
                          "model_year_end": reading.model_year_end,
                          "official_model_code": reading.official_model_code,
                          "trim": reading.trim,
                          "identity_dimensions": dict(reading.identity_dimensions)})
        repository.catalog_raw_records[(snapshot["id"], record["record_key"])] = record
        repository.catalog_candidates[(snapshot["id"], candidate["candidate_key"])] = candidate
    metadata = snapshot["retrieval_metadata"]
    refused = sum(issues.values())
    metadata.update({"reported_total": len(rows), "captured_record_count": len(rows),
                     "normalized_record_count": len(rows) - refused,
                     "normalization_issue_count": refused,
                     "normalization_issues": [{"reason": reason, "count": count}
                                              for reason, count in sorted(issues.items())],
                     "normalization_issue_records": [str(row["_id"]) for row in rows
                                                     if _outcome(row) in issues][:10]})
    snapshot["stored_record_count"] = snapshot["declared_record_count"] = len(rows)
    return repository, plan, lease, snapshot


def _prepare(repository, plan, lease, snapshot) -> dict:
    return repository.prepare_work_scope_queue(
        lease.run_id, {"work_scope_id": plan["id"], "revision": plan["head_revision"],
                       "scope_digest": plan["head_digest"],
                       "units": [{"unit_key": "toyota", "priority": 1, "state": "captured",
                                  "register_marque": "טויוטה", "snapshot_id": snapshot["id"],
                                  "reason_code": None}]},
        worker_id=lease.worker_id, attempt=lease.attempt, lease_token=lease.lease_token)


def test_before_pr_v_the_2018_scope_was_vocabulary_insufficient(pre_pr_v):
    pre_pr_v()
    rows = toyota_vocabulary_rows()
    repository, plan, lease, snapshot = _land(rows)
    (unit,) = _prepare(repository, plan, lease, snapshot)["units"]
    assert (unit["state"], unit["readable_count"], unit["ambiguous_count"]) == (
        "vocabulary_insufficient", TOYOTA_2018_READABLE_BEFORE, TOYOTA_2018_AMBIGUOUS_BEFORE)


@pytest.mark.parametrize("model_year_from,readable,ambiguous", [
    (2018, 4_517, 151),   # the 2018+ scope production refused
    (None, 6_184, 190),   # every year: the two diesel+hybrid rows included
])
def test_after_pr_v_the_complete_fixture_is_complete_and_passes_the_gate(
        model_year_from, readable, ambiguous):
    """The COMPLETE 6,374-row fixture, both diesel+hybrid rows included: no row
    is refused, the snapshot is complete, and preparation passes its
    vocabulary gate. (A reconstructed fixture with production's distribution,
    not a fresh production capture.)"""
    rows = toyota_vocabulary_rows()
    assert len(rows) == SNAPSHOT_ROWS
    repository, plan, lease, snapshot = _land(rows, model_year_from=model_year_from)
    metadata = snapshot["retrieval_metadata"]
    assert metadata["normalization_issue_count"] == 0
    assert metadata["normalized_record_count"] == SNAPSHOT_ROWS
    assert snapshot_usability(snapshot) is None
    (unit,) = _prepare(repository, plan, lease, snapshot)["units"]
    assert (unit["state"], unit["readable_count"], unit["ambiguous_count"]) == (
        "prepared", readable, ambiguous)
    assert unit["queued_count"] == 25
