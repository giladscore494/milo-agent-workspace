"""Lease-guarded Evidence Board backed by the existing evidence tables.

Only structured findings, brief rationale summaries, and bounded verbatim
source evidence fragments cross this boundary.  Model scratch work, provider
errors, credentials, and chain-of-thought are rejected rather than copied
into durable evidence.

Evidence fragments are captured at acquisition time from real tool material
(see .fragments) and land in the service-only relation
public.source_evidence_fragments -- never in the browser-visible source
metadata, the claim value, or a run event.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from backend.schemas import ClaimCreate, ConflictCreate, SourceCreate, ToolUsageCreate

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

    def record_source(self, source: SourceCreate, *, task_key: str) -> dict[str, Any]:
        payload = safe_durable_value(source.model_dump(mode="json"))
        payload.update(task_key=self._task(task_key),
                       evidence_key=_key("source", {"task_key": task_key, **payload}))
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
        """
        return [self.record_evidence_fragment(source_id, text, task_key=task_key,
                                              fragment_index=index)
                for index, text in enumerate(extract_source_fragments(tool_result))]

    def record_evidence_fragment(self, source_id: Any, fragment_text: str, *, task_key: str,
                                 fragment_index: int = 0) -> dict[str, Any]:
        """Persist one bounded, source-bound fragment through the guarded RPC.

        Identity is stable provenance only -- source + task + the final bounded
        text's content hash -- so an exact replay of the same fragment for the
        same source and task returns the same durable row instead of a
        duplicate.  No timestamp, UUID, or call sequence enters the key.
        """
        text = safe_fragment_text(fragment_text)
        if not isinstance(fragment_index, int) or isinstance(fragment_index, bool) or \
                not 0 <= fragment_index < MAX_FRAGMENTS_PER_SOURCE:
            raise EvidenceValidationError("fragment index is outside the durable bound")
        content_hash = fragment_content_hash(text)
        identity = {"task_key": task_key, "source_id": str(source_id),
                    "content_hash": content_hash}
        payload = {"source_id": str(source_id), "fragment_text": text,
                   "content_hash": content_hash, "fragment_index": fragment_index,
                   "task_key": self._task(task_key),
                   "evidence_key": _key("fragment", identity)}
        return self._repository.record_evidence_fragment(self.lease.run_id, payload,
                                                         **self._lease_kwargs)

    def record_claim(self, claim: ClaimCreate, *, task_key: str) -> dict[str, Any]:
        payload = safe_durable_value(claim.model_dump(mode="json"))
        # The idempotent evidence identity is derived from task provenance plus
        # the ORIGINAL claim payload only — exactly as before canonical scopes
        # existed — so a claim persisted by a pre-canonical release replays to
        # the same evidence_key.  Derived canonical metadata (and any future
        # SCOPE_NORMALIZATION_VERSION) must never change this identity.
        evidence_key = _key("claim", {"task_key": task_key, **payload})
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
        """Return compact references from the existing claim/source records."""
        return [{"claim_id": str(row["id"]), "source_id": str(row["source_id"]),
                 "run_id": str(self.lease.run_id), "task_id": row["task_key"],
                 "entity": row["entity_key"], "field": row["field_key"],
                 "geography": row.get("geography"), "market": row.get("market"),
                 "time_scope": row.get("time_scope") or {}, "value": row.get("value"),
                 "confidence": row["confidence"], "supported": True}
                for row in self._claims.values()]

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
