"""PR-L2: migration 20261002000100 (payload compaction) on ephemeral PostgreSQL.

Every migration applied, into a database that already has the production-
shaped read-only role. A snapshot of the REAL committed register rows (plus a
row whose `ramat_gimur` is null, one whose `ramat_gimur` is "" as 1,136
production rows state it, one whose `delek_cd` is null, and a placeholder
row) is captured, archived, built and then compacted. It must:

* refuse, with its static code and without writing, every missing
  precondition, and a dry-run must write nothing;
* keep every row, key, foreign key and link, and re-enable the triggers;
* answer every reader IDENTICALLY before and after (golden comparison): the
  register codes and content hash every restated reader takes, the coverage
  decisions and the placeholder filter, Prepare's queue build, the Government
  Tool's page, `resolve_variant`'s identity projection and unstated fields,
  the evidence mapper and REGISTER_FIELD_ABSENT;
* let the read-only role read what it could before;
* measure the bytes per register row after compaction (the capacity default).
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from backend.catalog.government import evidence as gov_evidence
from backend.catalog.government import source as src
from backend.catalog.government.normalize import read_wltp_record
from backend.catalog.government.query import GovernmentCatalogQuery
from backend.catalog.register import archive as arc
from backend.catalog.register import config as register_config
from backend.catalog.register import variants as mapper
from backend.engines.swarm_v2 import resolution
from backend.tools.government_vehicle import _provenance_payload, _variant_payload
from tests.test_catalog_variants import fixture_rows
from tests.test_catalog_variants_postgres import (RELEASE_RO, RO_ROLE, _build, _bulk_snapshot, _json,
                                                  _psql_file)
from tests.test_migrations_postgres import (BASELINE, MIGRATIONS, SEED_LEGACY_ROWS, SUPABASE_AUTH_SHIM,
                                            WSP_TOYOTA, EphemeralPostgres, _catalog_candidate_json,
                                            _catalog_record_json, _catalog_snapshot_json, _require_pg_bin,
                                            _rpc_as_service, _ws_create, _ws_scope, _ws_world, _wsb_finish,
                                            _wsb_start, _wsp_capture_run, _wsp_metadata, _wsp_prepare, _wsp_units)

COMPACTION_PG_PORT = "54996"
#: By name, never by position.
COMPACTION_MIGRATION = next(m for m in MIGRATIONS if m.name == "20261002000100_catalog_register_compaction.sql")
TOYOTA = WSP_TOYOTA
RESOURCE = src.WLTP_RESOURCE_ID


@pytest.fixture(scope="module")
def cdb():
    server = EphemeralPostgres(_require_pg_bin(), port=COMPACTION_PG_PORT)
    server.start()
    try:
        server.create_database()
        server.psql(f"create role {RELEASE_RO} login bypassrls; grant usage on schema public to {RELEASE_RO}; "
                    f"alter default privileges for role postgres in schema public "
                    f"grant select on tables to {RELEASE_RO}")
        server.psql(file=BASELINE)
        server.psql(sql=SEED_LEGACY_ROWS)
        server.psql(sql=SUPABASE_AUTH_SHIM)
        server.psql(f"create role {RO_ROLE} login bypassrls; grant pg_read_all_data to {RO_ROLE}")
        assert COMPACTION_MIGRATION in MIGRATIONS
        for migration in MIGRATIONS:
            server.psql(file=migration)
        yield server
    finally:
        server.stop()


def golden_rows() -> list[dict[str, Any]]:
    """The 16 committed rows, and the shapes production states."""
    rows = fixture_rows()
    base = next(r for r in rows if r["_id"] == 37425)
    # Each its own `degem_cd`, so each resolves to exactly one row.
    extra = [
        dict(copy.deepcopy(base), _id=90001, degem_cd=990001, ramat_gimur=None),       # null ramat_gimur
        dict(copy.deepcopy(base), _id=90002, degem_cd=990002, ramat_gimur=""),         # "" (1,136 production rows)
        dict(copy.deepcopy(base), _id=90003, degem_cd=990003, delek_cd=None,
             delek_nm="לא ידוע קוד"),                                                  # 264 production rows
        dict(copy.deepcopy(base), _id=90004, degem_cd=990004, kinuy_mishari="11111",
             degem_nm="11111111"),                                                     # placeholder
        dict(copy.deepcopy(base), _id=90005, degem_cd=990005, ramat_gimur=7),         # unparseable (a number)
    ]
    return rows + extra


def _world(db) -> dict[str, Any]:
    user, _project, conversation = _ws_world(db)
    run_id, args = _wsp_capture_run(db, conversation)
    return {"user": user, "conversation": conversation, "run_id": run_id, "args": args}


def _snapshot(db, world: dict, rows: list[dict], label: str, *, archived: bool = True,
              marque: str | None = TOYOTA) -> dict:
    """An ACTIVE scoped snapshot of `rows`, one candidate per row (PR-V's
    reading), archived like a register capture; its writer run then ends."""
    args = world["args"]
    payload = json.loads(_catalog_snapshot_json(f"cmp-{label}", declared=len(rows)))
    payload["retrieval_metadata"] = _wsp_metadata(marque, count=len(rows))
    snapshot = _rpc_as_service(db, f"select id from public.record_catalog_snapshot_guarded({args}, {_json(payload)})")
    for index, row in enumerate(rows):
        record = json.loads(_catalog_record_json(snapshot, f"cmp-{label}-r{index}", upstream=str(row["_id"]),
                                                 payload=row, locator={"page_index": 0, "page_number": 1,
                                                                       "page_offset": index,
                                                                       "capture_index": index}))
        record_id = _rpc_as_service(db, f"select id from public.record_catalog_raw_record_guarded({args}, "
                                        f"{_json(record)})")
        reading = read_wltp_record(row)
        candidate = json.loads(_catalog_candidate_json(
            snapshot, record_id, f"cmp-{label}-c{index}", status=reading.status, make=reading.manufacturer,
            model=reading.commercial_model, years=(reading.model_year_start, reading.model_year_end),
            code=reading.official_model_code, trim=reading.trim, dimensions=reading.identity_dimensions))
        _rpc_as_service(db, f"select id from public.record_catalog_candidate_guarded({args}, {_json(candidate)})")
    _rpc_as_service(db, f"select id from public.activate_catalog_snapshot_guarded({args}, "
                        f"'{json.dumps({'snapshot_id': snapshot})}'::jsonb)")
    key = db.psql(f"select snapshot_key from public.catalog_source_snapshots where id='{snapshot}'")
    if archived:
        obj = arc.build(rows)
        _rpc_as_service(db, f"select public.record_register_snapshot_archive({args}, '{snapshot}', "
                            f"'gs://milo-test-archive/register/{RESOURCE}/{'0' * 16}/{key}.jsonl.gz', "
                            f"{obj.byte_size}, '{obj.sha256}', {len(rows)})")
    db.psql(f"update public.runs set status = 'completed' where id = '{world['run_id']}'")
    return {"id": snapshot, "key": key, "rows": rows, "args": args}


def _compact(db, key: str, apply: bool = True) -> dict:
    return json.loads(_rpc_as_service(db, f"select public.compact_register_snapshot('{key}', {str(apply).lower()})"))


def _scalar(db, sql: str) -> int:
    return int(db.psql(sql))


class PsqlRepository:
    """The repository methods the Government query layer reads, over psql:
    the REAL RPCs, called as the service role."""

    def __init__(self, db) -> None:
        self.db = db

    def _one(self, sql: str) -> Any:
        text = _rpc_as_service(self.db, sql)
        return json.loads(text) if text else None

    def find_active_catalog_snapshot(self, family: str, resource_id: str, key: str) -> dict | None:
        return self._one(f"select to_jsonb(s) from public.catalog_source_snapshots s where source_family = '{family}' "
                         f"and resource_id = '{resource_id}' and snapshot_key = '{key}' and activated_at is not null")

    def catalog_candidate_variant_page(self, snapshot_id, *, manufacturer=None, commercial_model=None,
                                       model_year=None, official_model_code=None, trim=None,
                                       identity_dimensions=None, status=None, limit=50, offset=0,
                                       allow_incomplete=False, register_manufacturer_code=None,
                                       register_model_code=None, vehicle_type_code=None) -> list[dict]:
        def lit(value):
            if value is None:
                return "null"
            if isinstance(value, dict):
                return _json(value)
            if isinstance(value, bool):
                return str(value).lower()
            if isinstance(value, int):
                return str(value)
            return "$v$" + str(value) + "$v$"
        params = {"p_manufacturer": manufacturer, "p_commercial_model": commercial_model,
                  "p_model_year": model_year, "p_official_model_code": official_model_code, "p_trim": trim,
                  "p_identity_dimensions": identity_dimensions, "p_status": status, "p_limit": limit,
                  "p_offset": offset, "p_allow_incomplete": allow_incomplete,
                  "p_register_manufacturer_code": register_manufacturer_code,
                  "p_register_model_code": register_model_code, "p_vehicle_type_code": vehicle_type_code}
        named = ", ".join(f"{name} => {lit(value)}" for name, value in params.items())
        return self._one(f"select coalesce(json_agg(t), '[]') from public.catalog_candidate_variant_page("
                         f"'{snapshot_id}', {named}) t")

    def catalog_raw_record_by_upstream_id(self, snapshot_id, upstream_record_id, *, allow_incomplete=False):
        return self._one(f"select to_jsonb(t) from public.catalog_raw_record_by_upstream_id('{snapshot_id}', "
                         f"'{upstream_record_id}', {str(allow_incomplete).lower()}) t")

    def catalog_compacted_record_reading(self, snapshot_id, upstream_record_id, *, allow_incomplete=False):
        return self._one(f"select public.catalog_compacted_record_reading('{snapshot_id}', '{upstream_record_id}', "
                         f"{str(allow_incomplete).lower()})")


def _tool_result(resolution_result) -> dict:
    """`GovernmentVehicleTool._op_resolve_variant`'s result, as it builds it."""
    result = {"resolved": resolution_result.variant is not None, "ambiguous": resolution_result.ambiguous,
              "match_count": resolution_result.match_count,
              "variants": [_variant_payload(item) for item in resolution_result.matches],
              "provenance": _provenance_payload(resolution_result.provenance),
              "match_mode": resolution_result.match_mode}
    if resolution_result.variant is not None and resolution_result.identity_projection:
        result["source_record"] = {"upstream_record_id": resolution_result.variant.upstream_record_id,
                                   **dict(resolution_result.identity_projection)}
        result["register_unstated"] = list(resolution_result.unstated_fields)
    return result


