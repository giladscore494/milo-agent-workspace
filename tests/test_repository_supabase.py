"""Regression tests for the exact rows SupabaseRepository writes.

These prove the backend targets the reconciled schema: messages.role,
runs.input/output/error, JSONB run_events.progress, and message IDs that are
passed through as-is (production messages.id is bigint) and stored as strings
inside the run input JSON.
"""

from uuid import UUID, uuid4

import pytest

from backend.repository.supabase import SupabaseRepository


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, client, table):
        self.client = client
        self.table = table
        self.op = "select"
        self.payload = None

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def select(self, *args):
        return self

    def eq(self, *args):
        return self

    def in_(self, *args):
        return self

    def is_(self, *args):
        return self

    def limit(self, *args):
        return self

    def order(self, *args, **kwargs):
        return self

    def execute(self):
        if self.op == "insert":
            self.client.inserted.append((self.table, self.payload))
            return FakeResult([{"id": str(uuid4()), **self.payload}])
        if self.op == "update":
            self.client.updated.append((self.table, self.payload))
            return FakeResult([self.payload])
        return FakeResult(list(self.client.select_data.get(self.table, [])))


class FakeClient:
    def __init__(self):
        self.inserted = []
        self.updated = []
        self.select_data = {}

    def table(self, name):
        return FakeQuery(self, name)


@pytest.fixture
def repo():
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = FakeClient()
    return repository


def test_create_user_message_writes_role_column(repo):
    conversation_id = uuid4()
    repo.client.select_data["conversations"] = [{"id": str(conversation_id)}]
    repo.create_user_message(conversation_id, "hello", {"a": 1})
    table, payload = repo.client.inserted[0]
    assert table == "messages"
    assert payload["role"] == "user"
    assert "sender_role" not in payload
    assert payload["content"] == "hello"
    assert payload["metadata"] == {"a": 1}


@pytest.mark.parametrize("message_id,expected", [(1, "1"), (98765432101234, "98765432101234"), ("abc-uuid-like", "abc-uuid-like")])
def test_create_queued_run_stores_message_id_string_in_input(repo, message_id, expected):
    conversation_id = uuid4()
    run = repo.create_queued_run(conversation_id, message_id, "go", {})
    table, payload = repo.client.inserted[0]
    assert table == "runs"
    assert payload["input"] == {"message_id": expected, "content": "go", "metadata": {}}
    assert payload["idempotency_key"] == expected
    assert payload["status"] == "queued"
    assert "user_prompt" not in payload
    UUID(run["id"])  # run ids remain UUID


def test_create_queued_run_accepts_uuid_message_id(repo):
    message_id = uuid4()
    repo.create_queued_run(uuid4(), message_id, "go", {})
    _, payload = repo.client.inserted[0]
    assert payload["input"]["message_id"] == str(message_id)


def test_mark_run_complete_writes_output_column(repo):
    run_id = uuid4()
    repo.client.select_data["runs"] = [{"id": str(run_id), "status": "running"}]
    repo.mark_run_complete(run_id, {"models": []})
    table, payload = repo.client.updated[0]
    assert table == "runs"
    assert payload["output"] == {"models": []}
    assert payload["error"] is None
    assert payload["status"] == "completed"
    assert "result" not in payload


def test_mark_run_failed_writes_error_column(repo):
    run_id = uuid4()
    repo.client.select_data["runs"] = [{"id": str(run_id), "status": "running"}]
    repo.mark_run_failed(run_id, "ENGINE_FAILED", "boom")
    table, payload = repo.client.updated[0]
    assert table == "runs"
    assert payload["error"] == {"code": "ENGINE_FAILED", "message": "boom"}
    assert "error_message" not in payload


def test_append_run_event_passes_json_progress_through(repo):
    run_id = uuid4()
    repo.client.select_data["runs"] = [{"id": str(run_id), "status": "running"}]
    repo.append_run_event(run_id, "agent_progress", {
        "message": "chunk done",
        "agent": "builder",
        "phase": "fetch",
        "progress": {"done": 3, "total": 9},
        "payload": {"chunk": 1},
    })
    table, payload = repo.client.inserted[0]
    assert table == "run_events"
    assert payload["progress"] == {"done": 3, "total": 9}
    assert payload["agent"] == "builder"
    assert payload["phase"] == "fetch"
    assert payload["event_type"] == "agent_progress"
    assert "agent_name" not in payload
    assert "progress_percent" not in payload
