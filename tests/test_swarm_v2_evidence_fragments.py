"""B2: durable bounded evidence fragments.

Covers the acquisition contract (real tool material in, bounded fragments
out), the lease-guarded source-bound persistence path, its idempotency and
safety boundaries, the internal bounded read primitive, and the invariants
B2 must NOT break: source metadata, claim contracts, legacy rows, and the
browser-visible run event surface.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.engines.swarm_v2.engine import SwarmV2Engine
from backend.engines.swarm_v2.evidence import (EvidenceBoard, EvidenceValidationError,
                                               WorkerLease, safe_fragment_text)
from backend.engines.swarm_v2.fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENTS_PER_SOURCE,
                                                MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE,
                                                FragmentExtractionError, bound_fragment_text,
                                                extract_source_fragments, fragment_content_hash)
from backend.repository.supabase import SupabaseRepository
from backend.runtime import EVENT_TYPES
from backend.schemas import ClaimCreate, SourceCreate


class GuardedFragmentRepository:
    """A faithful mirror of record_evidence_fragment_guarded's guarantees."""

    def __init__(self, lease):
        self.lease = lease
        self.sources: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.fragments: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []

    def _assert_lease(self, run_id, kwargs):
        self.calls.append("write")
        if str(run_id) != str(self.lease.run_id) or kwargs != {
                "worker_id": self.lease.worker_id, "attempt": self.lease.attempt,
                "lease_token": self.lease.lease_token}:
            raise AssertionError("STALE_WORKER_WRITE")

    def create_source(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs)
        return self.sources.setdefault(payload["evidence_key"],
                                       {"id": str(uuid4()), "run_id": str(run_id), **payload})

    def create_claim(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs)
        if str(payload["source_id"]) not in {row["id"] for row in self.sources.values()}:
            raise AssertionError("invalid claim source")
        return self.claims.setdefault(payload["evidence_key"],
                                      {"id": str(uuid4()), "run_id": str(run_id), **payload})

    def record_evidence_fragment(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs)
        source = next((row for row in self.sources.values()
                       if row["id"] == payload["source_id"]
                       and row["run_id"] == str(run_id)), None)
        if source is None:
            raise AssertionError("invalid evidence fragment source")
        # The lineage is task -> source -> fragment: the same run is not enough.
        if source.get("task_key") != payload["task_key"]:
            raise AssertionError("evidence fragment task provenance mismatch")
        if len(payload["fragment_text"]) > MAX_FRAGMENT_CHARS:
            raise AssertionError("fragment_text exceeds the durable bound")
        if fragment_content_hash(payload["fragment_text"]) != payload["content_hash"]:
            raise AssertionError("content hash does not match the bounded text")
        key = (str(run_id), payload["evidence_key"])
        existing = self.fragments.get(key)
        if existing is not None:
            # An existing row may only be returned when it is the SAME logical
            # fragment; fragment_index is excluded from that identity and a
            # replay never rewrites it.  Concurrency is not simulated here --
            # the per-source lock is proven in the real PostgreSQL regression.
            if any(existing[field] != payload[field] for field in
                   ("source_id", "task_key", "fragment_text", "content_hash")):
                raise AssertionError("evidence fragment idempotency conflict")
            return existing
        owned = [row for row in self.fragments.values()
                 if row["source_id"] == payload["source_id"]]
        if len(owned) >= MAX_FRAGMENTS_PER_SOURCE:
            raise AssertionError("evidence fragment count limit reached for this source")
        if sum(len(row["fragment_text"]) for row in owned) + len(payload["fragment_text"]) > \
                MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE:
            raise AssertionError("evidence fragment character budget exhausted for this source")
        row = {"id": str(uuid4()), "run_id": str(run_id), **payload}
        self.fragments[key] = row
        return row

    def list_evidence_fragments_for_sources(self, run_id, source_ids, *, limit=200):
        wanted = {str(item) for item in source_ids}
        rows = [row for row in self.fragments.values()
                if row["run_id"] == str(run_id) and row["source_id"] in wanted]
        rows.sort(key=lambda row: (row["source_id"], row["fragment_index"], row["content_hash"]))
        return rows[:limit]