def readers(db, snapshot: dict) -> dict[str, Any]:
    """Every reader's answer over one snapshot (the golden)."""
    sid = snapshot["id"]
    answers: dict[str, Any] = {}
    # The register codes and content hash every restated reader takes.
    answers["facts"] = db.psql(
        "select string_agg(r.upstream_record_id || '|' || coalesce(public.catalog_raw_record_code(r, 'tozeret_cd'), '~')"
        " || '|' || coalesce(public.catalog_raw_record_code(r, 'degem_cd'), '~') || '|' || "
        "coalesce(public.catalog_raw_record_code(r, 'sug_degem'), '~') || '|' || "
        "public.catalog_raw_record_content_sha256(r), ',' order by r.upstream_record_id) "
        f"from public.catalog_raw_records r where r.snapshot_id = '{sid}'")
    # Coverage decisions (Prepare's queue build) with the placeholder filter.
    for include in ("false", "true"):
        answers[f"decisions:{include}"] = _rpc_as_service(
            db, "select coalesce(json_agg(d order by d.upstream_record_id), '[]') from "
                f"public.catalog_work_scope_coverage_decisions('{sid}', null, null, {include}) d")
    # The Government Tool's page: unfiltered and by each register code.
    repo = PsqlRepository(db)
    answers["page"] = repo.catalog_candidate_variant_page(sid, limit=100)
    answers["page:codes"] = repo.catalog_candidate_variant_page(
        sid, limit=100, register_manufacturer_code="413", register_model_code=str(snapshot["rows"][0]["degem_cd"]),
        vehicle_type_code="P")
    # resolve_variant, the evidence mapper and REGISTER_FIELD_ABSENT, per row.
    query = GovernmentCatalogQuery(repo, snapshot_key=snapshot["key"], allow_incomplete=True)
    mapper_ = gov_evidence.GovernmentVariantEvidenceMapper()
    for row in answers["page"]:
        codes = {"register_manufacturer_code": row["register_manufacturer_code"],
                 "register_model_code": row["register_model_code"], "vehicle_type_code": row["vehicle_type_code"]}
        resolved = query.resolve_variant(row["manufacturer"], row["commercial_model"], row["model_year_start"],
                                         trim=row["trim"], official_model_code=row["official_model_code"],
                                         identity_dimensions=row["identity_dimensions"], **codes)
        result = _tool_result(resolved)
        try:
            bundle = repr(mapper_.map(SimpleNamespace(result=result)))
        except Exception as failure:  # noqa: BLE001 - the answer is compared, whatever it is
            bundle = f"refused {type(failure).__name__}"
        outcome = resolution.candidate_outcome(task_id="t", call_id="c", tool="catalog.government_vehicle",
                                               operation="resolve_variant", arguments=dict(codes), result=result)
        answers[f"resolve:{row['upstream_record_id']}"] = {
            "match_count": resolved.match_count, "projection": dict(resolved.identity_projection),
            "unstated": list(resolved.unstated_fields), "evidence": bundle, "outcome": outcome}
    return answers


