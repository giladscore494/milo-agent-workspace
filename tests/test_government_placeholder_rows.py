"""PR-U S4: a placeholder register row is never handed to a run.

Run 6825eb96 queued register record 37363 -- Toyota, kinuy_mishari "11111",
degem_nm "11111111", trim SE, 2026 -- as a Mapping Plan candidate, and the run
"resolved" it as a real vehicle.

The ONE rule (`preparation.is_placeholder_identity`): a row is a placeholder
when its commercial model (kinuy_mishari) OR its official model code (degem_nm),
trimmed, matches ``^([0-9])\\1{2,}$``. At run preparation such a batch item is
left out of the work queue and counted in the preparation record under the
static reason EXCLUDED_PLACEHOLDER_SOURCE_RECORD, with its upstream_record_id.
The capture is untouched: the raw record and the candidate row are written and
kept exactly as before, and no table is added.
"""

from __future__ import annotations

import copy
from uuid import UUID, uuid4

import pytest

from backend.catalog.government import preparation as prep
from backend.catalog.government import source as src
from backend.catalog.government.preparation import (ARTIFACT_KEY,
                                                    EXCLUDED_PLACEHOLDER_SOURCE_RECORD,
                                                    PREPARATION_PHASE,
                                                    GovernmentPreparationError,
                                                    is_placeholder_identity,
                                                    prepare_government_work)
from backend.testing.memory_repository import MemoryRepository
from backend.testing.work_scope_seed import (committed_records, seed_prepared_plan,
                                             start_batch_run)

PLACEHOLDER_ID = "37363"


# =============================================================================
# the rule
# =============================================================================

@pytest.mark.parametrize("commercial_model,official_model_code,expected", [
    ("11111", "11111111", True),        # record 37363 exactly
    ("11111", None, True),              # either field is enough
    ("4RUNNER", "11111111", True),
    ("4RUNNER", "000", True),
    (" 222 ", None, True),              # trimmed first
    ("999\n", None, True),
    ("111", None, True),                # three is the minimum
    ("4RUNNER", "TRN285L-GKTSKA", False),
    ("1111A", None, False),             # not only one repeated digit
    ("12345", None, False),             # digits, but not one repeated digit
    ("00", None, False),                # fewer than three
    ("11", "22", False),
    ("١١١", None, False),               # ASCII digits only
    ("", None, False),
    (None, None, False),
])
def test_the_one_placeholder_rule(commercial_model, official_model_code, expected):
    assert is_placeholder_identity(commercial_model, official_model_code) is expected


# =============================================================================
# the batch
# =============================================================================

def _row(base: dict, record_id: int, *, model: str, code: str, trim: str,
         year: int = 2026) -> dict:
    row = copy.deepcopy(base)
    row.update({"_id": record_id, "kinuy_mishari": model, "degem_nm": code,
                "ramat_gimur": trim, "shnat_yitzur": year})
    return row


def register_rows() -> list[dict]:
    base = committed_records(1)[0]
    return [
        _row(base, 37363, model="11111", code="11111111", trim="SE"),        # placeholder
        _row(base, 37254, model="4RUNNER", code="TRN285L-GKTSKA", trim="SR5"),
        _row(base, 37291, model="1111A", code="TRN285L-GKTLKA", trim="LIMITED"),
        _row(base, 37293, model="12345", code="TRN285L-GKTXKA", trim="TRD PRO"),
        _row(base, 37096, model="4RUNNER", code="00", trim="TRAILHUNTER"),
    ]


def seeded_batch(rows: list[dict]) -> tuple[MemoryRepository, str]:
    repository = MemoryRepository()
    user, project = str(uuid4()), str(uuid4())
    repository.seed_user(user)
    repository.seed_project(project, "gov-prep", "Gov prep", [user], workflow_key="swarm_v2")
    conversation = repository.create_conversation(UUID(project), "c", UUID(user))["id"]
    plan = seed_prepared_plan(repository, user_id=user, conversation_id=conversation,
                              units=("toyota",), max_items=20, batch_size=20, records=rows)
    run = start_batch_run(repository, plan)["run"]["id"]
    return repository, run