@pytest.fixture
def board():
    lease = WorkerLease(uuid4(), "worker-1", 2, "lease-token")
    repo = GuardedFragmentRepository(lease)
    return EvidenceBoard(repo, lease), repo


def source(url="https://example.test/a"):
    return SourceCreate(agent="worker", url=url, title="Evidence", domain="example.test",
                        source_type="primary", source_strength="strong", query="q",
                        tool_operation="search")


def claim(source_id, value):
    return ClaimCreate(entity_key="vehicle:1", field_key="price", value=value,
                       time_scope={"as_of": "2026-08"}, market="IL", source_id=source_id,
                       source_strength="strong", confidence=0.9, agent="worker")


# The shape a registered tool actually returns today (backend/tools/mock.py).
TOOL_RESULT = {"rows": ["The 2020 Corolla Hybrid is rated 1798 cc.",
                        "Israeli list price was 129,900 NIS at launch."]}


# --- A. a valid source-bound fragment persists -------------------------------

def test_valid_source_bound_fragments_persist_from_real_tool_material(board):
    evidence, _ = board
    row = evidence.record_source(source(), task_key="task-1")
    fragments = evidence.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    assert [item["fragment_text"] for item in fragments] == TOOL_RESULT["rows"]
    assert {item["source_id"] for item in fragments} == {row["id"]}
    assert {item["run_id"] for item in fragments} == {str(evidence.lease.run_id)}
    assert {item["task_key"] for item in fragments} == {"task-1"}
    assert [item["fragment_index"] for item in fragments] == [0, 1]


def test_fragment_requires_a_real_source_of_the_same_run(board):
    evidence, _ = board
    with pytest.raises(AssertionError, match="invalid evidence fragment source"):
        evidence.record_evidence_fragment(uuid4(), "Orphan text.", task_key="task-1")


# --- B. exact replay is idempotent -------------------------------------------