def _coverage(db, snapshot: dict) -> dict:
    """The three coverage readers over the snapshot's first batch of a fresh
    plan: the batch read, the paid-work claim (a real claimed run) and the
    finalize path's ledger write. Everything they wrote is removed after, so
    the ledger and the claims are exactly as before for the next call."""
    user, _project, conversation = _ws_world(db)
    scope = _ws_scope(max_items=25, batch_size=10, model_year_from=2018)
    plan = _ws_create(db, conversation, user, scope)["work_scope"]["id"]
    _run, args = _wsp_capture_run(db, conversation)
    prepared = _wsp_prepare(db, args, plan, 1, scope.digest(), _wsp_units(snapshot["id"]))
    batch = prepared["batches"][0]["id"]
    read = json.loads(_rpc_as_service(db, f"select public.catalog_variant_coverage_for_batch('{batch}', 'register')"))
    world = {"plan": plan, "digest": scope.digest(), "user": user}
    run = _wsb_start(db, world, batch, key=f"cmp-{uuid.uuid4().hex[:12]}")["run"]["id"]
    attempt, token = db.psql(f"select attempt, lease_token from public.claim_run_lease('{run}', 'w-cmp', 300)").split("|")
    items = [item["candidate_id"] for item in read["items"]]
    claimed = json.loads(_rpc_as_service(db, "select public.acquire_catalog_variant_reservations_guarded("
                                             f"'{run}', 'w-cmp', {attempt}, '{token}', 'register', "
                                             f"$c${json.dumps(items)}$c$::jsonb)"))
    _wsb_finish(db, run, "partial_success")
    entries = [{"candidate_id": c, "status": "enriched" if n % 2 else "failed"} for n, c in enumerate(items)]
    written = json.loads(_rpc_as_service(db, f"select public.rebuild_catalog_variant_coverage('{run}', 'register', "
                                             f"$e${json.dumps(entries)}$e$::jsonb)"))
    ledger = db.psql("select string_agg(variant_identity_key || '|' || status || '|' || content_sha256 || '|' || "
                     "snapshot_key || '|' || coalesce(reason_code, '~'), ',' order by variant_identity_key) "
                     f"from public.catalog_variant_coverage where last_run_id = '{run}'")
    db.psql(f"delete from public.catalog_variant_reservations where run_id = '{run}'; "
            f"delete from public.catalog_variant_coverage where last_run_id = '{run}'")
    strip = ("owner_run_id", "batch_id", "run_id", "last_run_id")
    return {"read": [{k: v for k, v in item.items() if k not in strip} for item in read["items"]],
            "read_include": read["include_unresolved"],
            "claimed": [{k: v for k, v in item.items() if k not in strip} for item in claimed["items"]],
            "written": {k: v for k, v in written.items() if k not in strip}, "ledger": ledger}


def _queue(db, snapshot: dict) -> dict:
    """Prepare's queue build over the snapshot, on a fresh plan (a prepared
    revision answers its stored queue, so each call is a new plan)."""
    user, _project, conversation = _ws_world(db)
    scope = _ws_scope(max_items=25, batch_size=10, model_year_from=2018)
    plan = _ws_create(db, conversation, user, scope)["work_scope"]["id"]
    _run, args = _wsp_capture_run(db, conversation)
    prepared = _wsp_prepare(db, args, plan, 1, scope.digest(), _wsp_units(snapshot["id"]))
    preparation = db.psql("select id from public.catalog_work_scope_preparations "
                          f"where work_scope_id = '{plan}'")
    queued = db.psql("select string_agg(c.upstream || '@' || i.position || '/' || i.batch_position, ',' "
                     "order by i.position) from public.catalog_work_scope_queue_items i "
                     "join (select c.id, r.upstream_record_id as upstream from public.catalog_candidate_variants c "
                     "join public.catalog_raw_records r on r.id = c.raw_record_id) c on c.id = i.candidate_id "
                     f"where i.preparation_id = '{preparation}'")
    coverage = _rpc_as_service(db, "select json_agg(to_jsonb(c) - 'id' - 'preparation_id' - 'unit_id' - 'created_at' "
                                   "order by c.unit_key) from public.catalog_work_scope_unit_coverage c "
                                   f"where c.preparation_id = '{preparation}'")
    return {"queued": queued, "coverage": coverage,
            "counts": {k: v for k, v in prepared.items() if not isinstance(v, (dict, list)) and "id" not in k}}


@pytest.fixture(scope="module")
def golden(cdb):
    world = _world(cdb)
    snapshot = _snapshot(cdb, world, golden_rows(), "golden")
    assert _build(cdb, snapshot)["complete"] is True
    # A `register`-level ledger row, so a decision is `excluded_already_enriched`.
    cdb.psql("insert into public.catalog_variant_coverage (variant_identity_key, level, status, last_run_id, "
             "snapshot_key, content_sha256, vocabulary_version) select v.variant_identity_key, 'register', "
             f"'enriched', '{world['run_id']}', v.snapshot_key, v.content_sha256, public.catalog_vocabulary_version() "
             f"from public.catalog_variants v where v.snapshot_id = '{snapshot['id']}' and v.upstream_record_id = '37425'")
    identity = _identity(cdb, snapshot)
    before = {"readers": readers(cdb, snapshot), "queue": _queue(cdb, snapshot), "coverage": _coverage(cdb, snapshot)}
    counts = _counts(cdb, snapshot)
    dry = _compact(cdb, snapshot["key"], apply=False)
    assert dry["status"] == "ready" and dry["raw_rows"] == 21 and dry["mode"] == "active"
    assert _scalar(cdb, f"select count(*) from public.catalog_raw_records where snapshot_id='{snapshot['id']}' "
                        "and payload is null") == 0
    done = _compact(cdb, snapshot["key"])
    assert done["status"] == "compacted" and done["payloads_removed"] == 21
    after = {"readers": readers(cdb, snapshot), "queue": _queue(cdb, snapshot), "coverage": _coverage(cdb, snapshot)}
    return {"snapshot": snapshot, "world": world, "before": before, "after": after, "counts": counts,
            "done": done, "identity": identity}


def _identity(db, snapshot: dict, relation: str = "catalog_candidate_variants") -> str:
    """Every candidate's identity columns, as `relation` answers them."""
    return db.psql("select string_agg(concat_ws('|', c.id, c.manufacturer, c.commercial_model, c.model_year_start, "
                   "c.model_year_end, c.official_model_code, c.trim, c.identity_dimensions::text, c.status, "
                   f"c.candidate_key), ',' order by c.id) from public.{relation} c "
                   f"where c.snapshot_id = '{snapshot['id']}'")


def _counts(db, snapshot: dict) -> dict[str, int]:
    sid = snapshot["id"]
    return {name: _scalar(db, sql) for name, sql in {
        "raw": f"select count(*) from public.catalog_raw_records where snapshot_id='{sid}'",
        "candidates": f"select count(*) from public.catalog_candidate_variants where snapshot_id='{sid}'",
        "variants": f"select count(*) from public.catalog_variants where snapshot_id='{sid}'",
        "keys": f"select count(distinct record_key || payload_sha256 || source_locator::text) "
                f"from public.catalog_raw_records where snapshot_id='{sid}'"}.items()}


# -- 1. identical readers ---------------------------------------------------------------

def test_every_reader_answers_identically_after_compaction(golden):
    before, after = golden["before"]["readers"], golden["after"]["readers"]
    assert set(before) == set(after)
    for name in before:
        assert after[name] == before[name], name
    assert golden["after"]["queue"] == golden["before"]["queue"]
    assert golden["after"]["coverage"] == golden["before"]["coverage"]


