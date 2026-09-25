"""PR-T: a register ambiguity is an OUTCOME, not a failure.

Run 3c72bfbc planned nine tasks and completed all nine. One of them, t04, asked
the Government register to resolve two duplicate-signature candidates, and the
register truthfully answered that two identical rows match: ``resolved=false,
ambiguous=true, match_count=2``. An ambiguous answer quotes no single row, so
the trusted evidence mapper recorded nothing for it (``NO_EVIDENCE``), the
task's evidence requirement could not be met, and because the task had
``completion.allow_partial=false`` the engine raised a bare ``ValueError`` and
the whole batch -- eight resolved candidates, 40 claims -- was discarded before
verification ever ran.

This module is the typed record that stops that. For every
``catalog.government_vehicle.resolve_variant`` call a completed task made, it
states ONE per-candidate outcome:

====================  =========================================================
``resolved``          exactly one register row matched
``unresolved_ambiguous``  more than one row matched (``ambiguous=true``)
``unresolved_not_found``  no row matched (``resolved=false``, ``ambiguous=false``)
====================  =========================================================

Where the outcome comes from, and where it never comes from:

*   It is read from the REGISTRY-VALIDATED tool result -- the typed
    ``resolved`` / ``ambiguous`` / ``match_count`` fields the operation's
    declared output schema guarantees -- inside trusted worker code, and from
    the server-resolved call arguments that produced it.
*   It is NEVER inferred from the worker model's completion, from prose, from
    the absence of evidence, or from a task output field a Commander happened
    to name ``register_ambiguous``. A model cannot write one.

An unresolved candidate is a VALID terminal answer for the task that asked. The
engine therefore reports it as its own soft coverage gap (see
``CANDIDATE_GAP_CODES``), which keeps the result honest (``partial_success``,
never ``complete``) without making it a hard completion failure.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: The ONE operation whose result can carry a per-candidate outcome. A literal,
#: like ``PRODUCTION_EVIDENCE_MAPPER_OPERATIONS``: importing the catalog package
#: here would make the two packages initialize each other.
RESOLVE_VARIANT_OPERATION = ("catalog.government_vehicle", "resolve_variant")

RESOLVED = "resolved"
UNRESOLVED_AMBIGUOUS = "unresolved_ambiguous"
UNRESOLVED_NOT_FOUND = "unresolved_not_found"

CANDIDATE_OUTCOMES = frozenset({RESOLVED, UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND})
UNRESOLVED_OUTCOMES = frozenset({UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND})

#: The coverage-gap code an unresolved candidate is reported under. These are
#: SOFT gaps: they make the product outcome partial and are listed for review,
#: but they never make a task's completion criteria "unmet".
CANDIDATE_GAP_CODES: Mapping[str, str] = {
    UNRESOLVED_AMBIGUOUS: "CANDIDATE_UNRESOLVED_AMBIGUOUS",
    UNRESOLVED_NOT_FOUND: "CANDIDATE_UNRESOLVED_NOT_FOUND",
}
SOFT_GAP_CODES = frozenset(CANDIDATE_GAP_CODES.values())

#: The candidate identity is exactly the operation's input vocabulary.
CANDIDATE_KEYS = ("manufacturer", "commercial_model", "model_year", "trim",
                  "official_model_code")

#: Bounds on what one outcome can carry into durable state and the payload.
MAX_OUTCOME_RECORD_IDS = 8
MAX_OUTCOME_TEXT_CHARS = 200


def _bounded(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return value[:MAX_OUTCOME_TEXT_CHARS]
    return None


def candidate_outcome(*, task_id: str, call_id: str, tool: str, operation: str,
                      arguments: Mapping[str, Any], result: Any) -> dict[str, Any] | None:
    """The typed outcome of ONE validated tool call, or None.

    None for every operation other than ``resolve_variant`` and for a result
    whose typed fields are not exactly the declared booleans and integer -- a
    shape the Registry would already have refused, so this is fail-closed
    defence rather than a path production takes.
    """
    if (str(tool), str(operation)) != RESOLVE_VARIANT_OPERATION or not isinstance(result, Mapping):
        return None
    resolved, ambiguous, count = (result.get("resolved"), result.get("ambiguous"),
                                  result.get("match_count"))
    if (type(resolved) is not bool or type(ambiguous) is not bool
            or type(count) is not int or count < 0):
        return None
    if ambiguous:
        outcome = UNRESOLVED_AMBIGUOUS
    elif not resolved:
        outcome = UNRESOLVED_NOT_FOUND
    else:
        outcome = RESOLVED
    candidate = {key: _bounded(arguments.get(key)) for key in CANDIDATE_KEYS}
    variants = result.get("variants")
    record_ids = sorted({str(item["upstream_record_id"])[:MAX_OUTCOME_TEXT_CHARS]
                         for item in (variants if isinstance(variants, list) else [])
                         if isinstance(item, Mapping)
                         and isinstance(item.get("upstream_record_id"), str)})
    return {"task_id": str(task_id), "call_id": str(call_id), "outcome": outcome,
            "match_count": count,
            "candidate": {key: value for key, value in candidate.items() if value is not None},
            "record_ids": record_ids[:MAX_OUTCOME_RECORD_IDS]}


def unresolved_kinds(outcomes: Iterable[Mapping[str, Any]]) -> list[str]:
    """The distinct unresolved outcome kinds in one task's outcomes, sorted."""
    return sorted({str(item.get("outcome")) for item in outcomes
                   if isinstance(item, Mapping) and item.get("outcome") in UNRESOLVED_OUTCOMES})


__all__ = ["CANDIDATE_GAP_CODES", "CANDIDATE_KEYS", "CANDIDATE_OUTCOMES",
           "MAX_OUTCOME_RECORD_IDS", "RESOLVED", "RESOLVE_VARIANT_OPERATION",
           "SOFT_GAP_CODES", "UNRESOLVED_AMBIGUOUS", "UNRESOLVED_NOT_FOUND",
           "UNRESOLVED_OUTCOMES", "candidate_outcome", "unresolved_kinds"]
