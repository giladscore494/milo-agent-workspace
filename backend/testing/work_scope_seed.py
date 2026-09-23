"""Test-only: a PREPARED Mapping Plan and its batch runs, in memory.

Why this exists
---------------

A catalog-reading Swarm V2 run executes exactly one Mapping Plan batch (scoped
catalog PR3): the worker refuses a run the database binds to no batch. Offline
tests of the real worker, the API and the isolated E2E stack therefore need a
plan that has actually been PREPARED -- a scoped Government snapshot, a
durable queue and its batches -- and a run born bound to one of them.

What is real here, and what is not
----------------------------------

*   The Government rows are REAL: committed R5 capture rows, re-served as the
    `filters={"tozar": "<marque>"}` page a scoped capture reads, and landed
    through `GovernmentCatalogIngestor` under an authentic operator-capture
    lease with `FixtureTransport` standing in for the network. No socket is
    opened and nothing reaches `data.gov.il`.
*   The plan, its preparation and every batch run are written by the SAME
    repository methods production calls: `create_work_scope`,
    `prepare_work_scope_queue` (lease-guarded, operator capture only) and
    `create_work_scope_batch_run`. The operator capture run is finalized
    through the canonical finalizer afterwards, exactly as the capture job
    finalizes it, so it holds no concurrency slot.

Never deploy this module. It is imported by tests and by
`backend/testing/e2e_app.py` only (where `prepare_plan_head` stands in for the
operator capture job's `--prepare-work-scope`), and writes to nothing but an
in-memory repository.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import CaptureScope
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.ingest import GovernmentCatalogIngestor
from backend.catalog.scope import batches as work_scope_batches
from backend.catalog.scope import contract as wsc
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.finalization import RunFinalizer, TerminalClaim
from backend.run_identity import RunIdentity
from backend.testing import government_capture as capture_fixtures
from backend.testing.government_capture import FixtureTransport
from backend.testing.memory_repository import MemoryRepository

#: The one verified register spelling (`backend/catalog/scope/directory.py`).
TOYOTA_REGISTER_MARQUE = "טויוטה"
#: The page size a scoped capture reads at (the operator capture's own).
SCOPED_PAGE_LIMIT = src.MAX_PAGE_LIMIT
#: The marker `create_message_and_run` requires of an operator capture run.
_CAPTURE_OPERATION = "catalog.government.capture"


def committed_records(count: int | None = None) -> list[dict[str, Any]]:
    """The committed R5 register rows, in capture order. Every one is Toyota."""
    rows: list[dict[str, Any]] = []
    for offset in sorted(capture_fixtures.PAGE_SOURCE_KEYS):
        rows.extend(dict(row) for row in capture_fixtures.page_document(offset)["result"]["records"])
    return rows if count is None else rows[:count]


def scoped_page(records: Sequence[Mapping[str, Any]], *,
                marque: str = TOYOTA_REGISTER_MARQUE) -> bytes:
    """A committed page, re-shaped as the `filters={"tozar": marque}` page."""
    document = capture_fixtures.page_document(0)
    result = document["result"]
    result.pop("q", None)
    result["filters"] = {"tozar": marque}
    result["limit"], result["offset"] = SCOPED_PAGE_LIMIT, 0
    result["total"], result["total_was_estimated"] = len(records), False
    result["records"] = [dict(row) for row in records]
    return capture_fixtures.encode(document)


def _capture_lease(repository: MemoryRepository, conversation_id: str,
                   user_id: str) -> WorkerLease:
    """A claimed OPERATOR CAPTURE run in the plan's conversation."""
    run_id = uuid4()
    identity = RunIdentity.bind(run_id, "operator_capture")
    run = repository.create_message_and_run(
        UUID(conversation_id), "operator catalog capture run; not executed by a model worker",
        {"milo_operation": _CAPTURE_OPERATION}, UUID(user_id), None, "fp-work-scope-seed",
        run_id=run_id, run_identity=identity.as_record())["run"]
    worker = f"work-scope-seed-{str(run['id'])[:8]}"
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def seed_prepared_plan(repository: MemoryRepository, *, user_id: str, conversation_id: str,
                       units: Sequence[str] = ("toyota", "lexus"),
                       model_year_from: int | None = None, model_year_to: int | None = None,
                       max_items: int = 25, batch_size: int = 10,
                       records: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """A plan at revision 1, prepared into a durable queue and batches.

    Toyota -- the one verified register marque -- is captured from committed
    rows; every other unit is `register_unverified`, exactly as the capture job
    records it. Returns the plan id, its digest and the preparation summary.
    """
    scope = wsc.scope_from_fields({"units": list(units), "model_year_from": model_year_from,
                                   "model_year_to": model_year_to, "max_items": max_items,
                                   "batch_size": batch_size})
    plan = repository.create_work_scope(
        UUID(conversation_id), UUID(user_id),
        {"scope_text": scope.canonical_text(), "input_kind": "edit", "instruction": None,
         "notes": []})["work_scope"]
    return prepare_plan_head(repository, plan["id"], user_id=user_id, records=records)


def prepare_plan_head(repository: MemoryRepository, work_scope_id: Any, *,
                      user_id: str | None = None,
                      records: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Prepare the CURRENT head revision of an existing plan, as the capture job does.

    Under a claimed operator-capture lease in the plan's own conversation:
    Toyota is captured as a scoped snapshot from committed rows (a replay when
    the same rows were landed before), every other unit is recorded
    `register_unverified`, the database decides the queue, and the capture run
    is finalized. Returns the plan id, head revision, digest and summary.
    """
    plan = repository.get_work_scope(UUID(str(work_scope_id)))
    if plan is None:
        raise ValueError("no such plan")
    head = repository.get_work_scope_revision(UUID(plan["id"]), plan["head_revision"])
    units = list(wsc.scope_from_text(head["scope_text"]).fields()["units"])
    user = user_id or plan["created_by"]
    lease = _capture_lease(repository, plan["conversation_id"], user)
    lease_kwargs = {"worker_id": lease.worker_id, "attempt": lease.attempt,
                    "lease_token": lease.lease_token}
    snapshot = None
    if "toyota" in units:
        rows = committed_records(40) if records is None else list(records)
        client = DataGovClient(FixtureTransport(bodies={0: scoped_page(rows)}),
                               page_limit=SCOPED_PAGE_LIMIT, sleep_fn=lambda _seconds: None)
        report = GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
            src.WLTP_RESOURCE_ID,
            capture_scope=CaptureScope.for_register_marque(TOYOTA_REGISTER_MARQUE))
        snapshot = repository.find_active_catalog_snapshot(
            src.GOVERNMENT_SOURCE_FAMILY, src.WLTP_RESOURCE_ID, report.snapshot_key)
    submitted = []
    for priority, unit in enumerate(units, start=1):
        if unit == "toyota" and snapshot is not None:
            submitted.append({"unit_key": unit, "priority": priority, "state": "captured",
                              "register_marque": TOYOTA_REGISTER_MARQUE,
                              "snapshot_id": snapshot["id"], "reason_code": None})
        else:
            submitted.append({"unit_key": unit, "priority": priority,
                              "state": "register_unverified", "register_marque": None,
                              "snapshot_id": None, "reason_code": None})
    summary = repository.prepare_work_scope_queue(
        lease.run_id, {"work_scope_id": plan["id"], "revision": plan["head_revision"],
                       "scope_digest": plan["head_digest"], "units": submitted}, **lease_kwargs)
    # The capture job finalizes its run once the preparation is written, so the
    # run is terminal and holds no concurrency slot.
    RunFinalizer(repository, lease.run_id, "operator_capture", lease_kwargs).finalize(
        TerminalClaim.control_success("operator_capture", {"prepared": True}))
    return {"work_scope_id": plan["id"], "revision": plan["head_revision"],
            "digest": plan["head_digest"], "conversation_id": plan["conversation_id"],
            "user_id": user,
            "snapshot_key": snapshot["snapshot_key"] if snapshot is not None else None,
            "batches": list(summary["batches"]), "preparation": summary["preparation"]}


def start_batch_run(repository: MemoryRepository, plan: Mapping[str, Any], *,
                    batch_id: str | None = None, idempotency_key: str | None = None,
                    max_user_active: int | None = None,
                    max_project_active: int | None = None) -> dict[str, Any]:
    """Create ONE batch run the way the API does (without launching it).

    `batch_id` defaults to the plan's next batch, read from the durable
    progress. Returns the repository's answer: the run, its binding, created.
    """
    user = UUID(str(plan["user_id"]))
    scope_id = UUID(str(plan["work_scope_id"]))
    if batch_id is None:
        progress = repository.work_scope_progress(scope_id)
        batch_id = progress["preparation"]["next"]["batch_id"]
    request = work_scope_batches.batch_request(
        repository, user, scope_id, expected_revision=int(plan["revision"]),
        expected_digest=str(plan["digest"]), batch_id=UUID(str(batch_id)))
    run_id = uuid4()
    identity = RunIdentity.bind(run_id, work_scope_batches.BATCH_RUN_WORKFLOW)
    return work_scope_batches.create_batch_run(
        repository, user, scope_id, expected_revision=int(plan["revision"]),
        expected_digest=str(plan["digest"]), batch_id=UUID(str(batch_id)),
        idempotency_key=idempotency_key or f"seed-batch-{uuid4().hex}",
        content=request["content"], fingerprint=request["fingerprint"], run_id=run_id,
        run_identity=identity.as_record(), max_user_active=max_user_active,
        max_project_active=max_project_active)


__all__ = ["SCOPED_PAGE_LIMIT", "TOYOTA_REGISTER_MARQUE", "committed_records",
           "prepare_plan_head", "scoped_page", "seed_prepared_plan", "start_batch_run"]