def test_the_golden_covers_the_shapes_that_matter(golden):
    answers = golden["after"]["readers"]
    decisions = {d["upstream_record_id"]: d["decision"] for d in json.loads(answers["decisions:false"])}
    assert decisions["90004"] == "excluded_placeholder_source_record"
    assert decisions["37425"] == "excluded_already_enriched"
    # "" and null both read as "the register states no trim" (P32 soft gap).
    for record in ("90001", "90002"):
        (resolved,) = [v for k, v in answers.items() if k == f"resolve:{record}"]
        assert "ramat_gimur" in resolved["unstated"] and "ramat_gimur" not in resolved["projection"]
    null_fuel = answers["resolve:90003"]
    assert "delek_cd" in null_fuel["unstated"] and "delek_cd" not in null_fuel["projection"]
    # An unparseable value stays STATED: REGISTER_FIELD_ABSENT is a hard gap for it.
    unparseable = answers["resolve:90005"]
    assert "ramat_gimur" not in unparseable["unstated"] and "ramat_gimur" not in unparseable["projection"]
    coverage = golden["after"]["coverage"]
    assert coverage["read"] and coverage["claimed"] and coverage["written"]["written"] > 0 and coverage["ledger"]
    assert {item["decision"] for item in coverage["claimed"]} == {"reserved"}
    stated = answers["resolve:37425"]
    assert stated["match_count"] == 1 and stated["projection"]["tozeret_cd"] == "413"
    assert stated["projection"]["shnat_yitzur"] == golden["snapshot"]["rows"][8]["shnat_yitzur"]
    assert stated["evidence"].startswith("EvidenceBundle")
    queue = golden["after"]["queue"]
    assert queue["queued"] and "90004" not in queue["queued"]


# -- 2. rows, keys, links and triggers stay --------------------------------------------

def test_every_row_key_and_link_stays_and_the_triggers_are_back(golden, cdb):
    snapshot = golden["snapshot"]
    assert _counts(cdb, snapshot) == golden["counts"]
    assert _scalar(cdb, f"select count(*) from public.catalog_raw_records where snapshot_id='{snapshot['id']}' "
                        "and payload is null") == 21
    # Queue items (both plans) still name the candidates of the snapshot.
    assert _scalar(cdb, "select count(*) from public.catalog_work_scope_queue_items i "
                        "join public.catalog_candidate_variants c on c.id = i.candidate_id "
                        f"where c.snapshot_id = '{snapshot['id']}'") > 0
    for trigger in ("catalog_raw_records_append_only", "catalog_raw_records_payload_required",
                    "catalog_candidate_variants_identity_immutable", "catalog_candidate_variants_identity_required",
                    "catalog_variants_compacted_guard"):
        assert cdb.psql(f"select tgenabled from pg_trigger where tgname = '{trigger}'") == "O"
    with pytest.raises(AssertionError, match="append-only|CATALOG_SOURCE"):
        cdb.psql(f"update public.catalog_raw_records set payload = '{{}}' where snapshot_id = '{snapshot['id']}'")
    with pytest.raises(AssertionError, match="CATALOG_RAW_RECORD_PAYLOAD_REQUIRED"):
        cdb.psql("insert into public.catalog_raw_records (snapshot_id, resource_id, upstream_record_id, payload, "
                 f"payload_sha256, record_key) values ('{snapshot['id']}', '{RESOURCE}', 'x', null, '{'a' * 64}', "
                 f"'cr1.{'b' * 32}')")
    # Candidates keep their keys; the view reads every identity exactly as stored before.
    assert _scalar(cdb, "select count(*) from public.catalog_candidate_variants "
                        f"where snapshot_id = '{snapshot['id']}' and (manufacturer is not null or "
                        "commercial_model is not null or model_year_start is not null or trim is not null "
                        "or official_model_code is not null or identity_dimensions <> '{}')") == 0
    assert _identity(cdb, snapshot, "catalog_candidate_variants_resolved") == golden["identity"]
    for index in ("catalog_candidate_variants_natural_uidx", "catalog_candidate_variants_snapshot_identity_idx"):
        assert cdb.psql(f"select indexdef from pg_indexes where indexname = '{index}'").endswith(
            "WHERE (manufacturer IS NOT NULL)")
    with pytest.raises(AssertionError, match="CATALOG_CANDIDATE_IDENTITY_REQUIRED"):
        cdb.psql("insert into public.catalog_candidate_variants (snapshot_id, raw_record_id, commercial_model, "
                 f"candidate_key) select snapshot_id, raw_record_id, 'x', 'cc1.{'c' * 32}' "
                 f"from public.catalog_candidate_variants where snapshot_id = '{snapshot['id']}' limit 1")
    with pytest.raises(AssertionError, match="catalog candidate identity"):
        cdb.psql("update public.catalog_candidate_variants set manufacturer = 'x' "
                 f"where snapshot_id = '{snapshot['id']}'")
    row = json.loads(cdb.psql("select to_jsonb(c) from public.catalog_register_snapshot_compactions c "
                              f"where snapshot_id = '{snapshot['id']}'"))
    assert (row["readers"], row["kept_rows"]) == ("variants", 21)
    assert row["mapper_version"] == mapper.MAPPER_VERSION and row["raw_rows"] == 21
    assert row["bytes_after"] < row["bytes_before"]
    for change in ("update public.catalog_register_snapshot_compactions set raw_rows = 1",
                   "delete from public.catalog_register_snapshot_compactions"):
        with pytest.raises(AssertionError, match="CATALOG_REGISTER_IMMUTABLE"):
            cdb.psql(f"{change} where snapshot_id = '{snapshot['id']}'")


def test_a_second_compaction_is_unchanged_and_a_rebuild_is_refused(golden, cdb):
    snapshot = golden["snapshot"]
    assert _compact(cdb, snapshot["key"])["status"] == "unchanged"
    # A new mapper version cannot be built from the database (no payload), and
    # the database refuses any variant row for a compacted snapshot.
    with pytest.raises(AssertionError, match="CATALOG_VARIANT_MAPPER_MISMATCH"):
        _build(cdb, snapshot, version="gov.wltp.variant-mapper.9")
    with pytest.raises(AssertionError, match="CATALOG_VARIANT_SNAPSHOT_COMPACTED"):
        cdb.psql("insert into public.catalog_variants (snapshot_id, snapshot_key, upstream_record_id, content_sha256, "
                 "mapper_version, vehicle_segment) select snapshot_id, snapshot_key, upstream_record_id, content_sha256, "
                 f"mapper_version, vehicle_segment from public.catalog_variants where snapshot_id = '{snapshot['id']}' "
                 "limit 1")


def test_an_archive_line_is_checked_against_the_row_by_the_database(golden, cdb):
    snapshot = golden["snapshot"]
    row = golden_rows()[8]
    record = cdb.psql(f"select id from public.catalog_raw_records where snapshot_id='{snapshot['id']}' "
                      f"and upstream_record_id = '{row['_id']}'")
    line = arc.canonical_line(row).replace("'", "''")
    assert cdb.psql(f"select public.catalog_raw_record_payload_matches('{record}', '{line}')") == "t"
    other = arc.canonical_line(dict(row, ramat_gimur="X")).replace("'", "''")
    assert cdb.psql(f"select public.catalog_raw_record_payload_matches('{record}', '{other}')") == "f"


