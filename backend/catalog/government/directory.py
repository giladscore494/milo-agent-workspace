"""The register DIRECTORY: every distinct tozar and its row count (PR-D1, D1-2).

Read with bounded requests that never fetch row payload:

1. one bounded SCAN of the tozar column: ``datastore_search`` with
   ``fields=tozar``, ``sort=_id``, full pages of `SCAN_PAGE_LIMIT` rows, offset
   paging until the offset reaches the scan's ``total``. Every row's tozar is
   counted locally, by its exact string;
2. then, per capturable tozar, one count: ``limit=0`` with
   ``filters={"tozar": <exact>}``, whose ``total`` must equal the scan's count
   for that tozar and whose ``records`` must be empty.

CKAN's ``distinct=true`` is NOT read for values: on data.gov.il its ``total``
counts the distinct values while its ``records`` are truncated (1 of 137 with
``sort``, 26 without), so a paged distinct read stops after one short page.
One ``distinct=true, limit=0`` request is still made and its ``total``
reported as `RegisterDirectory.distinct_total` -- a cross-check only; its
records are never read, and an unavailable answer reports ``None``.

The scan is refused whole (``GOV_DIRECTORY_RESULT_INVALID``) unless every page
but the last is full, the rows counted add up to the scan's ``total``, and
every unit's scan count equals its independent filtered count. A ``total``
that moves between scan pages refuses with ``GOV_DIRECTORY_REGISTER_CHANGED``:
the register changed mid-read.

Every request goes through `DataGovClient._request` -- the same allowlisted
host, action, envelope, size, redirect and retry rules as a capture -- and the
whole discovery runs under two HARD caps: a request count
(`MILO_REGISTER_DIRECTORY_MAX_REQUESTS`) and a wall-clock time
(`MILO_REGISTER_DIRECTORY_MAX_SECONDS`). Exceeding either refuses the whole
directory; nothing partial is ever recorded.

A unit is always capturable: a value `CaptureScope` would refuse (padded,
over-long, a control or format character) is counted with the unfilterable
values instead.

The directory is keyed by the EXACT tozar string. Nothing is normalized
(that is D3): different spellings are different units. A value no filter can
select (null or empty) is counted in `unfilterable_values` and never a unit.

`register_version` is the SHA-256 of the canonical sorted (tozar, count) list
plus the resource id -- byte for byte what the database recomputes in
`public.catalog_register_version` (migration 20260929000100), so refreshing
an unchanged register lands on the same version.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from backend.catalog.government import source as src
from backend.catalog.government.capture_scope import CaptureScopeError, _filter_value
from backend.catalog.government.client import DataGovClient
from backend.catalog.government.source import GovernmentSourceError

DIRECTORY_CONTRACT = "gov.register.directory.1"
TOZAR_FIELD = "tozar"
#: Rows per tozar-column scan page (the client's own page ceiling).
SCAN_PAGE_LIMIT = src.MAX_PAGE_LIMIT
MAX_REQUESTS_ENV = "MILO_REGISTER_DIRECTORY_MAX_REQUESTS"
MAX_SECONDS_ENV = "MILO_REGISTER_DIRECTORY_MAX_SECONDS"
#: A full directory is one distinct cross-check, one scan page per 1000 rows
#: and one count per tozar: ~102 pages and ~137 counts at the register's
#: current size, far inside the cap.
DEFAULT_MAX_REQUESTS = 6000
DEFAULT_MAX_SECONDS = 3000.0
#: The database bounds a directory at 5000 units and a tozar at 200 chars.
MAX_UNITS = 5000
MAX_TOZAR_CHARS = 200


@dataclass(frozen=True)
class DirectoryUnit:
    tozar: str
    expected_rows: int


@dataclass(frozen=True)
class RegisterDirectory:
    resource_id: str
    units: tuple[DirectoryUnit, ...]
    fetched_at: datetime
    requests: int
    unfilterable_values: int = 0
    #: CKAN's ``distinct`` total for the tozar column, reported as a
    #: cross-check only (never trusted, never a unit); ``None`` if unavailable.
    distinct_total: int | None = None

    @property
    def register_version(self) -> str:
        return register_version(self.resource_id, self.units)

    @property
    def total_rows(self) -> int:
        return sum(unit.expected_rows for unit in self.units)

    def rpc_units(self) -> list[dict[str, Any]]:
        return [{"tozar": unit.tozar, "expected_rows": unit.expected_rows} for unit in self.units]


def register_version(resource_id: str, units: Any) -> str:
    """The directory's version (mirror of `public.catalog_register_version`)."""
    pairs = sorted(((unit.tozar, unit.expected_rows) if isinstance(unit, DirectoryUnit)
                    else (str(unit["tozar"]), int(unit["expected_rows"])) for unit in units),
                   key=lambda pair: pair[0].encode("utf-8"))
    text = f"{DIRECTORY_CONTRACT}\n{resource_id}\n" + "".join(
        f"{json.dumps(tozar, ensure_ascii=False)}:{count}\n" for tozar, count in pairs)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def configured_caps(env: Mapping[str, str] | None = None) -> tuple[int, float]:
    """(max requests, max seconds) from the environment, else the defaults.

    A malformed or non-positive value is NOT silently widened: it falls back to
    the default, which is the reviewed bound.
    """
    source = os.environ if env is None else env
    try:
        requests = int(str(source.get(MAX_REQUESTS_ENV) or DEFAULT_MAX_REQUESTS).strip())
    except ValueError:
        requests = DEFAULT_MAX_REQUESTS
    try:
        seconds = float(str(source.get(MAX_SECONDS_ENV) or DEFAULT_MAX_SECONDS).strip())
    except ValueError:
        seconds = DEFAULT_MAX_SECONDS
    return (requests if requests > 0 else DEFAULT_MAX_REQUESTS,
            seconds if seconds > 0 else DEFAULT_MAX_SECONDS)


