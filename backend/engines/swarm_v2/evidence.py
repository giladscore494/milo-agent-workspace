"""Lease-guarded Evidence Board backed by the existing evidence tables.

Only structured findings and brief rationale summaries cross this boundary.
Model scratch work, provider errors, credentials, and chain-of-thought are
rejected rather than copied into durable evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from backend.schemas import ClaimCreate, ConflictCreate, SourceCreate, ToolUsageCreate


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


def _safe(value: Any) -> Any:
    """Return JSON-compatible evidence or fail with a sanitized error."""
    if isinstance(value, Mapping):
        for key in value:
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise EvidenceValidationError("unsafe evidence metadata rejected")
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        folded = value.casefold()
        if any(marker in folded for marker in _FORBIDDEN_TEXT):
            raise EvidenceValidationError("unsafe evidence text rejected")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise EvidenceValidationError("evidence must be JSON structured")


def _key(kind: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_safe(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
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
        payload = _safe(payload)
        payload.update(task_key=self._task(task_key),
                       idempotency_key=_key("tool", {"task_key": task_key, **payload}))
        return self._repository.create_tool_usage(self.lease.run_id, payload, **self._lease_kwargs)

    def record_source(self, source: SourceCreate, *, task_key: str) -> dict[str, Any]:
        payload = _safe(source.model_dump(mode="json"))
        payload.update(task_key=self._task(task_key),
                       evidence_key=_key("source", {"task_key": task_key, **payload}))
        row = self._repository.create_source(self.lease.run_id, payload, **self._lease_kwargs)
        self._sources[str(row["id"])] = dict(row)
        return row

    def record_claim(self, claim: ClaimCreate, *, task_key: str) -> dict[str, Any]:
        payload = _safe(claim.model_dump(mode="json"))
        # Scope is exactly entity + field + market/geography + time.  Source,
        # confidence, run and task provenance remain attached to every claim.
        payload.update(task_key=self._task(task_key), evidence_key=_key("claim", {
            "task_key": task_key, **payload,
        }))
        row = self._repository.create_claim(self.lease.run_id, payload, **self._lease_kwargs)
        self._claims[str(row["id"])] = dict(row)
        return row

    def detect_and_record_conflicts(self, *, task_key: str,
                                    rationale: str = "Contradictory values in the same evidence scope.") -> list[dict[str, Any]]:
        rationale = self._rationale(rationale)
        groups: dict[str, list[dict[str, Any]]] = {}
        for claim in self._claims.values():
            scope = json.dumps([claim.get("entity_key"), claim.get("field_key"),
                                claim.get("market"), claim.get("geography"),
                                claim.get("time_scope") or {}], sort_keys=True, separators=(",", ":"))
            groups.setdefault(scope, []).append(claim)
        recorded = []
        for claims in groups.values():
            values = {json.dumps(item.get("value"), sort_keys=True, separators=(",", ":")) for item in claims}
            if len(claims) < 2 or len(values) < 2:
                continue
            ids = sorted(UUID(str(item["id"])) for item in claims)
            conflict = ConflictCreate(entity_key=claims[0]["entity_key"], field_key=claims[0]["field_key"],
                                      claim_ids=ids, rationale=rationale)
            payload = _safe(conflict.model_dump(mode="json"))
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
        _safe(goal)  # validate caller text, but never overwrite blackboard goal/state
        summary = {"known_entities": claims, "claims_conflict_summaries": summaries}
        return self._repository.patch_run_blackboard_evidence(self.lease.run_id, summary, **self._lease_kwargs)

    @staticmethod
    def _task(task_key: str) -> str:
        if not task_key or len(task_key) > 200:
            raise EvidenceValidationError("valid task provenance is required")
        return task_key

    @staticmethod
    def _rationale(value: str) -> str:
        safe = _safe(value.strip())
        if not safe or len(safe) > 500:
            raise EvidenceValidationError("rationale summary must be 1-500 characters")
        return safe


__all__ = ["EvidenceBoard", "EvidenceValidationError", "WorkerLease"]
