"""Test-only durable catalog state for the isolated E2E stack.

Why this exists
---------------

The CODE-3 review surface reads DURABLE rows. An E2E run against an empty
repository can prove the routes are reachable and that the unavailable state
renders, and nothing about how a real page looks. This lands a small, real
catalog so the E2E suite exercises both.

What is real here, and what is not
----------------------------------

*   The Government snapshot is REAL: the committed R5 capture fixtures, landed
    through `GovernmentCatalogIngestor` under an authentic `WorkerLease`, with
    `FixtureTransport` standing in for the network. No socket is opened, no
    request reaches `data.gov.il`, and the snapshot, its raw records and its
    candidates are written by the same guarded RPCs production uses.
*   The `ready_for_review` transition is REAL: `record_catalog_candidate`, the
    same guarded write `CatalogPromotionPipeline._review` performs.
*   The canonical variants are SEEDED, not promoted. A genuine promotion needs
    a verified evidence chain per field -- a plan, a tool call, an evidence
    mapper, verdicts and links -- which `tests/test_catalog_pr3_swarm_promotion.py`
    exercises properly and which would put the whole Swarm V2 engine inside a
    fixture. The rows written here carry exactly the columns
    `promote_catalog_variant` writes, so the READ under test reads the shape it
    reads in production.

This module is test infrastructure and is imported only by
`backend/testing/e2e_app.py`. Never deploy it. Nothing here runs unless that
module is imported, and it performs no network, no model call and no write to
anything but an in-memory repository.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from backend.catalog.government import source as src
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.ingest import GovernmentCatalogIngestor
from backend.engines.swarm_v2.evidence import WorkerLease
from backend.run_identity import RunIdentity
from backend.testing.government_capture import (FixtureTransport, PINNED_PAGE_LIMIT,
                                                PINNED_QUERY)
from backend.testing.memory_repository import MemoryRepository

#: How many of the captured candidates are moved to `ready_for_review`. Enough
#: to fill more than one page of the review surface (`DEFAULT_REVIEW_PAGE_ITEMS`
#: is 25) so E2E can page, and far short of the whole capture so the page's
#: status filter is visibly doing something.
SEEDED_REVIEW_CANDIDATES = 30

#: How many canonical variants are seeded. Also more than one page.
SEEDED_CANONICAL_VARIANTS = 28

_SEED_TIME = "2026-09-16T12:00:00+00:00"


def _leased_run(repository: MemoryRepository, user_id: str, project_id: str,
                worker: str) -> WorkerLease:
    """One claimed run to write the capture under, exactly as a worker holds it."""
    conversation = repository.create_conversation(project_id, "catalog seed", user_id)
    # The atomic V3 creator is the only run writer, in tests as in production:
    # the identity is bound from the TRUSTED project relation, never from a
    # payload, and the run id is chosen first so the identity can name it.
    run_id = uuid4()
    identity = RunIdentity.bind(
        run_id, (repository.projects.get(str(project_id)) or {}).get("workflow_key") or "")
    run = repository.create_message_and_run(
        conversation["id"], "seed", {}, requested_by=user_id, idempotency_key=None,
        request_fingerprint="fp-catalog-seed", run_id=run_id,
        run_identity=identity.as_record())["run"]
    claimed = repository.claim_run(run["id"], worker)
    return WorkerLease(claimed["id"], worker, int(claimed["attempt"]), claimed["lease_token"])


def land_pinned_government_snapshot(repository: MemoryRepository, *, user_id: str,
                                    project_id: str,
                                    worker: str = "offline-catalog-capture") -> str:
    """Land the committed Government capture as ONE usable active snapshot.

    The operator-capture path in miniature, over the pinned fixture transport:
    a leased run, the real `GovernmentCatalogIngestor`, the guarded writes and
    activation. It is what an offline test uses when a run must find a usable
    snapshot -- the product worker itself never imports one. Returns the
    snapshot key.
    """
    lease = _leased_run(repository, user_id, project_id, worker)
    client = DataGovClient(FixtureTransport(), page_limit=PINNED_PAGE_LIMIT,
                           sleep_fn=lambda _seconds: None)
    report = GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
        src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    return str(report.snapshot_key)


def seed_catalog_review_state(repository: MemoryRepository, *, user_id: str,
                              project_id: str) -> dict[str, Any]:
    """Land one active Government snapshot and a small canonical catalog.

    Returns a summary so the caller can log what it seeded. Failing here must
    never take the E2E backend down with it: an empty catalog is a state the
    review surface is required to present honestly, so a seed that could not
    run leaves a working stack that exercises exactly that path.
    """
    lease = _leased_run(repository, user_id, project_id, "e2e-catalog-seed")
    client = DataGovClient(FixtureTransport(), page_limit=PINNED_PAGE_LIMIT,
                           sleep_fn=lambda _seconds: None)
    report = GovernmentCatalogIngestor(repository, lease, client=client).ingest_resource(
        src.WLTP_RESOURCE_ID, query=dict(PINNED_QUERY))
    ready = _mark_ready_for_review(repository, lease)
    variants = _seed_canonical(repository)
    return {"snapshot_key": report.snapshot_key, "ready_for_review": ready,
            "canonical_variants": variants}


def _mark_ready_for_review(repository: MemoryRepository, lease: WorkerLease) -> int:
    """Move a bounded slice of the capture to `ready_for_review`.

    Through `record_catalog_candidate` -- the guarded RPC -- so the durable
    status the review surface filters on was written the way production writes
    it. Ordered by `candidate_key` so which candidates are seeded is
    deterministic across runs.
    """
    lease_kwargs = {"worker_id": lease.worker_id, "attempt": lease.attempt,
                    "lease_token": lease.lease_token}
    records = {row["id"]: row for row in repository.catalog_raw_records.values()}
    snapshots = {row["id"]: row for row in repository.catalog_snapshots.values()}
    candidates = sorted(repository.catalog_candidates.values(),
                        key=lambda row: str(row["candidate_key"]))
    marked = 0
    for candidate in candidates[:SEEDED_REVIEW_CANDIDATES]:
        record = records.get(candidate["raw_record_id"])
        if record is None:
            continue
        repository.record_catalog_candidate(lease.run_id, {
            "snapshot_key": snapshots[candidate["snapshot_id"]]["snapshot_key"],
            "record_key": record["record_key"],
            "snapshot_id": candidate["snapshot_id"],
            "raw_record_id": candidate["raw_record_id"],
            "manufacturer": candidate["manufacturer"],
            "commercial_model": candidate["commercial_model"],
            "model_year_start": candidate["model_year_start"],
            "model_year_end": candidate["model_year_end"],
            "official_model_code": candidate["official_model_code"],
            "trim": candidate["trim"],
            "identity_dimensions": candidate["identity_dimensions"],
            "status": "ready_for_review"}, **lease_kwargs)
        marked += 1
    return marked


def _seed_canonical(repository: MemoryRepository) -> int:
    """Canonical variants in the shape a promotion writes, seeded directly.

    See the module docstring for why these are seeded rather than promoted. The
    identities are taken from the capture's own candidates, so the rows name
    real vehicles from the pinned register rather than invented ones.
    """
    candidates = sorted(repository.catalog_candidates.values(),
                        key=lambda row: str(row["candidate_key"]))
    models: dict[tuple[str, str], str] = {}
    seeded = 0
    for index, candidate in enumerate(candidates):
        if seeded >= SEEDED_CANONICAL_VARIANTS:
            break
        identity = (str(candidate["manufacturer"]), str(candidate["commercial_model"]))
        model_id = models.get(identity)
        if model_id is None:
            model_id = str(uuid4())
            models[identity] = model_id
            repository.catalog_models.append({
                "id": model_id, "canonical_key": f"cm1.{len(models):032d}",
                "manufacturer": identity[0], "commercial_model": identity[1],
                "created_at": _SEED_TIME})
        variant_id = str(uuid4())
        repository.catalog_model_variants.append({
            "id": variant_id, "model_id": model_id,
            "canonical_key": f"cv1.{index:032d}",
            "promoted_from_candidate_id": candidate["id"],
            "promoted_from_verdict_id": None, "created_at": _SEED_TIME})
        fields: list[tuple[str, Any]] = [
            ("model_year_start", candidate["model_year_start"]),
            ("model_year_end", candidate["model_year_end"]),
        ]
        if candidate.get("official_model_code"):
            fields.append(("official_model_code", candidate["official_model_code"]))
        if candidate.get("trim"):
            fields.append(("trim", candidate["trim"]))
        for name, value in (candidate.get("identity_dimensions") or {}).items():
            fields.append((f"identity_dimensions.{name}", value))
        for field_key, value in fields:
            repository.catalog_canonical_field_provenance.append({
                "id": str(uuid4()), "model_id": model_id, "variant_id": variant_id,
                "field_key": field_key, "field_value": value, "revision": 1,
                "created_at": _SEED_TIME})
        seeded += 1
    return seeded


__all__ = ["SEEDED_CANONICAL_VARIANTS", "SEEDED_REVIEW_CANDIDATES",
           "seed_catalog_review_state"]
