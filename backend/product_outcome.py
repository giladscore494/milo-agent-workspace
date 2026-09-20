"""The ONE machine-readable product outcome, shared by both engines.

A run can finish for many reasons, and "the process exited zero" is not one of
the interesting ones. Before this module the semantic question -- *did this run
produce something worth having?* -- was answered in several places, each in its
own vocabulary:

* Swarm V2 owned a validated contract (``backend.engines.swarm_v2.outcome``)
  over ``status``/``result_kind``, decided once inside the engine;
* vehicle_catalog_v1 owned nothing: the worker read ``result["status"]``, fell
  back to a truthy ``result`` key, and a summary checkpoint could resume a run
  whose final builder said ``partial_success`` straight into durable
  ``completed``;
* the export envelope re-derived a THIRD classification from the stored row;
* Stage D asked only whether the durable status was ``completed`` -- so a run
  that technically finished and semantically produced nothing could pass.

This module is the single answer. It is engine-neutral, pure, and performs no
I/O. Every field is either a static allowlisted vocabulary entry, a count, or a
digest: no prompt, provider response, exception text, model prose, source
fragment or free-form reason ever reaches it, so a ProductOutcome is always
safe to record in an event, a probe record or an acceptance gate.

What it carries
---------------

``semantic_status``   what the run MEANS: complete / partial / unusable /
                      refused / not_produced.
``usability``         whether the result can be used at all: usable / partial /
                      unusable / refused / none.
``coverage``          how much of the product was produced and how much is
                      still outstanding.
``blocking``          the gaps and failures standing between this outcome and a
                      complete one, as static codes with counts.
``payload``           a SAFE reference to the final payload: presence, digest,
                      byte size and its top-level shape -- never its content.

The rule that made it necessary
-------------------------------

*Semantic success is never inferred from technical completion.* Every
derivation here starts from evidence that the product exists -- a verified
field, a merged model -- and every unresolved item is a demotion. A declared
status may only ever LOWER the outcome (it is a floor, never a lift):
``complete`` with outstanding review items is ``partial``, and an unknown
declared status is ``partial``, not ``complete``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

# --- static vocabulary -------------------------------------------------------

#: What a finished run MEANS, independent of engine and of durable run status.
SEMANTIC_STATUSES = ("complete", "partial", "unusable", "refused", "not_produced")

#: Whether the result can be used. Deliberately separate from the semantic
#: status: ``unusable`` and ``refused`` are both "nothing you can act on", but
#: one is a run that tried and produced nothing while the other is a run the
#: system declined to perform.
USABILITY = ("usable", "partial", "unusable", "refused", "none")

#: semantic status -> the usability it implies. One mapping, so the two can
#: never be set to a contradicting pair.
_USABILITY_OF: Mapping[str, str] = {
    "complete": "usable",
    "partial": "partial",
    "unusable": "unusable",
    "refused": "refused",
    "not_produced": "none",
}

#: The durable run status a PRODUCT outcome maps to.
#:
#: ``not_produced`` appears here for the one case where it IS a product claim:
#: the engine ran, finished, and reported that it produced nothing at all. That
#: is a failed run. The non-product terminals -- cancellation, timeout, budget
#: exhaustion -- also carry ``not_produced``, but they are never finalized
#: through this table: their durable status comes from the terminal REASON,
#: which only the finalizer decides.
_DURABLE_PRODUCT_STATUS: Mapping[str, str] = {
    "complete": "completed",
    "partial": "partial_success",
    "unusable": "partial_success",
    "refused": "failed",
    "not_produced": "failed",
}

#: The outcomes a consumer may act on. ``unusable`` is deliberately outside:
#: a run that verified nothing is truthful, terminal, and not a product.
ACCEPTABLE_USABILITY = frozenset({"usable", "partial"})

#: Every blocking code that may appear in ``ProductOutcome.blocking``. Static
#: by construction: a code is chosen by application code from this set, never
#: composed from a message, a model output or an exception.
BLOCKING_CODES = frozenset({
    "OUTSTANDING_REVIEW_ITEMS",   # items a human still has to resolve
    "TASK_FAILURES",              # planned work that did not complete
    "COVERAGE_GAPS",              # planned scope with no result at all
    "UNVERIFIED_CLAIMS",          # claims that did not reach a verified verdict
    "CONFLICTING_CLAIMS",         # contradicting claims left unresolved
    "REJECTED_ITEMS",             # candidates the pipeline actively rejected
    "FAILED_AGENTS",              # V1 agents/chunks that failed
    "DEGRADED_VERIFICATION",      # verification ran partially or not at all
    "DEGRADED_ENRICHMENT",        # enrichment ran partially or not at all
    "NO_USABLE_RESULT",           # nothing usable was produced or disproved
    "NO_PRODUCT_PAYLOAD",         # no final payload was recorded at all
    "OUTCOME_CONTRACT_VIOLATION", # the payload is not one the contract emits
    "ENGINE_REPORTED_FAILURE",    # the engine itself reported a failure
    "REFUSED_BEFORE_EXECUTION",   # a gate refused before the product existed
    "RUN_NOT_PRODUCED",           # cancelled / timed out / budget exhausted
})

#: The engines this repository ships. Only ``swarm_v2`` owns a product
#: contract of its own; everything else is read through the declared-envelope
#: reader, which never believes a claim upwards.
KNOWN_ENGINES = frozenset({"vehicle_catalog_v1", "swarm_v2"})

#: The engine label recorded on an outcome is bounded and character-filtered,
#: because a workflow key reaches it from server-owned routing and an outcome
#: record is durable evidence.
MAX_ENGINE_NAME_CHARS = 64
_ENGINE_NAME_SAFE = re.compile(r"[^A-Za-z0-9_]")

#: V1 records its own status inside its result envelope. These are the values
#: that CLAIM a complete product; everything else is at best partial. The claim
#: is only ever used to lower the derived outcome, never to raise it.
_V1_COMPLETE_CLAIMS = frozenset({"complete", "success"})
_V1_FAILURE_CLAIMS = frozenset({"failed"})

#: V1 model verdicts that mean "this model is NOT settled".
_V1_UNSETTLED_VERDICTS = frozenset({"partial", "needs_review"})
_V1_REJECTED_VERDICT = "rejected"

#: V1 pipeline_quality values that mean a stage did not fully run.
_V1_DEGRADED_STAGE = frozenset({"partial", "failed", "needs_review", "rejected"})

#: Bounds on the SHAPE record of a payload reference. The keys are ours, but a
#: reference is a safety object: it is bounded and character-filtered so it can
#: never become a channel for payload content.
MAX_SHAPE_KEYS = 24
MAX_SHAPE_KEY_CHARS = 48
_SHAPE_KEY_SAFE = re.compile(r"[^A-Za-z0-9_]")


class ProductOutcomeError(ValueError):
    """A safe, provider-neutral canonical-outcome violation."""


# --- value objects -----------------------------------------------------------


@dataclass(frozen=True)
class Coverage:
    """How much of the product exists, and how much is still outstanding.

    ``produced`` counts units the run actually established (a verified field
    entry, a merged and settled model). ``outstanding`` counts units the run
    knows it owes: review items, failures, gaps, conflicts. Both are counts of
    application facts; neither is read from a model.
    """

    produced: int = 0
    outstanding: int = 0

    def __post_init__(self) -> None:
        if self.produced < 0 or self.outstanding < 0:
            raise ProductOutcomeError("coverage counts cannot be negative")

    @property
    def total(self) -> int:
        return self.produced + self.outstanding

    @property
    def ratio(self) -> float | None:
        """Produced share of everything known about, or None when nothing is.

        A run that established nothing AND owes nothing has no coverage ratio.
        Returning 1.0 there would say "fully covered" about a run that did not
        cover anything, which is exactly the inference this module exists to
        prevent.
        """
        return None if self.total == 0 else round(self.produced / self.total, 6)

    def as_record(self) -> dict[str, Any]:
        return {"produced": self.produced, "outstanding": self.outstanding,
                "ratio": self.ratio}


@dataclass(frozen=True)
class BlockingItem:
    """One class of thing standing between this outcome and a complete one."""

    code: str
    count: int = 1

    def __post_init__(self) -> None:
        if self.code not in BLOCKING_CODES:
            raise ProductOutcomeError("blocking code is not allowlisted")
        if self.count < 1:
            raise ProductOutcomeError("a blocking item must have a positive count")

    def as_record(self) -> dict[str, Any]:
        return {"code": self.code, "count": self.count}


@dataclass(frozen=True)
class PayloadReference:
    """A SAFE reference to a final payload: never the payload itself.

    The digest is what makes duplicate finalization decidable: two
    finalizations of the same product payload produce the same digest, so the
    finalizer can tell "the same decision again" from "a different decision"
    without storing or comparing the payload.
    """

    present: bool = False
    digest: str = ""
    byte_size: int = 0
    shape: tuple[str, ...] = ()

    def as_record(self) -> dict[str, Any]:
        return {"present": self.present, "digest": self.digest,
                "byte_size": self.byte_size, "shape": list(self.shape)}


ABSENT_PAYLOAD = PayloadReference()


def safe_payload_reference(payload: Any) -> PayloadReference:
    """Describe a payload without reproducing any of it.

    Anything that cannot be canonicalized as JSON is described as present with
    no digest rather than raising: a reference is evidence about a payload, and
    failing to describe one must never be able to fail a run.
    """
    if payload is None:
        return ABSENT_PAYLOAD
    try:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError):
        return PayloadReference(present=True)
    shape: tuple[str, ...] = ()
    if isinstance(payload, Mapping):
        keys = sorted(_SHAPE_KEY_SAFE.sub("", str(key))[:MAX_SHAPE_KEY_CHARS]
                      for key in payload.keys())
        shape = tuple(key for key in keys if key)[:MAX_SHAPE_KEYS]
    return PayloadReference(present=True,
                            digest=hashlib.sha256(encoded).hexdigest(),
                            byte_size=len(encoded), shape=shape)


@dataclass(frozen=True)
class ProductOutcome:
    """The canonical, machine-readable answer to "what did this run produce?"."""

    engine: str
    semantic_status: str
    coverage: Coverage = Coverage()
    blocking: tuple[BlockingItem, ...] = ()
    payload: PayloadReference = ABSENT_PAYLOAD
    #: The engine-specific classification, kept so the existing Swarm V2
    #: contract and the export envelope stay expressible. It is DERIVED, never
    #: an independent decision.
    result_kind: str | None = None

    def __post_init__(self) -> None:
        if self.semantic_status not in SEMANTIC_STATUSES:
            raise ProductOutcomeError("semantic status is not allowlisted")
        seen = Counter(item.code for item in self.blocking)
        duplicated = [code for code, count in seen.items() if count > 1]
        if duplicated:
            raise ProductOutcomeError("a blocking code may appear at most once")

    # -- derived views --------------------------------------------------------

    @property
    def usability(self) -> str:
        return _USABILITY_OF[self.semantic_status]

    @property
    def is_usable(self) -> bool:
        """Did this run produce something a consumer can act on?

        Terminal is not usable, exiting zero is not usable, and "every stage
        returned" is not usable. Only ``complete`` and ``partial`` are.
        """
        return self.usability in ACCEPTABLE_USABILITY

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.blocking)

    def durable_product_status(self) -> str:
        """The durable run status this PRODUCT outcome maps to.

        Only the finalizer calls this, and only for a PRODUCT claim. A
        cancellation, a timeout and a budget stop carry ``not_produced`` too,
        but they are terminalized from their reason, never from here.
        """
        try:
            return _DURABLE_PRODUCT_STATUS[self.semantic_status]
        except KeyError:  # pragma: no cover - the table is total
            raise ProductOutcomeError(
                "no durable product status for this outcome") from None

    def demoted_with(self, counts: Mapping[str, int]) -> "ProductOutcome":
        """Fold in blocking facts the payload itself does not carry.

        A resumed run knows things its final document does not: the checkpoint
        records the agents that failed before the crash. Folding them in can
        only ever LOWER the outcome -- a new blocking condition demotes
        ``complete`` to ``partial`` and raises the outstanding count -- so this
        is a floor, never a correction, and it can never turn a partial run
        into a complete one.
        """
        merged = {item.code: item.count for item in self.blocking}
        changed = False
        for code, count in counts.items():
            if count <= 0:
                continue
            if code not in BLOCKING_CODES:
                raise ProductOutcomeError("blocking code is not allowlisted")
            if count > merged.get(code, 0):
                merged[code] = count
                changed = True
        if not changed:
            return self
        semantic = self.semantic_status
        result_kind = self.result_kind
        if semantic == "complete":
            semantic, result_kind = "partial", "partial_result"
        outstanding = max(self.coverage.outstanding, sum(merged.values()))
        return ProductOutcome(
            engine=self.engine, semantic_status=semantic,
            coverage=Coverage(produced=self.coverage.produced, outstanding=outstanding),
            blocking=_blocking(merged), payload=self.payload,
            result_kind=result_kind)

    def as_record(self) -> dict[str, Any]:
        """The bounded JSON form recorded in events and acceptance evidence."""
        return {
            "engine": self.engine,
            "semantic_status": self.semantic_status,
            "usability": self.usability,
            "result_kind": self.result_kind,
            "coverage": self.coverage.as_record(),
            "blocking": [item.as_record() for item in self.blocking],
            "payload": self.payload.as_record(),
        }


def acceptance_problems(outcome: ProductOutcome, *,
                        require_complete: bool = False) -> list[str]:
    """Why this outcome is NOT semantically acceptable, as static sentences.

    Empty means acceptable. This is the one function an acceptance gate calls:
    "the worker exited zero", "the run row is terminal" and "the durable status
    is completed" are all answers to different questions, and none of them is
    an answer to this one.
    """
    problems: list[str] = []
    if not outcome.is_usable:
        problems.append(
            f"product outcome is {outcome.semantic_status!r} (usability "
            f"{outcome.usability!r}) — the run produced no usable result")
    elif require_complete and outcome.semantic_status != "complete":
        problems.append(
            f"product outcome is {outcome.semantic_status!r}, and this gate "
            "requires a complete product result")
    if outcome.is_usable and not outcome.payload.present:
        problems.append(
            "product outcome claims a usable result but no final payload was "
            "recorded — failing closed")
    for item in outcome.blocking:
        if item.code in {"OUTCOME_CONTRACT_VIOLATION", "NO_PRODUCT_PAYLOAD",
                         "ENGINE_REPORTED_FAILURE", "REFUSED_BEFORE_EXECUTION",
                         "RUN_NOT_PRODUCED"}:
            problems.append(f"blocking condition {item.code} (x{item.count})")
    return problems


# --- non-product outcomes ----------------------------------------------------


def refused_outcome(engine: str, *, code: str = "REFUSED_BEFORE_EXECUTION",
                    payload: Any = None) -> ProductOutcome:
    """A gate declined to produce a product. Not a failure of the product."""
    blocking_code = code if code in BLOCKING_CODES else "REFUSED_BEFORE_EXECUTION"
    return ProductOutcome(engine=_engine_name(engine), semantic_status="refused",
                          blocking=(BlockingItem(blocking_code),),
                          payload=safe_payload_reference(payload))


def not_produced_outcome(engine: str) -> ProductOutcome:
    """The run ended before a product existed: cancelled, timed out, stopped."""
    return ProductOutcome(engine=_engine_name(engine),
                          semantic_status="not_produced",
                          blocking=(BlockingItem("RUN_NOT_PRODUCED"),))


def _engine_name(engine: Any) -> str:
    """The engine label recorded on an outcome: bounded, never free text.

    An engine key this repository does not ship (an allowlisted harness
    engine, a future one) keeps its own name rather than collapsing into
    "unknown": the label is evidence about which engine answered, and losing
    it would make an acceptance record less specific than the run it
    describes. The charset and length bound are what keeps it safe.
    """
    name = _ENGINE_NAME_SAFE.sub("", str(engine or "").strip())[:MAX_ENGINE_NAME_CHARS]
    return name or "unknown"


def _blocking(counts: Mapping[str, int]) -> tuple[BlockingItem, ...]:
    """Build the blocking tuple in a deterministic (sorted-by-code) order."""
    return tuple(BlockingItem(code, count)
                 for code, count in sorted(counts.items()) if count > 0)


def _mapping_len(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def _is_catalog_document(final: Any) -> bool:
    """Is this the document ``build_final_json_python`` deterministically emits?

    Coverage may only be COUNTED from a document whose structure is known.
    Counting "zero merged models" out of a payload that was never a catalog
    document would classify the zero-cost staging engine, and every harness
    double, as having produced nothing.
    """
    return (isinstance(final, Mapping) and isinstance(final.get("models"), list)
            and isinstance(final.get("pipeline_quality"), Mapping))


# --- Swarm V2 ----------------------------------------------------------------


def derive_swarm_v2_outcome(output: Any) -> ProductOutcome:
    """Canonicalize a Swarm V2 product payload.

    The V2 contract is unchanged and still authoritative for V2: this function
    VALIDATES through it rather than re-deciding anything. A payload the
    contract would not have produced is ``refused``, which is exactly what the
    worker already did with ``SWARM_V2_OUTCOME_INVALID`` -- it is stated here
    in the shared vocabulary so an acceptance gate can see it.
    """
    from backend.engines.swarm_v2.outcome import (
        NO_USABLE_RESULT_CODE, ProductOutcomeError as V2OutcomeError,
        validate_product_outcome)

    reference = safe_payload_reference(output)
    try:
        validated = validate_product_outcome(output)
    except V2OutcomeError:
        return ProductOutcome(engine="swarm_v2", semantic_status="refused",
                              blocking=(BlockingItem("OUTCOME_CONTRACT_VIOLATION"),),
                              payload=reference)
    fields = output.get("fields") or {}
    review = output.get("needs_review") or []
    produced = sum(len(value) for value in fields.values()
                   if isinstance(value, (list, tuple)))
    outstanding_items = [item for item in review
                         if not (isinstance(item, Mapping)
                                 and item.get("code") == NO_USABLE_RESULT_CODE)]
    counts: dict[str, int] = {}
    if outstanding_items:
        counts["OUTSTANDING_REVIEW_ITEMS"] = len(outstanding_items)
    if validated.result_kind == "no_usable_result":
        counts["NO_USABLE_RESULT"] = 1
        semantic = "unusable"
    elif validated.result_kind == "partial_result":
        semantic = "partial"
    else:
        # ``usable_result`` and ``not_found`` are both complete answers: one
        # produced the product, the other PROVED there is none. Neither leaves
        # anything outstanding -- the contract refuses a complete outcome that
        # carries review items.
        semantic = "complete"
    return ProductOutcome(engine="swarm_v2", semantic_status=semantic,
                          coverage=Coverage(produced=produced,
                                            outstanding=len(outstanding_items)),
                          blocking=_blocking(counts), payload=reference,
                          result_kind=validated.result_kind)


# --- vehicle_catalog_v1 ------------------------------------------------------


def derive_vehicle_catalog_v1_outcome(result: Any, *, engine: str = "vehicle_catalog_v1") -> ProductOutcome:
    """Canonicalize a vehicle_catalog_v1 run result envelope.

    V1 has no contract of its own, so this reads only facts the deterministic
    Python assembly wrote: the merged models and their verification verdicts,
    the review/rejected views, the failed agents, and the pipeline_quality
    record. The engine's declared ``status`` is a FLOOR -- a declared
    ``complete`` alongside outstanding items derives ``partial``, and a
    declared status this module does not know derives ``partial`` rather than
    being trusted.

    This is what stops a summary phase or a final checkpoint from upgrading a
    run: neither of them can remove a failed agent, a needs_review model or a
    degraded verifier from the record the outcome is read from.

    It doubles as the reader for any engine with no contract of its own (an
    allowlisted harness engine, a future one): a payload that is not the
    deterministic catalog document cannot be counted, so only the declared
    status is read -- downwards.
    """
    name = _engine_name(engine)
    reference = safe_payload_reference(result)
    if not isinstance(result, Mapping):
        return ProductOutcome(engine=name,
                              semantic_status="not_produced",
                              blocking=(BlockingItem("NO_PRODUCT_PAYLOAD"),),
                              payload=reference)
    declared = result.get("status")
    declared_text = declared if isinstance(declared, str) else ""
    final = result.get("result")
    if not isinstance(final, Mapping) and ("models" in result or "pipeline_quality" in result):
        # A caller holding the FINAL DOCUMENT rather than the run envelope
        # that wraps it (the export projection reads the stored payload
        # directly). Same facts, one level up.
        final = result
    if declared_text in _V1_FAILURE_CLAIMS or not isinstance(final, Mapping):
        # The engine said it failed, or recorded no final document at all.
        # Either way there is no product, and the absence is never read as a
        # quiet success.
        code = ("ENGINE_REPORTED_FAILURE" if declared_text in _V1_FAILURE_CLAIMS
                else "NO_PRODUCT_PAYLOAD")
        return ProductOutcome(engine=name,
                              semantic_status="not_produced",
                              blocking=(BlockingItem(code),), payload=reference)

    if not _is_catalog_document(final):
        # A final document this engine's deterministic assembly did not
        # produce -- the zero-cost staging engine, a harness double -- cannot
        # be counted, so the DECLARED status is the only evidence there is.
        # It is still only ever believed downwards: an unrecognised claim is
        # ``partial``, never ``complete``.
        if declared_text in _V1_COMPLETE_CLAIMS:
            return ProductOutcome(engine=name,
                                  semantic_status="complete",
                                  coverage=Coverage(produced=1),
                                  payload=reference, result_kind="usable_result")
        return ProductOutcome(engine=name, semantic_status="partial",
                              coverage=Coverage(produced=1, outstanding=1),
                              blocking=(BlockingItem("OUTSTANDING_REVIEW_ITEMS"),),
                              payload=reference, result_kind="partial_result")

    models = final.get("models") if isinstance(final.get("models"), list) else []
    settled = 0
    unsettled = 0
    rejected = 0
    for model in models:
        verdict = model.get("verification_status") if isinstance(model, Mapping) else None
        if verdict == _V1_REJECTED_VERDICT:
            rejected += 1
        elif verdict in _V1_UNSETTLED_VERDICTS:
            unsettled += 1
        else:
            settled += 1

    # The review/rejected VIEWS are recomputed by the deterministic source
    # policy AFTER the final builder decided its status, so they can carry
    # items the declared status never saw. Taking the larger of the view and
    # the per-model count means a later downgrade can only ever lower the
    # outcome.
    review_count = max(unsettled, _mapping_len(final.get("needs_review")))
    rejected_count = max(rejected, _mapping_len(final.get("rejected")))
    failed_agents = _mapping_len(final.get("failed_agents"))

    quality = final.get("pipeline_quality")
    quality = quality if isinstance(quality, Mapping) else {}
    degraded_verification = str(quality.get("verifier") or "") in _V1_DEGRADED_STAGE
    degraded_enrichment = str(quality.get("technical_enrichment") or "") in _V1_DEGRADED_STAGE

    counts: dict[str, int] = {}
    if review_count:
        counts["OUTSTANDING_REVIEW_ITEMS"] = review_count
    if rejected_count:
        counts["REJECTED_ITEMS"] = rejected_count
    if failed_agents:
        counts["FAILED_AGENTS"] = failed_agents
    if degraded_verification:
        counts["DEGRADED_VERIFICATION"] = 1
    if degraded_enrichment:
        counts["DEGRADED_ENRICHMENT"] = 1

    outstanding = review_count + rejected_count + failed_agents
    produced = settled
    if produced == 0:
        counts["NO_USABLE_RESULT"] = 1
        return ProductOutcome(engine=name,
                              semantic_status="unusable",
                              coverage=Coverage(produced=0, outstanding=outstanding),
                              blocking=_blocking(counts), payload=reference,
                              result_kind="no_usable_result")

    # A usable product exists. It is COMPLETE only when nothing is outstanding,
    # no stage was degraded, and the engine itself claimed completeness.
    complete = (not counts and declared_text in _V1_COMPLETE_CLAIMS
                and str(final.get("status") or "") in _V1_COMPLETE_CLAIMS)
    semantic = "complete" if complete else "partial"
    return ProductOutcome(engine=name, semantic_status=semantic,
                          coverage=Coverage(produced=produced, outstanding=outstanding),
                          blocking=_blocking(counts), payload=reference,
                          result_kind=("usable_result" if complete else "partial_result"))


# --- dispatch ----------------------------------------------------------------


def derive_product_outcome(engine: Any, output: Any) -> ProductOutcome:
    """The canonical outcome for ONE engine's final payload.

    Swarm V2 is validated through its own contract, which stays authoritative
    for V2. Every other engine is read through the declared-envelope reader,
    which counts a catalog document when it sees one and otherwise believes
    the declared status only downwards. No engine is ever classified from
    dictionary truthiness.
    """
    name = _engine_name(engine)
    if name == "swarm_v2":
        return derive_swarm_v2_outcome(output)
    return derive_vehicle_catalog_v1_outcome(output, engine=name)


def outcome_from_record(record: Any) -> ProductOutcome:
    """Rebuild an outcome from its ``as_record`` form (bounded, fail-closed).

    Acceptance gates read a recorded outcome rather than a live object, and a
    record that is not exactly one ``as_record`` produces is refused instead of
    being partially believed.
    """
    if not isinstance(record, Mapping):
        raise ProductOutcomeError("product outcome record must be a mapping")
    semantic = record.get("semantic_status")
    if not isinstance(semantic, str) or semantic not in SEMANTIC_STATUSES:
        raise ProductOutcomeError("recorded semantic status is not allowlisted")
    coverage_record = record.get("coverage")
    coverage_record = coverage_record if isinstance(coverage_record, Mapping) else {}
    try:
        coverage = Coverage(produced=int(coverage_record.get("produced") or 0),
                            outstanding=int(coverage_record.get("outstanding") or 0))
    except (TypeError, ValueError) as exc:
        raise ProductOutcomeError("recorded coverage is not countable") from exc
    blocking: list[BlockingItem] = []
    for item in record.get("blocking") or []:
        if not isinstance(item, Mapping):
            raise ProductOutcomeError("recorded blocking item is not an object")
        code = item.get("code")
        if not isinstance(code, str) or code not in BLOCKING_CODES:
            raise ProductOutcomeError("recorded blocking code is not allowlisted")
        try:
            blocking.append(BlockingItem(code, int(item.get("count") or 1)))
        except (TypeError, ValueError) as exc:
            raise ProductOutcomeError("recorded blocking count is not an integer") from exc
    payload_record = record.get("payload")
    payload_record = payload_record if isinstance(payload_record, Mapping) else {}
    shape = payload_record.get("shape")
    payload = PayloadReference(
        present=bool(payload_record.get("present")),
        digest=str(payload_record.get("digest") or ""),
        byte_size=int(payload_record.get("byte_size") or 0),
        shape=tuple(str(key) for key in shape) if isinstance(shape, (list, tuple)) else ())
    result_kind = record.get("result_kind")
    return ProductOutcome(engine=_engine_name(record.get("engine")),
                          semantic_status=semantic, coverage=coverage,
                          blocking=tuple(blocking), payload=payload,
                          result_kind=result_kind if isinstance(result_kind, str) else None)


__all__ = [
    "ABSENT_PAYLOAD", "ACCEPTABLE_USABILITY", "BLOCKING_CODES", "BlockingItem",
    "Coverage", "KNOWN_ENGINES", "PayloadReference", "ProductOutcome",
    "ProductOutcomeError", "SEMANTIC_STATUSES", "USABILITY",
    "acceptance_problems", "derive_product_outcome",
    "derive_swarm_v2_outcome", "derive_vehicle_catalog_v1_outcome",
    "not_produced_outcome", "outcome_from_record", "refused_outcome",
    "safe_payload_reference",
]