def test_exact_fragment_replay_is_idempotent_across_boards(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    first = evidence.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    resumed = EvidenceBoard(repo, evidence.lease)
    replay = resumed.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    assert [item["id"] for item in first] == [item["id"] for item in replay]
    assert [item["evidence_key"] for item in first] == [item["evidence_key"] for item in replay]
    assert len(repo.fragments) == 2


def test_fragment_identity_excludes_position_and_randomness(board):
    """Identity is run/source/task + the final bounded text only."""
    evidence, _ = board
    row = evidence.record_source(source(), task_key="task-1")
    keys = {evidence.record_evidence_fragment(row["id"], "Same durable sentence.",
                                              task_key="task-1",
                                              fragment_index=index)["evidence_key"]
            for index in range(3)}
    assert len(keys) == 1


# --- C/D. fragments never migrate between sources or runs --------------------

def test_each_source_gets_its_own_fragment_identity(board):
    evidence, _ = board
    first = evidence.record_source(source("https://example.test/a"), task_key="task-1")
    second = evidence.record_source(source("https://example.test/b"), task_key="task-1")
    a = evidence.record_evidence_fragment(first["id"], "Shared sentence.", task_key="task-1")
    b = evidence.record_evidence_fragment(second["id"], "Shared sentence.", task_key="task-1")
    assert a["evidence_key"] != b["evidence_key"]
    assert a["id"] != b["id"]
    assert (a["source_id"], b["source_id"]) == (first["id"], second["id"])


def test_reusing_another_sources_fragment_key_is_rejected(board):
    evidence, repo = board
    first = evidence.record_source(source("https://example.test/a"), task_key="task-1")
    second = evidence.record_source(source("https://example.test/b"), task_key="task-1")
    stored = evidence.record_evidence_fragment(first["id"], "Shared sentence.", task_key="task-1")
    text = "Shared sentence."
    hijack = {"source_id": second["id"], "fragment_text": text, "task_key": "task-1",
              "content_hash": fragment_content_hash(text), "fragment_index": 0,
              "evidence_key": stored["evidence_key"]}
    with pytest.raises(AssertionError, match="evidence fragment idempotency conflict"):
        repo.record_evidence_fragment(evidence.lease.run_id, hijack, worker_id="worker-1",
                                      attempt=2, lease_token="lease-token")


def test_same_source_key_with_different_text_is_a_hard_idempotency_conflict(board):
    """A replay is only a replay when the logical fragment is identical."""
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    stored = evidence.record_evidence_fragment(row["id"], "Original sentence.", task_key="task-1")
    for field, value in (("fragment_text", "Different sentence."),
                         ("content_hash", fragment_content_hash("Different sentence.")),
                         ("task_key", "task-2")):
        forged = {**{key: stored[key] for key in
                     ("source_id", "task_key", "fragment_text", "content_hash",
                      "fragment_index", "evidence_key")}, field: value}
        with pytest.raises(AssertionError):
            repo.record_evidence_fragment(evidence.lease.run_id, forged, worker_id="worker-1",
                                          attempt=2, lease_token="lease-token")
    assert len(repo.fragments) == 1
    assert repo.fragments[(str(evidence.lease.run_id), stored["evidence_key"])] == stored


def test_replay_at_a_different_position_returns_the_row_without_rewriting_the_index(board):
    """fragment_index is deliberately outside the fragment's logical identity."""
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    stored = evidence.record_evidence_fragment(row["id"], "Stable sentence.",
                                               task_key="task-1", fragment_index=0)
    replay = evidence.record_evidence_fragment(row["id"], "Stable sentence.",
                                               task_key="task-1", fragment_index=2)
    assert replay["id"] == stored["id"]
    assert replay["fragment_index"] == 0  # the stored position is never mutated
    assert len(repo.fragments) == 1


def test_fragment_task_must_match_the_durable_source_task(board):
    """task -> source -> fragment: the same run is not enough provenance."""
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-a")
    with pytest.raises(AssertionError, match="task provenance mismatch"):
        evidence.record_evidence_fragment(row["id"], "Wrong task text.", task_key="task-b")
    assert repo.fragments == {}
    assert evidence.record_evidence_fragment(row["id"], "Right task text.",
                                             task_key="task-a")["task_key"] == "task-a"


def test_cross_run_source_is_rejected(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    other_run = WorkerLease(uuid4(), "worker-1", 2, "lease-token")
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        EvidenceBoard(repo, other_run).record_evidence_fragment(
            row["id"], "Cross-run text.", task_key="task-1")
    assert repo.fragments == {}


# --- E. lease guarding -------------------------------------------------------

@pytest.mark.parametrize("worker_id,attempt,token", [
    ("", 2, "lease-token"), ("worker-1", 0, "lease-token"), ("worker-1", 2, "")])
def test_incomplete_lease_can_never_construct_a_fragment_writer(worker_id, attempt, token):
    with pytest.raises(EvidenceValidationError, match="complete worker lease is required"):
        WorkerLease(uuid4(), worker_id, attempt, token)


def test_stale_lease_is_rejected_before_any_fragment_is_written(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    stale = WorkerLease(evidence.lease.run_id, "worker-1", 3, "lease-token")
    with pytest.raises(AssertionError, match="STALE_WORKER_WRITE"):
        EvidenceBoard(repo, stale).record_evidence_fragment(row["id"], "Text.", task_key="task-1")
    assert repo.fragments == {}


# --- F. hard bounds ----------------------------------------------------------

def test_acquisition_bounds_deterministically_and_persistence_rejects(board):
    evidence, _ = board
    row = evidence.record_source(source(), task_key="task-1")
    oversized = "word " * 400
    extracted = extract_source_fragments({"rows": [oversized]})
    assert len(extracted) == 1 and len(extracted[0]) <= MAX_FRAGMENT_CHARS
    assert extracted == extract_source_fragments({"rows": [oversized]})
    assert oversized.strip().startswith(extracted[0])  # verbatim prefix, no marker appended
    # The durable boundary rejects rather than silently truncating.
    with pytest.raises(EvidenceValidationError, match="exceeds the durable size bound"):
        evidence.record_evidence_fragment(row["id"], oversized, task_key="task-1")


def test_count_and_total_character_budgets_are_enforced_at_acquisition():
    many = extract_source_fragments({"rows": [f"Sentence number {index}." for index in range(20)]})
    assert len(many) == MAX_FRAGMENTS_PER_SOURCE
    long_rows = extract_source_fragments({"rows": ["q " * 300, "w " * 300, "e " * 300, "r " * 300]})
    assert sum(len(item) for item in long_rows) <= MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE


def test_durable_count_limit_is_enforced_per_source(board):
    evidence, _ = board
    row = evidence.record_source(source(), task_key="task-1")
    for index in range(MAX_FRAGMENTS_PER_SOURCE):
        evidence.record_evidence_fragment(row["id"], f"Sentence {index}.",
                                          task_key="task-1", fragment_index=index)
    with pytest.raises(EvidenceValidationError, match="fragment index is outside"):
        evidence.record_evidence_fragment(row["id"], "One too many.", task_key="task-1",
                                          fragment_index=MAX_FRAGMENTS_PER_SOURCE)
    with pytest.raises(AssertionError, match="count limit reached"):
        evidence.record_evidence_fragment(row["id"], "One too many.", task_key="task-1")


# --- G. empty fragments ------------------------------------------------------

@pytest.mark.parametrize("value", ["", "   ", " \t ", " "])
def test_empty_fragment_is_rejected(board, value):
    evidence, _ = board
    row = evidence.record_source(source(), task_key="task-1")
    with pytest.raises(EvidenceValidationError, match="must not be empty"):
        evidence.record_evidence_fragment(row["id"], value, task_key="task-1")


def test_empty_candidates_never_become_fragments():
    assert extract_source_fragments({"rows": ["", "   ", " \t "]}) == []
    assert extract_source_fragments({}) == []
    with pytest.raises(FragmentExtractionError):
        extract_source_fragments(["not", "a", "tool", "result"])


# --- H. unsafe durable content -----------------------------------------------

@pytest.mark.parametrize("unsafe", [
    "api_key=ABCDEFGHIJKLMNOP",
    "Authorization: Token abcdef123456",
    "the lease_token is 9f2c",
    "-----begin certificate-----",
    "aws_secret_access_key rotated",
    "client_secret published in the manual",
    "x-api-key: 12345",
    "the agent's chain of thought was logged",
    "hidden reasoning follows",
])
def test_secret_and_reasoning_markers_are_rejected(board, unsafe):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    with pytest.raises(EvidenceValidationError):
        evidence.record_evidence_fragment(row["id"], unsafe, task_key="task-1")
    assert repo.fragments == {}


def test_ordinary_reasoning_prose_is_not_censored():
    """The policy is a finite marker set, never broad semantic censorship."""
    prose = ("The committee therefore concluded that, because demand fell, the analysis "
             "supports a lower estimate; the reasoning is set out in section 4.")
    assert safe_fragment_text(prose) == prose


@pytest.mark.parametrize("value", [None, 42, {"text": "x"}, ["x"]])
def test_non_text_fragments_are_rejected(value):
    with pytest.raises(EvidenceValidationError, match="must be a string"):
        safe_fragment_text(value)


# --- I/J. source metadata and claims are untouched ---------------------------

def test_source_metadata_is_identical_with_and_without_fragments(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    before = dict(row)
    evidence.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    stored = repo.sources[row["evidence_key"]]
    assert stored == before
    assert not any(key in stored for key in
                   ("fragment_text", "content_hash", "fragments", "evidence_fragments"))


def test_claims_are_unaffected_and_never_carry_evidence_text(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    baseline = evidence.record_claim(claim(UUID(row["id"]), 100), task_key="task-1")
    evidence.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    replay = EvidenceBoard(repo, evidence.lease).record_claim(
        claim(UUID(row["id"]), 100), task_key="task-1")
    assert replay == baseline  # same durable row, same evidence_key
    assert baseline["value"] == 100
    assert not any(key in baseline for key in ("fragment_text", "content_hash", "fragment_index"))
    assert all(text not in str(baseline) for text in TOOL_RESULT["rows"])


# --- K/L. deterministic ordering and hashing ---------------------------------

def test_multiple_fragments_have_deterministic_order_and_read_back_in_it(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    result = {"rows": ["Third sentence.", "First sentence.", "Second sentence."]}
    written = evidence.record_source_evidence(row["id"], result, task_key="task-1")
    assert [item["fragment_text"] for item in written] == result["rows"]  # tool order preserved
    read = repo.list_evidence_fragments_for_sources(evidence.lease.run_id, [row["id"]])
    assert [item["fragment_index"] for item in read] == [0, 1, 2]
    assert [item["fragment_text"] for item in read] == result["rows"]


def test_content_hash_is_deterministic_and_text_specific():
    assert fragment_content_hash("A sentence.") == fragment_content_hash("A sentence.")
    assert fragment_content_hash("A sentence.") != fragment_content_hash("A sentence!")
    assert len(fragment_content_hash("x")) == 64
    # Normalization happens before hashing, so the durable hash always
    # describes exactly the durable text.
    assert bound_fragment_text("A   sentence. ") == "A sentence."
    assert fragment_content_hash(bound_fragment_text("A   sentence. ")) == \
        fragment_content_hash("A sentence.")


def test_duplicate_tool_text_is_collapsed_once():
    assert extract_source_fragments({"rows": ["Same.", "Same.", "Other."]}) == ["Same.", "Other."]


# --- M. legacy sources without fragments -------------------------------------

def test_legacy_source_without_fragments_stays_valid_and_is_never_fabricated(board):
    evidence, repo = board
    legacy = evidence.record_source(source("https://legacy.test/x"), task_key="task-legacy")
    grounded = evidence.record_source(source("https://example.test/y"), task_key="task-1")
    evidence.record_source_evidence(grounded["id"], TOOL_RESULT, task_key="task-1")
    assert repo.list_evidence_fragments_for_sources(evidence.lease.run_id, [legacy["id"]]) == []
    assert evidence.record_claim(claim(UUID(legacy["id"]), 100), task_key="task-legacy")["id"]
    assert len(repo.list_evidence_fragments_for_sources(
        evidence.lease.run_id, [legacy["id"], grounded["id"]])) == 2


# --- O. internal bounded read primitive --------------------------------------

class RecordingQuery:
    def __init__(self, client, table):
        self.client, self.table = client, table
        self.filters: dict = {}
        self.orders: list[str] = []
        self.columns = ""
        self.rows_limit = None

    def select(self, columns="*"):
        self.columns = columns
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def in_(self, column, values):
        self.filters[column] = list(values)
        return self

    def order(self, column, **kwargs):
        self.orders.append(column)
        return self

    def limit(self, value):
        self.rows_limit = value
        return self

    def execute(self):
        self.client.queries.append(self)
        rows = [row for row in self.client.rows
                if row["run_id"] == self.filters.get("run_id")
                and row["source_id"] in self.filters.get("source_id", [])]
        rows.sort(key=lambda row: tuple(str(row[column]) for column in self.orders))
        return type("Result", (), {"data": rows[:self.rows_limit]})()


class RecordingClient:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def table(self, name):
        return RecordingQuery(self, name)


def _repository(rows):
    repository = SupabaseRepository.__new__(SupabaseRepository)
    repository.client = RecordingClient(rows)
    return repository


def _row(run_id, source_id, index, text):
    return {"id": str(uuid4()), "run_id": str(run_id), "source_id": str(source_id),
            "task_key": "task", "evidence_key": f"fragment:{text}", "fragment_text": text,
            "content_hash": fragment_content_hash(text), "fragment_index": index,
            "created_at": "2026-08-28T00:00:00Z"}


def test_internal_read_is_scoped_ordered_and_bounded():
    run_id, other_run = uuid4(), uuid4()
    wanted, unwanted = uuid4(), uuid4()
    rows = [_row(run_id, wanted, 1, "second"), _row(run_id, wanted, 0, "first"),
            _row(run_id, unwanted, 0, "other source"), _row(other_run, wanted, 0, "other run")]
    repository = _repository(rows)
    read = repository.list_evidence_fragments_for_sources(run_id, [wanted])
    assert [item["fragment_text"] for item in read] == ["first", "second"]
    query = repository.client.queries[-1]
    assert query.table == "source_evidence_fragments"
    assert query.orders == ["source_id", "fragment_index", "content_hash"]
    assert query.filters["run_id"] == str(run_id)
    assert query.filters["source_id"] == [str(wanted)]
    assert query.rows_limit == SupabaseRepository.MAX_EVIDENCE_FRAGMENT_ROWS
    assert "fragment_text" in query.columns and "*" not in query.columns


def test_internal_read_caps_the_requested_limit_and_short_circuits_empty_input():
    run_id, source_id = uuid4(), uuid4()
    repository = _repository([_row(run_id, source_id, index, f"t{index}") for index in range(4)])
    assert len(repository.list_evidence_fragments_for_sources(run_id, [source_id], limit=2)) == 2
    assert repository.client.queries[-1].rows_limit == 2
    repository.list_evidence_fragments_for_sources(run_id, [source_id], limit=10_000)
    assert repository.client.queries[-1].rows_limit == SupabaseRepository.MAX_EVIDENCE_FRAGMENT_ROWS
    assert repository.list_evidence_fragments_for_sources(run_id, []) == []
    assert len(repository.client.queries) == 2  # no query is issued for an empty source set


# --- P. no fragment body reaches the run event surface -----------------------

def test_fragment_capture_emits_no_run_event(board):
    evidence, repo = board
    row = evidence.record_source(source(), task_key="task-1")
    evidence.record_source_evidence(row["id"], TOOL_RESULT, task_key="task-1")
    assert not hasattr(repo, "append_run_event")
    assert repo.calls == ["write"] * 3  # one source, two fragments, nothing else


def test_no_fragment_event_type_exists_and_the_engine_sink_drops_fragment_text():
    assert not [name for name in EVENT_TYPES if "fragment" in name]
    emitted: list[tuple[str, dict]] = []
    engine = SwarmV2Engine.__new__(SwarmV2Engine)
    engine._event_sink = lambda kind, payload: emitted.append((kind, payload))
    engine._emit("evidence_added", {"source_id": "src-1", "fragment_text": TOOL_RESULT["rows"][0],
                                    "content_hash": "0" * 64, "fragment_count": 2})
    assert emitted == [("evidence_added", {"source_id": "src-1"})]


# --- acquisition-time ordering (source -> fragment -> claim) ------------------

def test_source_then_fragments_then_claim_is_one_acquisition_time_flow(board):
    evidence, repo = board
    row, fragments = evidence.record_source_with_evidence(source(), TOOL_RESULT, task_key="task-1")
    assert repo.calls == ["write"] * 3  # the source is durable before any fragment
    assert {item["source_id"] for item in fragments} == {row["id"]}
    stored = evidence.record_claim(claim(UUID(row["id"]), 100), task_key="task-1")
    assert stored["source_id"] == row["id"]
    # claim -> source_id -> fragments resolves without re-fetching anything.
    resolved = repo.list_evidence_fragments_for_sources(
        evidence.lease.run_id, [stored["source_id"]])
    assert [item["fragment_text"] for item in resolved] == TOOL_RESULT["rows"]


def test_acquisition_flow_replays_to_the_same_durable_rows(board):
    evidence, repo = board
    first_source, first_fragments = evidence.record_source_with_evidence(
        source(), TOOL_RESULT, task_key="task-1")
    resumed = EvidenceBoard(repo, evidence.lease)
    replay_source, replay_fragments = resumed.record_source_with_evidence(
        source(), TOOL_RESULT, task_key="task-1")
    assert replay_source["id"] == first_source["id"]
    assert [item["id"] for item in replay_fragments] == [item["id"] for item in first_fragments]
    assert len(repo.sources) == 1 and len(repo.fragments) == 2


def test_acquisition_flow_keeps_source_and_fragment_task_provenance_aligned(board):
    evidence, repo = board
    row, fragments = evidence.record_source_with_evidence(source(), TOOL_RESULT, task_key="task-a")
    assert row["task_key"] == "task-a"
    assert {item["task_key"] for item in fragments} == {"task-a"}
    # The same source can never gain a fragment attributed to another task.
    with pytest.raises(AssertionError, match="task provenance mismatch"):
        evidence.record_source_evidence(row["id"], {"rows": ["Other task."]}, task_key="task-b")
    assert len(repo.fragments) == 2