def test_a_compacted_snapshot_is_still_pruned_whole(cdb):
    world = _world(cdb)
    rows = [dict(r, _id=r["_id"] + 700000) for r in fixture_rows()[:3]]
    first = _snapshot(cdb, world, rows, "prune-old", marque="פרונה")
    assert _build(cdb, first)["complete"] is True and _compact(cdb, first["key"])["status"] == "compacted"
    for label in ("prune-mid", "prune-new"):
        newer = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 1, ramat_gimur=label) for r in rows], label,
                          marque="פרונה")
        _build(cdb, newer)
    listed = json.loads(_rpc_as_service(cdb, "select public.catalog_register_prunable_list()"))
    keys = [item["snapshot_key"] for item in listed["snapshots"]]
    assert first["key"] in keys
    json.loads(_rpc_as_service(cdb, "select public.prune_register_snapshots("
                                    f"array{json.dumps(keys + [b['item'] for b in listed['variant_builds']])}"
                                    f"::text[], '{listed['digest']}')".replace('"', "'")))
    # The compaction record is history, like the archive record: it stays.
    assert _scalar(cdb, f"select count(*) from public.catalog_register_snapshot_compactions "
                        f"where snapshot_key = '{first['key']}'") == 1
    assert _scalar(cdb, f"select count(*) from public.catalog_raw_records where snapshot_id = '{first['id']}'") == 0


# -- 3. every refusal -------------------------------------------------------------------

def _refused(db, key: str) -> str:
    answer = _compact(db, key, apply=True)
    assert answer["status"] == "refused"
    return answer["code"]


def test_every_missing_precondition_is_refused_and_writes_nothing(cdb):
    world = _world(cdb)
    rows = [dict(r, _id=r["_id"] + 800000) for r in fixture_rows()[:4]]
    assert _refused(cdb, "cs1." + "0" * 32) == "CATALOG_COMPACTION_SNAPSHOT_UNKNOWN"
    # An unscoped (whole-register) snapshot.
    unscoped = _snapshot(cdb, world, [dict(r, _id=r["_id"] + 1000) for r in rows], "unscoped", marque=None)
    assert _refused(cdb, unscoped["key"]) == "CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE"
    # Never activated.
    pending = json.loads(_catalog_snapshot_json("cmp-pending", declared=1))
    pending["retrieval_metadata"] = _wsp_metadata("ממתין", count=1)
    pending_world = _world(cdb)
    pid = _rpc_as_service(cdb, f"select id from public.record_catalog_snapshot_guarded({pending_world['args']}, "
                               f"{_json(pending)})")
    pkey = cdb.psql(f"select snapshot_key from public.catalog_source_snapshots where id = '{pid}'")
    assert _refused(cdb, pkey) == "CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE"
    # Not built, then built only in part.
    unbuilt = _snapshot(cdb, _world(cdb), rows, "unbuilt", marque="בנייה")
    assert _refused(cdb, unbuilt["key"]) == "CATALOG_COMPACTION_BUILD_INCOMPLETE"
    _build(cdb, unbuilt, rows=rows[:2])
    assert _refused(cdb, unbuilt["key"]) == "CATALOG_COMPACTION_BUILD_INCOMPLETE"
    _build(cdb, unbuilt)
    assert _compact(cdb, unbuilt["key"], apply=False)["status"] == "ready"
    # No archive.
    bare = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 2000) for r in rows], "bare", archived=False,
                     marque="ארכיון")
    _build(cdb, bare)
    assert _refused(cdb, bare["key"]) == "CATALOG_COMPACTION_ARCHIVE_MISSING"
    # A register unit recorded the capture count-unverified (the newest decides).
    unverified = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 3000) for r in rows], "unverified",
                           marque="ספירה")
    _build(cdb, unverified)
    group = cdb.psql("insert into public.catalog_register_capture_groups (register_version, requested_by, "
                     f"expected_rows) values ('{'d' * 64}', '{uuid.uuid4()}', 4) returning id")
    cdb.psql("insert into public.catalog_register_capture_units (group_id, register_version, tozar, expected_rows, "
             "status, snapshot_id, snapshot_key, api_total, captured_rows, count_verified, failure_code) values "
             f"('{group}', '{'d' * 64}', 'ספירה', 4, 'failed', '{unverified['id']}', '{unverified['key']}', 5, 4, "
             "false, 'CATALOG_CAPTURE_COUNT_MISMATCH')")
    assert _refused(cdb, unverified["key"]) == "CATALOG_COMPACTION_COUNT_UNVERIFIED"
    # A row whose typed variant cannot read as its payload: a manufacturer code
    # stated as zero-padded text (`payload->>'tozeret_cd'` = '0413', typed 413).
    typed = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 4000) for r in rows[:3]]
                      + [dict(rows[3], _id=rows[3]["_id"] + 4000, tozeret_cd="0413")],
                      "typed", marque="טיפוס")
    _build(cdb, typed)
    answer = _compact(cdb, typed["key"])
    assert (answer["code"], answer["mismatched_rows"]) == ("CATALOG_COMPACTION_TYPED_MISMATCH", 1)
    # A candidate whose identity is not its variant's (a trim the variant does not state).
    drifted = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 5000) for r in rows], "drifted", marque="סחיפה")
    _build(cdb, drifted)
    cdb.psql("alter table public.catalog_candidate_variants disable trigger catalog_candidate_variants_identity_immutable; "
             "update public.catalog_candidate_variants set trim = 'DRIFT' "
             f"where id = (select id from public.catalog_candidate_variants where snapshot_id = '{drifted['id']}' "
             "order by id limit 1); "
             "alter table public.catalog_candidate_variants enable trigger catalog_candidate_variants_identity_immutable")
    answer = _compact(cdb, drifted["key"])
    assert (answer["code"], answer["mismatched_rows"]) == ("CATALOG_COMPACTION_TYPED_MISMATCH", 1)
    # Nothing was written by any refusal (nor by the dry-run above).
    assert _scalar(cdb, "select count(*) from public.catalog_raw_records r "
                        f"where r.snapshot_id in ('{unbuilt['id']}', '{bare['id']}', '{unverified['id']}', "
                        f"'{typed['id']}', '{unscoped['id']}', '{drifted['id']}') and r.payload is null") == 0
    assert _scalar(cdb, "select count(*) from public.catalog_candidate_variants c "
                        f"where c.snapshot_id in ('{typed['id']}', '{drifted['id']}') and c.manufacturer is null") == 0
    assert _scalar(cdb, "select count(*) from public.catalog_register_snapshot_compactions c "
                        f"where c.snapshot_id in ('{unbuilt['id']}', '{bare['id']}', '{unverified['id']}', "
                        f"'{typed['id']}')") == 0
    with pytest.raises(AssertionError, match="CATALOG_COMPACTION_REQUEST_INVALID"):
        _rpc_as_service(cdb, "select public.compact_register_snapshot('bad key', true)")


