"""Phase 2: the vehicle-centric view of a Swarm V2 catalog result.

Why this exists
---------------

The Swarm V2 result groups values BY FIELD under the claim's entity, and a
Government claim's entity is the canonical MODEL at one year: every 4RUNNER
2026 variant of run 6825eb96 shares one entity, so ``result.fields.trim`` is a
list of eight trims and nothing says which trim belongs to which vehicle. This
module answers that question, and only that one, with ADDITIVE keys:

``vehicles``           one entry per register row a task RESOLVED
``unresolved_groups``  every ambiguous / not-found candidate, on its own
``summary``            counts of the two lists

The existing keys (``status``, ``result_kind``, ``fields``, ``needs_review``,
``candidate_outcomes``) are neither read back nor changed.

Where identity comes from, and where it never comes from
--------------------------------------------------------

* A vehicle EXISTS because a typed ``candidate_outcomes`` entry says a task's
  ``resolve_variant`` call resolved to exactly one register row
  (``outcome == "resolved"``, one ``record_ids`` entry). Its key is that row's
  own ``upstream_record_id``, and its identity is the call's server-resolved
  arguments the outcome carries.
* A value BELONGS to a vehicle only when its evidence provenance says so: the
  claim was recorded under a task that resolved that row (``task_id``) AND its
  record locator names that row (``["record_field", "<snapshot>:<row>", ...]``).
  Evidence without a record locator -- a web page, a pre-R3 claim -- is about
  no particular row and stays where it always was, in ``fields``.
* Nothing is read from task output, model text, a goal string or an entity
  spelling. An ambiguous or not-found candidate is never merged into a
  vehicle and never carries a value: it appears only in its own group.

Pure: no I/O, no model, no network, no database read, no clock and no
randomness. The same inputs produce byte-identical output, because every list
is sorted on a stable key and every set is emitted sorted.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from backend.engines.swarm_v2.contracts import EvidenceReference, VerificationVerdict
from backend.engines.swarm_v2.current_verdict import current_verdict_by_claim
from backend.engines.swarm_v2.evidence_contracts import (EvidenceContractError,
                                                         parse_locator_key)
from backend.engines.swarm_v2.resolution import (CANDIDATE_KEYS, RESOLVED, SOFT_GAP_CODES,
                                                 UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND)

#: The identity every vehicle and every unresolved group states, in this order.
#: `resolution.CANDIDATE_KEYS` is the operation's input vocabulary; a key the
#: outcomes do not state is present as None rather than absent.
IDENTITY_KEYS = CANDIDATE_KEYS

#: Verdicts a vehicle field may be shown with. A rejected claim, a claim with
#: no current verdict and an unsupported one never become a value.
SHOWN_VERDICTS = ("verified", "needs_review")

#: Static per-vehicle review codes this module adds. Task-level codes (a task
#: failure, a hard coverage gap) are passed through as the engine wrote them.
VEHICLE_REVIEW_CODES = frozenset({
    "FIELD_NEEDS_REVIEW",           # the field's value is awaiting review
    "FIELD_REJECTED",               # a claim for the field was rejected
    "FIELD_UNVERIFIED",             # a claim for the field has no verified verdict
    "FIELD_VALUES_DISAGREE",        # two shown claims state different values
    "IDENTITY_ARGUMENTS_DISAGREE",  # two resolutions of one row were asked differently
})

UNRESOLVED_GROUP_OUTCOMES = (UNRESOLVED_AMBIGUOUS, UNRESOLVED_NOT_FOUND)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str)


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _record_of(evidence: EvidenceReference) -> str | None:
    """The register row a claim's locator names, or None.

    Only a `record_field` locator names a row. Its record id is the catalog's
    `record_locator_id` -- `<snapshot_key>:<upstream_record_id>` -- so the row
    is the text after the LAST colon (an upstream id holds none).
    """
    if not evidence.locator:
        return None
    try:
        locator = parse_locator_key(evidence.locator)
    except EvidenceContractError:
        return None
    if locator.kind != "record_field":
        return None
    _, separator, row = locator.record_id.rpartition(":")
    return row if separator and row else None


def _merged_identity(candidates: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], bool]:
    """The identity every candidate agrees on, and whether any key disagreed.

    A key stated by some candidates and not by others keeps the stated value;
    a key stated with two different values is None and reported.
    """
    stated: dict[str, set[str]] = {key: set() for key in IDENTITY_KEYS}
    values: dict[tuple[str, str], Any] = {}
    for candidate in candidates:
        for key in IDENTITY_KEYS:
            value = candidate.get(key) if isinstance(candidate, Mapping) else None
            if value is None:
                continue
            stated[key].add(_canonical(value))
            values[(key, _canonical(value))] = value
    identity: dict[str, Any] = {}
    disagree = False
    for key in IDENTITY_KEYS:
        if len(stated[key]) == 1:
            (only,) = stated[key]
            identity[key] = values[(key, only)]
        else:
            identity[key] = None
            disagree = disagree or len(stated[key]) > 1
    return identity, disagree


class VehicleCatalogResultAssembler:
    """Assemble ``vehicles``, ``unresolved_groups`` and ``summary``. Pure."""

    def assemble(self, *, evidence: Iterable[EvidenceReference],
                 verdicts: Iterable[VerificationVerdict],
                 candidate_outcomes: Iterable[Mapping[str, Any]],
                 coverage_gaps: Iterable[Mapping[str, Any]] = (),
                 task_failures: Iterable[Mapping[str, Any]] = ()) -> dict[str, Any]:
        outcomes = [item for item in candidate_outcomes if isinstance(item, Mapping)]
        verdict_by_claim = current_verdict_by_claim(list(verdicts))
        vehicles = self._vehicles(outcomes, list(evidence), verdict_by_claim,
                                  self._task_codes(coverage_gaps, task_failures))
        groups = self._unresolved_groups(outcomes)
        return {
            "vehicles": vehicles,
            "unresolved_groups": groups,
            "summary": {
                "vehicles_resolved": len(vehicles),
                "vehicles_with_review": sum(1 for item in vehicles if item["needs_review"]),
                "unresolved_ambiguous": sum(1 for item in groups
                                            if item["outcome"] == UNRESOLVED_AMBIGUOUS),
                "unresolved_not_found": sum(1 for item in groups
                                            if item["outcome"] == UNRESOLVED_NOT_FOUND),
            },
        }

    # --- vehicles --------------------------------------------------------------

    @staticmethod
    def _task_codes(coverage_gaps: Iterable[Mapping[str, Any]],
                    task_failures: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
        """Task id -> the engine's own codes for it, minus the candidate gaps.

        `CANDIDATE_UNRESOLVED_*` is about an unresolved candidate, which has
        its own group; it is never attributed to a vehicle the same task
        resolved with another call.
        """
        codes: dict[str, set[str]] = {}
        for item in [*coverage_gaps, *task_failures]:
            if not isinstance(item, Mapping):
                continue
            task_id, code = _text(item.get("task_id")), _text(item.get("code"))
            if task_id and code and code not in SOFT_GAP_CODES:
                codes.setdefault(task_id, set()).add(code)
        return codes

    def _vehicles(self, outcomes: list[Mapping[str, Any]], evidence: list[EvidenceReference],
                  verdict_by_claim: Mapping[str, VerificationVerdict],
                  task_codes: Mapping[str, set[str]]) -> list[dict[str, Any]]:
        # One vehicle per resolved row. A resolved outcome names exactly one
        # row by construction (`resolution.candidate_outcome`: resolved means
        # the register matched exactly one variant); one that does not is not
        # linkable and is left out rather than guessed at.
        resolutions: dict[str, list[Mapping[str, Any]]] = {}
        for item in outcomes:
            record_ids = item.get("record_ids")
            if item.get("outcome") != RESOLVED or not isinstance(record_ids, list) \
                    or len(record_ids) != 1 or not _text(record_ids[0]) \
                    or not _text(item.get("task_id")):
                continue
            resolutions.setdefault(record_ids[0], []).append(item)
        owners = {(str(item["task_id"]), record) for record, items in resolutions.items()
                  for item in items}

        attached: dict[str, list[EvidenceReference]] = {}
        for claim in evidence:
            record = _record_of(claim)
            if record is not None and (claim.task_id, record) in owners:
                attached.setdefault(record, []).append(claim)

        vehicles = []
        for record in sorted(resolutions):
            items = resolutions[record]
            identity, disagree = _merged_identity(item.get("candidate") or {} for item in items)
            review: set[tuple[str, str, str]] = set()
            if disagree:
                review.add(("IDENTITY_ARGUMENTS_DISAGREE", "", ""))
            task_ids = sorted({str(item["task_id"]) for item in items})
            for task_id in task_ids:
                review.update((code, "", task_id) for code in task_codes.get(task_id, ()))
            fields = self._fields(attached.get(record, []), verdict_by_claim, review)
            vehicles.append({
                "vehicle_key": record,
                "identity": identity,
                "fields": fields,
                "needs_review": [self._review_entry(*entry) for entry in sorted(review)],
                "sources": sorted({entry["source_id"] for field in fields.values()
                                   for entry in field["provenance"]}),
            })
        return vehicles

    @staticmethod
    def _fields(claims: list[EvidenceReference],
                verdict_by_claim: Mapping[str, VerificationVerdict],
                review: set[tuple[str, str, str]]) -> dict[str, dict[str, Any]]:
        shown: dict[str, list[tuple[EvidenceReference, str]]] = {}
        for claim in sorted(claims, key=lambda item: (item.field, item.claim_id)):
            verdict = verdict_by_claim.get(claim.claim_id)
            state = verdict.verdict if verdict is not None else None
            if state == "verified" and claim.supported:
                shown.setdefault(claim.field, []).append((claim, "verified"))
            elif state == "needs_review":
                shown.setdefault(claim.field, []).append((claim, "needs_review"))
            elif state == "rejected":
                review.add(("FIELD_REJECTED", claim.field, ""))
            else:
                review.add(("FIELD_UNVERIFIED", claim.field, ""))
        fields: dict[str, dict[str, Any]] = {}
        for name in sorted(shown):
            entries = shown[name]
            if len({_canonical([claim.value, claim.unit]) for claim, _ in entries}) != 1:
                # Two statements about ONE row's field that disagree: neither
                # is shown as the vehicle's value, and the disagreement is said.
                review.add(("FIELD_VALUES_DISAGREE", name, ""))
                continue
            verdict = "verified" if all(state == "verified" for _, state in entries) \
                else "needs_review"
            if verdict == "needs_review":
                review.add(("FIELD_NEEDS_REVIEW", name, ""))
            fields[name] = {
                "value": entries[0][0].value,
                "verdict": verdict,
                "provenance": [{"claim_id": claim.claim_id, "source_id": claim.source_id,
                                "task_id": claim.task_id} for claim, _ in entries],
            }
        return fields

    @staticmethod
    def _review_entry(code: str, field: str, task_id: str) -> dict[str, str]:
        entry = {"code": code}
        if field:
            entry["field"] = field
        if task_id:
            entry["task_id"] = task_id
        return entry

    # --- unresolved groups -----------------------------------------------------

    @staticmethod
    def _unresolved_groups(outcomes: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Every unresolved candidate, grouped, and never merged into a vehicle.

        Two tasks that asked about the SAME rows and got the same answer are
        one group (run 6825eb96: t04 and t05 -> 37350 / 37439). A not-found
        answer names no row, so it groups by the identity that was asked.
        """
        groups: dict[str, dict[str, Any]] = {}
        for item in outcomes:
            outcome = item.get("outcome")
            if outcome not in UNRESOLVED_GROUP_OUTCOMES or not _text(item.get("task_id")):
                continue
            raw_ids = item.get("record_ids")
            record_ids = sorted({str(value) for value in raw_ids
                                 if _text(value)}) if isinstance(raw_ids, list) else []
            candidate = item.get("candidate") if isinstance(item.get("candidate"), Mapping) else {}
            basis = record_ids if record_ids else {key: candidate.get(key)
                                                   for key in IDENTITY_KEYS}
            key = _canonical([outcome, basis])
            group = groups.setdefault(key, {"outcome": outcome, "candidates": [],
                                            "record_ids": record_ids, "task_ids": set()})
            group["candidates"].append(candidate)
            group["task_ids"].add(str(item["task_id"]))
        result = []
        for key in sorted(groups):
            group = groups[key]
            identity, _ = _merged_identity(group["candidates"])
            result.append({"outcome": group["outcome"], "candidate": identity,
                           "record_ids": group["record_ids"],
                           "task_ids": sorted(group["task_ids"])})
        return result


__all__ = ["IDENTITY_KEYS", "SHOWN_VERDICTS", "UNRESOLVED_GROUP_OUTCOMES",
           "VEHICLE_REVIEW_CODES", "VehicleCatalogResultAssembler"]
