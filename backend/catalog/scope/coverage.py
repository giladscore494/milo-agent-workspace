"""How much of each directory marque the canonical catalog ALREADY holds.

One bounded read, and one honest answer per entry
-------------------------------------------------

The canonical catalog stores a marque exactly as the register wrote it
(`normalize.py` rule 5), so the only way to attribute canonical variants to a
directory entry is by that entry's VERIFIED register spelling. The read is one
reviewed RPC, `catalog_canonical_manufacturer_coverage`, over exactly the
verified spellings, which returns an exact count per spelling plus the
catalog-wide total.

Every entry is then in one of three states, and they are never merged:

*   ``known``        -- the count is exact, and it may be zero;
*   ``unverifiable`` -- the entry has no verified register spelling, so no
    count can be attributed to it. This is NOT zero: the catalog may hold
    variants of that marque under a spelling this table does not know;
*   ``unavailable``  -- the read failed. Nothing is stated.

The catalog-wide total is carried separately, so variants no directory
spelling accounts for are visible as a difference rather than hidden.

This is a READ. It needs no lease, no flag and no run, and it writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.errors import AppError

from . import directory as mdir

COVERAGE_STATES = ("known", "unverifiable", "unavailable")


@dataclass(frozen=True)
class Coverage:
    """Per-entry canonical coverage, and the catalog-wide total."""

    available: bool
    by_key: Mapping[str, int | None]
    catalog_variants: int | None

    def state(self, key: str) -> str:
        entry = mdir.entry_for(key)
        if not self.available:
            return "unavailable"
        if entry is None or not entry.register_marque_verified:
            return "unverifiable"
        return "known"

    def count(self, key: str) -> int | None:
        return self.by_key.get(key) if self.state(key) == "known" else None

    @property
    def attributed_variants(self) -> int | None:
        if not self.available:
            return None
        return sum(value for value in self.by_key.values() if isinstance(value, int))


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def read_coverage(repository: Any) -> Coverage:
    """Read coverage for every verified directory spelling, or say it failed.

    A repository failure is not an exception here: the directory is still
    worth showing without its counts, and every count then reads
    ``unavailable`` rather than zero. A caller that NEEDS the counts -- the
    "not mapped yet" reading -- checks `available` and refuses on its own.
    """
    verified = mdir.verified_register_marques()
    try:
        rows = repository.catalog_canonical_manufacturer_coverage(
            [marque for _key, marque in verified])
    except AppError:
        return Coverage(available=False, by_key={}, catalog_variants=None)
    by_marque: dict[str, int | None] = {}
    total: int | None = None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        count = _whole(row.get("canonical_variants"))
        if row.get("manufacturer") is None:
            total = count
        elif isinstance(row.get("manufacturer"), str):
            by_marque[row["manufacturer"]] = count
    by_key = {key: by_marque.get(marque) for key, marque in verified}
    if total is None or any(value is None for value in by_key.values()):
        # A read that did not account for every spelling it was asked about,
        # or stated no total, is not a partial answer to show -- it is no answer.
        return Coverage(available=False, by_key={}, catalog_variants=None)
    return Coverage(available=True, by_key=by_key, catalog_variants=total)


def interpretation_coverage(coverage: Coverage) -> dict[str, int | None]:
    """The map `interpret.apply_reading` takes: an exact count, or None."""
    return {entry.key: coverage.count(entry.key) for entry in mdir.DIRECTORY}


__all__ = ["COVERAGE_STATES", "Coverage", "interpretation_coverage", "read_coverage"]
