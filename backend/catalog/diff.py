"""Catalog PR3: what changed between two snapshots of one resource.

Why this is its own module
--------------------------

The comparison has THREE callers and must be one rule for all of them:

*   `public.catalog_snapshot_candidate_diff` computes it inside PostgreSQL, over
    the real full resource, without reading a row into this process;
*   `backend/testing/memory_repository.py` mirrors that function, so the
    in-memory repository answers the same question the database does;
*   `backend/catalog/government/refresh.py` turns either answer into the
    `SnapshotDiff` a refresh reports.

Putting the rule here keeps `refresh.py` -- which owns an HTTP client -- out of
the repository's import graph, and keeps the ordering, the identity and the
bound in exactly one place rather than three.

Pure module: dictionaries in, dictionaries out. No I/O, no clock, no
randomness, no global mutable state.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: How many changed candidates one diff reports INDIVIDUALLY. The three COUNTS
#: are always exact and are never bounded; only the item lists are, because a
#: refresh that changed everything must not produce an unbounded work item.
MAX_DIFF_ITEMS = 100

#: What a diff compares: the candidate's COMPLETE stated identity, never its
#: surrogate id and never the register's own `_id`.
#:
#: Not the id, because a re-capture produces new rows with new uuids for the
#: same vehicles, so comparing ids would report every row as added and every
#: row as removed. Not the register's `_id` either -- PR2 recorded that the
#: datastore reuses that number space across captures, so it identifies a row
#: within ONE retrieval and nothing beyond it.
#:
#: The COMPLETE identity, dimensions included, because the register publishes
#: several rows that share a marque, model, year, code and trim and differ only
#: in their coded dimensions. Leaving the dimensions out would make those rows
#: one identity, and which of them "won" would then depend on the order they
#: were read in -- a diff that changes with the read order is not a diff.
DIFF_IDENTITY = ("manufacturer", "commercial_model", "model_year_start",
                 "model_year_end", "official_model_code", "trim", "identity_dimensions")

#: The columns one delta row carries, in the order
#: `public.catalog_snapshot_candidate_diff` returns them.
DIFF_ROW_FIELDS = ("state", "manufacturer", "commercial_model", "model_year_start",
                   "model_year_end", "official_model_code", "trim", "changed_fields",
                   "upstream_record_id")

#: The three exact counts every row of that function carries -- including the
#: COUNT ROW it returns when the diff has no items to report.
DIFF_COUNT_FIELDS = ("added_count", "changed_count", "removed_count")

#: The only reading difference two snapshots can state about ONE identity.
#: Every other stated field IS the identity, so a difference in one makes two
#: identities rather than one that changed.
CHANGED_FIELD = "status"


def candidate_identity(row: Mapping[str, Any]) -> tuple:
    """One candidate's complete stated identity, as a hashable, ordered tuple.

    The dimensions are sorted, so two readings that stated the same dimensions
    in a different key order are the same identity -- which they are, and which
    is also how PostgreSQL compares the `jsonb` column.
    """
    identity: list[Any] = []
    for name in DIFF_IDENTITY:
        value = row.get(name)
        if name == "identity_dimensions":
            identity.append(tuple(sorted((str(key), str(item))
                                         for key, item in (value or {}).items())))
        else:
            identity.append(value)
    return tuple(identity)


def diff_sort_key(row: Mapping[str, Any]) -> tuple:
    """The deterministic order delta rows are reported in.

    Byte-for-byte the ordering `catalog_snapshot_candidate_diff` applies, which
    is why it ends on `upstream_record_id`: that is unique within a snapshot,
    so the order is TOTAL and two runs over the same pair of snapshots produce
    the same list in the same order, in either implementation.
    """
    return (str(row.get("state") or ""), str(row.get("manufacturer") or ""),
            str(row.get("commercial_model") or ""),
            int(row.get("model_year_start") or 0), int(row.get("model_year_end") or 0),
            str(row.get("official_model_code") or ""), str(row.get("trim") or ""),
            str(row.get("upstream_record_id") or ""))


def _reduce(rows: Iterable[Mapping[str, Any]]) -> dict[tuple, Mapping[str, Any]]:
    """One row per identity. The FIRST wins, because the rows arrive ordered.

    A snapshot may state one identity more than once; the register does. Taking
    the first is deterministic given the page order; last-wins would make the
    diff depend on which duplicate happened to be read last.
    """
    reduced: dict[tuple, Mapping[str, Any]] = {}
    for row in rows:
        reduced.setdefault(candidate_identity(row), row)
    return reduced


def _delta(row: Mapping[str, Any], state: str, changed: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"state": state, "manufacturer": row.get("manufacturer"),
            "commercial_model": row.get("commercial_model"),
            "model_year_start": row.get("model_year_start"),
            "model_year_end": row.get("model_year_end"),
            "official_model_code": row.get("official_model_code"),
            "trim": row.get("trim"), "changed_fields": list(changed),
            "upstream_record_id": row.get("upstream_record_id") or ""}


def diff_rows(previous: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]], *,
              limit: int = MAX_DIFF_ITEMS) -> list[dict[str, Any]]:
    """The delta rows and the three exact counts, in the RPC's own shape.

    `added` and `removed` are identities one side states and the other does
    not. `changed` is an identity BOTH state whose READING differs -- which,
    since every stated identity field is part of the identity itself, means its
    STATUS: a candidate the newer capture could read where the older one left
    it `ambiguous`, or the reverse.

    The item rows are dropped WHOLE when the total exceeds `limit`, never
    truncated: a truncated list is a diff claiming a completeness it does not
    have. The counts stay exact either way, which is the whole point -- they
    are computed over every matching row, not over the page.

    The result ALWAYS has at least one row. When there are no items -- either
    because nothing changed or because the list was dropped -- it is a single
    COUNT ROW whose every delta column is null and whose three counts are real,
    exactly as the SQL function returns one.
    """
    bound = max(0, int(limit))
    before, after = _reduce(previous), _reduce(current)
    items: list[dict[str, Any]] = []
    for key in set(after) - set(before):
        items.append(_delta(after[key], "added"))
    for key in set(before) - set(after):
        items.append(_delta(before[key], "removed"))
    for key in set(before) & set(after):
        if before[key].get(CHANGED_FIELD) != after[key].get(CHANGED_FIELD):
            items.append(_delta(after[key], "changed", (CHANGED_FIELD,)))
    counts = {"added_count": sum(1 for item in items if item["state"] == "added"),
              "changed_count": sum(1 for item in items if item["state"] == "changed"),
              "removed_count": sum(1 for item in items if item["state"] == "removed")}
    if not items or len(items) > bound:
        return [{name: None for name in DIFF_ROW_FIELDS} | counts]
    return [item | counts for item in sorted(items, key=diff_sort_key)]


def is_count_row(row: Mapping[str, Any]) -> bool:
    """Whether one returned row is the COUNT ROW rather than a delta."""
    return all(row.get(name) is None for name in DIFF_ROW_FIELDS)


__all__ = ["CHANGED_FIELD", "DIFF_COUNT_FIELDS", "DIFF_IDENTITY", "DIFF_ROW_FIELDS",
           "MAX_DIFF_ITEMS", "candidate_identity", "diff_rows", "diff_sort_key",
           "is_count_row"]
