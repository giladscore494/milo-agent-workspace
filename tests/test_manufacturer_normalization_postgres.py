"""PR-D3: migration 20261003000100 (manufacturer normalisation) on ephemeral PostgreSQL.

Every migration applied, into a database with the production-shaped read-only
role. It must: admit a register capture group of kind `normalisation` and no
other new kind; keep one normalisation live at a time; record a proposal only
under its run's lease and only when the output keeps the contract (every
member an input name, none in two groups) -- checked again in the database;
approve only exactly a proposed group or a code-owned rule, for tozars of the
latest directory, as a compare-and-set on the active version; keep versions
and entries append-only and carry the active entries forward; let the
read-only role read and write nothing.
"""

from __future__ import annotations

import json
import uuid

import pytest

from tests.test_migrations_postgres import (BASELINE, MIGRATIONS, SEED_LEGACY_ROWS, SUPABASE_AUTH_SHIM,
                                            EphemeralPostgres, _require_pg_bin, _rpc_as_service, _ws_world,
                                            _wsp_capture_run)

PG_PORT = "54994"
MIGRATION = next(m for m in MIGRATIONS if m.name == "20261003000100_catalog_manufacturer_normalization.sql")
RELEASE_RO = "milo_release_readonly_5c2e8f1d3b71"
TOYOTA, LEXUS, MERCEDES, MERCEDES_DASH = "טויוטה", "לקסוס", "מרצדס בנץ", "מרצדס-בנץ"


