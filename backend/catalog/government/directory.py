"""The register DIRECTORY: every distinct tozar and its row count (PR-D1, D1-2).

Read with bounded METADATA requests only -- no row payload is fetched here:

1. one distinct read: ``datastore_search`` with ``fields=tozar`` and
   ``distinct=true`` (paged, sorted), which answers tozar VALUES only;
2. then, per tozar, one count: ``limit=0`` with ``filters={"tozar": <exact>}``,
   whose ``total`` is the unit's expected rows and whose ``records`` must be
   empty.

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
#: Distinct values per distinct-read page (the client's own page ceiling).
DISTINCT_PAGE_LIMIT = src.MAX_PAGE_LIMIT
MAX_REQUESTS_ENV = "MILO_REGISTER_DIRECTORY_MAX_REQUESTS"
MAX_SECONDS_ENV = "MILO_REGISTER_DIRECTORY_MAX_SECONDS"
#: A full directory is one distinct page per 1000 values plus one count per
#: tozar, so the request cap sits above the database's 5000-unit bound.
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
    values, unfilterable = _distinct_values(client, resource_id, budget)
    units = []
    for tozar in values:
        units.append(DirectoryUnit(tozar=tozar, expected_rows=_count(client, resource_id, tozar, budget)))
    return RegisterDirectory(resource_id=resource_id, units=tuple(units), fetched_at=now(),
                             requests=budget.used, unfilterable_values=unfilterable)


def _search(client: DataGovClient, params: Mapping[str, str], budget: _Budget) -> Mapping[str, Any]:
    budget.spend()
    document, _response, _url = client._request(src.DATASTORE_SEARCH, params)  # noqa: SLF001 - same package seam
    result = document["result"]
    if str(result.get("resource_id")) != str(params["resource_id"]):
        raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
    return result


def _distinct_values(client: DataGovClient, resource_id: str,
                     budget: _Budget) -> tuple[list[str], int]:
    values: list[str] = []
    seen: set[str] = set()
    unfilterable = 0
    offset = 0
    while True:
        result = _search(client, {"resource_id": resource_id, "fields": TOZAR_FIELD,
                                  "distinct": "true", "sort": TOZAR_FIELD,
                                  "limit": str(DISTINCT_PAGE_LIMIT), "offset": str(offset)}, budget)
        records = result.get("records")
        if not isinstance(records, list):
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
        for record in records:
            # A distinct read answers the requested field ONLY: anything else
            # would be row payload, which this step never takes.
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
                unfilterable += 1
                continue
            if value in seen:
                raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
            seen.add(value)
            values.append(value)
        if len(values) > MAX_UNITS:
            raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
        if len(records) < DISTINCT_PAGE_LIMIT:
            return values, unfilterable
        offset += DISTINCT_PAGE_LIMIT


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
    total = result.get("total")
    records = result.get("records")
    echoed = result.get("filters")
    if isinstance(echoed, str):
        try:
            echoed = json.loads(echoed)
        except ValueError:
            echoed = None
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0
            or result.get("total_was_estimated") not in (None, False)
            or records not in (None, []) or echoed != {TOZAR_FIELD: tozar}):
        raise GovernmentSourceError("GOV_DIRECTORY_RESULT_INVALID")
    return total


__all__ = ["DIRECTORY_CONTRACT", "DirectoryUnit", "RegisterDirectory", "configured_caps",
           "discover_directory", "register_version"]