def _capture_state(repository: MemoryRepository) -> tuple:
    return (copy.deepcopy(repository.catalog_raw_records),
            copy.deepcopy(repository.catalog_candidates),
            copy.deepcopy(repository.catalog_snapshots))


def test_the_37363_shape_is_excluded_and_counted_and_normal_rows_are_kept():
    repository, run = seeded_batch(register_rows())
    batch = repository.work_scope_batch_for_run(UUID(run))["items"]
    assert len(batch) == 5                                   # the batch still holds it
    preparation = prepare_government_work(repository, run_id=run)

    models = sorted(item.commercial_model for item in preparation.queue)
    assert models == ["1111A", "12345", "4RUNNER", "4RUNNER"]
    assert "11111" not in models
    assert preparation.excluded == ((PLACEHOLDER_ID, EXCLUDED_PLACEHOLDER_SOURCE_RECORD),)
    assert preparation.excluded_placeholder == 1
    assert preparation.total_candidates == 4

    artifact = preparation.as_artifact()
    assert artifact["excluded_placeholder"] == 1
    assert artifact["excluded_records"] == [
        {"upstream_record_id": PLACEHOLDER_ID, "reason": EXCLUDED_PLACEHOLDER_SOURCE_RECORD}]
    assert [item["commercial_model"] for item in artifact["queue"]] == \
        [item.commercial_model for item in preparation.queue]
    context = preparation.work_context({})
    assert "11111" not in {item["commercial_model"] for item in context["items"]}
    assert context["remaining"] == 4


def test_a_batch_without_placeholders_is_unchanged_and_reads_nothing_more(monkeypatch):
    rows = [row for row in register_rows() if row["_id"] != 37363]
    repository, run = seeded_batch(rows)

    def no_projection(*_args, **_kwargs):
        raise AssertionError("no placeholder, so no extra read")
    monkeypatch.setattr(prep, "GovernmentCatalogProjection", no_projection)
    preparation = prepare_government_work(repository, run_id=run)
    assert len(preparation.queue) == 4
    assert preparation.excluded == () and preparation.excluded_placeholder == 0
    assert preparation.as_artifact()["excluded_records"] == []


def test_capture_raw_records_and_the_snapshot_hash_are_untouched():
    rows = register_rows()
    repository, run = seeded_batch(rows)
    before = _capture_state(repository)
    (snapshot,) = [row for row in repository.catalog_snapshots.values()
                   if row.get("activated_at")]
    content_sha256 = snapshot["content_sha256"]

    prepare_government_work(repository, run_id=run)

    assert _capture_state(repository) == before
    # The placeholder row was captured exactly like any other: its raw record
    # holds the row verbatim, and its candidate row exists and is unmodified.
    raw = [row for row in repository.catalog_raw_records.values()
           if str(row["upstream_record_id"]) == PLACEHOLDER_ID]
    assert len(raw) == 1 and raw[0]["payload"] == rows[0]
    candidates = [row for row in repository.catalog_candidates.values()
                  if row["raw_record_id"] == raw[0]["id"]]
    assert len(candidates) == 1 and candidates[0]["commercial_model"] == "11111"
    (after,) = [row for row in repository.catalog_snapshots.values() if row.get("activated_at")]
    assert after["content_sha256"] == content_sha256
    assert int(after["stored_record_count"]) == len(rows)


def test_a_batch_of_only_placeholders_is_refused_before_any_work():
    base = committed_records(1)[0]
    rows = [_row(base, 37363, model="11111", code="11111111", trim="SE"),
            _row(base, 37364, model="RAV4", code="0000", trim="XLE")]
    repository, run = seeded_batch(rows)
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(repository, run_id=run)
    assert refused.value.code == "GOVERNMENT_BATCH_ONLY_PLACEHOLDERS"
    assert refused.value.safe_message == prep.PREPARATION_REASONS[refused.value.code]


