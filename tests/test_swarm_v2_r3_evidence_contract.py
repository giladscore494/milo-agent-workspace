"""R3: the structured, versioned and focused evidence contract.

The blocker this replaces: evidence acquisition scanned a tool result for
generic text keys ("snippet", "text", "content", "rows") and stored a
400-character PREFIX of the first one it found.  A fact stated after a long
introduction was lost, a structured record was flattened into an arbitrary
string, no version of the source was kept, no location inside the source was
kept, and two identical sentences read from two different records collapsed
onto one row.

Everything here is offline: fixture tools, trusted offline mappers, a fake
repository that mirrors the guarded RPCs, and no network, provider or paid
call anywhere.  The production ToolRegistry and PRODUCTION_EVIDENCE_MAPPERS
both stay empty.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.engines.swarm_v2 import (FRAGMENT_TYPES, MAX_FACTS_PER_BUNDLE,
                                      MAX_LOCATOR_KEY_CHARS, MAX_LOCATOR_PATH_SEGMENTS,
                                      PRODUCTION_EVIDENCE_MAPPERS, SOURCE_VERSION_KINDS,
                                      EvidenceBundle, EvidenceContractError, EvidenceLocator,
                                      EvidenceMapperRegistry, EvidenceMappingError,
                                      FocusedEvidenceFragment, GenericWorker, SourceVersion,
                                      StructuredEvidenceFact, ToolCallRecord,
                                      TrustedEvidenceAcquisition, VersionedEvidenceSource,
                                      build_evidence_bundle, canonical_projection,
                                      document_span_locator, record_field_locator,
                                      snapshot_version, structured_projection,
                                      verbatim_excerpt)
from backend.engines.swarm_v2.contracts import EvidenceReference
from backend.engines.swarm_v2.evidence import EvidenceBoard, EvidenceValidationError, WorkerLease
from backend.engines.swarm_v2.fragments import (MAX_FRAGMENT_CHARS, MAX_FRAGMENTS_PER_SOURCE,
                                                MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE,
                                                extract_source_fragments, fragment_content_hash)
from backend.engines.swarm_v2.grounding import (GroundedCandidate, GroundingContractError,
                                                RepositoryEvidenceResolver,
                                                ResolvedSourceEvidence, SourceFragment,
                                                resolve_source_context)
from backend.engines.swarm_v2.verifier import serialize_verifier_candidates
from backend.schemas import ClaimCreate, SourceCreate
from backend.testing.evidence_mappers import (StructuredRegistryEvidenceMapper,
                                              offline_evidence_mappers)
from backend.tools import (MockDocumentArchiveTool, MockStructuredRegistryTool, ToolContext,
                           ToolRegistry)

# --- deterministic offline fixture data --------------------------------------

RECORD = {"model_name": "Fixture Hatch", "model_year": 2020, "market": "IL",
          "engine_displacement_cc": 1798, "list_price": 129900.0,
          "price_currency": "ILS", "fuel_type": "hybrid"}
RECORDS = {"rec-1": RECORD, "rec-2": {**RECORD, "engine_displacement_cc": 1598}}

# 620 characters of preamble: more than MAX_FRAGMENT_CHARS, so a generic
# prefix extractor stores THIS and never reaches the fact below it.
INTRO = ("This archive page opens with a long editorial preamble about the "
         "publication itself, its history, its editorial standards, its "
         "correction policy, its funding, its contributors and its stance on "
         "reader feedback, none of which says anything at all about any "
         "vehicle, any engine, any price or any model year, and which runs on "
         "for considerably more than four hundred characters precisely so that "
         "a prefix of the page cannot accidentally contain the fact that "
         "follows it. ")
SENTENCE = "The 2020 Fixture Hatch is rated at 1798 cc by the official importer."
DOCUMENTS = {
    "doc-1": {"revision": "rev-7", "section": "Engine specifications", "intro": INTRO,
              "sentence": SENTENCE, "field": "engine_displacement_cc",
              "value": 1798, "unit": "cc"},
}

SCOPES = frozenset({"mock:structured_registry", "mock:document_archive"})
CONTEXT = ToolContext(scopes=SCOPES)
OUTPUT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
                 "required": ["answer"], "additionalProperties": False}


def tools() -> ToolRegistry:
    return ToolRegistry([MockStructuredRegistryTool(RECORDS),
                         MockDocumentArchiveTool(DOCUMENTS)])


def record_result(record_id: str = "rec-1") -> dict:
    return tools().execute("mock.structured_registry", "get_record", CONTEXT,
                           {"record_id": record_id})


def document_result(document_id: str = "doc-1") -> dict:
    return tools().execute("mock.document_archive", "locate_passage", CONTEXT,
                           {"document_id": document_id, "field": "engine_displacement_cc"})


def call_record(result, *, tool="mock.structured_registry", operation="get_record",
                task_id="task-1", call_id="c1") -> ToolCallRecord:
    """A record shaped exactly like the one the R2 worker seam constructs."""
    return ToolCallRecord(task_id=task_id, call_id=call_id, tool=tool,
                          operation=operation, result=result)


# --- a fake repository mirroring the guarded RPCs -----------------------------

class R3GuardedRepository:
    """A faithful mirror of the R3 guarded evidence RPCs' guarantees.

    Every invariant the migration enforces in PostgreSQL is enforced here too,
    so the offline suite and the executable PostgreSQL suite
    (tests/test_migrations_postgres.py) prove the same contract from both
    sides.  Concurrency is not simulated: the per-source lock is proven in the
    real PostgreSQL regression.
    """

    def __init__(self, lease):
        self.lease = lease
        self.sources: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.fragments: dict[str, dict] = {}
        self.writes: list[str] = []

    def _assert_lease(self, run_id, kwargs, kind):
        self.writes.append(kind)
        if str(run_id) != str(self.lease.run_id) or kwargs != {
                "worker_id": self.lease.worker_id, "attempt": self.lease.attempt,
                "lease_token": self.lease.lease_token}:
            raise AssertionError("STALE_WORKER_WRITE")

    def create_source(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs, "source")
        kind, identifier = payload.get("source_version_kind"), payload.get("source_version_id")
        if (kind is None) != (identifier is None):
            raise AssertionError("a source version requires both a kind and an identifier")
        if kind is not None and (kind not in SOURCE_VERSION_KINDS or len(identifier) > 128):
            raise AssertionError("unknown or oversized source version")
        existing = self.sources.get(payload["evidence_key"])
        if existing is not None:
            if (existing.get("source_version_kind"), existing.get("source_version_id")) != \
                    (kind, identifier):
                raise AssertionError("source version identity conflict")
            return existing
        row = {"id": str(uuid4()), "run_id": str(run_id), **payload}
        self.sources[payload["evidence_key"]] = row
        return row

    def create_claim(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs, "claim")
        source = next((row for row in self.sources.values()
                       if row["id"] == str(payload["source_id"])), None)
        if source is None:
            raise AssertionError("invalid claim source")
        locator = payload.get("evidence_locator")
        if locator is not None:
            if len(locator) > MAX_LOCATOR_KEY_CHARS:
                raise AssertionError("evidence locator exceeds the durable bound")
            if isinstance(payload["value"], (int, float)) and not isinstance(payload["value"], bool) \
                    and not payload.get("unit"):
                raise AssertionError("a numeric located fact requires an explicit unit")
            if source.get("source_version_kind") is None:
                raise AssertionError("a located fact requires a versioned source")
        existing = self.claims.get(payload["evidence_key"])
        if existing is not None:
            if existing.get("evidence_locator") != locator:
                raise AssertionError("claim evidence locator mismatch")
            return existing
        row = {"id": str(uuid4()), "run_id": str(run_id), **payload}
        self.claims[payload["evidence_key"]] = row
        return row

    def record_evidence_fragment(self, run_id, payload, **kwargs):
        self._assert_lease(run_id, kwargs, "fragment")
        source = next((row for row in self.sources.values()
                       if row["id"] == payload["source_id"]), None)
        if source is None:
            raise AssertionError("invalid evidence fragment source")
        if source.get("task_key") != payload["task_key"]:
            raise AssertionError("evidence fragment task provenance mismatch")
        if len(payload["fragment_text"]) > MAX_FRAGMENT_CHARS:
            raise AssertionError("fragment_text exceeds the durable bound")
        if fragment_content_hash(payload["fragment_text"]) != payload["content_hash"]:
            raise AssertionError("content hash does not match the bounded text")
        kind, locator = payload.get("fragment_type"), payload.get("locator_key")
        if (kind is None) != (locator is None):
            raise AssertionError("a focused fragment requires both a type and a locator")
        if kind is not None:
            if kind not in FRAGMENT_TYPES or len(locator) > MAX_LOCATOR_KEY_CHARS:
                raise AssertionError("unknown fragment type or oversized locator")
            if source.get("source_version_kind") is None:
                raise AssertionError("a focused fragment requires a versioned source")
        existing = self.fragments.get(payload["evidence_key"])
        if existing is not None:
            if any(existing.get(name) != payload.get(name) for name in
                   ("source_id", "task_key", "fragment_text", "content_hash",
                    "fragment_type", "locator_key")):
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
        self.fragments[payload["evidence_key"]] = row
        return row

    # --- the two bounded internal reads the grounding resolver performs ---
    def list_sources_for_ids(self, run_id, source_ids, *, limit=50):
        wanted = {str(item) for item in source_ids}
        rows = [row for row in self.sources.values()
                if row["run_id"] == str(run_id) and row["id"] in wanted]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    def list_evidence_fragments_for_sources(self, run_id, source_ids, *, limit=200):
        wanted = {str(item) for item in source_ids}
        rows = [row for row in self.fragments.values()
                if row["run_id"] == str(run_id) and row["source_id"] in wanted]
        rows.sort(key=lambda row: (row["source_id"], row["fragment_index"], row["content_hash"]))
        return rows[:limit]


@pytest.fixture
def board():
    lease = WorkerLease(uuid4(), "worker-1", 2, "lease-token")
    repository = R3GuardedRepository(lease)
    return EvidenceBoard(repository, lease), repository


def acquisition(board, mappers=None) -> TrustedEvidenceAcquisition:
    return TrustedEvidenceAcquisition(board=board,
                                      mappers=mappers if mappers is not None
                                      else offline_evidence_mappers())


# =============================================================================
# 1. a fact after a long irrelevant introduction is captured, not the intro
# =============================================================================

def test_a_fact_after_a_long_intro_is_captured_and_the_intro_is_not_stored(board):
    evidence, repository = board
    result = document_result()
    assert result["match_start"] > MAX_FRAGMENT_CHARS  # the fact really is out of prefix reach

    # What the PRE-R3 generic extractor would have stored for this exact
    # result: a bounded prefix of the introduction, and nothing else.
    legacy = extract_source_fragments(result)
    assert legacy and legacy[0].startswith("This archive page opens")
    assert "1798" not in legacy[0]

    acquired = acquisition(evidence).acquire(call_record(result, tool="mock.document_archive",
                                                         operation="locate_passage"))
    stored = [row["fragment_text"] for row in acquired.fragments]
    assert stored == [SENTENCE]
    assert not any("editorial preamble" in text for text in stored)
    assert repository.fragments and all(len(row["fragment_text"]) <= MAX_FRAGMENT_CHARS
                                        for row in repository.fragments.values())


# =============================================================================
# 2. a structured record becomes facts, never one arbitrary string
# =============================================================================

def test_a_structured_record_becomes_located_facts_not_a_flattened_string(board):
    evidence, repository = board
    acquired = acquisition(evidence).acquire(call_record(record_result()))

    fields = {row["field_key"]: row for row in acquired.claims}
    assert set(fields) == {"engine_displacement_cc", "fuel_type", "list_price"}
    assert fields["engine_displacement_cc"]["value"] == 1798      # the value itself, not text
    assert fields["fuel_type"]["value"] == "hybrid"
    # No claim, and no fragment, is the whole record rendered as one blob.
    blob = json.dumps(RECORD, sort_keys=True)
    assert all(row["value"] != blob for row in acquired.claims)
    assert all(row["fragment_text"] != blob for row in acquired.fragments)
    # Every fragment is explicitly a PROJECTION and never a quote, and carries
    # the qualifying context (model year, market) alongside the projected field.
    assert {row["fragment_type"] for row in acquired.fragments} == {"structured_projection"}
    engine = next(row for row in acquired.fragments
                  if "engine_displacement_cc" in row["locator_key"])
    assert "model_year=2020" in engine["fragment_text"] and "market=IL" in engine["fragment_text"]
    assert "engine_displacement_cc=1798" in engine["fragment_text"]


# =============================================================================
# 3. value, unit, source version, record id and field locator are exact
# =============================================================================

def test_value_unit_version_record_and_field_locator_are_preserved_exactly(board):
    evidence, repository = board
    acquired = acquisition(evidence).acquire(call_record(record_result()))

    source = acquired.source
    assert (source["source_version_kind"], source["source_version_id"]) == \
        ("dataset_version", "2026.08.1")
    engine = next(row for row in acquired.claims
                  if row["field_key"] == "engine_displacement_cc")
    assert (engine["value"], engine["unit"]) == (1798, "cc")
    assert json.loads(engine["evidence_locator"]) == \
        ["record_field", "rec-1", ["engine_displacement_cc"], None, None, None]
    price = next(row for row in acquired.claims if row["field_key"] == "list_price")
    # The unit of a price is the record's OWN currency field, carried verbatim.
    assert (price["value"], price["unit"]) == (129900.0, "ILS")
    # A non-quantitative fact uses unit=None explicitly rather than inventing one.
    assert next(row for row in acquired.claims
                if row["field_key"] == "fuel_type")["unit"] is None
    # The document path preserves the document's own revision, span and section.
    document = acquisition(evidence).acquire(
        call_record(document_result(), tool="mock.document_archive", operation="locate_passage"))
    assert (document.source["source_version_kind"], document.source["source_version_id"]) == \
        ("document_revision", "rev-7")
    locator = json.loads(document.fragments[0]["locator_key"])
    assert locator[:2] == ["document_span", "doc-1"]
    assert locator[3] == "Engine specifications"
    assert locator[4:] == [len(INTRO), len(INTRO) + len(SENTENCE)]


def test_a_source_version_is_never_a_timestamp_or_a_fragment_hash(board):
    evidence, repository = board
    acquired = acquisition(evidence).acquire(
        call_record(document_result(), tool="mock.document_archive", operation="locate_passage"))
    version = acquired.source["source_version_id"]
    assert version == "rev-7"
    assert version not in {row["content_hash"] for row in acquired.fragments}
    assert version != acquired.source.get("retrieved_at")
    # A snapshot version covers the EXACT FULL snapshot, never the excerpt.
    result = document_result()
    whole = snapshot_version(result)
    assert whole.kind == "content_sha256"
    assert whole.identifier != fragment_content_hash(SENTENCE)
    assert whole.identifier == snapshot_version(dict(result)).identifier  # deterministic


# =============================================================================
# 4. identical text from two different locators is two pieces of evidence
# =============================================================================

def twin_bundle(*, locators, version="2026.08.1", text="Identical evidence sentence.") -> EvidenceBundle:
    """Two fragments with IDENTICAL text read from two different locations."""
    source = VersionedEvidenceSource(
        agent="evidence", url="https://registry.example.test/twins", title="Twins",
        domain="registry.example.test", source_type="structured", source_strength="strong",
        query="twins", tool_operation="mock.structured_registry.get_record",
        version=SourceVersion(kind="dataset_version", identifier=version), confidence=0.9)
    fragments = tuple(FocusedEvidenceFragment(
        fragment_type="structured_projection", text=text, locator=locator,
        fragment_index=index, content_hash=fragment_content_hash(text))
        for index, locator in enumerate(locators))
    fact = StructuredEvidenceFact(entity_key="twins", field_key="note", value=text,
                                 locator=locators[0])
    return build_evidence_bundle(source=source,
                                 locator_scope=tuple({item.record_id for item in locators}),
                                 facts=(fact,), fragments=fragments)


def test_identical_text_from_two_locators_stays_two_distinct_pieces_of_evidence(board):
    evidence, repository = board
    bundle = twin_bundle(locators=(record_field_locator("rec-1", ("note",)),
                                   record_field_locator("rec-2", ("note",))))
    acquired = evidence.record_evidence_bundle(bundle, task_key="task-1")

    assert len({row["id"] for row in acquired.fragments}) == 2
    assert len({row["content_hash"] for row in acquired.fragments}) == 1   # same text
    assert len({row["locator_key"] for row in acquired.fragments}) == 2    # different places
    assert len(repository.fragments) == 2

    # The same holds for two FIELDS of one record, not just two records.
    same_record = twin_bundle(locators=(record_field_locator("rec-9", ("note",)),
                                        record_field_locator("rec-9", ("footnote",))))
    second = evidence.record_evidence_bundle(same_record, task_key="task-1")
    assert len({row["id"] for row in second.fragments}) == 2


# =============================================================================
# 5 & 13. replay and resume after partial persistence are idempotent
# =============================================================================

def test_replaying_the_same_version_locator_and_content_creates_no_duplicates(board):
    evidence, repository = board
    call = call_record(record_result())
    first = acquisition(evidence).acquire(call)
    resumed = EvidenceBoard(repository, evidence.lease)
    second = acquisition(resumed).acquire(call)

    assert first.source["id"] == second.source["id"]
    assert [row["id"] for row in first.fragments] == [row["id"] for row in second.fragments]
    assert [row["id"] for row in first.claims] == [row["id"] for row in second.claims]
    assert (len(repository.sources), len(repository.fragments), len(repository.claims)) == (1, 3, 3)


def test_resume_after_partial_persistence_is_idempotent(board):
    evidence, repository = board
    bundle = offline_evidence_mappers().map(call_record(record_result()))
    # A worker that died after the source and its FIRST fragment were durable.
    partial = evidence.record_source(SourceCreate(
        agent=bundle.source.agent, url=bundle.source.url, title=bundle.source.title,
        domain=bundle.source.domain, source_type=bundle.source.source_type,
        source_strength=bundle.source.source_strength, source_date=bundle.source.source_date,
        query=bundle.source.query, tool_operation=bundle.source.tool_operation),
        task_key="task-1", version=bundle.source.version)
    evidence.record_focused_fragment(partial["id"], bundle.fragments[0], task_key="task-1")
    assert (len(repository.sources), len(repository.fragments)) == (1, 1)

    resumed = EvidenceBoard(repository, evidence.lease)
    acquired = acquisition(resumed).acquire(call_record(record_result()))
    assert acquired.source["id"] == partial["id"]
    assert acquired.fragments[0]["id"] == next(iter(repository.fragments.values()))["id"]
    assert (len(repository.sources), len(repository.fragments), len(repository.claims)) == (1, 3, 3)


# =============================================================================
# 6. a new source version creates new provenance instead of merging
# =============================================================================

def test_a_new_source_version_creates_new_provenance_instead_of_merging(board):
    evidence, repository = board
    first = acquisition(evidence).acquire(call_record(record_result()))
    republished = {**record_result(), "dataset_version": "2026.09.1"}
    second = acquisition(evidence).acquire(call_record(republished))

    assert first.source["id"] != second.source["id"]
    assert second.source["source_version_id"] == "2026.09.1"
    assert len(repository.sources) == 2
    # Evidence never crosses versions: each source owns its own fragments.
    by_source = {row["source_id"] for row in repository.fragments.values()}
    assert by_source == {first.source["id"], second.source["id"]}
    assert len(repository.fragments) == 6 and len(repository.claims) == 6


def test_reusing_one_evidence_key_across_versions_fails_closed(board):
    evidence, repository = board
    source = SourceCreate(agent="a", url="https://example.test/x", title="t",
                          domain="example.test", source_type="structured",
                          source_strength="strong", query="q", tool_operation="t.op")
    evidence.record_source(source, task_key="task-1",
                           version=SourceVersion(kind="dataset_version", identifier="v1"))
    # The version is part of the key, so a genuine caller can never collide;
    # forcing the collision proves the durable guard still refuses to merge.
    stored = next(iter(repository.sources.values()))
    repository.sources["forced"] = {**stored}
    payload = dict(stored)
    payload.update(source_version_id="v2")
    with pytest.raises(AssertionError, match="source version identity conflict"):
        repository.create_source(evidence.lease.run_id, payload, worker_id="worker-1",
                                 attempt=2, lease_token="lease-token")


# =============================================================================
# 7 & 8. missing units, missing versions and missing locators are rejected
# =============================================================================

def test_a_numeric_fact_without_a_unit_is_rejected():
    with pytest.raises(EvidenceContractError) as failure:
        StructuredEvidenceFact(entity_key="rec-1", field_key="engine_displacement_cc",
                               value=1798, locator=record_field_locator("rec-1", ("engine",)))
    assert failure.value.reason_code == "EVIDENCE_FACT_UNIT_REQUIRED"
    # A float is numeric too, and a bool is not.
    with pytest.raises(EvidenceContractError):
        StructuredEvidenceFact(entity_key="rec-1", field_key="price", value=1.5,
                               locator=record_field_locator("rec-1", ("price",)))
    assert StructuredEvidenceFact(entity_key="rec-1", field_key="is_hybrid", value=True,
                                  locator=record_field_locator("rec-1", ("h",))).unit is None
    # Non-quantitative facts may state unit=None explicitly.
    assert StructuredEvidenceFact(entity_key="rec-1", field_key="fuel", value="hybrid",
                                  locator=record_field_locator("rec-1", ("f",))).unit is None


def test_an_r3_source_without_a_version_or_a_fragment_without_a_locator_is_rejected(board):
    evidence, repository = board
    with pytest.raises(EvidenceContractError):
        VersionedEvidenceSource(agent="a", url="https://example.test/x", title="t",
                                domain="example.test", source_type="structured",
                                source_strength="strong", query="q",
                                tool_operation="t.op", confidence=0.9)
    with pytest.raises(EvidenceContractError):
        FocusedEvidenceFragment(fragment_type="verbatim_excerpt", text="text",
                                fragment_index=0, content_hash=fragment_content_hash("text"))
    # Half-specified focus provenance fails closed in the board and in the RPC.
    unversioned = evidence.record_source(
        SourceCreate(agent="a", url="https://example.test/x", title="t", domain="example.test",
                     source_type="structured", source_strength="strong", query="q",
                     tool_operation="t.op"), task_key="task-1")
    with pytest.raises(EvidenceValidationError, match="both a type and a locator"):
        evidence.record_evidence_fragment(unversioned["id"], "text", task_key="task-1",
                                          fragment_type="verbatim_excerpt")
    with pytest.raises(AssertionError, match="requires a versioned source"):
        evidence.record_evidence_fragment(unversioned["id"], "text", task_key="task-1",
                                          fragment_type="verbatim_excerpt",
                                          locator_key=record_field_locator("r", ("f",)).locator_key)
    with pytest.raises(AssertionError, match="requires a versioned source"):
        evidence.record_claim(
            ClaimCreate(entity_key="e", field_key="f", value="v", source_id=unversioned["id"],
                        source_strength="strong", confidence=0.9, agent="a"),
            task_key="task-1", evidence_locator=record_field_locator("r", ("f",)).locator_key)


# =============================================================================
# 9. an operation without a mapper never falls back to prefix extraction
# =============================================================================

def test_an_operation_without_an_evidence_mapper_fails_closed(board):
    evidence, repository = board
    listing = tools().execute("mock.structured_registry", "list_records", CONTEXT,
                              {"dataset": "fixtures"})
    with pytest.raises(EvidenceMappingError) as failure:
        acquisition(evidence).acquire(call_record(listing, operation="list_records"))
    assert failure.value.reason_code == "EVIDENCE_MAPPER_NOT_REGISTERED"
    # Nothing at all was written, and in particular no prefix of the result.
    assert (repository.sources, repository.fragments, repository.claims, repository.writes) == \
        ({}, {}, {}, [])
    # The pre-R3 extractor WOULD have produced material from this same result,
    # which is exactly the fallback R3 must not have.
    assert extract_source_fragments({"rows": ["some text"]})


def test_the_production_mapper_registry_and_tool_registry_stay_empty():
    assert PRODUCTION_EVIDENCE_MAPPERS.registered == frozenset()
    assert PRODUCTION_EVIDENCE_MAPPERS.mapper_for("mock.structured_registry", "get_record") is None
    assert ToolRegistry().allowed_names == frozenset()
    # The offline fixtures are registered nowhere but in the test allowlist.
    assert offline_evidence_mappers().registered == {
        ("mock.structured_registry", "get_record"), ("mock.document_archive", "locate_passage")}


# =============================================================================
# 10. worker output and model completions cannot become evidence
# =============================================================================

@pytest.mark.parametrize("forged", [
    {"tool": "mock.structured_registry", "operation": "get_record", "result": RECORD},
    SimpleNamespace(task_id="task-1", call_id="c1", tool="mock.structured_registry",
                    operation="get_record", result=RECORD),
    '{"record": {"engine_displacement_cc": 1798}}',
    None,
])
def test_worker_output_and_model_completions_cannot_enter_the_evidence_path(board, forged):
    evidence, repository = board
    with pytest.raises(EvidenceMappingError) as failure:
        acquisition(evidence).acquire(forged)
    assert failure.value.reason_code == "EVIDENCE_SOURCE_NOT_TRUSTED"
    assert repository.writes == []


def test_a_mapper_may_not_attribute_evidence_to_another_operation(board):
    evidence, repository = board
    mappers = EvidenceMapperRegistry((StructuredRegistryEvidenceMapper(),))
    forged = call_record(record_result(), tool="mock.structured_registry", operation="get_record")

    class Impostor:
        tool, operation = "mock.structured_registry", "get_record"

        def map(self, call):
            bundle = StructuredRegistryEvidenceMapper().map(call)
            return bundle.model_copy(update={"source": bundle.source.model_copy(
                update={"tool_operation": "yeda.official.lookup"})})

    with pytest.raises(EvidenceMappingError) as failure:
        acquisition(evidence, EvidenceMapperRegistry((Impostor(),))).acquire(forged)
    assert failure.value.reason_code == "EVIDENCE_BUNDLE_PROVENANCE_MISMATCH"
    assert repository.writes == []
    assert mappers.map(forged).source.tool_operation == "mock.structured_registry.get_record"


def test_a_mapper_that_returns_anything_but_a_bundle_fails_closed(board):
    evidence, repository = board

    class Broken:
        tool, operation = "mock.structured_registry", "get_record"

        def map(self, call):
            return {"source": "trust me"}

    class Exploding(Broken):
        def map(self, call):
            raise RuntimeError("secret sentinel in a traceback")

    for mapper, reason in ((Broken(), "EVIDENCE_BUNDLE_INVALID"),
                           (Exploding(), "EVIDENCE_BUNDLE_INVALID")):
        with pytest.raises(EvidenceMappingError) as failure:
            acquisition(evidence, EvidenceMapperRegistry((mapper,))).acquire(
                call_record(record_result()))
        assert failure.value.reason_code == reason
        # The mapper's own message never travels with the classification.
        assert "secret sentinel" not in str(failure.value)
    assert repository.writes == []


# =============================================================================
# 11. text, count, total-size, depth and locator limits fail before persistence
# =============================================================================

def test_text_count_total_size_depth_and_locator_limits_are_rejected(board):
    evidence, repository = board
    locator = record_field_locator("rec-1", ("note",))
    text = "x" * MAX_FRAGMENT_CHARS

    def fragment(index, body):
        return FocusedEvidenceFragment(fragment_type="structured_projection", text=body,
                                       locator=record_field_locator("rec-1", (f"n{index}",)),
                                       fragment_index=index,
                                       content_hash=fragment_content_hash(body))

    fact = StructuredEvidenceFact(entity_key="rec-1", field_key="note", value="v", locator=locator)
    source = VersionedEvidenceSource(
        agent="a", url="https://example.test/x", title="t", domain="example.test",
        source_type="structured", source_strength="strong", query="q",
        tool_operation="mock.structured_registry.get_record",
        version=SourceVersion(kind="dataset_version", identifier="v1"), confidence=0.9)

    # TEXT: one character past the durable fragment bound.
    with pytest.raises(EvidenceContractError):
        fragment(0, "x" * (MAX_FRAGMENT_CHARS + 1))
    # COUNT: a fifth fragment for one source has no legal position at all, so
    # it is refused one level before a bundle could even be assembled...
    with pytest.raises(EvidenceContractError):
        fragment(MAX_FRAGMENTS_PER_SOURCE, "fifth")
    # ...and the durable boundary refuses it again if one is forced through.
    versioned = evidence.record_source(
        SourceCreate(agent="a", url="https://example.test/x", title="t", domain="example.test",
                     source_type="structured", source_strength="strong", query="q",
                     tool_operation="t.op"),
        task_key="task-1", version=SourceVersion(kind="dataset_version", identifier="v1"))
    for index in range(MAX_FRAGMENTS_PER_SOURCE):
        evidence.record_focused_fragment(versioned["id"], fragment(index, f"{index}text"),
                                         task_key="task-1")
    with pytest.raises(AssertionError, match="count limit reached"):
        evidence.record_evidence_fragment(
            versioned["id"], "one too many", task_key="task-1", fragment_index=0,
            fragment_type="structured_projection",
            locator_key=record_field_locator("rec-1", ("extra",)).locator_key)
    # TOTAL SIZE: four maximum-length fragments exceed the per-source budget.
    assert MAX_FRAGMENTS_PER_SOURCE * MAX_FRAGMENT_CHARS > MAX_FRAGMENT_TOTAL_CHARS_PER_SOURCE
    with pytest.raises(EvidenceContractError) as failure:
        build_evidence_bundle(source=source, locator_scope=("rec-1",), facts=(fact,),
                              fragments=tuple(fragment(i, f"{i}{text[1:]}") for i in range(4)))
    assert failure.value.reason_code == "EVIDENCE_LIMIT_EXCEEDED"
    # FACT COUNT: more facts than one bundle may carry.
    with pytest.raises(EvidenceContractError):
        build_evidence_bundle(source=source, locator_scope=("rec-1",),
                              facts=tuple(fact for _ in range(MAX_FACTS_PER_BUNDLE + 1)),
                              fragments=(fragment(0, "text"),))
    # DEPTH: a structured value nested past the contract's depth bound.
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    with pytest.raises(EvidenceContractError) as failure:
        StructuredEvidenceFact(entity_key="rec-1", field_key="note", value=deep, locator=locator)
    assert failure.value.reason_code == "EVIDENCE_VALUE_INVALID"
    # LOCATOR: too many segments, an oversized record id, and a projection
    # whose text would exceed the durable bound (rejected, never trimmed).
    with pytest.raises(EvidenceContractError):
        record_field_locator("rec-1", tuple(f"s{i}" for i in range(MAX_LOCATOR_PATH_SEGMENTS + 1)))
    with pytest.raises(EvidenceContractError):
        record_field_locator("r" * 129, ("note",))
    with pytest.raises(EvidenceContractError) as failure:
        canonical_projection({"note": "y" * (MAX_FRAGMENT_CHARS + 1)}, ("note",))
    assert failure.value.reason_code == "EVIDENCE_LIMIT_EXCEEDED"


@pytest.mark.parametrize("field_path", [
    ("$..price",), ("records[0]",), ("*",), ("a b",), ("../secret",), ("",),
    ("price?filter=1",), ("a'b",),
])
def test_a_locator_is_never_a_query_expression(field_path):
    with pytest.raises(EvidenceContractError) as failure:
        record_field_locator("rec-1", field_path)
    assert failure.value.reason_code in {"EVIDENCE_LOCATOR_INVALID", "EVIDENCE_CONTRACT_INVALID"}


def test_locator_shapes_are_closed_and_a_span_can_never_hold_a_page():
    with pytest.raises(EvidenceContractError):   # a record locator with offsets
        EvidenceLocator(kind="record_field", record_id="rec-1", field_path=("note",),
                        char_start=0, char_end=5)
    with pytest.raises(EvidenceContractError):   # a span locator with a field path
        EvidenceLocator(kind="document_span", record_id="doc-1", field_path=("note",),
                        char_start=0, char_end=5)
    with pytest.raises(EvidenceContractError):   # an inverted span
        document_span_locator("doc-1", 10, 10)
    with pytest.raises(EvidenceContractError) as failure:   # a whole-page span
        document_span_locator("doc-1", 0, MAX_FRAGMENT_CHARS + 1)
    assert failure.value.reason_code == "EVIDENCE_LIMIT_EXCEEDED"
    assert len(document_span_locator("doc-1", 0, 10, section="Engine").locator_key) \
        <= MAX_LOCATOR_KEY_CHARS


def test_a_fragment_type_matches_its_locator_shape_structurally():
    span = document_span_locator("doc-1", 0, len(SENTENCE))
    field = record_field_locator("rec-1", ("note",))
    # A projection can never be dressed up as a verbatim quote, and a document
    # excerpt can never claim to be a record projection.
    with pytest.raises(EvidenceContractError) as failure:
        FocusedEvidenceFragment(fragment_type="verbatim_excerpt", text=SENTENCE, locator=field,
                                fragment_index=0, content_hash=fragment_content_hash(SENTENCE))
    assert failure.value.reason_code == "EVIDENCE_LOCATOR_INVALID"
    with pytest.raises(EvidenceContractError):
        FocusedEvidenceFragment(fragment_type="structured_projection", text=SENTENCE,
                                locator=span, fragment_index=0,
                                content_hash=fragment_content_hash(SENTENCE))


def test_every_contract_is_closed_to_unknown_fields():
    with pytest.raises(EvidenceContractError):
        SourceVersion(kind="dataset_version", identifier="v1", extra="x")
    with pytest.raises(EvidenceContractError):
        EvidenceLocator(kind="record_field", record_id="r", field_path=("f",), extra="x")
    with pytest.raises(EvidenceContractError):
        StructuredEvidenceFact(entity_key="e", field_key="f", value="v", extra="x",
                               locator=record_field_locator("r", ("f",)))


@pytest.mark.parametrize("kind,identifier", [
    ("dataset_version", ""), ("git_commit", "zzzzzzz"), ("content_sha256", "abc"),
    ("document_revision", "rev 7"), ("dataset_version", "v" * 129),
])
def test_a_malformed_source_version_is_rejected(kind, identifier):
    with pytest.raises(EvidenceContractError):
        SourceVersion(kind=kind, identifier=identifier)


# =============================================================================
# 12. a mismatched hash, or a locator outside the source, is rejected
# =============================================================================

def test_a_mismatched_content_hash_is_rejected(board):
    evidence, repository = board
    with pytest.raises(EvidenceContractError) as failure:
        FocusedEvidenceFragment(fragment_type="structured_projection", text="real text",
                                locator=record_field_locator("rec-1", ("note",)),
                                fragment_index=0,
                                content_hash=fragment_content_hash("different text"))
    assert failure.value.reason_code == "EVIDENCE_FRAGMENT_HASH_MISMATCH"
    # And the durable boundary recomputes it too, exactly as PostgreSQL does.
    versioned = evidence.record_source(
        SourceCreate(agent="a", url="https://example.test/x", title="t", domain="example.test",
                     source_type="structured", source_strength="strong", query="q",
                     tool_operation="t.op"),
        task_key="task-1", version=SourceVersion(kind="dataset_version", identifier="v1"))
    payload = {"source_id": versioned["id"], "fragment_text": "real text",
               "content_hash": fragment_content_hash("other"), "fragment_index": 0,
               "fragment_type": "structured_projection",
               "locator_key": record_field_locator("rec-1", ("note",)).locator_key,
               "task_key": "task-1", "evidence_key": "forced"}
    with pytest.raises(AssertionError, match="content hash does not match"):
        repository.record_evidence_fragment(evidence.lease.run_id, payload, worker_id="worker-1",
                                            attempt=2, lease_token="lease-token")


def test_a_locator_that_does_not_belong_to_the_source_is_rejected():
    result = document_result()
    # A span past the end of the document the tool actually returned.
    with pytest.raises(EvidenceContractError) as failure:
        verbatim_excerpt(document_text=result["text"],
                         locator=document_span_locator("doc-1", len(result["text"]) - 2,
                                                       len(result["text"]) + 60),
                         fragment_index=0)
    assert failure.value.reason_code == "EVIDENCE_LOCATOR_OUT_OF_SCOPE"
    # A record field the record does not have.
    with pytest.raises(EvidenceContractError) as failure:
        structured_projection(record=RECORD, fields=("model_name",),
                              locator=record_field_locator("rec-1", ("no_such_field",)),
                              fragment_index=0)
    assert failure.value.reason_code == "EVIDENCE_LOCATOR_OUT_OF_SCOPE"
    # A projection may only include fields the record actually holds.
    with pytest.raises(EvidenceContractError):
        canonical_projection(RECORD, ("model_name", "invented_field"))


def test_a_bundle_rejects_a_locator_outside_its_declared_scope():
    source = VersionedEvidenceSource(
        agent="a", url="https://example.test/x", title="t", domain="example.test",
        source_type="structured", source_strength="strong", query="q",
        tool_operation="mock.structured_registry.get_record",
        version=SourceVersion(kind="dataset_version", identifier="v1"), confidence=0.9)
    stray = record_field_locator("rec-999", ("note",))
    fragment = FocusedEvidenceFragment(fragment_type="structured_projection", text="t",
                                       locator=stray, fragment_index=0,
                                       content_hash=fragment_content_hash("t"))
    fact = StructuredEvidenceFact(entity_key="rec-1", field_key="note", value="v",
                                  locator=record_field_locator("rec-1", ("note",)))
    with pytest.raises(EvidenceContractError) as failure:
        build_evidence_bundle(source=source, locator_scope=("rec-1",), facts=(fact,),
                              fragments=(fragment,))
    assert failure.value.reason_code == "EVIDENCE_LOCATOR_OUT_OF_SCOPE"


# =============================================================================
# 14. grounding receives the provenance; public APIs expose nothing new
# =============================================================================

def resolved(repository, run_id, source_id) -> ResolvedSourceEvidence:
    resolver = RepositoryEvidenceResolver(repository, run_id=run_id)
    reference = EvidenceReference(claim_id="c", source_id=source_id, run_id=str(run_id),
                                  task_id="task-1", field="f", value=1, confidence=0.9)
    return resolver.resolve([reference])[source_id]


def test_grounding_receives_the_unit_source_version_and_locator(board):
    evidence, repository = board
    acquired = acquisition(evidence).acquire(call_record(record_result()))

    references = [EvidenceReference.model_validate(item) for item in evidence.references()]
    engine = next(item for item in references if item.field == "engine_displacement_cc")
    assert (engine.unit, engine.source_version) == ("cc", "dataset_version:2026.08.1")
    assert json.loads(engine.locator)[:3] == ["record_field", "rec-1", ["engine_displacement_cc"]]

    context = resolved(repository, evidence.lease.run_id, acquired.source["id"])
    assert context.source_version == "dataset_version:2026.08.1"
    assert {item.fragment_type for item in context.fragments} == {"structured_projection"}
    assert all(item.locator for item in context.fragments)

    # ... and the verifier payload carries it, once, in the right block.
    candidate = GroundedCandidate(reference=engine, source=context)
    document = json.loads(serialize_verifier_candidates([candidate]))
    assert document["claims"][0]["unit"] == "cc"
    assert document["claims"][0]["locator"] == engine.locator
    assert document["sources"][0]["source_version"] == "dataset_version:2026.08.1"
    assert all(item["fragment_type"] == "structured_projection"
               for item in document["sources"][0]["fragments"])


def test_pre_r3_evidence_still_resolves_and_serializes_exactly_as_before():
    """Historical records stay readable; absent provenance is omitted, not null."""
    legacy_fragment = SourceFragment(fragment_index=0, text=SENTENCE,
                                     content_hash=fragment_content_hash(SENTENCE))
    legacy_source = ResolvedSourceEvidence(
        source_id="source-1", task_id="task-1", url="https://example.test/x", title="t",
        domain="example.test", source_type="primary", source_strength="strong",
        source_date=None, fragments=(legacy_fragment,))
    reference = EvidenceReference(claim_id="c", source_id="source-1", run_id="r",
                                  task_id="task-1", field="f", value=1, confidence=0.9)
    document = json.loads(serialize_verifier_candidates(
        [GroundedCandidate(reference=reference, source=legacy_source)]))
    assert "unit" not in document["claims"][0] and "locator" not in document["claims"][0]
    assert "source_version" not in document["sources"][0]
    assert set(document["sources"][0]["fragments"][0]) == {"fragment_index", "content_hash", "text"}
    # Readable, and honestly NOT complete R3 evidence.
    assert legacy_source.source_version is None and not legacy_source.is_r3_qualified


def test_half_specified_grounding_provenance_fails_closed():
    for kwargs in ({"fragment_type": "verbatim_excerpt"}, {"locator": "some-locator"},
                   {"fragment_type": "made_up", "locator": "l"},
                   {"fragment_type": "verbatim_excerpt", "locator": "l" * (MAX_LOCATOR_KEY_CHARS + 1)}):
        with pytest.raises(GroundingContractError):
            SourceFragment(fragment_index=0, text=SENTENCE,
                           content_hash=fragment_content_hash(SENTENCE), **kwargs)
    for version in ("dataset_version", "made_up:v1", "x" * 300):
        with pytest.raises(GroundingContractError):
            ResolvedSourceEvidence(source_id="s", task_id="t", url="u", title="t",
                                   domain="d", source_type="p", source_strength="strong",
                                   source_date=None, source_version=version)


def test_a_claim_may_only_be_grounded_by_the_version_it_was_read_at():
    context = ResolvedSourceEvidence(
        source_id="source-1", task_id="task-1", url="u", title="t", domain="d",
        source_type="structured", source_strength="strong", source_date=None,
        source_version="dataset_version:2026.09.1")
    stale = EvidenceReference(claim_id="c", source_id="source-1", run_id="r", task_id="task-1",
                              field="f", value=1, confidence=0.9,
                              source_version="dataset_version:2026.08.1")
    with pytest.raises(GroundingContractError):
        GroundedCandidate(reference=stale, source=context)
    # A pre-R3 reference is not held to a version it never had.
    legacy = stale.model_copy(update={"source_version": None})
    assert GroundedCandidate(reference=legacy, source=context).claim_id == "c"


def test_the_public_api_contracts_expose_no_r3_evidence_provenance():
    """R3 provenance is INTERNAL: it is added by the trusted board, never by
    the worker-facing HTTP schemas whose rows become browser run events."""
    assert not {"source_version_kind", "source_version_id"} & set(SourceCreate.model_fields)
    assert "evidence_locator" not in ClaimCreate.model_fields
    from pathlib import Path
    api = Path("backend/main.py").read_text()
    for internal in ("source_version_kind", "evidence_locator", "locator_key",
                     "fragment_type", "source_evidence_fragments"):
        assert internal not in api


# =============================================================================
# integration: validated ToolCallRecord -> ... -> grounding, through the worker
# =============================================================================

class StubGateway:
    """A worker model that returns one scripted, schema-valid completion."""

    def __init__(self):
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"answer": "recorded"}


def planned_task(call_id: str, tool: str, operation: str, arguments: dict):
    from backend.engines.swarm_v2 import TaskGraph

    return TaskGraph.model_validate({"tasks": [{
        "task_id": "task-1", "goal": "read one record", "scope": "offline fixtures only",
        "dependencies": [], "tools": [{"call_id": call_id, "name": tool,
                                       "operation": operation, "arguments": arguments,
                                       "dependency_bindings": []}],
        "output_schema": OUTPUT_SCHEMA,
        "evidence": {"minimum_sources": 1, "required_fields": ["answer"], "min_confidence": 0.5},
        "priority": 50, "recursion_depth": 0, "estimated_cost_units": 1,
        "completion": {"required_outputs": ["answer"], "evidence_satisfied": True,
                       "allow_partial": False}}]}).tasks[0]


def test_the_whole_r3_flow_runs_from_a_validated_tool_call_to_grounding(board):
    """validated ToolCallRecord -> trusted mapper -> versioned source ->
    structured fact -> focused fragment -> lease-guarded persistence ->
    grounding, with no model deciding any of it."""
    evidence, repository = board
    gateway = StubGateway()
    sink = acquisition(evidence)
    worker = GenericWorker(gateway=gateway, tools=tools(), model="fake", tool_context=CONTEXT,
                           tool_result_sink=sink)

    result = worker.execute(planned_task("c1", "mock.structured_registry", "get_record",
                                         {"record_id": "rec-1"}), {})
    assert result.status == "completed" and result.output == {"answer": "recorded"}
    # Persistence happened during the TOOL loop, before the model was asked
    # anything: the completion cannot have influenced the evidence.
    assert repository.writes == ["source", "fragment", "fragment", "fragment",
                                 "claim", "claim", "claim"]
    assert len(gateway.calls) == 1

    source = next(iter(repository.sources.values()))
    assert (source["source_version_kind"], source["source_version_id"]) == \
        ("dataset_version", "2026.08.1")
    assert source["task_key"] == "task-1"       # server-resolved plan provenance

    references = [EvidenceReference.model_validate(item) for item in evidence.references()]
    contexts = resolve_source_context(
        RepositoryEvidenceResolver(repository, run_id=evidence.lease.run_id), references)
    assert len(contexts) == 3
    grounded = contexts[references[0].claim_id]
    assert grounded.source_version == "dataset_version:2026.08.1"
    assert len(grounded.fragments) == 3
    assert all(item.fragment_type == "structured_projection" for item in grounded.fragments)
    assert grounded.is_r3_qualified


def test_the_document_flow_captures_the_passage_not_the_page(board):
    evidence, repository = board
    worker = GenericWorker(gateway=StubGateway(), tools=tools(), model="fake",
                           tool_context=CONTEXT, tool_result_sink=acquisition(evidence))
    assert worker.execute(planned_task("c1", "mock.document_archive", "locate_passage",
                                       {"document_id": "doc-1",
                                        "field": "engine_displacement_cc"}), {}).status == "completed"
    fragment = next(iter(repository.fragments.values()))
    assert fragment["fragment_text"] == SENTENCE
    assert fragment["fragment_type"] == "verbatim_excerpt"
    assert len(fragment["fragment_text"]) < len(INTRO)   # never the whole page
    claim = next(iter(repository.claims.values()))
    assert (claim["value"], claim["unit"]) == (1798, "cc")


def test_no_real_vehicle_or_web_tool_is_registered_or_mapped():
    """R3 proves the contract offline; R5 connects real sources."""
    for name in ("yeda", "gov", "ckan", "web", "http", "search_engine"):
        assert not any(name in registered for registered in ToolRegistry().allowed_names)
        assert not any(name in tool for tool, _ in PRODUCTION_EVIDENCE_MAPPERS.registered)
    from pathlib import Path
    worker_main = Path("backend/worker/main.py").read_text()
    assert "tools = ToolRegistry()" in worker_main
    assert "tool_result_sink" in worker_main and "deliberately left unwired" in worker_main


# =============================================================================
# backward compatibility: pre-R3 identities and the retired generic extractor
# =============================================================================

def test_a_pre_r3_source_and_claim_replay_to_their_exact_pre_r3_identity(board):
    """A resumed pre-R3 run must land on the rows it already wrote.

    The R3 columns participate in identity only when they are PRESENT, so the
    evidence_key of a version-less source and a locator-less claim is byte-for
    -byte what the pre-R3 release computed.
    """
    import hashlib

    from backend.engines.swarm_v2.evidence import safe_durable_value

    def pre_r3_key(kind, payload):
        encoded = json.dumps(safe_durable_value(payload), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=True)
        return f"{kind}:{hashlib.sha256(encoded.encode()).hexdigest()}"

    evidence, repository = board
    source = SourceCreate(agent="worker", url="https://example.test/a", title="Evidence",
                          domain="example.test", source_type="primary",
                          source_strength="strong", query="q", tool_operation="search")
    row = evidence.record_source(source, task_key="task-1")
    assert row["evidence_key"] == pre_r3_key(
        "source", {"task_key": "task-1", **source.model_dump(mode="json")})

    claim = ClaimCreate(entity_key="vehicle:1", field_key="price", value=100,
                        time_scope={"as_of": "2026-08"}, market="IL", source_id=row["id"],
                        source_strength="strong", confidence=0.9, agent="worker")
    stored = evidence.record_claim(claim, task_key="task-1")
    assert stored["evidence_key"] == pre_r3_key(
        "claim", {"task_key": "task-1", **claim.model_dump(mode="json")})

    # And a pre-R3 fragment keeps its (task, source, content hash) identity.
    fragment = evidence.record_evidence_fragment(row["id"], "legacy text", task_key="task-1")
    assert fragment["evidence_key"] == pre_r3_key(
        "fragment", {"task_key": "task-1", "source_id": row["id"],
                     "content_hash": fragment_content_hash("legacy text")})
    assert fragment["fragment_type"] is None and fragment["locator_key"] is None


def test_the_r3_path_never_calls_the_generic_prefix_extractor(board, monkeypatch):
    evidence, repository = board

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the R3 path must never fall back to prefix extraction")

    monkeypatch.setattr("backend.engines.swarm_v2.evidence.extract_source_fragments", forbidden)
    acquired = acquisition(evidence).acquire(call_record(record_result()))
    assert len(acquired.fragments) == 3
    # The legacy helper is still importable for historical callers...
    assert extract_source_fragments({"rows": ["still supported"]}) == ["still supported"]
    # ...and the legacy path is the only caller left in the board.
    from pathlib import Path
    source = Path("backend/engines/swarm_v2/evidence.py").read_text()
    assert source.count("extract_source_fragments(") == 1