def test_a_prepare_snapshot_is_archived_from_its_stored_rows_then_compacted(cdb):
    rows = [dict(r, _id=r["_id"] + 900000) for r in fixture_rows()[:5]]
    snapshot = _snapshot(cdb, _world(cdb), rows, "prepare", archived=False, marque="הכנה")
    _build(cdb, snapshot)
    obj = arc.build(rows)
    uri = f"gs://milo-test-archive/register/{RESOURCE}/{'1' * 16}/{snapshot['key']}.jsonl.gz"
    call = (f"select public.record_register_snapshot_archive_from_database('{snapshot['id']}', '{uri}', "
            f"{obj.byte_size}, '{obj.sha256}', %d)")
    with pytest.raises(AssertionError, match="CATALOG_CAPTURE_COUNT_MISMATCH"):
        _rpc_as_service(cdb, call % 4)
    recorded = json.loads(_rpc_as_service(cdb, call % 5))
    assert recorded["sha256"] == obj.sha256 and recorded["line_count"] == 5
    assert json.loads(_rpc_as_service(cdb, call % 5))["id"] == recorded["id"]
    with pytest.raises(AssertionError, match="CATALOG_ARCHIVE_CONFLICT"):
        _rpc_as_service(cdb, call.replace(obj.sha256, "e" * 64) % 5)
    assert _compact(cdb, snapshot["key"])["status"] == "compacted"


# -- 3b. a superseded snapshot keeps only its referenced rows ---------------------------

class ArchiveReader:
    """The archive bucket, in memory: one object per name."""

    bucket = "milo-test-archive"

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get(self, name: str) -> bytes:
        return self.objects[name]


class ArchiveRepository(PsqlRepository):
    """What `compaction.source_record` reads, over psql."""

    def register_snapshot_archive(self, snapshot_id):
        return self._one(f"select to_jsonb(a) from public.catalog_register_snapshot_archives a "
                         f"where a.snapshot_id = '{snapshot_id}'")

    def register_snapshot_archived(self, snapshot_id) -> bool:
        return self.db.psql("select count(*) from public.catalog_register_snapshot_compactions "
                            f"where snapshot_id = '{snapshot_id}' and readers = 'archive'") == "1"

    def catalog_raw_record_payload_matches(self, raw_record_id, line) -> bool:
        return self.db.psql(f"select public.catalog_raw_record_payload_matches('{raw_record_id}', "
                            f"$l${line}$l$)") == "t"


def _plan_on(db, snapshot: dict, marque: str) -> dict:
    """A prepared plan over the snapshot (its unit is the snapshot's tozar):
    its queue names candidates of it."""
    user, _project, conversation = _ws_world(db)
    scope = _ws_scope(max_items=10, batch_size=5, model_year_from=2018)
    plan = _ws_create(db, conversation, user, scope)["work_scope"]["id"]
    capture_run, args = _wsp_capture_run(db, conversation)
    units = [dict(unit, register_marque=marque) if unit["snapshot_id"] else unit for unit in _wsp_units(snapshot["id"])]
    prepared = _wsp_prepare(db, args, plan, 1, scope.digest(), units)
    return {"plan": plan, "digest": scope.digest(), "user": user, "capture_run": capture_run,
            "batches": [batch["id"] for batch in prepared["batches"]]}


def _settle_capture_runs(db) -> None:
    """End every capture-job run earlier tests left live (a Prepare's run
    blocks every superseded compaction until it ends)."""
    db.psql("update public.runs r set status = 'completed' where r.run_identity->>'workflow_key' = 'operator_capture' "
            "and r.status not in ('completed', 'partial_success', 'failed', 'cancelled', 'timed_out', "
            "'budget_exhausted')")


def test_a_superseded_snapshot_keeps_its_referenced_rows_as_skeletons(cdb):
    from backend.catalog.register import compaction

    marque = "מוחלף"
    rows = [dict(r, _id=r["_id"] + 600000, degem_cd=r["degem_cd"] + 600000) for r in fixture_rows()]
    old = _snapshot(cdb, _world(cdb), rows, "sup-old", marque=marque)
    assert _build(cdb, old)["complete"] is True and _compact(cdb, old["key"])["mode"] == "active"
    plan = _plan_on(cdb, old, marque)
    queued = _scalar(cdb, f"select count(*) from public.catalog_work_scope_queue_items where snapshot_id = '{old['id']}'")
    assert 0 < queued < len(rows)
    run = _wsb_start(cdb, plan, plan["batches"][0], key=f"sup-{old['key'][-8:]}")["run"]["id"]
    # A new capture of the tozar (one field changed), built and compacted: the old one is superseded.
    new = _snapshot(cdb, _world(cdb), [dict(r, koah_sus=(r.get("koah_sus") or 0) + 1) for r in rows], "sup-new",
                    marque=marque)
    assert _build(cdb, new)["complete"] is True and _compact(cdb, new["key"])["mode"] == "active"
    listed = json.loads(_rpc_as_service(cdb, f"select public.catalog_register_superseded_snapshots('{new['key']}')"))
    assert [item["snapshot_key"] for item in listed] == [old["key"]]
    # Something live can still read it: a run on its batch, a startable batch, the preparation's run.
    assert _refused(cdb, old["key"]) == "CATALOG_COMPACTION_SNAPSHOT_IN_USE"
    _wsb_finish(cdb, run, "partial_success")
    assert _refused(cdb, old["key"]) == "CATALOG_COMPACTION_SNAPSHOT_IN_USE"
    cdb.psql(f"update public.catalog_work_scopes set closed_at = now() where id = '{plan['plan']}'")
    assert _refused(cdb, old["key"]) == "CATALOG_COMPACTION_SNAPSHOT_IN_USE"
    _wsb_finish(cdb, plan["capture_run"], "completed")
    # Any capture-job run that is not a register capture -- a Prepare, which can read a snapshot for
    # reuse before it records a unit -- blocks it too, whatever it prepares.
    _settle_capture_runs(cdb)
    other, _args = _wsp_capture_run(cdb, _ws_world(cdb)[2])
    assert _refused(cdb, old["key"]) == "CATALOG_COMPACTION_SNAPSHOT_IN_USE"
    _wsb_finish(cdb, other, "completed")
    ready = _compact(cdb, old["key"], apply=False)
    assert (ready["status"], ready["mode"], ready["raw_rows"], ready["kept_rows"]) == (
        "ready", "superseded", len(rows), queued)
    done = _compact(cdb, old["key"])
    assert (done["status"], done["kept_rows"]) == ("compacted", queued)
    # Only the referenced rows stay, as skeletons; no variant, no build.
    sid = old["id"]
    assert _counts(cdb, old)["raw"] == queued == _counts(cdb, old)["candidates"]
    assert _counts(cdb, old)["variants"] == 0
    assert _scalar(cdb, f"select count(*) from public.catalog_variant_builds where snapshot_id = '{sid}'") == 0
    assert _scalar(cdb, f"select count(*) from public.catalog_raw_records where snapshot_id = '{sid}' "
                        "and payload is not null") == 0
    assert _scalar(cdb, f"select count(*) from public.catalog_candidate_variants where snapshot_id = '{sid}' "
                        "and manufacturer is not null") == 0
    assert _scalar(cdb, f"select count(*) from public.catalog_work_scope_queue_items where snapshot_id = '{sid}'") \
        == queued
    assert cdb.psql("select string_agg(readers, ',' order by readers) from public.catalog_register_snapshot_compactions "
                    f"where snapshot_id = '{sid}'") == "archive,variants"
    # It is never browsed again; its archive is the record, for a kept row and a dropped one alike.
    with pytest.raises(AssertionError, match="CATALOG_SNAPSHOT_ARCHIVED"):
        PsqlRepository(cdb).catalog_candidate_variant_page(sid)
    reader = ArchiveReader({f"register/{RESOURCE}/{'0' * 16}/{old['key']}.jsonl.gz": arc.build(rows).data})
    kept = cdb.psql(f"select min(upstream_record_id) from public.catalog_raw_records where snapshot_id = '{sid}'")
    dropped = next(str(r["_id"]) for r in rows if not _scalar(
        cdb, f"select count(*) from public.catalog_raw_records where snapshot_id = '{sid}' "
             f"and upstream_record_id = '{r['_id']}'"))
    for upstream in (kept, dropped):
        record = compaction.source_record(ArchiveRepository(cdb), reader, sid, upstream)
        assert record == next(r for r in rows if str(r["_id"]) == upstream)
    # The active snapshot answers as before.
    assert len(PsqlRepository(cdb).catalog_candidate_variant_page(new["id"], limit=100)) == len(rows)
    assert json.loads(_rpc_as_service(cdb, f"select public.catalog_register_superseded_snapshots('{new['key']}')")) == []
    assert _compact(cdb, old["key"])["status"] == "unchanged"


