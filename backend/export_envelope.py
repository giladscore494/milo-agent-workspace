"""The private, engine-neutral JSON envelope a finished run exports as.

This is a READ-ONLY projection. It adds no route, imports nothing into Yeda,
and never asks a model to post-process anything: it wraps what the engines
already produced so a run can be exported without the caller having to know
which engine wrote it.

The two engines finish differently and the envelope keeps that difference
visible rather than flattening it:

* ``swarm_v2`` owns a validated product-outcome contract, so its ``runs.output``
  is re-validated through ``outcome.validate_product_outcome`` and carried
  through unchanged. A payload that is not exactly one that
  ``finalize_product_outcome`` could have produced is refused, not exported.
* ``vehicle_catalog_v1`` predates that contract. Its durable final output is
  wrapped as-is and classified from its own recorded status. Nothing rewrites,
  summarizes or re-judges it.

Terminal honesty is the point of the ``terminal_status`` field. A Cloud Run
process exiting zero is not a product result, and neither is a timeout or a
cancellation: those stay distinct from ``completed`` and from
``partial_success``, and ``no_usable_result`` stays distinct from both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

SCHEMA_VERSION = "milo-run-export/1"

#: Terminal run states that carry a product result worth exporting.
USEFUL_TERMINAL_STATES = frozenset({"completed", "partial_success"})

#: Terminal states that are truthful outcomes but are NOT product results.
#: They are exportable -- an operator wants to see them -- but they never
#: claim a result kind.
NON_PRODUCT_TERMINAL_STATES = frozenset({"failed", "timed_out", "cancelled",
                                         "budget_exhausted"})

TERMINAL_STATES = USEFUL_TERMINAL_STATES | NON_PRODUCT_TERMINAL_STATES

#: V1 records its own product status inside its output. These are the values
#: that mean "this run produced something usable".
_V1_USABLE_STATUSES = frozenset({"complete", "success"})
_V1_PARTIAL_STATUSES = frozenset({"partial_success", "partial"})


class ExportRefused(ValueError):
    """The run cannot be exported as a truthful envelope."""


def _government_provenance(output: Any) -> dict[str, Any]:
    """Collect the government source identity a result already carries.

    Provenance is READ from the durable result; it is never synthesized. A run
    that touched no government source exports an explicitly empty record rather
    than an absent one, so "no provenance" and "we did not look" are
    distinguishable downstream.
    """
    sources: set[str] = set()
    datasets: set[str] = set()
    if isinstance(output, Mapping):
        for entries in (output.get("fields") or {}).values():
            if not isinstance(entries, (list, tuple)):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                trace = entry.get("provenance")
                if isinstance(trace, Mapping) and trace.get("source_id"):
                    sources.add(str(trace["source_id"]))
        meta = output.get("dataset_provenance") or output.get("provenance")
        if isinstance(meta, Mapping):
            for key in ("resource_id", "dataset_id", "upstream_version"):
                if meta.get(key):
                    datasets.add(f"{key}={meta[key]}")
    return {"source_ids": sorted(sources), "datasets": sorted(datasets),
            "present": bool(sources or datasets)}


def build_export_envelope(run: Mapping[str, Any], *,
                          generated_at: datetime | None = None) -> dict[str, Any]:
    """Wrap ONE finished run as the engine-neutral export document."""
    if not isinstance(run, Mapping):
        raise ExportRefused("run must be a mapping")
    run_id = str(run.get("id") or "")
    if not run_id:
        raise ExportRefused("run id is required")
    status = run.get("status")
    if status not in TERMINAL_STATES:
        raise ExportRefused("only a run in a terminal state can be exported")
    engine = str((run.get("input") or {}).get("workflow_key")
                 or run.get("workflow_key") or "vehicle_catalog_v1")
    output = run.get("output")

    result_kind: str | None = None
    if status in USEFUL_TERMINAL_STATES:
        if engine == "swarm_v2":
            from backend.engines.swarm_v2.outcome import (ProductOutcomeError,
                                                          validate_product_outcome)
            try:
                outcome = validate_product_outcome(output)
            except ProductOutcomeError as exc:
                # A stored payload that the contract would not have produced is
                # not exportable: exporting it would launder an invalid result
                # into a document that looks authoritative.
                raise ExportRefused("stored Swarm V2 outcome is not contract-valid") from exc
            result_kind = outcome.result_kind
        else:
            declared = (output or {}).get("status") if isinstance(output, Mapping) else None
            if declared in _V1_USABLE_STATUSES:
                result_kind = "usable_result"
            elif declared in _V1_PARTIAL_STATUSES:
                result_kind = "partial_result"
            else:
                result_kind = "no_usable_result"

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "engine": engine,
        "terminal_status": status,
        "result_kind": result_kind,
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "government_provenance": _government_provenance(output),
        "result": output,
        "usage": run.get("usage") or {},
        "error": run.get("error"),
    }


def validate_export_envelope(envelope: Any) -> None:
    """Refuse an envelope that is not exactly what this module produces."""
    if not isinstance(envelope, Mapping):
        raise ExportRefused("envelope must be a mapping")
    required = {"schema_version", "run_id", "engine", "terminal_status",
                "result_kind", "generated_at", "government_provenance", "result"}
    missing = required - set(envelope)
    if missing:
        raise ExportRefused(f"envelope is missing required fields: {sorted(missing)}")
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise ExportRefused("unknown envelope schema version")
    if envelope["terminal_status"] not in TERMINAL_STATES:
        raise ExportRefused("envelope terminal status is not allowlisted")
    kind = envelope["result_kind"]
    if envelope["terminal_status"] in USEFUL_TERMINAL_STATES:
        from backend.engines.swarm_v2.outcome import RESULT_KINDS

        if kind not in RESULT_KINDS:
            raise ExportRefused("a useful terminal status requires an allowlisted result kind")
    elif kind is not None:
        # A timeout, a cancellation or a failure has no product result. Letting
        # one carry a result kind is how "the process exited" gets read as
        # "the run produced something".
        raise ExportRefused("a non-product terminal status cannot carry a result kind")
    if not isinstance(envelope["government_provenance"], Mapping):
        raise ExportRefused("government provenance must be an object")


__all__ = ["ExportRefused", "NON_PRODUCT_TERMINAL_STATES", "SCHEMA_VERSION",
           "TERMINAL_STATES", "USEFUL_TERMINAL_STATES", "build_export_envelope",
           "validate_export_envelope"]
