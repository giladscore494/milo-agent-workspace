"""MemoryRepository keeps Production's Console 6 creation and claim posture.

`SupabaseRepository.create_queued_run` is a refusal-only compatibility method
(`RUN_IDENTITY_ATOMIC_CREATION_REQUIRED`) and `claim_run_lease` predicates on
`run_identity is not null`, so a legacy identity-less run is readable history
that no worker can ever claim. The in-memory mirror every offline suite runs
against must refuse exactly the same things, or a test could "prove" a path
over a run the product itself would never create or execute.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.errors import AppError, NotFoundError
from backend.testing.memory_repository import MemoryRepository
from tests.run_factory import identity_kwargs


def seeded(repository: MemoryRepository) -> tuple[UUID, UUID]:
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "parity", "Parity", [user])
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))
    return UUID(conversation["id"]), UUID(user)


def test_the_split_creator_refuses_with_the_production_code_and_writes_nothing():
    repository = MemoryRepository()
    conversation_id, user_id = seeded(repository)
    message = repository.create_user_message(conversation_id, "go", {})
    with pytest.raises(AppError) as raised:
        repository.create_queued_run(conversation_id, message["id"], "go", {}, requested_by=user_id)
    assert raised.value.code == "RUN_IDENTITY_ATOMIC_CREATION_REQUIRED"
    assert raised.value.status_code == 503
    assert repository.runs == {}


def test_the_atomic_creator_is_the_only_way_to_a_claimable_run():
    repository = MemoryRepository()
    conversation_id, user_id = seeded(repository)
    created = repository.create_message_and_run(
        conversation_id, "go", {}, requested_by=user_id, idempotency_key=None,
        request_fingerprint="fp", **identity_kwargs(repository, conversation_id))
    run = created["run"]
    assert created["created"] is True
    assert run["run_identity"]["run_id"] == run["id"]
    claimed = repository.claim_run(UUID(run["id"]), "worker-1")
    assert claimed["status"] == "starting"
    assert claimed["lease_token"]


def test_a_legacy_identity_less_run_is_readable_but_never_claimable():
    """Mirror of `claim_run_lease ... and run_identity is not null`."""
    repository = MemoryRepository()
    conversation_id, user_id = seeded(repository)
    run_id = uuid4()
    # A row that pre-dates Console 6: same shape as production history, no
    # identity. Inserted directly because no creator can produce it any more.
    repository.runs[str(run_id)] = {
        "id": str(run_id), "conversation_id": str(conversation_id), "status": "queued",
        "attempt": 1, "launch_state": "pending", "requested_by": str(user_id),
        "idempotency_key": None, "request_fingerprint": None,
        "input": {"message_id": "1", "content": "legacy", "metadata": {}},
        "output": None, "error": None, "usage": {},
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }
    assert repository.get_run(run_id, user_id)["status"] == "queued"
    with pytest.raises(AppError) as raised:
        repository.claim_run(run_id, "worker-1")
    assert raised.value.code == "RUN_ALREADY_CLAIMED"
    assert raised.value.status_code == 409
    after = repository.get_run(run_id, user_id)
    assert after["status"] == "queued"
    assert "worker_id" not in after and "lease_token" not in after


def test_a_missing_run_is_still_not_found_before_any_identity_check():
    repository = MemoryRepository()
    with pytest.raises(NotFoundError):
        repository.claim_run(uuid4(), "worker-1")