def test_maintenance_blockers_and_the_mapper_gate(cdb):
    blockers = json.loads(_rpc_as_service(cdb, "select public.catalog_register_maintenance_blockers()"))
    assert blockers["live_runs"] == _scalar(cdb, "select count(*) from public.runs where status not in "
                                                 "('completed','partial_success','failed','cancelled','timed_out',"
                                                 "'budget_exhausted')")
    _user, _project, conversation = _ws_world(cdb)
    _wsp_capture_run(cdb, conversation)
    assert json.loads(_rpc_as_service(cdb, "select public.catalog_register_maintenance_blockers()"))["live_runs"] \
        == blockers["live_runs"] + 1
    assert cdb.psql("select public.catalog_register_compaction_mapper_mismatches()") == "0"
    # A compacted snapshot read through another mapper version: the deployed gate's finding.
    probe = _snapshot(cdb, _world(cdb), [dict(r, _id=r["_id"] + 650000) for r in fixture_rows()[:2]], "mapper",
                      marque="ממפה")
    cdb.psql("insert into public.catalog_register_snapshot_compactions (snapshot_id, snapshot_key, readers, "
             f"mapper_version, raw_rows, kept_rows, bytes_before, bytes_after) values ('{probe['id']}', "
             f"'{probe['key']}', 'variants', 'gov.wltp.variant-mapper.0', 2, 2, 1, 1)")
    assert cdb.psql("select public.catalog_register_compaction_mapper_mismatches()") == "1"
    cdb.psql("alter table public.catalog_register_snapshot_compactions disable trigger "
             "catalog_register_snapshot_compactions_append_only; "
             f"delete from public.catalog_register_snapshot_compactions where snapshot_id = '{probe['id']}'; "
             "alter table public.catalog_register_snapshot_compactions enable trigger "
             "catalog_register_snapshot_compactions_append_only")


# -- 4. privileges ----------------------------------------------------------------------

def test_the_read_only_role_reads_what_it_did_and_writes_nothing(golden, cdb):
    snapshot = golden["snapshot"]
    for role in (RELEASE_RO, RO_ROLE):
        cdb.psql(f"set role {role}; select count(*) from public.catalog_register_snapshot_compactions; "
                 f"select public.catalog_register_prunable_list(); "
                 f"select public.catalog_register_measured_bytes('{snapshot['id']}'); "
                 f"select count(*) from public.catalog_raw_records; "
                 f"select count(*) from public.catalog_candidate_variants; reset role")
        # The candidates as every reader reads them, and the maintenance reads.
        assert _identity(cdb, snapshot, "catalog_candidate_variants_resolved") == golden["identity"]
        cdb.psql(f"set role {role}; select count(manufacturer) from public.catalog_candidate_variants_resolved; "
                 "select public.catalog_register_maintenance_blockers(); "
                 "select public.catalog_register_compaction_mapper_mismatches(); reset role")
        with pytest.raises(AssertionError, match="permission denied"):
            cdb.psql(f"set role {role}; select public.compact_register_snapshot('{snapshot['key']}', false)")
    for role in ("anon", "authenticated"):
        with pytest.raises(AssertionError, match="permission denied"):
            cdb.psql(f"set role {role}; select public.compact_register_snapshot('{snapshot['key']}', false)")
    for role in ("anon", "authenticated"):
        with pytest.raises(AssertionError, match="permission denied"):
            cdb.psql(f"set role {role}; select count(*) from public.catalog_candidate_variants_resolved")
    assert cdb.psql("select prosecdef::text || '|' || array_to_string(proconfig, ',') from pg_proc "
                    "where proname = 'compact_register_snapshot'") == "true|search_path=pg_catalog"
    # MAINTAIN (the operator's VACUUM FULL) exists from PostgreSQL 17: granted there to the release
    # read-only role on exactly the two compacted tables; an older server has no such privilege.
    if int(cdb.psql("select current_setting('server_version_num')")) >= 170000:
        for table, granted in (("catalog_raw_records", "t"), ("catalog_candidate_variants", "t"),
                               ("catalog_variants", "f"), ("runs", "f")):
            assert cdb.psql(f"select has_table_privilege('{RELEASE_RO}', 'public.{table}', 'MAINTAIN')") == granted
        assert cdb.psql(f"select string_agg(c.oid::regclass::text, ',' order by 1) from pg_class c "
                        f"join pg_namespace n on n.oid = c.relnamespace where c.relkind in ('r', 'p', 'm') "
                        f"and n.nspname = 'public' and has_table_privilege('{RELEASE_RO}', c.oid, 'MAINTAIN')") \
            == "catalog_candidate_variants,catalog_raw_records"
    # Neither read-only role can write anywhere, whatever this migration granted.
    for role in (RELEASE_RO, RO_ROLE):
        assert cdb.psql("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                        "where c.relkind in ('r', 'p', 'v', 'm', 'f') "
                        "and n.nspname not in ('pg_catalog', 'information_schema') and n.nspname !~ '^pg_toast' "
                        f"and (has_table_privilege('{role}', c.oid, 'INSERT') "
                        f"or has_table_privilege('{role}', c.oid, 'UPDATE') "
                        f"or has_table_privilege('{role}', c.oid, 'DELETE') "
                        f"or has_table_privilege('{role}', c.oid, 'TRUNCATE'))") == "0"
    text = COMPACTION_MIGRATION.read_text()
    assert "current_setting('server_version_num')::integer >= 170000" in text
    assert "grant maintain on table public.catalog_raw_records, public.catalog_candidate_variants to %I" in text


