"""Scoped catalog PR1: the ONE canonical, server-owned WorkScope.

What this proves
----------------

1.  The contract: one canonical record, one canonical text, one digest, and a
    validator that refuses rather than repairs.
2.  The directory: closed, sorted, and honest about evidence -- the one
    register spelling it records is the one the committed register rows state.
3.  The instruction reader: the golden sentences in English and Hebrew, how an
    instruction changes an existing plan, the "not mapped yet" qualifier, and
    what it refuses. It is deterministic, and it is not a model.
4.  Chat and clicks converge: a typed instruction and the equivalent Mapping
    Plan edit produce the SAME stored record and the SAME digest, through the
    real API.
5.  The API surface: gated writes, membership-scoped reads, a stale head that
    fails closed, and no path from any of it to a run, a launch or a provider.

Offline and deterministic: an autouse fixture refuses every outbound
connection, and no model is called anywhere. `tests/test_migrations_postgres.py`
carries the half that is SQL.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.catalog.scope import contract as wsc
from backend.catalog.scope import coverage as wcov
from backend.catalog.scope import directory as mdir
from backend.catalog.scope import interpret as wi
from backend.catalog.scope import service as ws
from backend.dependencies import get_job_launcher, get_repository
from backend.errors import AppError
from backend.execution_guard import SURFACE_RULES, find_disabled_surface
from backend.main import app
from backend.testing.memory_repository import MemoryRepository

USER = UUID("11111111-2222-4333-8444-555555555555")
OUTSIDER = UUID("99999999-2222-4333-8444-555555555555")
FLAG = ws.WORK_SCOPE_MUTATIONS_FLAG
R5_PAGES = sorted(Path("backend/testing/r5_proof/fixtures/government").glob("wltp_page_*.json"))

GOLDEN_EN = (
    "Map Toyota and Lexus, starting with 2018+, up to 800 variants.",
    "Continue with Japanese manufacturers we have not mapped yet.",
    "Do Toyota first, then Mazda. Stop after 500 vehicles.",
)
GOLDEN_HE = (
    "מפה את טויוטה ולקסוס, החל מ-2018, עד 800 דגמים.",
    "המשך עם יצרנים יפניים שעוד לא מיפינו.",
    "קודם טויוטה, אחר כך מאזדה. עצור אחרי 500 רכבים.",
)
JAPANESE = ("honda", "isuzu", "lexus", "mazda", "mitsubishi", "nissan", "subaru", "suzuki",
            "toyota")


@pytest.fixture(autouse=True)
def no_outbound_connections(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a work scope test attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def fields(**overrides: Any) -> dict[str, Any]:
    base = {"units": ["toyota", "lexus"], "model_year_from": 2018, "model_year_to": None,
            "max_items": 800, "batch_size": 10}
    return {**base, **overrides}


def interpret(text: str, current: wsc.WorkScope | None = None,
              coverage: dict[str, int | None] | None = None) -> wi.Interpretation:
    reading = wi.read_instruction(text)
    if reading.needs_coverage and coverage is None:
        coverage = {entry.key: (0 if entry.register_marque_verified else None)
                    for entry in mdir.DIRECTORY}
    return wi.apply_reading(reading, current, coverage)


def note_codes(interpretation: wi.Interpretation) -> list[str]:
    return [note.code for note in interpretation.notes]


# =============================================================================
# 1. the contract
# =============================================================================

def test_the_canonical_record_text_and_digest_are_one_deterministic_rendering():
    scope = wsc.scope_from_fields(fields())
    text = scope.canonical_text()
    assert text == json.dumps(scope.as_record(), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=True)
    assert text.isascii()
    assert scope.digest() == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # The record states the contract, the directory it was validated against
    # and the pinned source -- all server-owned, none editable.
    record = scope.as_record()
    assert record["contract"] == "milo-work-scope/1"
    assert record["directory_version"] == mdir.DIRECTORY_VERSION
    assert record["source"] == {"family": "government", "package_id": "degem-rechev-wltp",
                                "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40"}
    # Equal fields are one digest; any change is a different digest, and unit
    # ORDER is part of the plan (it is the priority).
    assert wsc.scope_from_fields(fields()).digest() == scope.digest()
    assert wsc.scope_from_fields(fields(units=["lexus", "toyota"])).digest() != scope.digest()
    assert wsc.scope_from_fields(fields(batch_size=11)).digest() != scope.digest()


def test_the_server_bounds_are_the_first_paid_posture():
    assert (wsc.DEFAULT_BATCH_SIZE, wsc.MAX_BATCH_SIZE) == (10, 20)
    assert wsc.MAX_WORK_SCOPE_ITEMS == 2000
    assert wsc.DEFAULT_MAX_ITEMS <= wsc.MAX_WORK_SCOPE_ITEMS
    assert wsc.MAX_UNITS == len(mdir.DIRECTORY)


@pytest.mark.parametrize("edit, code", [
    ({"units": ["toyota"]}, "WORK_SCOPE_FIELDS_INVALID"),
    ({**fields(), "directory_version": "x"}, "WORK_SCOPE_FIELDS_INVALID"),
    ({**fields(), "digest": "0" * 64}, "WORK_SCOPE_FIELDS_INVALID"),
    ("not a mapping", "WORK_SCOPE_FIELDS_INVALID"),
    (fields(units=[]), "WORK_SCOPE_UNITS_INVALID"),
    (fields(units="toyota"), "WORK_SCOPE_UNITS_INVALID"),
    (fields(units=["toyota", "toyota"]), "WORK_SCOPE_UNITS_INVALID"),
    (fields(units=["Toyota"]), "WORK_SCOPE_UNITS_INVALID"),
    (fields(units=["polestar"]), "WORK_SCOPE_UNITS_INVALID"),
    (fields(units=[" toyota"]), "WORK_SCOPE_UNITS_INVALID"),
    (fields(model_year_from=1899), "WORK_SCOPE_YEARS_INVALID"),
    (fields(model_year_to=2101), "WORK_SCOPE_YEARS_INVALID"),
    (fields(model_year_from=2024, model_year_to=2018), "WORK_SCOPE_YEARS_INVALID"),
    (fields(model_year_from="2018"), "WORK_SCOPE_YEARS_INVALID"),
    (fields(model_year_from=2018.0), "WORK_SCOPE_YEARS_INVALID"),
    (fields(model_year_from=True), "WORK_SCOPE_YEARS_INVALID"),
    (fields(max_items=0), "WORK_SCOPE_MAX_ITEMS_INVALID"),
    (fields(max_items=2001), "WORK_SCOPE_MAX_ITEMS_INVALID"),
    (fields(max_items="800"), "WORK_SCOPE_MAX_ITEMS_INVALID"),
    (fields(max_items=None), "WORK_SCOPE_MAX_ITEMS_INVALID"),
    (fields(batch_size=0), "WORK_SCOPE_BATCH_SIZE_INVALID"),
    (fields(batch_size=21), "WORK_SCOPE_BATCH_SIZE_INVALID"),
    (fields(batch_size=True), "WORK_SCOPE_BATCH_SIZE_INVALID"),
    (fields(batch_size=10.0), "WORK_SCOPE_BATCH_SIZE_INVALID"),
])
def test_the_validator_refuses_by_field_and_never_repairs(edit, code):
    with pytest.raises(wsc.WorkScopeError) as refused:
        wsc.scope_from_fields(edit)
    assert refused.value.code == code
    # The refusal carries static copy only: nothing that was sent is echoed.
    assert refused.value.safe_message == wsc.WORK_SCOPE_REASONS[code]


def test_every_directory_entry_fits_in_one_plan_within_the_stored_bound():
    scope = wsc.scope_from_fields(fields(units=[entry.key for entry in mdir.DIRECTORY],
                                         model_year_to=2100, max_items=2000))
    assert len(scope.canonical_text()) <= wsc.MAX_SCOPE_TEXT_CHARS
    assert wsc.stored_record_valid(scope.as_record())


def test_a_stored_plan_reads_back_strictly():
    scope = wsc.scope_from_fields(fields())
    assert wsc.scope_from_text(scope.canonical_text()) == scope
    record = scope.as_record()
    for broken in (json.dumps(record),                                   # not canonical
                   wsc.canonical_text({**record, "extra": 1}),           # closed key set
                   wsc.canonical_text({**record, "contract": "milo-work-scope/2"}),
                   wsc.canonical_text({**record, "source": {"family": "government"}}),
                   wsc.canonical_text({**record, "batch_size": 21}),
                   wsc.canonical_text({**record, "model_years": {"from": 2018}}),
                   "[]", "not json", "", None, "x" * (wsc.MAX_SCOPE_TEXT_CHARS + 1)):
        with pytest.raises(wsc.WorkScopeError) as refused:
            wsc.scope_from_text(broken)
        assert refused.value.code == "WORK_SCOPE_RECORD_INVALID"


def test_a_plan_from_an_older_directory_still_reads_and_says_it_is_not_current():
    record = {**wsc.scope_from_fields(fields()).as_record(),
              "directory_version": "milo-manufacturer-directory/0"}
    scope = wsc.scope_from_text(wsc.canonical_text(record))
    assert scope.directory_version == "milo-manufacturer-directory/0"
    assert scope.current is False
    assert wsc.scope_from_fields(fields()).current is True


def test_the_stored_shape_twin_refuses_exactly_what_the_database_refuses():
    """The same broken records `test_no_path_can_store_a_revision_that_
    disagrees_with_its_own_text` sends to PostgreSQL."""
    good = wsc.scope_from_fields(fields(units=["mazda"])).as_record()
    assert wsc.stored_record_valid(good)
    for broken in ({**good, "batch_size": 21}, {**good, "max_items": 2001},
                   {**good, "units": ["toyota", "toyota"]}, {**good, "units": []},
                   {**good, "model_years": {"from": 2024, "to": 2018}}, {**good, "extra": True},
                   {**good, "contract": "milo-work-scope/2"},
                   {**good, "source": {"family": "government"}},
                   {**good, "units": ["Toyota"]}, {**good, "batch_size": True},
                   {**good, "directory_version": ""}):
        assert not wsc.stored_record_valid(broken), broken
    # SHAPE, not the directory: SQL cannot know which keys exist.
    assert wsc.stored_record_valid({**good, "units": ["a_future_marque"]})


# =============================================================================
# 2. the directory
# =============================================================================

def test_the_directory_is_closed_sorted_and_ascii_keyed():
    keys = [entry.key for entry in mdir.DIRECTORY]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)
    assert all(key.isascii() and key == key.lower() for key in keys)
    assert {entry.origin for entry in mdir.DIRECTORY} <= set(mdir.ORIGIN_LABELS)
    assert mdir.entry_for("toyota").name == "Toyota"
    assert mdir.entry_for("Toyota") is None and mdir.entry_for(None) is None


def test_the_only_verified_register_spelling_is_the_one_the_committed_register_rows_state():
    """The evidence claim, re-derived from the evidence.

    `VERIFIED_REGISTER_MARQUES` names exactly one spelling, and every row of
    the committed R5 capture states exactly that `tozar`. Every other entry
    says, on every read, that its register spelling is NOT verified.
    """
    assert dict(mdir.VERIFIED_REGISTER_MARQUES) == {"toyota": "טויוטה"}
    rows = [row for page in R5_PAGES
            for row in json.loads(page.read_text(encoding="utf-8"))["result"]["records"]]
    assert len(rows) == 233
    assert {row["tozar"] for row in rows} == {"טויוטה"}
    verified = [entry.key for entry in mdir.DIRECTORY if entry.register_marque_verified]
    assert verified == ["toyota"]
    assert all(entry.register_marque is None for entry in mdir.DIRECTORY if entry.key != "toyota")


def test_every_entry_is_recognized_by_its_own_names_and_no_alias_is_shared():
    for entry in mdir.DIRECTORY:
        for name in (entry.name, entry.name_he):
            reading = wi.read_instruction(f"map {name}")
            assert [m.keys for m in reading.mentions] == [(entry.key,)], (entry.key, name)
    # `_alias_index` refuses at import if two entries shared a spelling; the
    # index covers every entry.
    assert set(wi.ALIASES.values()) == {entry.key for entry in mdir.DIRECTORY}


# =============================================================================
# 3. the instruction reader
# =============================================================================

@pytest.mark.parametrize("english, hebrew", list(zip(GOLDEN_EN, GOLDEN_HE)))
def test_the_golden_sentences_read_identically_in_english_and_hebrew(english, hebrew):
    assert interpret(english).fields == interpret(hebrew).fields
    assert note_codes(interpret(english)) == note_codes(interpret(hebrew))


def test_golden_one_names_two_marques_a_lower_year_bound_and_a_limit():
    result = interpret(GOLDEN_EN[0])
    assert result.fields == {"units": ["toyota", "lexus"], "model_year_from": 2018,
                             "model_year_to": None, "max_items": 800, "batch_size": 10}
    assert result.notes == ()


def test_golden_two_expands_the_japanese_marques_and_states_what_it_cannot_know():
    result = interpret(GOLDEN_EN[1])
    assert result.fields["units"] == list(JAPANESE)
    # Only Toyota's coverage can be stated (it is verified and zero); the rest
    # are KEPT, with a note -- a marque is never skipped for what is unknown.
    notes = {note.code: note for note in result.notes}
    assert notes["WORK_SCOPE_NOTE_COVERAGE_UNKNOWN"].units == tuple(k for k in JAPANESE
                                                                     if k != "toyota")
    assert "WORK_SCOPE_NOTE_DEFAULT_LIMIT" in notes
    assert result.fields["max_items"] == wsc.DEFAULT_MAX_ITEMS


def test_golden_two_drops_a_marque_the_catalog_is_known_to_hold():
    coverage = {entry.key: None for entry in mdir.DIRECTORY} | {"toyota": 12}
    result = interpret(GOLDEN_EN[1], coverage=coverage)
    assert "toyota" not in result.fields["units"]
    assert {note.code: note.units for note in result.notes}["WORK_SCOPE_NOTE_ALREADY_MAPPED"] \
        == ("toyota",)


def test_golden_three_orders_by_priority_and_caps_the_plan():
    result = interpret(GOLDEN_EN[2])
    assert result.fields["units"] == ["toyota", "mazda"]
    assert result.fields["max_items"] == 500


def test_an_instruction_changes_only_what_it_states():
    current = wsc.scope_from_fields(fields(units=["toyota", "lexus"], batch_size=5))
    cases = {
        "add Honda": (["toyota", "lexus", "honda"], 2018, None, 800, 5),
        "remove Lexus": (["toyota"], 2018, None, 800, 5),
        "without lexus": (["toyota"], 2018, None, 800, 5),
        "do not map Lexus": (["toyota"], 2018, None, 800, 5),
        "Lexus first": (["lexus", "toyota"], 2018, None, 800, 5),
        "Mazda first": (["mazda", "toyota", "lexus"], 2018, None, 800, 5),
        "stop after 300": (["toyota", "lexus"], 2018, None, 300, 5),
        "all years": (["toyota", "lexus"], None, None, 800, 5),
        "2015-2020": (["toyota", "lexus"], 2015, 2020, 800, 5),
        "between 2015 and 2020": (["toyota", "lexus"], 2015, 2020, 800, 5),
        "until 2020": (["toyota", "lexus"], None, 2020, 800, 5),
        "before 2020": (["toyota", "lexus"], None, 2019, 800, 5),
        "batches of 20": (["toyota", "lexus"], 2018, None, 800, 20),
        "only Kia": (["kia"], 2018, None, 800, 5),
        "גם הונדה": (["toyota", "lexus", "honda"], 2018, None, 800, 5),
        "בלי לקסוס": (["toyota"], 2018, None, 800, 5),
        "באצוות של 15": (["toyota", "lexus"], 2018, None, 800, 15),
    }
    for text, expected in cases.items():
        result = interpret(text, current)
        got = result.fields
        assert (got["units"], got["model_year_from"], got["model_year_to"], got["max_items"],
                got["batch_size"]) == expected, text


def test_a_bare_number_is_a_year_only_where_a_year_is_plausible():
    assert interpret("Map Toyota up to 2020").fields["model_year_to"] == 2020
    assert interpret("Map Toyota up to 800").fields["max_items"] == 800
    assert interpret("Map Toyota up to 2000 vehicles").fields["max_items"] == 2000
    assert interpret("Map Toyota, 1,500 variants").fields["max_items"] == 1500


def test_what_is_not_understood_is_reported_back_verbatim_and_bounded():
    result = interpret("Map Toyota and Polestar, pronto")
    assert result.fields["units"] == ["toyota"]
    unrecognized = {note.code: note.terms for note in result.notes}["WORK_SCOPE_NOTE_UNRECOGNIZED"]
    assert unrecognized == ("polestar", "pronto")
    many = interpret("Map Toyota " + " ".join(f"zz{index}" for index in range(20)))
    terms = {note.code: note.terms for note in many.notes}["WORK_SCOPE_NOTE_UNRECOGNIZED"]
    assert len(terms) == wi.MAX_REPORTED_TERMS


@pytest.mark.parametrize("text, code", [
    ("", "WORK_SCOPE_INSTRUCTION_INVALID"),
    ("   ", "WORK_SCOPE_INSTRUCTION_INVALID"),
    ("x" * (wi.MAX_INSTRUCTION_CHARS + 1), "WORK_SCOPE_INSTRUCTION_INVALID"),
    ("map toyota\x00", "WORK_SCOPE_INSTRUCTION_INVALID"),
    (None, "WORK_SCOPE_INSTRUCTION_INVALID"),
    ("hello there", "WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD"),
    ("Map Toyta and Lexsus", "WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD"),
    ("first", "WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD"),
])
def test_an_instruction_that_is_not_one_is_refused(text, code):
    with pytest.raises(wi.InstructionError) as refused:
        wi.read_instruction(text)
    assert refused.value.code == code


def test_the_reader_is_deterministic_and_calls_nothing(monkeypatch):
    import backend.catalog.scope.interpret as module

    assert not hasattr(module, "ProviderAdapter")
    first = [interpret(text).fields for text in GOLDEN_EN + GOLDEN_HE]
    second = [interpret(text).fields for text in GOLDEN_EN + GOLDEN_HE]
    assert first == second


def test_spelling_variants_fold_onto_one_reading():
    assert interpret("Map Citroën and Škoda").fields["units"] == ["citroen", "skoda"]
    assert interpret("map mercedes-benz and b.m.w").fields["units"] == ["mercedes_benz", "bmw"]
    assert interpret("מפה ג׳יפ ופיג’ו").fields["units"] == ["jeep", "peugeot"]
    assert interpret("MAP TOYOTA").fields["units"] == ["toyota"]


# =============================================================================
# 4. coverage
# =============================================================================

def test_coverage_is_known_only_where_a_register_spelling_is_verified():
    repo = MemoryRepository()
    coverage = wcov.read_coverage(repo)
    assert coverage.available and coverage.catalog_variants == 0
    assert coverage.state("toyota") == "known" and coverage.count("toyota") == 0
    assert coverage.state("mazda") == "unverifiable" and coverage.count("mazda") is None


def test_a_failed_coverage_read_is_unavailable_never_zero(monkeypatch):
    repo = MemoryRepository()

    def fail(_names):
        raise AppError("REPOSITORY_ERROR", "SELECT secret FROM somewhere", 502)

    monkeypatch.setattr(repo, "catalog_canonical_manufacturer_coverage", fail)
    coverage = wcov.read_coverage(repo)
    assert not coverage.available
    assert coverage.state("toyota") == "unavailable" and coverage.count("toyota") is None
    assert coverage.catalog_variants is None


def test_a_coverage_read_that_omits_a_marque_it_was_asked_about_is_no_answer(monkeypatch):
    repo = MemoryRepository()
    monkeypatch.setattr(repo, "catalog_canonical_manufacturer_coverage",
                        lambda _names: [{"manufacturer": None, "canonical_variants": 3}])
    assert not wcov.read_coverage(repo).available


# =============================================================================
# 5. the API
# =============================================================================

def world(workflow_key: str = "swarm_v2") -> tuple[MemoryRepository, str, UUID]:
    repo = MemoryRepository()
    repo.seed_user(str(USER))
    repo.seed_user(str(OUTSIDER))
    project = str(uuid4())
    repo.seed_project(project, f"p-{project[:8]}", "P", [str(USER)], workflow_key=workflow_key)
    conversation = repo.create_conversation(UUID(project), "plan", USER)
    return repo, project, UUID(conversation["id"])


class RecordingLauncher:
    def __init__(self):
        self.launched: list[str] = []

    def launch(self, run_id):
        self.launched.append(str(run_id))
        return {"mode": "recording", "execution": "recorded"}


@pytest.fixture()
def launcher():
    recorder = RecordingLauncher()
    app.dependency_overrides[get_job_launcher] = lambda: recorder
    yield recorder
    app.dependency_overrides.clear()


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv(FLAG, "true")


def client(repo) -> TestClient:
    app.dependency_overrides[get_repository] = lambda: repo
    return TestClient(app)


def as_user(user: UUID = USER) -> dict[str, str]:
    return {"x-milo-auth-user-id": str(user)}


def create(repo, conversation, body, user=USER):
    return client(repo).post(f"/conversations/{conversation}/work-scopes", json=body,
                             headers=as_user(user))


def revise(repo, plan, body, user=USER):
    return client(repo).post(f"/work-scopes/{plan}/revisions", json=body, headers=as_user(user))


def edit_body(**overrides: Any) -> dict[str, Any]:
    return {"edit": fields(**overrides)}


def test_both_writes_are_behind_the_flag_before_the_body_is_read(launcher, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    repo, _project, conversation = world()
    for response in (create(repo, conversation, {"instruction": GOLDEN_EN[0]}),
                     client(repo).post(f"/conversations/{conversation}/work-scopes",
                                       content=b"not json", headers=as_user()),
                     revise(repo, uuid4(), {"garbage": True})):
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "EXECUTION_SURFACE_DISABLED"
    assert repo.work_scopes == {} and repo.work_scope_revisions == []
    # The guard names the flag for exactly these two writes.
    assert find_disabled_surface("POST", f"/conversations/{conversation}/work-scopes") == (
        FLAG, "work scope creation")
    assert find_disabled_surface("POST", f"/work-scopes/{uuid4()}/revisions") == (
        FLAG, "work scope revision")
    assert {flag for _m, flag, pattern, _s in SURFACE_RULES
            if pattern.match(f"/work-scopes/{uuid4()}/revisions")} == {FLAG}
    # The reads are not execution and are not gated.
    assert find_disabled_surface("GET", f"/work-scopes/{uuid4()}") is None


def test_capabilities_say_whether_the_surface_applies_from_server_truth(launcher, monkeypatch):
    repo, project, _conversation = world()
    monkeypatch.delenv(FLAG, raising=False)
    off = client(repo).get(f"/projects/{project}/work-scope/capabilities", headers=as_user()).json()
    assert (off["available"], off["reason"]) == (False, "mutations_disabled")
    monkeypatch.setenv(FLAG, "true")
    on = client(repo).get(f"/projects/{project}/work-scope/capabilities", headers=as_user()).json()
    assert (on["available"], on["reason"]) == (True, None)
    assert on["limits"]["max_batch_size"] == 20 and on["limits"]["default_batch_size"] == 10
    assert on["limits"]["max_items"] == 2000
    # Nothing can be prepared or started in this release, whatever the flags.
    assert on["can_prepare"] is False and on["can_start_batches"] is False

    v1, v1_project, _c = world("vehicle_catalog_v1")
    answer = client(v1).get(f"/projects/{v1_project}/work-scope/capabilities",
                            headers=as_user()).json()
    assert (answer["available"], answer["reason"]) == (False, "workflow_not_supported")
    hidden = client(repo).get(f"/projects/{project}/work-scope/capabilities",
                              headers=as_user(OUTSIDER))
    assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_the_directory_read_states_coverage_honestly(launcher):
    repo, project, _conversation = world()
    body = client(repo).get(f"/projects/{project}/work-scope/directory", headers=as_user()).json()
    by_key = {entry["key"]: entry for entry in body["entries"]}
    assert list(by_key) == [entry.key for entry in mdir.DIRECTORY]
    assert by_key["toyota"]["register_marque"] == "טויוטה"
    assert by_key["toyota"]["coverage"] == {"state": "known", "canonical_variants": 0}
    assert by_key["mazda"]["register_marque_verified"] is False
    assert by_key["mazda"]["coverage"] == {"state": "unverifiable", "canonical_variants": None}
    assert body["coverage"] == {"available": True, "catalog_variants": 0, "attributed_variants": 0}
    assert client(repo).get(f"/projects/{project}/work-scope/directory",
                            headers=as_user(OUTSIDER)).status_code == 404


def test_chat_and_clicks_produce_the_same_stored_plan_and_digest(launcher, enabled):
    """THE convergence property, through the real API and repository."""
    chat_repo, _p, chat_conversation = world()
    ui_repo, _q, ui_conversation = world()
    chat = create(chat_repo, chat_conversation, {"instruction": GOLDEN_EN[0]})
    ui = create(ui_repo, ui_conversation, edit_body())
    assert chat.status_code == ui.status_code == 201
    chat_scope, ui_scope = chat.json()["work_scope"], ui.json()["work_scope"]
    assert chat_scope["plan"] == ui_scope["plan"]
    assert chat_scope["digest"] == ui_scope["digest"] == wsc.scope_from_fields(fields()).digest()
    assert chat_repo.work_scope_revisions[0]["scope_text"] == \
        ui_repo.work_scope_revisions[0]["scope_text"]
    # Only HOW the plan was stated differs, and it is recorded as such.
    assert chat_scope["head"]["input_kind"] == "instruction"
    assert chat_scope["head"]["instruction"] == GOLDEN_EN[0]
    assert ui_scope["head"]["input_kind"] == "edit" and ui_scope["head"]["instruction"] is None


def test_the_hebrew_instruction_converges_on_the_same_digest_too(launcher, enabled):
    repo, _p, conversation = world()
    response = create(repo, conversation, {"instruction": GOLDEN_HE[0]})
    assert response.status_code == 201
    assert response.json()["work_scope"]["digest"] == wsc.scope_from_fields(fields()).digest()


def test_a_revision_must_name_the_current_head_or_nothing_is_written(launcher, enabled):
    repo, _p, conversation = world()
    first = create(repo, conversation, edit_body()).json()["work_scope"]
    plan, digest = first["work_scope_id"], first["digest"]

    second = revise(repo, plan, {"expected_revision": 1, "expected_digest": digest,
                                 "instruction": "add Honda"})
    assert second.status_code == 200
    body = second.json()
    assert body["applied"] is True
    assert body["work_scope"]["revision"] == 2
    assert body["work_scope"]["plan"]["units"] == ["toyota", "lexus", "honda"]

    for stale in ({"expected_revision": 1, "expected_digest": digest},
                  {"expected_revision": 2, "expected_digest": digest}):
        refused = revise(repo, plan, {**stale, "instruction": "add Kia"})
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "WORK_SCOPE_STALE"
    assert len(repo.work_scope_revisions) == 2
    # The repository's own compare-and-set holds too, for a caller that skips
    # the service's check.
    with pytest.raises(AppError) as raced:
        repo.revise_work_scope(UUID(plan), 1, digest, USER, {
            "scope_text": wsc.scope_from_fields(fields(units=["kia"])).canonical_text(),
            "input_kind": "edit", "instruction": None, "notes": []})
    assert raced.value.code == "WORK_SCOPE_STALE"


def test_an_understood_instruction_that_changes_nothing_writes_nothing(launcher, enabled):
    repo, _p, conversation = world()
    first = create(repo, conversation, edit_body()).json()["work_scope"]
    response = revise(repo, first["work_scope_id"], {
        "expected_revision": 1, "expected_digest": first["digest"],
        "instruction": "Map Toyota and Lexus"})
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert [note["code"] for note in body["notes"]] == ["WORK_SCOPE_NOTE_NO_CHANGE"]
    assert body["work_scope"]["revision"] == 1
    assert len(repo.work_scope_revisions) == 1


def test_one_conversation_has_one_open_plan(launcher, enabled):
    repo, _p, conversation = world()
    assert create(repo, conversation, edit_body()).status_code == 201
    second = create(repo, conversation, {"instruction": "Map Kia"})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "WORK_SCOPE_OPEN_EXISTS"
    assert len(repo.work_scopes) == 1


def test_a_project_whose_engine_reads_no_plan_is_refused_before_anything_is_written(launcher, enabled):
    repo, _p, conversation = world("vehicle_catalog_v1")
    response = create(repo, conversation, edit_body())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORK_SCOPE_WORKFLOW_UNSUPPORTED"
    assert repo.work_scopes == {}


@pytest.mark.parametrize("body, status, code", [
    ({}, 422, "WORK_SCOPE_REQUEST_INVALID"),
    ({"instruction": GOLDEN_EN[0], "edit": fields()}, 422, "WORK_SCOPE_REQUEST_INVALID"),
    (edit_body(batch_size=21), 422, "WORK_SCOPE_BATCH_SIZE_INVALID"),
    (edit_body(units=["polestar"]), 422, "WORK_SCOPE_UNITS_INVALID"),
    ({"edit": {**fields(), "digest": "0" * 64}}, 422, "WORK_SCOPE_FIELDS_INVALID"),
    ({"instruction": "hello"}, 422, "WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD"),
    ({"instruction": "Map Toyota in batches of 50"}, 422, "WORK_SCOPE_BATCH_SIZE_INVALID"),
    ({"instruction": "Map Toyota, up to 5000 vehicles"}, 422, "WORK_SCOPE_MAX_ITEMS_INVALID"),
    ({"instruction": "x" * 501}, 422, "WORK_SCOPE_INSTRUCTION_INVALID"),
])
def test_invalid_requests_are_refused_with_a_static_code_and_write_nothing(launcher, enabled, body,
                                                                          status, code):
    repo, _p, conversation = world()
    response = create(repo, conversation, body)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert repo.work_scopes == {} and repo.work_scope_revisions == []


def test_a_request_body_cannot_smuggle_server_owned_fields(launcher, enabled):
    repo, _p, conversation = world()
    response = create(repo, conversation, {**edit_body(), "digest": "0" * 64})
    assert response.status_code == 422  # extra="forbid" on the request model
    assert repo.work_scopes == {}


def test_reads_are_membership_scoped_and_absent_equals_forbidden(launcher, enabled):
    repo, _p, conversation = world()
    plan = create(repo, conversation, edit_body()).json()["work_scope"]["work_scope_id"]
    mine = client(repo).get(f"/work-scopes/{plan}", headers=as_user())
    assert mine.status_code == 200 and mine.json()["work_scope_id"] == plan
    opened = client(repo).get(f"/conversations/{conversation}/work-scopes/open",
                              headers=as_user()).json()
    assert opened["work_scope"]["work_scope_id"] == plan

    hidden = client(repo).get(f"/work-scopes/{plan}", headers=as_user(OUTSIDER))
    absent = client(repo).get(f"/work-scopes/{uuid4()}", headers=as_user())
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()
    assert client(repo).get(f"/conversations/{conversation}/work-scopes/open",
                            headers=as_user(OUTSIDER)).status_code == 404
    refused = revise(repo, plan, {"expected_revision": 1, "expected_digest": "0" * 64,
                                  "instruction": "add Kia"}, user=OUTSIDER)
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "WORK_SCOPE_NOT_FOUND"


def test_a_conversation_without_a_plan_says_so(launcher):
    repo, _p, conversation = world()
    body = client(repo).get(f"/conversations/{conversation}/work-scopes/open",
                            headers=as_user()).json()
    assert body == {"work_scope": None}


def test_the_state_is_a_closed_projection_with_bounded_history(launcher, enabled):
    repo, _p, conversation = world()
    state = create(repo, conversation, {"instruction": GOLDEN_EN[0]}).json()["work_scope"]
    for index in range(ws.MAX_HISTORY + 3):
        state = revise(repo, state["work_scope_id"], {
            "expected_revision": state["revision"], "expected_digest": state["digest"],
            "edit": fields(max_items=100 + index)}).json()["work_scope"]
    assert state["revision"] == ws.MAX_HISTORY + 4
    assert [item["revision"] for item in state["history"]] == list(
        range(state["revision"] - 1, state["revision"] - 1 - ws.MAX_HISTORY, -1))
    assert set(state) == {"work_scope_id", "conversation_id", "project_id", "status", "revision",
                          "digest", "current", "plan", "head", "history", "created_at",
                          "updated_at"}
    # Never: who wrote it, the raw stored text, or any internal row id.
    serialized = json.dumps(state)
    assert "created_by" not in serialized and "scope_text" not in serialized
    assert str(USER) not in serialized


def test_an_instruction_is_echoed_back_redacted(launcher, enabled):
    repo, _p, conversation = world()
    secret = "sk-" + "a" * 40
    state = create(repo, conversation, {"instruction": f"Map Toyota {secret}"}).json()["work_scope"]
    assert secret not in json.dumps(state)


def test_a_not_mapped_yet_instruction_refuses_when_coverage_cannot_be_read(launcher, enabled,
                                                                           monkeypatch):
    repo, _p, conversation = world()

    def fail(_names):
        raise AppError("REPOSITORY_ERROR", "boom", 502)

    monkeypatch.setattr(repo, "catalog_canonical_manufacturer_coverage", fail)
    refused = create(repo, conversation, {"instruction": GOLDEN_EN[1]})
    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "WORK_SCOPE_COVERAGE_UNAVAILABLE"
    # An instruction that does not depend on coverage never reads it.
    assert create(repo, conversation, {"instruction": GOLDEN_EN[0]}).status_code == 201


def test_no_plan_operation_reaches_a_run_a_launch_or_a_provider(launcher, enabled, monkeypatch):
    """Every repository method the whole surface reaches, recorded."""
    import backend.catalog.scope.service as service_module

    repo, project, conversation = world()
    calls: list[str] = []
    original = repo.__class__

    class Recording(original):  # type: ignore[misc,valid-type]
        def __getattribute__(self, name):
            attribute = super().__getattribute__(name)
            if callable(attribute) and not name.startswith("_"):
                calls.append(name)
            return attribute

    repo.__class__ = Recording
    c = client(repo)
    c.get(f"/projects/{project}/work-scope/capabilities", headers=as_user())
    c.get(f"/projects/{project}/work-scope/directory", headers=as_user())
    state = create(repo, conversation, {"instruction": GOLDEN_EN[1]}).json()["work_scope"]
    revise(repo, state["work_scope_id"], {"expected_revision": 1,
                                          "expected_digest": state["digest"], "edit": fields()})
    c.get(f"/work-scopes/{state['work_scope_id']}", headers=as_user())
    c.get(f"/conversations/{conversation}/work-scopes/open", headers=as_user())
    assert {"create_work_scope", "revise_work_scope", "list_work_scope_revisions"} <= set(calls)
    assert set(calls) <= {"get_project", "get_conversation", "catalog_canonical_manufacturer_coverage",
                          "create_work_scope", "revise_work_scope", "get_work_scope",
                          "open_work_scope", "list_work_scope_revisions"}, sorted(set(calls))
    assert repo.runs == {} and repo.messages == [] and launcher.launched == []
    assert "ProviderAdapter" not in dir(service_module)


def test_the_memory_mirror_derives_the_digest_and_refuses_a_malformed_revision():
    repo, _p, conversation = world()
    scope = wsc.scope_from_fields(fields())
    created = repo.create_work_scope(conversation, USER, {
        "scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
        "notes": []})
    assert created["revision"]["digest"] == scope.digest()
    assert created["work_scope"]["head_digest"] == scope.digest()
    other, _q, other_conversation = world()
    record = {**scope.as_record(), "batch_size": 21}
    for revision in ({"scope_text": wsc.canonical_text(record), "input_kind": "edit",
                      "instruction": None, "notes": []},
                     {"scope_text": scope.canonical_text(), "input_kind": "instruction",
                      "instruction": None, "notes": []},
                     {"scope_text": "not json", "input_kind": "edit", "instruction": None},
                     {"input_kind": "edit"}):
        with pytest.raises(AppError) as refused:
            other.create_work_scope(other_conversation, USER, revision)
        assert refused.value.code == "WORK_SCOPE_REVISION_INVALID"
    assert other.work_scopes == {} and other.work_scope_revisions == []


# =============================================================================
# 6. the flag is pinned off everywhere a deployment states its posture
# =============================================================================

def test_the_flag_is_an_execution_flag_pinned_off_in_every_contract():
    from backend.production_config import EXECUTION_FLAGS

    assert FLAG in EXECUTION_FLAGS
    contract = Path("scripts/deploy/deployment-contract.sh").read_text(encoding="utf-8")
    assert f"{FLAG}=false" in contract.split("MILO_STAGE_A_EXECUTION_FLAGS=(", 1)[1].split("\n)", 1)[0]
    assert f"{FLAG}: false" in Path("config/production.example.yaml").read_text(encoding="utf-8")
    assert f"{FLAG}=false" in Path("scripts/release/generate-deployment-plan.sh").read_text(
        encoding="utf-8")
    import scripts.check_unsafe_defaults as scanner

    assert FLAG in scanner.EXECUTION_FLAGS


# =============================================================================
# 7. the Supabase repository: exact RPC names, bounded reads, sanitized refusals
# =============================================================================

class _RpcFailure:
    def __init__(self, message):
        self.message = message

    def execute(self):
        raise RuntimeError(self.message)


def _supabase(rpc_result=None, rpc_error: str | None = None):
    from backend.repository.supabase import SupabaseRepository
    from tests.test_repository_supabase import FakeClient, FakeResult

    client = FakeClient()

    def rpc(name, params):
        client.rpc_calls.append((name, params))
        if rpc_error is not None:
            return _RpcFailure(rpc_error)
        return type("Rpc", (), {"execute": lambda self: FakeResult(rpc_result)})()

    client.rpc = rpc
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = client
    return repository


def test_the_supabase_writers_call_exactly_the_reviewed_rpcs():
    scope = wsc.scope_from_fields(fields())
    revision = {"scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
                "notes": []}
    ok = {"work_scope": {"id": "s"}, "revision": {"id": "r"}}
    repo = _supabase(rpc_result=ok)
    conversation, plan = uuid4(), uuid4()
    assert repo.create_work_scope(conversation, USER, revision) == ok
    assert repo.revise_work_scope(plan, 3, "a" * 64, USER, revision) == ok
    (create_name, create_params), (revise_name, revise_params) = repo.client.rpc_calls
    assert create_name == "create_work_scope"
    assert create_params == {"p_conversation_id": str(conversation), "p_created_by": str(USER),
                             "p_revision": revision}
    assert revise_name == "revise_work_scope"
    assert revise_params == {"p_work_scope_id": str(plan), "p_expected_revision": 3,
                             "p_expected_digest": "a" * 64, "p_created_by": str(USER),
                             "p_revision": revision}
    # Nothing is written through a table builder: the RPCs are the only writers.
    assert repo.client.inserted == [] and repo.client.updated == []


@pytest.mark.parametrize("message, code, status", [
    ("P0001: WORK_SCOPE_STALE", "WORK_SCOPE_STALE", 409),
    ("WORK_SCOPE_OPEN_EXISTS", "WORK_SCOPE_OPEN_EXISTS", 409),
    ("WORK_SCOPE_NOT_EDITABLE", "WORK_SCOPE_NOT_EDITABLE", 409),
    ("WORK_SCOPE_WORKFLOW_UNSUPPORTED", "WORK_SCOPE_WORKFLOW_UNSUPPORTED", 409),
    ("WORK_SCOPE_REVISION_INVALID", "WORK_SCOPE_REVISION_INVALID", 422),
    ("WORK_SCOPE_CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_FOUND", 404),
    ("WORK_SCOPE_NOT_FOUND", "WORK_SCOPE_NOT_FOUND", 404),
    ('duplicate key value violates "x" DETAIL: Key (secret)=(value)', "REPOSITORY_ERROR", 502),
])
def test_the_supabase_writers_map_every_named_refusal_and_sanitize_the_rest(message, code, status):
    repo = _supabase(rpc_error=message)
    with pytest.raises(AppError) as refused:
        repo.create_work_scope(uuid4(), USER, {"scope_text": "{}", "input_kind": "edit"})
    assert (refused.value.code, refused.value.status_code) == (code, status)
    # The database's own words never become the message.
    assert "DETAIL" not in refused.value.message and "secret" not in refused.value.message


def test_the_supabase_reads_are_bounded_and_ordered():
    from backend.repository.supabase import SupabaseRepository

    repo = _supabase(rpc_result=[{"manufacturer": None, "canonical_variants": 0}])
    plan, conversation = uuid4(), uuid4()
    repo.get_work_scope(plan)
    repo.open_work_scope(conversation)
    repo.list_work_scope_revisions(plan, limit=10_000)
    by_id, open_read, revisions = repo.client.selected
    assert (by_id.table, by_id.filters, by_id.bounds) == (
        "catalog_work_scopes", [("eq", "id", str(plan))], ("limit", 1))
    assert open_read.filters == [("eq", "conversation_id", str(conversation)),
                                 ("is", "closed_at", "null")]
    assert revisions.table == "catalog_work_scope_revisions"
    assert revisions.orders == [("revision", True)]
    assert revisions.bounds == ("limit", SupabaseRepository.MAX_WORK_SCOPE_REVISION_ROWS)
    # Explicit columns only -- never `*`.
    assert "*" not in by_id.columns and "*" not in revisions.columns
    repo.catalog_canonical_manufacturer_coverage(["טויוטה"])
    assert repo.client.rpc_calls[-1] == ("catalog_canonical_manufacturer_coverage",
                                         {"p_manufacturers": ["טויוטה"]})