@dataclass
class _Budget:
    max_requests: int
    max_seconds: float
    clock: Callable[[], float]
    started: float = 0.0
    used: int = 0
    _started: bool = field(default=False, repr=False)

    def spend(self) -> None:
        if not self._started:
            self.started, self._started = self.clock(), True
        if self.used >= self.max_requests:
            raise GovernmentSourceError("GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED")
        if self.clock() - self.started > self.max_seconds:
            raise GovernmentSourceError("GOV_DIRECTORY_TIME_BUDGET_EXCEEDED")
        self.used += 1


def discover_directory(client: DataGovClient, *, resource_id: str = src.WLTP_RESOURCE_ID,
                       max_requests: int | None = None, max_seconds: float | None = None,
                       clock: Callable[[], float] = time.monotonic,
                       now: Callable[[], datetime] = lambda: datetime.now(UTC),
                       env: Mapping[str, str] | None = None) -> RegisterDirectory:
    """Read the register directory, or refuse the whole of it."""
    default_requests, default_seconds = configured_caps(env)
    budget = _Budget(max_requests=int(max_requests or default_requests),
                     max_seconds=float(max_seconds or default_seconds), clock=clock)
    resource_id = src.require_allowed_resource(resource_id)
    distinct_total = _distinct_total(client, resource_id, budget)
    scanned, unfilterable = _scan_counts(client, resource_id, budget)
    units = []
    for tozar in sorted(scanned, key=lambda value: value.encode("utf-8")):
        # The scan's count and an independent filtered count must agree.
        if _count(client, resource_id, tozar, budget) != scanned[tozar]:
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
        units.append(DirectoryUnit(tozar=tozar, expected_rows=scanned[tozar]))
    return RegisterDirectory(resource_id=resource_id, units=tuple(units), fetched_at=now(),
                             requests=budget.used, unfilterable_values=unfilterable,
                             distinct_total=distinct_total)


def _search(client: DataGovClient, params: Mapping[str, str], budget: _Budget) -> Mapping[str, Any]:
    budget.spend()
    return _answer(client, params)


def _answer(client: DataGovClient, params: Mapping[str, str]) -> Mapping[str, Any]:
    document, _response, _url = client._request(src.DATASTORE_SEARCH, params)  # noqa: SLF001 - same package seam
    result = document["result"]
    if str(result.get("resource_id")) != str(params["resource_id"]):
        raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
    return result


