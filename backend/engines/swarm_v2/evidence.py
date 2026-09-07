"""Lease-guarded Evidence Board backed by the existing evidence tables.

Only structured findings, brief rationale summaries, and bounded verbatim
source evidence fragments cross this boundary.  Model scratch work, provider
errors, credentials, and chain-of-thought are rejected rather than copied
into durable evidence.

Evidence fragments are captured at acquisition time from real tool material
and land in the service-only relation public.source_evidence_fragments --
never in the browser-visible source metadata, the claim value, or a run
event.

There are two acquisition paths and they must not be confused:

*   The R3 path (`record_evidence_bundle`).  A trusted mapper turned ONE
    validated ToolCallRecord into a versioned source, structured facts with
    units, and focused fragments carrying an exact locator.  This is the only
    path new evidence may take.
*   The pre-R3 generic path (`record_source_with_evidence`).  It scans a tool
    result for text-shaped keys and stores a 400-character prefix.  It is
    retained ONLY so historical callers and their durable rows keep working;
    R3-qualified evidence never uses it, and nothing falls back to it.

The R3 additions -- the source version, the fragment locator/type and the
claim's evidence locator -- are INTERNAL durable columns added here, by the
trusted board.  They are deliberately not fields of SourceCreate/ClaimCreate,
because those schemas are the worker-facing HTTP contract whose rows are
echoed into browser-visible run events (backend/main.py).  Keeping the R3
provenance on this side of the boundary is what lets grounding receive it
while the public API surface stays exactly as it was.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from backend.schemas import ClaimCreate, ConflictCreate, SourceCreate, ToolUsageCreate

from .evidence_contracts import (FRAGMENT_TYPES, MAX_LOCATOR_KEY_CHARS, EvidenceBundle,
                                 FocusedEvidenceFragment, SourceVersion,
                                 StructuredEvidenceFact, VersionedEvidenceSource,
                                 revalidate_evidence_bundle)
from .evidence_mapping import AcquiredEvidence
from .fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENTS_PER_SOURCE, extract_source_fragments,
                        fragment_content_hash, normalize_fragment_text)
from .normalization import (SCOPE_NORMALIZATION_VERSION, CanonicalScope, canonical_scope_hash,
                            canonical_scope_key, canonical_value_key)


class EvidenceValidationError(ValueError):
    """A safe, provider-neutral evidence validation failure."""


@dataclass(frozen=True)
class WorkerLease:
    run_id: UUID
    worker_id: str
    attempt: int
    lease_token: str

    def __post_init__(self) -> None:
        if not self.worker_id or not self.lease_token or self.attempt < 1:
            raise EvidenceValidationError("complete worker lease is required")


_FORBIDDEN_KEYS = frozenset({
    "api_key", "authorization", "chain_of_thought", "credentials", "exception",
    "lease_token", "password", "provider_detail", "raw_error", "secret", "token",
})
_FORBIDDEN_TEXT = ("chain of thought", "hidden reasoning", "secret sentinel", "begin private key")


def _unsafe_key(key: Any) -> bool:
    folded = str(key).casefold()
    return (folded in _FORBIDDEN_KEYS or "api_key" in folded or "password" in folded or
            "credential" in folded or "authorization" in folded or
            folded.endswith("_secret") or folded in {"access_token", "refresh_token"})


def safe_durable_value(value: Any) -> Any:
    """Return JSON-compatible evidence or fail with a sanitized error."""
    if isinstance(value, Mapping):
        for key in value:
            if _unsafe_key(key):
                raise EvidenceValidationError("unsafe evidence metadata rejected")
        return {str(key): safe_durable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_durable_value(item) for item in value]
    if isinstance(value, str):
        folded = value.casefold()
        if any(marker in folded for marker in _FORBIDDEN_TEXT):
            raise EvidenceValidationError("unsafe evidence text rejected")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise EvidenceValidationError("evidence must be JSON structured")


# Durable fragment safety is a FINITE, mechanical marker set: credential and
# hidden-reasoning shapes that can never be ordinary source prose.  Quoted
# source text that merely reasons ("therefore", "we concluded") is legitimate
# evidence and is never censored here.
# supabase/migrations/20260828000200_source_evidence_fragments.sql enforces
# the same set at the durable boundary, so a direct RPC call cannot bypass it.
_FRAGMENT_SECRET_MARKERS = (
    "-----begin", "-----end", "api_key=", "apikey=", "aws_secret_access_key",
    "authorization:", "client_secret", "lease_token", "password=", "private_key",
    "refresh_token", "secret_key", "x-api-key",
)


def safe_fragment_text(value: Any) -> str:
    """Return bounded, safe durable evidence text or fail with a safe error.

    This is the persistence boundary, so it REJECTS rather than repairs: an
    over-long fragment is a caller bug (acquisition already bounds text in
    .fragments), and silently truncating here would hide it.
    """
    if not isinstance(value, str):
        raise EvidenceValidationError("evidence fragment text must be a string")
    text = normalize_fragment_text(value)
    if not text:
        raise EvidenceValidationError("evidence fragment text must not be empty")
    if len(text) > MAX_FRAGMENT_CHARS:
        raise EvidenceValidationError("evidence fragment exceeds the durable size bound")
    safe_durable_value(text)  # the existing finite hidden-reasoning/sentinel policy
    folded = text.casefold()
    if any(marker in folded for marker in _FRAGMENT_SECRET_MARKERS):
        raise EvidenceValidationError("unsafe evidence fragment rejected")
    return text


def _key(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(safe_durable_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"{kind}:{hashlib.sha256(encoded.encode()).hexdigest()}"


# R3 provenance that participates in evidence IDENTITY when it is present.
# A key whose value is None is dropped from the identity payload, so a source
# or claim written by a pre-R3 release replays to exactly the same
# evidence_key it had before this contract existed.  When the value IS
# present it changes identity on purpose: evidence read from a different
# source version, or from a different record/field, is different evidence and
# must never be deduplicated onto an existing row.
_R3_IDENTITY_KEYS = ("source_version_kind", "source_version_id", "evidence_locator")


def _identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items()
            if value is not None or key not in _R3_IDENTITY_KEYS}


def _locator_key(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_LOCATOR_KEY_CHARS:
        raise EvidenceValidationError("evidence locator is outside the durable bound")
    return value


class EvidenceBoard:
    """Persist retry-safe evidence and maintain trace summaries on run_blackboard."""

    def __init__(self, repository: Any, lease: WorkerLease):
        self._repository = repository
        self.lease = lease
        self._sources: dict[str, dict[str, Any]] = {}
        self._claims: dict[str, dict[str, Any]] = {}
        self._conflicts: dict[str, dict[str, Any]] = {}

    @property
    def _lease_kwargs(self) -> dict[str, Any]:
        return {"worker_id": self.lease.worker_id, "attempt": self.lease.attempt,
                "lease_token": self.lease.lease_token}

    def record_tool_usage(self, usage: ToolUsageCreate, *, task_key: str) -> dict[str, Any]:
        payload = usage.model_dump(mode="json")
        # Reduce potentially hostile provider error objects before the
        # general evidence validator sees them.  Only a bounded code crosses
        # the persistence boundary; messages/details/sentinels are discarded.
        if payload.get("error") is not None:
            code = payload["error"].get("code") if isinstance(payload["error"], Mapping) else None
            raw_code = str(code or "TOOL_OPERATION_FAILED")
            safe_code = "".join(ch for ch in raw_code if ch.isascii() and (ch.isalnum() or ch in "_-"))[:80]
            payload["error"] = {"code": safe_code or "TOOL_OPERATION_FAILED"}
        payload = safe_durable_value(payload)
        payload.update(task_key=self._task(task_key),
                       idempotency_key=_key("tool", {"task_key": task_key, **payload}))
        return self._repository.create_tool_usage(self.lease.run_id, payload, **self._lease_kwargs)

    def record_source(self, source: SourceCreate, *, task_key: str,
                      version: SourceVersion | None = None) -> dict[str, Any]:
        """Persist source metadata, optionally pinned to its exact version.

        `version` comes from the trusted adapter/mapper, never from a model
        and never from the fragment: `retrieved_at` records when we looked and
        a content hash records what we quoted, so neither can stand in for
        which version of the source was read.  It participates in the
        source's durable identity, so acquiring the same source at a NEW
        version creates new provenance instead of merging into the old row.

        `version=None` keeps the exact pre-R3 behaviour and the exact pre-R3
        evidence_key, so historical callers and resumed legacy runs are
        unaffected.
        """
        if version is not None and not isinstance(version, SourceVersion):
            raise EvidenceValidationError("a trusted source version contract is required")
        payload = safe_durable_value(source.model_dump(mode="json"))
        payload.update(source_version_kind=version.kind if version else None,
                       source_version_id=version.identifier if version else None)
        payload.update(task_key=self._task(task_key),
                       evidence_key=_key("source", _identity({"task_key": task_key, **payload})))
        row = self._repository.create_source(self.lease.run_id, payload, **self._lease_kwargs)
        self._sources[str(row["id"])] = dict(row)
        return row

    def record_source_with_evidence(self, source: SourceCreate, tool_result: Mapping[str, Any], *,
                                    task_key: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Persist a source and the bounded evidence that supports it, in order.

        This is the acquisition-time entry point and it encodes the required
        ordering in one call -- source metadata durable first, then fragments
        bound to the durable source id it returned -- so no caller can persist
        a fragment against a source id it guessed, and evidence acquisition can
        never be deferred to the verifier.  The structured claim is recorded
        afterwards by the caller and continues to reference only source_id.
        """
        row = self.record_source(source, task_key=task_key)
        return row, self.record_source_evidence(row["id"], tool_result, task_key=task_key)

    def record_source_evidence(self, source_id: Any, tool_result: Mapping[str, Any], *,
                               task_key: str) -> list[dict[str, Any]]:
        """Capture bounded source evidence at acquisition time.

        `tool_result` MUST be the structured result a registered tool returned
        for `source_id` while the source was being recorded.  A worker/model
        completion is never a valid input: B2 has no path that asks a model to
        write the excerpt supporting its own claim.

        The caller records the source first, so every fragment is bound to a
        real durable source_id; free-floating excerpts are impossible.

        This is the PRE-R3 generic path: it takes a bounded prefix of whatever
        text-shaped key the result happens to carry, and it keeps neither a
        source version nor a locator.  It is retained for backward
        compatibility with historical callers only.  R3-qualified evidence is
        acquired through `record_evidence_bundle`, and an operation with no
        registered evidence mapper never falls back to here.
        """
        return [self.record_evidence_fragment(source_id, text, task_key=task_key,
                                              fragment_index=index)
                for index, text in enumerate(extract_source_fragments(tool_result))]

    def record_evidence_fragment(self, source_id: Any, fragment_text: str, *, task_key: str,
                                 fragment_index: int = 0, fragment_type: str | None = None,
                                 locator_key: str | None = None) -> dict[str, Any]:
        """Persist one bounded, source-bound fragment through the guarded RPC.

        Identity is stable provenance only -- source + task + the final bounded
        text's content hash, plus the R3 locator and fragment type when the
        fragment carries them -- so an exact replay of the same fragment for
        the same source, task and location returns the same durable row
        instead of a duplicate.  No timestamp, UUID, or call sequence enters
        the key.

        The locator is what keeps two IDENTICAL sentences read from two
        different records (or two different fields) as two distinct pieces of
        evidence: without it their content hashes would collide and the
        second one would silently vanish into the first.

        Locator and fragment type are all-or-nothing: an R3 fragment always
        has both, and a legacy fragment has neither.  A half-specified
        fragment is a caller bug and fails closed here and again in the RPC.
        """
        text = safe_fragment_text(fragment_text)
        if not isinstance(fragment_index, int) or isinstance(fragment_index, bool) or \
                not 0 <= fragment_index < MAX_FRAGMENTS_PER_SOURCE:
            raise EvidenceValidationError("fragment index is outside the durable bound")
        if (fragment_type is None) != (locator_key is None):
            raise EvidenceValidationError("a focused fragment requires both a type and a locator")
        content_hash = fragment_content_hash(text)
        identity = {"task_key": task_key, "source_id": str(source_id),
                    "content_hash": content_hash}
        if fragment_type is not None:
            if fragment_type not in FRAGMENT_TYPES:
                raise EvidenceValidationError("unknown evidence fragment type")
            identity.update(fragment_type=fragment_type, locator_key=_locator_key(locator_key))
        payload = {"source_id": str(source_id), "fragment_text": text,
                   "content_hash": content_hash, "fragment_index": fragment_index,
                   "fragment_type": fragment_type, "locator_key": locator_key,
                   "task_key": self._task(task_key),
                   "evidence_key": _key("fragment", identity)}
        return self._repository.record_evidence_fragment(self.lease.run_id, payload,
                                                         **self._lease_kwargs)

    def record_focused_fragment(self, source_id: Any, fragment: FocusedEvidenceFragment, *,
                                task_key: str) -> dict[str, Any]:
        """Persist ONE validated R3 fragment, with its locator and its type."""
        if not isinstance(fragment, FocusedEvidenceFragment):
            raise EvidenceValidationError("a validated focused evidence fragment is required")
        return self.record_evidence_fragment(
            source_id, fragment.text, task_key=task_key,
            fragment_index=fragment.fragment_index, fragment_type=fragment.fragment_type,
            locator_key=fragment.locator.locator_key)

    def record_claim(self, claim: ClaimCreate, *, task_key: str,
                     evidence_locator: str | None = None) -> dict[str, Any]:
        payload = safe_durable_value(claim.model_dump(mode="json"))
        # `evidence_locator` is the exact record/field or document span the
        # fact was read from.  It is an INTERNAL durable column (never a
        # ClaimCreate field, so it never reaches a browser-visible run event)
        # and it participates in identity, so the same value read from two
        # different records stays two claims.
        payload["evidence_locator"] = (None if evidence_locator is None
                                       else _locator_key(evidence_locator))
        # The idempotent evidence identity is derived from task provenance plus
        # the ORIGINAL claim payload only — exactly as before canonical scopes
        # existed — so a claim persisted by a pre-canonical release replays to
        # the same evidence_key.  Derived canonical metadata (and any future
        # SCOPE_NORMALIZATION_VERSION) must never change this identity.  A
        # claim with no locator drops the key entirely, so a pre-R3 claim
        # replays to exactly the evidence_key it already has.
        evidence_key = _key("claim", _identity({"task_key": task_key, **payload}))
        # Scope is exactly entity + field + market/geography + time.  Source,
        # confidence, run and task provenance remain attached to every claim.
        # The trusted canonical identity travels with the claim so the durable
        # conflict firewall validates the same scope equality as this board;
        # the original scope fields are stored untouched for provenance.
        scope = canonical_scope_key(entity=payload["entity_key"], field=payload["field_key"],
                                    geography=payload.get("geography"), market=payload.get("market"),
                                    time_scope=payload.get("time_scope") or {})
        payload.update(canonical_scope_hash=canonical_scope_hash(scope),
                       scope_normalization_version=SCOPE_NORMALIZATION_VERSION,
                       task_key=self._task(task_key), evidence_key=evidence_key)
        row = self._repository.create_claim(self.lease.run_id, payload, **self._lease_kwargs)
        self._claims[str(row["id"])] = dict(row)
        return row

    def record_evidence_bundle(self, bundle: EvidenceBundle, *,
                               task_key: str) -> AcquiredEvidence:
        """The R3 acquisition entry point: one validated bundle, one source.

        The write ORDER is part of the contract and is encoded here so no
        caller can get it wrong:

        1.  the versioned source, so every fragment and claim below is bound
            to a real durable source id that was never guessed;
        2.  its focused fragments, so the evidence a claim rests on is durable
            BEFORE the claim that cites it;
        3.  its structured facts as claims, each carrying its unit and the
            locator it was read from.

        Every write goes through the same lease-guarded, idempotent RPCs as
        every other evidence write, so a resumed run that already persisted
        the source and some of its fragments replays onto the same rows
        instead of duplicating them.

        The bundle is trusted mapper output, not model output: see
        .evidence_mapping for the only path that produces one.  It is still
        revalidated HERE, from a copy of its own data, immediately before the
        first write: whatever happened to the object between mapping and
        persistence, nothing that fails the contract now can cause any
        durable write -- not even the source row.
        """
        if not isinstance(bundle, EvidenceBundle):
            raise EvidenceValidationError("a validated evidence bundle is required")
        bundle = revalidate_evidence_bundle(bundle)
        descriptor = bundle.source
        source = SourceCreate(agent=descriptor.agent, url=descriptor.url, title=descriptor.title,
                              domain=descriptor.domain, source_type=descriptor.source_type,
                              source_strength=descriptor.source_strength,
                              source_date=descriptor.source_date, query=descriptor.query,
                              tool_operation=descriptor.tool_operation)
        row = self.record_source(source, task_key=task_key, version=descriptor.version)
        fragments = tuple(self.record_focused_fragment(row["id"], fragment, task_key=task_key)
                          for fragment in bundle.fragments)
        claims = tuple(self._record_fact(fact, source_row=row, descriptor=descriptor,
                                         task_key=task_key) for fact in bundle.facts)
        return AcquiredEvidence(source=row, fragments=fragments, claims=claims)

    def _record_fact(self, fact: StructuredEvidenceFact, *, source_row: Mapping[str, Any],
                     descriptor: VersionedEvidenceSource, task_key: str) -> dict[str, Any]:
        """Persist ONE structured fact as a durable claim of its own source.

        The unit travels from the fact into the claim untouched.  R3 never
        converts, normalizes or compares units -- that is R4.
        """
        if not isinstance(fact, StructuredEvidenceFact):
            raise EvidenceValidationError("a validated structured evidence fact is required")
        claim = ClaimCreate(entity_key=fact.entity_key, field_key=fact.field_key,
                            value=fact.value, unit=fact.unit, time_scope=dict(fact.time_scope),
                            geography=fact.geography, market=fact.market,
                            source_id=UUID(str(source_row["id"])),
                            source_strength=descriptor.source_strength,
                            confidence=descriptor.confidence, agent=descriptor.agent)
        return self.record_claim(claim, task_key=task_key,
                                 evidence_locator=fact.locator.locator_key)

    def detect_and_record_conflicts(self, *, task_key: str,
                                    rationale: str = "Contradictory values in the same evidence scope.") -> list[dict[str, Any]]:
        rationale = self._rationale(rationale)
        groups: dict[CanonicalScope, list[dict[str, Any]]] = {}
        for claim in self._claims.values():
            scope = canonical_scope_key(entity=claim["entity_key"], field=claim["field_key"],
                                        geography=claim.get("geography"), market=claim.get("market"),
                                        time_scope=claim.get("time_scope") or {})
            groups.setdefault(scope, []).append(claim)
        recorded = []
        for claims in groups.values():
            values = {canonical_value_key(item.get("value")) for item in claims}
            if len(claims) < 2 or len(values) < 2:
                continue
            # Formatting variants may differ across a group's claims; keying the
            # conflict on the lowest claim id keeps it insertion-order independent.
            claims = sorted(claims, key=lambda item: UUID(str(item["id"])))
            ids = [UUID(str(item["id"])) for item in claims]
            conflict = ConflictCreate(entity_key=claims[0]["entity_key"], field_key=claims[0]["field_key"],
                                      claim_ids=ids, rationale=rationale)
            payload = safe_durable_value(conflict.model_dump(mode="json"))
            payload.update(task_key=self._task(task_key), evidence_key=_key("conflict", payload))
            row = self._repository.create_conflict(self.lease.run_id, payload, **self._lease_kwargs)
            self._conflicts[str(row["id"])] = dict(row)
            recorded.append(row)
        return recorded

    def persist_trace_summary(self, *, goal: str = "") -> dict[str, Any]:
        """Write a compact, fully traceable view to the existing run blackboard."""
        claims = [{"claim_id": row["id"], "source_id": row["source_id"],
                   "run_id": str(self.lease.run_id), "task_key": row["task_key"],
                   "entity_key": row["entity_key"], "field_key": row["field_key"],
                   "market": row.get("market"), "time_scope": row.get("time_scope") or {},
                   "source_strength": row["source_strength"], "confidence": row["confidence"]}
                  for row in self._claims.values()]
        summaries = [{"conflict_id": row["id"], "claim_ids": row["claim_ids"],
                      "task_key": row["task_key"], "rationale": row.get("rationale")}
                     for row in self._conflicts.values()]
        safe_durable_value(goal)  # validate caller text, but never overwrite blackboard goal/state
        summary = {"known_entities": claims, "claims_conflict_summaries": summaries}
        return self._repository.patch_run_blackboard_evidence(self.lease.run_id, summary, **self._lease_kwargs)

    def references(self) -> list[dict[str, Any]]:
        """Return compact references from the existing claim/source records.

        R3 adds three provenance fields the grounding layer previously had no
        way to see: the claim's own unit, the exact locator the fact was read
        from, and the version of the source it was read at.  All three are
        optional, so a reference rebuilt from a pre-R3 claim is byte-identical
        to what this returned before.
        """
        return [{"claim_id": str(row["id"]), "source_id": str(row["source_id"]),
                 "run_id": str(self.lease.run_id), "task_id": row["task_key"],
                 "entity": row["entity_key"], "field": row["field_key"],
                 "geography": row.get("geography"), "market": row.get("market"),
                 "time_scope": row.get("time_scope") or {}, "value": row.get("value"),
                 "unit": row.get("unit"), "locator": row.get("evidence_locator"),
                 "source_version": self._source_version(row.get("source_id")),
                 "confidence": row["confidence"], "supported": True}
                for row in self._claims.values()]

    def _source_version(self, source_id: Any) -> str | None:
        """The canonical `kind:identifier` of a recorded source, if it has one."""
        row = self._sources.get(str(source_id)) or {}
        kind, identifier = row.get("source_version_kind"), row.get("source_version_id")
        return f"{kind}:{identifier}" if kind and identifier else None

    @staticmethod
    def _task(task_key: str) -> str:
        if not task_key or len(task_key) > 200:
            raise EvidenceValidationError("valid task provenance is required")
        return task_key

    @staticmethod
    def _rationale(value: str) -> str:
        safe = safe_durable_value(value.strip())
        if not safe or len(safe) > 500:
            raise EvidenceValidationError("rationale summary must be 1-500 characters")
        return safe


__all__ = ["EvidenceBoard", "EvidenceValidationError", "WorkerLease", "safe_durable_value",
           "safe_fragment_text"]
