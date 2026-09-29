"""Retention of register snapshots (PR-D1, D1-5 / O22).

The RULE lives in the database (`public.catalog_register_prunable_snapshots`,
migration 20260929000100), so the dry-run, the apply and any operator query
agree by construction. Kept, always:

* per tozar, the ACTIVE snapshot and the one before it (the two latest
  activations), and (PR-L1b, 20261001000100) its CURRENT variant build;
* the snapshot of the latest CAPTURED register unit of each tozar;
* any snapshot referenced by evidence, claims (canonical field provenance),
  runs (adoptions, run checkpoints), work-scope units / batches / queue items,
  a `register`-level coverage-ledger row, or whose candidates are referenced
  anywhere;
* any snapshot whose writer run is still live.

Everything else among the SCOPED Government snapshots is prunable, with its
variants (and an old mapper version's variant build once the current one is
complete). Prune deletes database rows only -- never an archive object -- and only for the
exact list whose digest the dry-run printed.

This module holds the digest (mirror of `public.catalog_register_prune_digest`)
and a pure mirror of the rule over plain rows, used by the in-memory
repository in tests.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable, Mapping

CONFIRM_WORD = "PRUNE"
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial_success", "failed", "cancelled",
                                   "timed_out", "budget_exhausted"})


def prune_digest(snapshot_keys: Iterable[str]) -> str:
    """SHA-256 of the sorted keys (UTF-8 byte order), one per line + "\\n"."""
    ordered = sorted((str(key) for key in snapshot_keys), key=lambda key: key.encode("utf-8"))
    return hashlib.sha256("".join(f"{key}\n" for key in ordered).encode("utf-8")).hexdigest()


def scoped_tozar(snapshot: Mapping[str, Any]) -> str | None:
    metadata = snapshot.get("retrieval_metadata")
    scope = metadata.get("capture_scope") if isinstance(metadata, Mapping) else None
    filters = scope.get("filters") if isinstance(scope, Mapping) else None
    value = filters.get("tozar") if isinstance(filters, Mapping) else None
    return value if isinstance(value, str) else None


def prunable(snapshots: Iterable[Mapping[str, Any]], *, referenced_ids: set[str],
             referenced_keys: set[str], live_run_ids: set[str]) -> list[Mapping[str, Any]]:
    """The pure mirror of the database rule over snapshot rows."""
    rows = [row for row in snapshots
            if row.get("source_family") == "government" and scoped_tozar(row) is not None]
    kept: set[str] = set()
    by_tozar: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("activated_at") is not None:
            by_tozar.setdefault(str(scoped_tozar(row)), []).append(row)
    for activated in by_tozar.values():
        # The database's order: activated_at DESC, then id ASC.
        activated.sort(key=lambda row: str(row["id"]))
        activated.sort(key=lambda row: str(row["activated_at"]), reverse=True)
        kept.update(str(row["id"]) for row in activated[:2])
    out = [row for row in rows
           if str(row["id"]) not in kept and str(row["id"]) not in referenced_ids
           and str(row["snapshot_key"]) not in referenced_keys
           and str(row.get("created_by_run_id")) not in live_run_ids]
    return sorted(out, key=lambda row: str(row["snapshot_key"]).encode("utf-8"))


__all__ = ["CONFIRM_WORD", "TERMINAL_RUN_STATUSES", "prunable", "prune_digest", "scoped_tozar"]
