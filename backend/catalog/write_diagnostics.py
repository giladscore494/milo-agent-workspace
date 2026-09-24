"""WHERE a catalog ingestion write failed: bounded context for one log line.

`backend/repository/supabase.py::SupabaseRepository._guarded_rpc` logs every failed attempt with static fields
only (the function, the cause's class, its code, the HTTP status, the failure
class, the attempt). That says WHAT failed. For the catalog ingestion writes an
operator also needs WHERE: which run, which snapshot, which phase and which
batch. This object carries exactly that, and nothing else.

It is built only by the reviewed ingestion caller
(`backend/catalog/government/ingest.py`) and accepted by the repository only
for the catalog ingestion RPCs (`CATALOG_INGESTION_RPCS`). Every field is an
identifier or a small whole number, normalized here: a run or snapshot id that
is not a UUID renders as `none`, a phase must come from the static vocabulary,
and a position must be a non-negative whole number. No payload, message, URL,
lease token or row content can reach it, because nothing here accepts text.
It is written to the server log only -- never to a report, an event or an API
answer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence
from uuid import UUID

#: The ingestion steps a failed write can belong to.
CATALOG_WRITE_PHASES = ("snapshot", "adopt", "raw", "candidates", "activate")

#: The RPCs that accept this context. Every other guarded RPC logs exactly the
#: static line it always did.
CATALOG_INGESTION_RPCS = frozenset({
    "record_catalog_snapshot_guarded",
    "adopt_catalog_snapshot_guarded",
    "record_catalog_raw_records_batch_guarded",
    "record_catalog_candidates_batch_guarded",
    "activate_catalog_snapshot_guarded",
})

_MAX_POSITION = 2_147_483_647


def _uuid(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_POSITION:
        return None
    return value


def _span(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    first, last = _whole(value[0]), _whole(value[1])
    return None if first is None or last is None or last < first else (first, last)


@dataclass(frozen=True)
class CatalogWriteDiagnostics:
    """Run, snapshot, phase and batch position of one catalog ingestion write."""

    run_id: Any
    phase: str
    snapshot_id: Any = None
    #: 1-based ordinal of the ingestion batch within its phase.
    batch: Any = None
    #: First and last row offset, within that batch, of the part actually sent
    #: (a split batch sends halves).
    rows: Any = None
    #: First and last `capture_index` of the raw records sent, where stated.
    capture_index: Any = None

    def __post_init__(self) -> None:
        if self.phase not in CATALOG_WRITE_PHASES:
            raise ValueError("catalog write phase must come from the static vocabulary")
        object.__setattr__(self, "run_id", _uuid(self.run_id))
        object.__setattr__(self, "snapshot_id", _uuid(self.snapshot_id))
        object.__setattr__(self, "batch", _whole(self.batch))
        object.__setattr__(self, "rows", _span(self.rows))
        object.__setattr__(self, "capture_index", _span(self.capture_index))

    def for_part(self, offset: int, part: Sequence[Mapping[str, Any]]) -> "CatalogWriteDiagnostics":
        """The same context narrowed to the rows of one (possibly split) call."""
        positions = [(row.get("source_locator") or {}).get("capture_index")
                     if isinstance(row, Mapping) else None for row in part]
        whole = [position for position in positions if _whole(position) is not None]
        capture = (min(whole), max(whole)) if part and len(whole) == len(part) else None
        return replace(self, rows=(offset, offset + len(part) - 1) if part else None,
                       capture_index=capture)

    def log_fields(self) -> str:
        def show(value: Any) -> str:
            if value is None:
                return "none"
            if isinstance(value, tuple):
                return f"{value[0]}-{value[1]}"
            return str(value)
        return (f"run_id={show(self.run_id)} snapshot_id={show(self.snapshot_id)} "
                f"phase={self.phase} batch={show(self.batch)} rows={show(self.rows)} "
                f"capture_index={show(self.capture_index)}")


__all__ = ["CATALOG_INGESTION_RPCS", "CATALOG_WRITE_PHASES", "CatalogWriteDiagnostics"]
