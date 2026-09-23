"""Government preparation: the stage between the lease and the first paid call.

A website-triggered catalog-processing run (a ``swarm_v2`` run in a deployment
whose posture allows the Government read) reaches the provider in this order,
and in no other::

    worker claims the run (run + worker + attempt + lease exist)
      -> resolve and PIN one usable immutable Government snapshot
      -> select a deterministic, bounded, resumable work queue from the
         PERSISTED candidate state of that snapshot
      -> only then construct or reach any paid provider path

Everything here is a READ of durable state plus one lease-guarded checkpoint.
No upstream transport exists in this module and none is constructed by the
product worker: the operator capture (`backend/catalog/operator_capture.py`)
is the only importer, and a run that finds no usable snapshot is REFUSED
through the canonical finalizer before a single paid request rather than
fetching one for itself.

Where the pin and the queue live
--------------------------------
In ``run_checkpoints`` -- the existing durable, run-scoped, lease-guarded
authority -- under ``artifacts.government``. Chosen over a new event type on
purpose: registering one would change the event-registry fingerprint bound
into every new RunIdentity, which is a release-level change, and nothing about
a pinned snapshot needs a vocabulary of its own.

The Swarm V2 engine resumes from ``latest_checkpoint`` and expects
``artifacts.swarm_state`` there, so the preparation record must never displace
the engine's own state. Two rules keep them apart:

* the preparation checkpoint (phase ``government_prepared``) is written ONCE,
  before any engine checkpoint exists, and the worker hands the engine NO
  checkpoint when the latest one is a preparation record;
* the worker copies ``artifacts.government`` forward into every later engine
  checkpoint, so the latest checkpoint of the run always carries the pin and
  the queue, whichever phase wrote it.

A resumed attempt therefore re-reads the SAME snapshot by its exact key
(never "the newest usable one", which may have moved) and the SAME queue in
the SAME order.

A BATCH-BOUND run (scoped catalog PR2)
--------------------------------------
When the durable binding table (`catalog_work_scope_batch_runs`, read through
`work_scope_batch_for_run`) binds this run to a Mapping Plan batch, the run is
handed EXACTLY that batch: its one scoped snapshot, pinned by key, and its
items in batch order -- never "the newest usable snapshot" and never "the
first N candidates". The binding is the authority, read from the database; a
browser, the run input and the model can supply none of it. The record then
carries the batch's identity, and a resume refuses unless the binding still
names the same batch. A run bound to nothing behaves exactly as before. Per-item progress is not stored at all: it is reconstructed
from durable state -- the run's own verified evidence rows
(`catalog_run_pending_promotions`) and its durable promotion events -- so a
crash between two writes cannot leave the queue claiming progress the
database does not hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from backend.catalog.contracts import MAX_PROMOTIONS_PER_RUN
from backend.catalog.government import source as src
from backend.catalog.government.projection import (GovernmentProjectionError,
                                                   resolve_active_snapshot)
from backend.catalog.government.query import TOTAL_COUNT_FIELD, is_count_row
from backend.errors import AppError
from backend.runtime import CancellationRequested

#: The checkpoint phase the preparation record is written under. The worker
#: never hands a checkpoint of this phase to an engine.
PREPARATION_PHASE = "government_prepared"
#: Where the record lives inside a checkpoint's ``artifacts``.
ARTIFACT_KEY = "government"
#: The persisted record's own schema, so a later reader can refuse a shape it
#: does not know instead of guessing.
ARTIFACT_SCHEMA = "milo-government-preparation/1"
#: Where a batch-bound run's record names its batch (scoped catalog PR2).
BATCH_ARTIFACT_KEY = "work_scope_batch"
#: The batch identity a record carries, and nothing else.
BATCH_IDENTITY_FIELDS = ("batch_id", "work_scope_id", "revision", "scope_digest",
                         "batch_number", "attempt")
#: The most candidates one run is handed. The same bound the promotion path
#: works under, so a run is never asked to research more than it could ever
#: promote.
GOVERNMENT_WORK_QUEUE_LIMIT = MAX_PROMOTIONS_PER_RUN
#: The persisted candidate status the queue is selected from: an UNREAD
#: reading of the register. `ambiguous` is a first-class answer, `rejected` a
#: decision, and `ready_for_review` already holds evidence.
QUEUED_CANDIDATE_STATUS = "candidate"
#: The registered Government operation whose evidence counts as progress.
#: Same constant the promotion pipeline matches on, imported lazily to keep
#: this module free of the pipeline's dependencies.
_PROMOTABLE_OPERATION: str | None = None

#: Per-item progress states, all DERIVED from durable state.
PROGRESS_PENDING = "pending"        # no durable trace of work on this item yet
PROGRESS_EVIDENCED = "evidenced"    # this run holds verified evidence for it
PROGRESS_PROMOTED = "promoted"      # this run durably promoted it
PROGRESS_STATES = (PROGRESS_PENDING, PROGRESS_EVIDENCED, PROGRESS_PROMOTED)

#: The static refusal codes this stage can produce. Each names a condition,
#: never a snapshot, a row or a SQL value.
PREPARATION_REASONS: Mapping[str, str] = {
    "GOVERNMENT_SNAPSHOT_UNAVAILABLE":
        "no usable immutable Government snapshot is available to this run",
    "GOVERNMENT_QUEUE_UNAVAILABLE":
        "the Government work queue could not be read from durable candidate state",
    "GOVERNMENT_PREPARATION_RECORD_INVALID":
        "the run's persisted Government preparation record is not readable",
    "GOVERNMENT_BATCH_INVALID":
        "the Mapping Plan batch bound to this run is not readable as one exact batch",
}


class GovernmentPreparationError(Exception):
    """Preparation refused. Carries ONLY static, code-owned reasons."""

    def __init__(self, code: str, *, reason_code: str = "") -> None:
        if code not in PREPARATION_REASONS:
            raise ValueError("government preparation code must come from the static allowlist")
        self.code = code
        self.safe_message = PREPARATION_REASONS[code]
        #: The projection's own static reason (e.g. GOV_PROJECTION_NO_ACTIVE_SNAPSHOT),
        #: when one exists. Static by construction: it is an allowlisted code.
        self.reason_code = reason_code
        super().__init__(self.safe_message)


@dataclass(frozen=True)
class GovernmentWorkItem:
    """One candidate variant the run is asked to work on.

    Identity text and the derived candidate key ONLY, copied from the persisted
    candidate row. Nothing here is model text and nothing was chosen by a
    browser.
    """

    candidate_key: str
    candidate_id: str
    manufacturer: str
    commercial_model: str
    model_year_start: int | None
    model_year_end: int | None
    official_model_code: str | None
    trim: str | None

    def as_record(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "candidate_id": self.candidate_id,
            "manufacturer": self.manufacturer,
            "commercial_model": self.commercial_model,
            "model_year_start": self.model_year_start,
            "model_year_end": self.model_year_end,
            "official_model_code": self.official_model_code,
            "trim": self.trim,
        }

    @classmethod
    def from_record(cls, record: Any) -> "GovernmentWorkItem":
        if not isinstance(record, Mapping):
            raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
        try:
            key = str(record["candidate_key"])
            candidate_id = str(record["candidate_id"])
            manufacturer = str(record["manufacturer"])
            model = str(record["commercial_model"])
        except (KeyError, TypeError):
            raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID") from None
        if not key or not candidate_id or not manufacturer or not model:
            raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
        return cls(
            candidate_key=key, candidate_id=candidate_id, manufacturer=manufacturer,
            commercial_model=model,
            model_year_start=_optional_int(record.get("model_year_start")),
            model_year_end=_optional_int(record.get("model_year_end")),
            official_model_code=_optional_text(record.get("official_model_code")),
            trim=_optional_text(record.get("trim")),
        )


@dataclass(frozen=True)
class GovernmentPreparation:
    """The pinned snapshot and the deterministic work queue of ONE run."""

    snapshot_key: str
    snapshot_id: str
    resource_id: str
    upstream_version: str
    upstream_version_kind: str
    queue: tuple[GovernmentWorkItem, ...]
    #: The EXACT number of queueable candidates the snapshot holds, from the
    #: same aggregation that returned the page. `bounded` is true when the
    #: queue is a strict prefix of that.
    total_candidates: int
    bounded: bool
    #: True when this preparation was read back from the run's own durable
    #: record rather than selected afresh.
    resumed: bool
    #: The Mapping Plan batch this run executes, when it is batch-bound: its
    #: identity only (ids, revision, digest, number, attempt) -- never its
    #: items, which ARE the queue above.
    work_scope_batch: Mapping[str, Any] | None = None

    def as_artifact(self) -> dict[str, Any]:
        """The persisted shape, stored under ``artifacts.government``."""
        artifact = {
            "schema": ARTIFACT_SCHEMA,
            "snapshot_key": self.snapshot_key,
            "snapshot_id": self.snapshot_id,
            "resource_id": self.resource_id,
            "upstream_version": self.upstream_version,
            "upstream_version_kind": self.upstream_version_kind,
            "queue": [item.as_record() for item in self.queue],
            "total_candidates": int(self.total_candidates),
            "bounded": bool(self.bounded),
        }
        if self.work_scope_batch is not None:
            artifact[BATCH_ARTIFACT_KEY] = dict(self.work_scope_batch)
        return artifact

    def work_context(self, progress: Mapping[str, str]) -> dict[str, Any]:
        """The server-owned work selection handed to the engine.

        It travels through the Commander's existing ``context`` seam -- the
        run input's ``context`` mapping, which the API never writes and the
        worker overwrites -- so the model receives the selection without any
        engine or prompt-format change, and a browser cannot supply one.
        """
        items = []
        for item in self.queue:
            record = item.as_record()
            record["progress"] = progress.get(item.candidate_key, PROGRESS_PENDING)
            items.append(record)
        return {
            "source": "israel_ministry_of_transport_vehicle_register",
            "snapshot_key": self.snapshot_key,
            "resource_id": self.resource_id,
            "upstream_version": self.upstream_version,
            "items": items,
            "total_candidates": int(self.total_candidates),
            "bounded": bool(self.bounded),
            "remaining": sum(1 for item in items if item["progress"] == PROGRESS_PENDING),
        }


def prepared_artifact(checkpoint: Any) -> dict[str, Any] | None:
    """The preparation record a checkpoint carries, or None when it carries none."""
    if not isinstance(checkpoint, Mapping):
        return None
    artifacts = checkpoint.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return None
    record = artifacts.get(ARTIFACT_KEY)
    return dict(record) if isinstance(record, Mapping) else None


def is_preparation_checkpoint(checkpoint: Any) -> bool:
    """Whether a checkpoint is the preparation record itself (no engine state)."""
    return isinstance(checkpoint, Mapping) and checkpoint.get("phase") == PREPARATION_PHASE


def prepare_government_work(repository: Any, *,
                            resource_id: str = src.WLTP_RESOURCE_ID,
                            checkpoint: Mapping[str, Any] | None = None,
                            limit: int = GOVERNMENT_WORK_QUEUE_LIMIT,
                            cancellation_checker: Callable[[], bool] | None = None,
                            run_id: Any = None,
                            ) -> GovernmentPreparation:
    """Resolve the pinned snapshot and the work queue for ONE run.

    With a prior preparation record (a resumed attempt) the SAME snapshot is
    resolved by its exact key and the SAME queue is returned in the SAME order.
    Without one, a run the database binds to a Mapping Plan batch is handed
    exactly that batch; any other run pins the newest USABLE snapshot and
    takes the first ``limit`` unread candidates in the repository's
    deterministic candidate order (codepoint order over identity text, then
    candidate key -- the order both PostgreSQL and the in-memory mirror
    return).

    Refusals are static and total; a repository failure while reading the
    queue -- or the binding -- is a refusal too, never an empty queue.
    """
    _check_cancelled(cancellation_checker)
    batch = _bound_batch(repository, run_id)
    record = prepared_artifact(checkpoint)
    if record is not None:
        return _resume(repository, record, cancellation_checker, batch=batch)
    if batch is not None:
        return _from_batch(repository, batch, cancellation_checker)

    resource_id = src.require_allowed_resource(resource_id)
    try:
        snapshot = resolve_active_snapshot(repository, resource_id=resource_id,
                                           snapshot_key=None, allow_incomplete=False)
    except GovernmentProjectionError as refusal:
        raise GovernmentPreparationError("GOVERNMENT_SNAPSHOT_UNAVAILABLE",
                                         reason_code=refusal.reason_code) from None
    _check_cancelled(cancellation_checker)
    rows, total = _read_queue(repository, snapshot, limit)
    _check_cancelled(cancellation_checker)
    queue = tuple(_item_from_row(row) for row in rows)
    return GovernmentPreparation(
        snapshot_key=str(snapshot["snapshot_key"]), snapshot_id=str(snapshot["id"]),
        resource_id=resource_id,
        upstream_version=str(snapshot.get("upstream_version") or ""),
        upstream_version_kind=str(snapshot.get("upstream_version_kind") or ""),
        queue=queue, total_candidates=total, bounded=total > len(queue), resumed=False)


def government_work_progress(repository: Any, run_id: Any,
                             preparation: GovernmentPreparation) -> dict[str, str]:
    """Per-item progress, RECONSTRUCTED from durable state and events.

    * ``evidenced``: the run holds a verified claim whose evidence resolves to
      the candidate -- the same durable read the promotion pipeline derives its
      work from (`catalog_run_pending_promotions`);
    * ``promoted``: the run durably emitted ``catalog_variant_promoted`` for it;
    * ``pending`` otherwise.

    A repository that offers no pending-promotion read has no catalog schema
    behind it, so nothing can be evidenced there. A read that FAILS propagates
    as the infrastructure error it is: "no progress" is a claim this function
    is in no position to make from an absent answer.
    """
    keys = {item.candidate_key for item in preparation.queue}
    progress = {key: PROGRESS_PENDING for key in keys}
    read = getattr(repository, "catalog_run_pending_promotions", None)
    if callable(read):
        for row in read(run_id, _promotable_operation(), limit=MAX_PROMOTIONS_PER_RUN):
            key = str(row.get("candidate_key") or "")
            if key in keys:
                progress[key] = PROGRESS_EVIDENCED
    list_events = getattr(repository, "list_run_events", None)
    if callable(list_events):
        for event in list_events(run_id):
            if event.get("event_type") != "catalog_variant_promoted":
                continue
            payload = event.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            key = str(payload.get("candidate_key") or "")
            if key in keys and payload.get("promoted") is True:
                progress[key] = PROGRESS_PROMOTED
    return progress


# --- helpers -----------------------------------------------------------------

def _bound_batch(repository: Any, run_id: Any) -> Mapping[str, Any] | None:
    """The batch the database binds this run to, or None when it is unbound.

    A repository with no binding read has no Mapping Plan schema behind it, so
    nothing can be bound there. A read that FAILS is a refusal: "not bound" is
    not something this function may conclude from an absent answer.
    """
    if run_id is None:
        return None
    read = getattr(repository, "work_scope_batch_for_run", None)
    if not callable(read):
        return None
    try:
        bound = read(run_id)
    except AppError:
        raise GovernmentPreparationError("GOVERNMENT_QUEUE_UNAVAILABLE") from None
    if bound is None:
        return None
    if not isinstance(bound, Mapping) or not isinstance(bound.get("batch"), Mapping) \
            or not isinstance(bound.get("binding"), Mapping) \
            or not isinstance(bound.get("items"), Sequence) \
            or isinstance(bound.get("items"), (str, bytes)):
        raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID")
    return bound


def _batch_identity(bound: Mapping[str, Any]) -> dict[str, Any]:
    batch, binding = bound["batch"], bound["binding"]
    try:
        identity = {"batch_id": str(batch["id"]), "work_scope_id": str(batch["work_scope_id"]),
                    "revision": int(batch["revision"]), "scope_digest": str(batch["scope_digest"]),
                    "batch_number": int(batch["batch_number"]),
                    "attempt": int(binding["attempt"])}
    except (KeyError, TypeError, ValueError):
        raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID") from None
    if str(binding.get("batch_id")) != identity["batch_id"]:
        raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID")
    return identity


def _from_batch(repository: Any, bound: Mapping[str, Any],
                cancellation_checker: Callable[[], bool] | None) -> GovernmentPreparation:
    """EXACTLY the bound batch: its one snapshot, pinned, and its items in order."""
    batch = bound["batch"]
    identity = _batch_identity(bound)
    snapshot_key = str(batch.get("snapshot_key") or "")
    if not snapshot_key:
        raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID")
    try:
        snapshot = resolve_active_snapshot(repository, resource_id=src.WLTP_RESOURCE_ID,
                                           snapshot_key=snapshot_key, allow_incomplete=False)
    except GovernmentProjectionError as refusal:
        raise GovernmentPreparationError("GOVERNMENT_SNAPSHOT_UNAVAILABLE",
                                         reason_code=refusal.reason_code) from None
    _check_cancelled(cancellation_checker)
    items = list(bound["items"])
    queue: list[GovernmentWorkItem] = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping) or item.get("batch_position") != position \
                or str(item.get("snapshot_id")) != str(snapshot["id"]):
            raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID")
        try:
            queue.append(GovernmentWorkItem.from_record(item))
        except GovernmentPreparationError:
            raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID") from None
    if not queue or len(queue) != int(batch.get("item_count") or -1) \
            or len(queue) > GOVERNMENT_WORK_QUEUE_LIMIT \
            or len({item.candidate_key for item in queue}) != len(queue):
        raise GovernmentPreparationError("GOVERNMENT_BATCH_INVALID")
    return GovernmentPreparation(
        snapshot_key=snapshot_key, snapshot_id=str(snapshot["id"]),
        resource_id=src.WLTP_RESOURCE_ID,
        upstream_version=str(snapshot.get("upstream_version") or ""),
        upstream_version_kind=str(snapshot.get("upstream_version_kind") or ""),
        queue=tuple(queue), total_candidates=len(queue), bounded=False, resumed=False,
        work_scope_batch=identity)


def _resume(repository: Any, record: Mapping[str, Any],
            cancellation_checker: Callable[[], bool] | None, *,
            batch: Mapping[str, Any] | None = None) -> GovernmentPreparation:
    if record.get("schema") != ARTIFACT_SCHEMA:
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    # A batch-bound record resumes only while the binding still names the SAME
    # batch, and an unbound record only while the run is still unbound: the
    # durable binding is the authority, never the checkpoint alone.
    recorded_batch = record.get(BATCH_ARTIFACT_KEY)
    if recorded_batch is not None:
        if not isinstance(recorded_batch, Mapping) or batch is None \
                or set(recorded_batch) != set(BATCH_IDENTITY_FIELDS) \
                or dict(recorded_batch) != _batch_identity(batch):
            raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    elif batch is not None:
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    snapshot_key = str(record.get("snapshot_key") or "")
    resource_id = str(record.get("resource_id") or "")
    if not snapshot_key or not resource_id:
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    try:
        resource_id = src.require_allowed_resource(resource_id)
    except Exception:
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID") from None
    queue_records = record.get("queue")
    if not isinstance(queue_records, Sequence) or isinstance(queue_records, (str, bytes)):
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    queue = tuple(GovernmentWorkItem.from_record(item) for item in queue_records)
    if len({item.candidate_key for item in queue}) != len(queue):
        raise GovernmentPreparationError("GOVERNMENT_PREPARATION_RECORD_INVALID")
    try:
        # The EXACT pinned key, through the same usability rule a read applies.
        # A snapshot that has since become unusable refuses the resume rather
        # than silently answering from a different one.
        snapshot = resolve_active_snapshot(repository, resource_id=resource_id,
                                           snapshot_key=snapshot_key, allow_incomplete=False)
    except GovernmentProjectionError as refusal:
        raise GovernmentPreparationError("GOVERNMENT_SNAPSHOT_UNAVAILABLE",
                                         reason_code=refusal.reason_code) from None
    _check_cancelled(cancellation_checker)
    return GovernmentPreparation(
        snapshot_key=snapshot_key, snapshot_id=str(snapshot["id"]), resource_id=resource_id,
        upstream_version=str(snapshot.get("upstream_version") or ""),
        upstream_version_kind=str(snapshot.get("upstream_version_kind") or ""),
        queue=queue,
        total_candidates=max(len(queue), _optional_int(record.get("total_candidates")) or 0),
        bounded=bool(record.get("bounded")), resumed=True,
        work_scope_batch=dict(recorded_batch) if recorded_batch is not None else None)


def _read_queue(repository: Any, snapshot: Mapping[str, Any],
                limit: int) -> tuple[list[Mapping[str, Any]], int]:
    bound = max(1, min(int(limit), GOVERNMENT_WORK_QUEUE_LIMIT))
    try:
        rows = list(repository.catalog_candidate_variant_page(
            snapshot["id"], status=QUEUED_CANDIDATE_STATUS, limit=bound, offset=0,
            allow_incomplete=False))
    except AppError:
        raise GovernmentPreparationError("GOVERNMENT_QUEUE_UNAVAILABLE") from None
    total = 0
    items: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise GovernmentPreparationError("GOVERNMENT_QUEUE_UNAVAILABLE")
        try:
            total = max(total, int(row.get(TOTAL_COUNT_FIELD) or 0))
        except (TypeError, ValueError):
            raise GovernmentPreparationError("GOVERNMENT_QUEUE_UNAVAILABLE") from None
        if is_count_row(row):
            continue
        items.append(row)
    return items, max(total, len(items))


def _item_from_row(row: Mapping[str, Any]) -> GovernmentWorkItem:
    try:
        return GovernmentWorkItem(
            candidate_key=str(row["candidate_key"]), candidate_id=str(row["id"]),
            manufacturer=str(row["manufacturer"]), commercial_model=str(row["commercial_model"]),
            model_year_start=_optional_int(row.get("model_year_start")),
            model_year_end=_optional_int(row.get("model_year_end")),
            official_model_code=_optional_text(row.get("official_model_code")),
            trim=_optional_text(row.get("trim")))
    except (KeyError, TypeError):
        raise GovernmentPreparationError("GOVERNMENT_QUEUE_UNAVAILABLE") from None


def _promotable_operation() -> str:
    global _PROMOTABLE_OPERATION
    if _PROMOTABLE_OPERATION is None:
        from backend.catalog.pipeline import PROMOTABLE_TOOL_OPERATION
        _PROMOTABLE_OPERATION = PROMOTABLE_TOOL_OPERATION
    return _PROMOTABLE_OPERATION


def _check_cancelled(checker: Callable[[], bool] | None) -> None:
    if checker is not None and checker():
        raise CancellationRequested()


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "ARTIFACT_KEY", "ARTIFACT_SCHEMA", "BATCH_ARTIFACT_KEY", "BATCH_IDENTITY_FIELDS",
    "GOVERNMENT_WORK_QUEUE_LIMIT", "PREPARATION_PHASE",
    "PREPARATION_REASONS", "PROGRESS_EVIDENCED", "PROGRESS_PENDING", "PROGRESS_PROMOTED",
    "PROGRESS_STATES", "QUEUED_CANDIDATE_STATUS", "GovernmentPreparation",
    "GovernmentPreparationError", "GovernmentWorkItem", "government_work_progress",
    "is_preparation_checkpoint", "prepare_government_work", "prepared_artifact",
]