def test_a_resumed_attempt_keeps_the_same_queue_and_the_same_exclusions():
    repository, run = seeded_batch(register_rows())
    first = prepare_government_work(repository, run_id=run)
    checkpoint = {"phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: first.as_artifact()}}
    resumed = prepare_government_work(repository, run_id=run, checkpoint=checkpoint)
    assert resumed.resumed is True
    assert resumed.queue == first.queue
    assert resumed.excluded == first.excluded
    assert resumed.as_artifact() == first.as_artifact()


def test_a_record_written_before_pr_u_resumes_with_no_exclusions():
    repository, run = seeded_batch([row for row in register_rows() if row["_id"] != 37363])
    record = prepare_government_work(repository, run_id=run).as_artifact()
    record.pop("excluded_placeholder")
    record.pop("excluded_records")
    resumed = prepare_government_work(
        repository, run_id=run,
        checkpoint={"phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: record}})
    assert resumed.excluded == ()


@pytest.mark.parametrize("corrupt", [
    lambda record: record.update({"excluded_placeholder": 2}),
    lambda record: record.update({"excluded_records": "37363"}),
    lambda record: record["excluded_records"][0].update({"reason": "SOMETHING_ELSE"}),
    lambda record: record["excluded_records"][0].update({"note": "free text"}),
    lambda record: record.pop("excluded_records"),
])
def test_a_corrupt_exclusion_record_is_refused_not_repaired(corrupt):
    repository, run = seeded_batch(register_rows())
    record = prepare_government_work(repository, run_id=run).as_artifact()
    corrupt(record)
    with pytest.raises(GovernmentPreparationError) as refused:
        prepare_government_work(
            repository, run_id=run,
            checkpoint={"phase": PREPARATION_PHASE, "artifacts": {ARTIFACT_KEY: record}})
    assert refused.value.code == "GOVERNMENT_PREPARATION_RECORD_INVALID"


def test_the_exclusion_reads_the_register_id_from_the_pinned_snapshot_only():
    repository, run = seeded_batch(register_rows())
    seen: list[str | None] = []
    real = prep.GovernmentCatalogProjection

    def spy(repo, *, resource_id, snapshot_key):
        seen.append(snapshot_key)
        assert resource_id == src.WLTP_RESOURCE_ID
        return real(repo, resource_id=resource_id, snapshot_key=snapshot_key)

    prep.GovernmentCatalogProjection = spy
    try:
        preparation = prepare_government_work(repository, run_id=run)
    finally:
        prep.GovernmentCatalogProjection = real
    assert seen == [preparation.snapshot_key]


# =============================================================================
# the real worker
# =============================================================================

def test_the_worker_hands_the_commander_no_placeholder_and_reports_the_count(
        monkeypatch, capsys):
    import json

    import backend.worker.main as worker_main
    from backend.catalog.execution import (CATALOG_EXECUTION_FLAG, CATALOG_PROMOTION_FLAG,
                                           GOVERNMENT_READ_FLAG)
    from test_swarm_v2_smoke_offline import (USER, FakeKimiCompletions, build_repo,
                                             patch_client, swarm_env)

    swarm_env(monkeypatch, **{CATALOG_EXECUTION_FLAG: "true", GOVERNMENT_READ_FLAG: "true",
                              CATALOG_PROMOTION_FLAG: "false"})
    repository, conversation_id = build_repo()
    plan = seed_prepared_plan(repository, user_id=USER, conversation_id=conversation_id,
                              units=("toyota",), max_items=20, batch_size=20,
                              records=register_rows())
    run_id = UUID(start_batch_run(repository, plan)["run"]["id"])
    completions = FakeKimiCompletions()
    patch_client(monkeypatch, completions)

    assert worker_main.execute_run(run_id, repository) == 0

    (prepared,) = [event for event in repository.run_events
                   if str(event["run_id"]) == str(run_id)
                   and event["event_type"] == "checkpoint_saved"
                   and (event.get("payload") or {}).get("phase") == PREPARATION_PHASE]
    assert prepared["payload"]["queued"] == 4
    assert prepared["payload"]["excluded_placeholder"] == 1
    first_call = completions.calls[0]
    context = json.loads([message for message in first_call["messages"]
                          if message["role"] == "user"][0]["content"])["context"]
    models = {item["commercial_model"] for item in context["government_work"]["items"]}
    assert "11111" not in models and len(context["government_work"]["items"]) == 4
    assert f"government preparation: run_id={run_id} queued=4 excluded_placeholder=1" \
        in capsys.readouterr().out