def _exact_total(result: Mapping[str, Any]) -> int:
    total = result.get("total")
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0
            or result.get("total_was_estimated") not in (None, False)):
        raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
    return total


def _distinct_total(client: DataGovClient, resource_id: str, budget: _Budget) -> int | None:
    """CKAN's distinct-value total, for the report only. Its records are never
    read (data.gov.il truncates them), and an unusable answer is ``None``:
    the directory never depends on it."""
    budget.spend()
    try:
        result = _answer(client, {"resource_id": resource_id, "fields": TOZAR_FIELD,
                                  "distinct": "true", "limit": "0"})
        return _exact_total(result)
    except GovernmentSourceError:
        return None


def _scan_counts(client: DataGovClient, resource_id: str,
                 budget: _Budget) -> tuple[dict[str, int], int]:
    """Every row's exact tozar, counted: (capturable tozar -> rows, number of
    distinct unfilterable values)."""
    counts: dict[str, int] = {}
    unfilterable: set[str | None] = set()
    scanned = 0
    first_total: int | None = None
    offset = 0
    while True:
        result = _search(client, {"resource_id": resource_id, "fields": TOZAR_FIELD,
                                  "sort": "_id", "limit": str(SCAN_PAGE_LIMIT),
                                  "offset": str(offset)}, budget)
        total = _exact_total(result)
        if first_total is None:
            first_total = total
        elif total != first_total:
            raise GovernmentSourceError("GOV_DIRECTORY_REGISTER_CHANGED")
        records = result.get("records")
        if not isinstance(records, list):
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
        for record in records:
            # The scan answers the requested field ONLY: anything else would
            # be row payload, which this step never takes.
            if not isinstance(record, Mapping) or set(record) - {TOZAR_FIELD, "_id"} \
                    or TOZAR_FIELD not in record:
                raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
            value = record[TOZAR_FIELD]
            if value is not None and not isinstance(value, str):
                raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
            if not _capturable(value):
                # A value no scoped capture can filter on -- null, empty,
                # padded, over-long, or carrying a control/format character
                # (CaptureScope refuses exactly these) -- is counted, never a
                # unit: a unit is always capturable.
                unfilterable.add(value)
                continue
            counts[value] = counts.get(value, 0) + 1
        if len(counts) > MAX_UNITS:
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
        scanned += len(records)
        offset += SCAN_PAGE_LIMIT
        if offset >= first_total:
            # The last page: every row the total names was counted, no more.
            if scanned != first_total:
                raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
            return counts, len(unfilterable)
        if len(records) != SCAN_PAGE_LIMIT:
            # A short page before the total is reached is a truncated answer.
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")


def _capturable(value: str | None) -> bool:
    if not value:
        return False
    try:
        _filter_value(value)
    except CaptureScopeError:
        return False
    return True


def _count(client: DataGovClient, resource_id: str, tozar: str, budget: _Budget) -> int:
    filters = json.dumps({TOZAR_FIELD: tozar}, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    result = _search(client, {"resource_id": resource_id, "limit": "0", "filters": filters}, budget)
    total = _exact_total(result)
    records = result.get("records")
    echoed = result.get("filters")
    if isinstance(echoed, str):
        try:
            echoed = json.loads(echoed)
        except ValueError:
            echoed = None
    if records not in (None, []) or echoed != {TOZAR_FIELD: tozar}:
        raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
    return total


def count_tozar(client: DataGovClient, tozar: str, *, resource_id: str = src.WLTP_RESOURCE_ID,
                clock: Callable[[], float] = time.monotonic) -> int:
    """ONE fresh count of an exact tozar: the directory's own bounded
    ``limit=0`` request (one request, no row payload). Register capture takes
    it at the END of a capture to verify the stored rows independently of the
    capture's own reported total."""
    budget = _Budget(max_requests=1, max_seconds=DEFAULT_MAX_SECONDS, clock=clock)
    return _count(client, src.require_allowed_resource(resource_id), tozar, budget)


__all__ = ["DIRECTORY_CONTRACT", "DirectoryUnit", "RegisterDirectory", "configured_caps",
           "count_tozar", "discover_directory", "register_version"]