@pytest.fixture(scope="module")
def ndb():
    server = EphemeralPostgres(_require_pg_bin(), port=PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(f"create role {RELEASE_RO} login bypassrls; grant usage on schema public to {RELEASE_RO}")
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        assert MIGRATION in MIGRATIONS
        for migration in MIGRATIONS:
            server.psql(file=migration)
        units = json.dumps([{"tozar": t, "expected_rows": n} for t, n in
                            ((TOYOTA, 28), (LEXUS, 5), (MERCEDES, 10), (MERCEDES_DASH, 3))], ensure_ascii=False)
        _rpc_as_service(server, "select public.record_register_directory('142afde2-6228-49f9-8a29-9b6c3a0cbe40', "
                                f"now(), $j${units}$j$::jsonb)")
        yield server
    finally:
        server.stop()


def _json(value) -> str:
    return "$j$" + json.dumps(value, ensure_ascii=False) + "$j$::jsonb"


INPUT = [{"name": n, "rows": r, "tozeret_nm": [], "samples": []}
         for n, r in ((TOYOTA, 28), (LEXUS, 5), (MERCEDES, 10), (MERCEDES_DASH, 3))]
GROUPS = [{"canonical": "Mercedes-Benz", "members": [MERCEDES, MERCEDES_DASH], "confidence": "high", "reason": "x"},
          {"canonical": "Lexus", "members": [LEXUS], "confidence": "low", "reason": "y"}]


def _claim(db, user: str) -> dict:
    return json.loads(_rpc_as_service(db, f"select public.request_manufacturer_normalization('{user}', 900, "
                                          f"{_json(INPUT)})"))


@pytest.fixture(scope="module")
def proposal(ndb):
    user, _project, conversation = _ws_world(ndb)
    claim = _claim(ndb, user)
    assert claim["decision"] == "claimed" and claim["group"]["kind"] == "normalisation"
    run_id, args = _wsp_capture_run(ndb, conversation)
    _rpc_as_service(ndb, f"select public.record_register_capture_trigger('{claim['group']['id']}', '{run_id}', "
                         "'triggered', 'exec-1')")
    return {"user": user, "id": claim["proposal"]["id"], "run_id": run_id, "args": args,
            "group": claim["group"]["id"]}


def _record(db, proposal: dict, status: str, groups, code=None, args=None) -> str:
    return _rpc_as_service(db, f"select public.record_manufacturer_normalization_proposal({args or proposal['args']}, "
                               f"'{proposal['id']}', '{status}', {_json(groups) if groups is not None else 'null'}, "
                               f"{repr(code) if code else 'null'}, 'kimi-k3')")


def test_one_normalisation_is_live_at_a_time_and_kinds_are_closed(ndb, proposal):
    again = _claim(ndb, proposal["user"])
    assert again["decision"] == "existing" and again["proposal"]["id"] == proposal["id"]
    assert "input" not in again["proposal"]
    with pytest.raises(AssertionError, match="catalog_register_capture_groups_check"):
        ndb.psql("insert into public.catalog_register_capture_groups (kind, register_version, requested_by, "
                 f"expected_rows) values ('normalisation', '{'a' * 64}', '{uuid.uuid4()}', 0)")
    with pytest.raises(AssertionError, match="catalog_register_capture_groups_(kind_)?check"):
        ndb.psql("insert into public.catalog_register_capture_groups (kind, requested_by, expected_rows) "
                 f"values ('other', '{uuid.uuid4()}', 0)")


def test_the_database_holds_no_invented_or_duplicated_name(ndb, proposal):
    invented = [dict(GROUPS[0], members=[MERCEDES, "מרצדס"])]
    duplicated = [GROUPS[0], dict(GROUPS[1], members=[LEXUS, MERCEDES])]
    extra_key = [dict(GROUPS[0], note="x")]
    # Parity with the Python contract: a null confidence, a canonical with a
    # leading tab, a control character, and a group that is not an object.
    null_confidence = [dict(GROUPS[0], confidence=None)]
    tabbed = [dict(GROUPS[0], canonical="\tMercedes-Benz")]
    control = [dict(GROUPS[0], reason="a\u0001b")]
    for groups in (invented, duplicated, extra_key, null_confidence, tabbed, control, ["x"], [[MERCEDES]]):
        with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_OUTPUT_INVALID"):
            _record(ndb, proposal, "proposed", groups)
    # Another run's lease is refused.
    _user, _project, conversation = _ws_world(ndb)
    _other_run, other_args = _wsp_capture_run(ndb, conversation)
    with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_NOT_THIS_RUN"):
        _record(ndb, proposal, "proposed", GROUPS, args=other_args)


def test_approval_needs_exact_provenance_and_the_active_version(ndb, proposal):
    recorded = json.loads(_record(ndb, proposal, "proposed", GROUPS))
    assert recorded["status"] == "proposed" and json.loads(_record(ndb, proposal, "proposed", GROUPS))["id"]
    with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_ALREADY_RECORDED"):
        _record(ndb, proposal, "refused", None, "NORMALIZATION_MODEL_FAILED")
    approver = proposal["user"]

    def approve(version: int, entries: list[dict]) -> str:
        return _rpc_as_service(ndb, f"select public.approve_manufacturer_normalization('{approver}', {version}, "
                                    f"{_json(entries)})")

    model = {"provenance": "model", "proposal_id": proposal["id"]}
    for bad in ([{"source_tozar": LEXUS, "canonical_name": "Lexus Motors", **model}],          # not as proposed
                [{"source_tozar": TOYOTA, "canonical_name": "Lexus", **model}],                # not a member
                [{"source_tozar": "הונדה", "canonical_name": "Honda", "provenance": "rule",
                  "rule_id": "R1_SPELLING"}],                                                 # not in the directory
                [{"source_tozar": TOYOTA, "canonical_name": "Toyota", "provenance": "rule",
                  "rule_id": "R9_GUESS"}]):                                                    # not a code-owned rule
        with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_APPROVAL_INVALID"):
            approve(0, bad)
    first = json.loads(approve(0, [{"source_tozar": m, "canonical_name": "Mercedes-Benz", **model}
                                   for m in (MERCEDES, MERCEDES_DASH)]))
    assert first == {"version": 1, "entry_count": 2}
    with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_VERSION_STALE"):
        approve(0, [{"source_tozar": LEXUS, "canonical_name": "Lexus", **model}])
    second = json.loads(approve(1, [{"source_tozar": LEXUS, "canonical_name": "Lexus", **model},
                                    {"source_tozar": TOYOTA, "canonical_name": "Toyota", "provenance": "rule",
                                     "rule_id": "R1_SPELLING"}]))
    assert second == {"version": 2, "entry_count": 4}
    current = json.loads(_rpc_as_service(ndb, "select public.catalog_manufacturer_normalization_current()"))
    entries = {e["source_tozar"]: e for e in current["entries"]}
    assert current["version"] == 2 and set(entries) == {TOYOTA, LEXUS, MERCEDES, MERCEDES_DASH}
    assert (entries[LEXUS]["provenance"], entries[LEXUS]["confidence"]) == ("model", "low")
    assert (entries[TOYOTA]["rule_id"], entries[TOYOTA]["proposal_id"]) == ("R1_SPELLING", None)
    for table in ("versions", "entries"):
        with pytest.raises(AssertionError, match="CATALOG_REGISTER_IMMUTABLE"):
            ndb.psql(f"delete from public.catalog_manufacturer_normalization_{table}")


def test_a_rejection_is_exactly_a_pending_group_and_append_only(ndb, proposal):
    if ndb.psql(f"select status from public.catalog_manufacturer_normalization_proposals "
                f"where id = '{proposal['id']}'") == "requested":
        _record(ndb, proposal, "proposed", GROUPS)
    user = proposal["user"]

    def reject(group: dict) -> str:
        return _rpc_as_service(ndb, f"select public.reject_manufacturer_normalization_group('{user}', {_json(group)})")

    for bad in ({"canonical": "Lexus Motors", "members": [LEXUS], "proposal_id": proposal["id"]},   # not as proposed
                {"canonical": "Lexus", "members": [LEXUS, LEXUS], "proposal_id": proposal["id"]},  # twice
                {"canonical": "Honda", "members": ["הונדה"], "rule_id": "R1_SPELLING"},            # not in the directory
                {"canonical": "Toyota", "members": [TOYOTA], "rule_id": "R9_GUESS"},               # not a rule
                {"canonical": "Lexus", "members": [LEXUS]},                                       # no provenance
                {"canonical": "Lexus", "members": [LEXUS], "rule_id": None},                      # a null one
                {"canonical": "Lexus", "members": [LEXUS], "rule_id": None, "proposal_id": None}):
        with pytest.raises(AssertionError, match="CATALOG_NORMALIZATION_REJECTION_INVALID"):
            reject(bad)
    first = json.loads(reject({"canonical": "Lexus", "members": [LEXUS], "proposal_id": proposal["id"]}))
    again = json.loads(reject({"canonical": "Lexus", "members": [LEXUS], "proposal_id": proposal["id"]}))
    assert first["id"] == again["id"] and first["members"] == [LEXUS]
    ruled = json.loads(reject({"canonical": MERCEDES, "members": [MERCEDES_DASH, MERCEDES], "rule_id": "R1_SPELLING"}))
    assert ruled["members"] == sorted([MERCEDES, MERCEDES_DASH]) and ruled["rule_id"] == "R1_SPELLING"
    with pytest.raises(AssertionError, match="CATALOG_REGISTER_IMMUTABLE"):
        ndb.psql("delete from public.catalog_manufacturer_normalization_rejections")


def test_the_read_only_role_reads_and_writes_nothing(ndb, proposal):
    # The migration granted the production-shaped role its reads.
    ndb.psql(f"set role {RELEASE_RO}; select public.catalog_manufacturer_normalization_current(); "
             "select count(*) from public.catalog_manufacturer_normalization_entries; reset role")
    for sql in (f"select public.request_manufacturer_normalization('{proposal['user']}', 900, {_json(INPUT)})",
                f"select public.approve_manufacturer_normalization('{proposal['user']}', 0, '[]'::jsonb)",
                f"select public.reject_manufacturer_normalization_group('{proposal['user']}', '{{}}'::jsonb)"):
        with pytest.raises(AssertionError, match="permission denied"):
            ndb.psql(f"set role {RELEASE_RO}; {sql}")
    for role in ("anon", "authenticated"):
        with pytest.raises(AssertionError, match="permission denied"):
            ndb.psql(f"set role {role}; select public.catalog_manufacturer_normalization_current()")


def test_the_migration_is_rerun_safe(ndb):
    ndb.psql(file=MIGRATION)
