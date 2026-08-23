from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.engines.swarm_v2.evidence import EvidenceBoard, EvidenceValidationError, WorkerLease
from backend.schemas import ClaimCreate, SourceCreate, ToolUsageCreate


class GuardedEvidenceRepository:
    def __init__(self, lease):
        self.lease = lease
        self.tables = {name: {} for name in ("tool", "source", "claim", "conflict")}
        self.links = set()
        self.blackboard = None

    def _write(self, table, payload, kwargs):
        assert kwargs == {"worker_id": self.lease.worker_id, "attempt": self.lease.attempt,
                          "lease_token": self.lease.lease_token}
        key = payload.get("evidence_key") or payload["idempotency_key"]
        row = self.tables[table].setdefault(key, {"id": str(uuid4()), "run_id": str(self.lease.run_id), **payload})
        return row

    def create_tool_usage(self, run_id, payload, **kwargs): return self._write("tool", payload, kwargs)
    def create_source(self, run_id, payload, **kwargs): return self._write("source", payload, kwargs)
    def create_claim(self, run_id, payload, **kwargs):
        if not any(row["id"] == str(payload["source_id"]) for row in self.tables["source"].values()):
            raise ValueError("source rejected")
        row = self._write("claim", payload, kwargs)
        self.links.add((str(payload["source_id"]), row["id"]))
        return row
    def create_conflict(self, run_id, payload, **kwargs): return self._write("conflict", payload, kwargs)
    def upsert_run_blackboard(self, run_id, payload, **kwargs):
        assert kwargs["lease_token"] == self.lease.lease_token
        self.blackboard = payload
        return {"run_id": str(run_id), **payload}


@pytest.fixture
def board():
    lease = WorkerLease(uuid4(), "worker-1", 2, "lease-token")
    repo = GuardedEvidenceRepository(lease)
    return EvidenceBoard(repo, lease), repo


def source(url="https://example.test/a"):
    return SourceCreate(agent="worker", url=url, title="Evidence", domain="example.test",
                        source_type="primary", source_strength="strong", query="q", tool_operation="search")


def claim(source_id, value, *, market="IL", time_scope=None):
    return ClaimCreate(entity_key="vehicle:1", field_key="price", value=value,
                       time_scope=time_scope or {"as_of": "2026-08"}, market=market,
                       source_id=source_id, source_strength="strong", confidence=.9, agent="worker")


def test_retry_resume_is_idempotent_and_traceable(board):
    evidence, repo = board
    usage = ToolUsageCreate(grant_id=uuid4(), agent="worker", tool="search", operation="query")
    assert evidence.record_tool_usage(usage, task_key="task-1")["id"] == evidence.record_tool_usage(usage, task_key="task-1")["id"]
    first = evidence.record_source(source(), task_key="task-1")
    assert first["id"] == evidence.record_source(source(), task_key="task-1")["id"]
    item = claim(UUID(first["id"]), 100)
    saved = evidence.record_claim(item, task_key="task-1")
    assert saved["id"] == evidence.record_claim(item, task_key="task-1")["id"]
    trace = evidence.persist_trace_summary(goal="Compare price")
    assert trace["known_entities"] == [{
        "claim_id": saved["id"], "source_id": first["id"], "run_id": str(evidence.lease.run_id),
        "task_key": "task-1", "entity_key": "vehicle:1", "field_key": "price", "market": "IL",
        "time_scope": {"as_of": "2026-08"}, "source_strength": "strong", "confidence": .9,
    }]
    assert {name: len(rows) for name, rows in repo.tables.items()} == {"tool": 1, "source": 1, "claim": 1, "conflict": 0}
    assert len(repo.links) == 1


def test_only_contradictory_values_in_same_scope_conflict(board):
    evidence, repo = board
    ids = [UUID(evidence.record_source(source(f"https://example.test/{i}"), task_key=f"task-{i}")["id"])
           for i in range(3)]
    evidence.record_claim(claim(ids[0], 100), task_key="task-0")
    evidence.record_claim(claim(ids[1], 120), task_key="task-1")
    evidence.record_claim(claim(ids[2], 130, market="US"), task_key="task-2")
    rows = evidence.detect_and_record_conflicts(task_key="review")
    assert len(rows) == 1
    assert len(repo.tables["conflict"]) == 1
    assert evidence.detect_and_record_conflicts(task_key="review")[0]["id"] == rows[0]["id"]


def test_invalid_source_and_sensitive_reasoning_are_rejected(board):
    evidence, repo = board
    with pytest.raises(ValueError):
        evidence.record_claim(claim(uuid4(), 100), task_key="task")
    assert repo.tables["claim"] == {} and repo.links == set()
    unsafe = claim(uuid4(), {"chain_of_thought": "private"})
    with pytest.raises(EvidenceValidationError, match="unsafe evidence metadata rejected"):
        evidence.record_claim(unsafe, task_key="task")
    with pytest.raises(EvidenceValidationError, match="unsafe evidence text rejected"):
        evidence.detect_and_record_conflicts(task_key="task", rationale="hidden reasoning from provider")