def test_the_migration_is_rerun_safe(cdb):
    cdb.psql(file=COMPACTION_MIGRATION)
    assert cdb.psql("select is_nullable from information_schema.columns where table_name = 'catalog_raw_records' "
                    "and column_name = 'payload'") == "YES"
    # The view, and the three identity indexes still partial, after the second run.
    assert cdb.psql("select count(*) from pg_views where schemaname = 'public' "
                    "and viewname = 'catalog_candidate_variants_resolved'") == "1"
    assert cdb.psql("select string_agg(indexname, ',' order by indexname) from pg_indexes "
                    "where schemaname = 'public' and tablename = 'catalog_candidate_variants' "
                    "and indexdef like '%WHERE (manufacturer IS NOT NULL)%'") == (
        "catalog_candidate_variants_identity_idx,catalog_candidate_variants_natural_uidx,"
        "catalog_candidate_variants_snapshot_identity_idx")


# -- 5. bytes per register row after compaction (the capacity default) ------------------

#: The two superseded Toyota snapshots' referenced rows (production 2026-09-30:
#: 10 + 12 queue items), kept as skeletons once they are compacted.
TOYOTA_SUPERSEDED_KEPT_ROWS = 22
#: The share of the previous capture of every tozar a plan still references
#: after a refresh (its queue items), planned generously: the skeletons it keeps.
REFERENCED_SHARE = 0.10
#: The candidate-slimming target this PR is held to (measured after VACUUM
#: FULL), and the byte-regression bound on it.
TARGET_BYTES_PER_ROW, REGRESSION = 2_890, 1.15


def test_bytes_per_register_row_after_compaction_and_the_projections(cdb, capsys):
    """5,000 rows derived from the committed fixtures, captured, built,
    archived and compacted; every table's size (table + TOAST + indexes) after
    the heap is rewritten, as `vacuum full` leaves it. Then ONE refresh of the
    tozar (a changed field: the same variants, new content), built and
    compacted, the first capture superseded and kept as its referenced rows:
    the steady state, two snapshots per tozar."""
    marque, count = "מדידה-דחיסה", 5000
    _settle_capture_runs(cdb)
    base = fixture_rows()
    rows = []
    for index in range(count):
        row = copy.deepcopy(base[index % len(base)])
        row.update(_id=2_000_000 + index, tozar=marque, kinuy_mishari=f"{row['kinuy_mishari']}-{index % 40}",
                   degem_cd=row["degem_cd"] + index)
        rows.append(row)
    tables = ("catalog_raw_records", "catalog_candidate_variants", "catalog_variants", "catalog_variant_coverage")

    def sizes() -> dict[str, int]:
        for table in tables:
            cdb.psql(f"vacuum full analyze public.{table}")
        return {t: _scalar(cdb, f"select pg_total_relation_size('public.{t}')") for t in tables}

    def captured(capture_rows: list[dict]) -> dict:
        snapshot = _bulk_snapshot(cdb, capture_rows, marque)
        snapshot.update(key=cdb.psql("select snapshot_key from public.catalog_source_snapshots "
                                     f"where id='{snapshot['id']}'"))
        for first in range(0, count, mapper.BUILD_BATCH_ROWS):
            _build(cdb, snapshot, capture_rows[first:first + mapper.BUILD_BATCH_ROWS])
        run = cdb.psql(f"select created_by_run_id from public.catalog_source_snapshots where id='{snapshot['id']}'")
        cdb.psql("insert into public.catalog_register_snapshot_archives (snapshot_id, snapshot_key, gcs_uri, "
                 f"byte_size, sha256, line_count, recorded_by_run_id) values ('{snapshot['id']}', '{snapshot['key']}', "
                 f"'gs://milo-test-archive/register/{RESOURCE}/{'2' * 16}/{snapshot['key']}.jsonl.gz', 10, "
                 f"'{'a' * 64}', {count}, '{run}'); update public.runs set status = 'completed' where id = '{run}'")
        return snapshot

    start = sizes()
    first = captured(rows)
    built = sizes()
    assert _compact(cdb, first["key"])["status"] == "compacted"
    compacted = sizes()
    before = {t: (built[t] - start[t]) / count for t in tables}
    after = {t: (compacted[t] - start[t]) / count for t in tables}
    per_row = sum(after.values())
    skeleton = after["catalog_raw_records"] + after["catalog_candidate_variants"]
    # One refresh: the second capture active and compacted, the first kept as skeletons (none referenced here).
    second = captured([dict(r, koah_sus=(r.get("koah_sus") or 0) + 1) for r in rows])
    assert _compact(cdb, second["key"])["mode"] == "active"
    superseded = _compact(cdb, first["key"])
    assert (superseded["mode"], superseded["kept_rows"]) == ("superseded", 0)
    steady_row = sum((v - start[t]) for t, v in sizes().items()) / count

    full = (register_config.NON_REGISTER_BASE_BYTES + register_config.FULL_REGISTER_ROWS * per_row
            + TOYOTA_SUPERSEDED_KEPT_ROWS * skeleton)
    steady = (register_config.NON_REGISTER_BASE_BYTES + register_config.FULL_REGISTER_ROWS * steady_row
              + REFERENCED_SHARE * register_config.FULL_REGISTER_ROWS * skeleton)
    with capsys.disabled():
        print("\nPR-L2 bytes per register row (table+TOAST+indexes), built -> compacted: "
              + ", ".join(f"{t.removeprefix('catalog_')} {before[t]:.0f} -> {after[t]:.0f}" for t in tables)
              + f"; total {sum(before.values()):.0f} -> {per_row:.0f} (default {register_config.DEFAULT_BYTES_PER_ROW},"
              f" bound {TARGET_BYTES_PER_ROW * REGRESSION:.0f}). Full register: base "
              f"{register_config.NON_REGISTER_BASE_BYTES / 1e6:.1f} MB + {register_config.FULL_REGISTER_ROWS:,} rows "
              f"x {per_row:.0f} B + {TOYOTA_SUPERSEDED_KEPT_ROWS} superseded Toyota skeletons = {full / 1e6:.1f} MB"
              f" (<= 360 MB). After one refresh: {steady_row:.0f} B per tozar row for both snapshots"
              f" + {REFERENCED_SHARE:.0%} of the previous capture referenced ({skeleton:.0f} B each) ="
              f" {steady / 1e6:.1f} MB (<= 400 MB)")
    assert after["catalog_raw_records"] < before["catalog_raw_records"] / 2
    assert after["catalog_candidate_variants"] < before["catalog_candidate_variants"] / 2
    assert per_row <= TARGET_BYTES_PER_ROW * REGRESSION
    assert per_row <= register_config.DEFAULT_BYTES_PER_ROW
    assert full <= 360_000_000
    assert steady <= 400_000_000
