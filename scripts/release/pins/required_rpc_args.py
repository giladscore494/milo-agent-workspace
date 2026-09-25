"""The RPCs current main requires, with their required argument names.

``scripts/release/release_inventory.py rpcs`` derives this inventory from the
code (the runtime's RPC call sites and the release tooling's) and from the
migrations (the required arguments of each migration-created function);
``tests/test_release_inventory.py`` fails unless this literal is exactly that.

Copied byte-identically from ``scripts/release/stage-d/probe_db.py`` (cleanup
D8). One entry, ``settle_model_call_budget``, is required only by that probe's
own cleanup path (release_inventory.py ``TOOLING_SOURCES``); it leaves this
inventory together with its only caller. Optional (defaulted) arguments are
excluded, so an ADDED optional parameter passes while a missing or renamed
required one fails.

Regenerate with:
  python3 scripts/release/release_inventory.py rpcs
"""

from __future__ import annotations

REQUIRED_RPC_ARGS: dict[str, set[str]] = {
    "activate_catalog_snapshot_guarded": {
        "p_activation", "p_attempt", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "adopt_catalog_snapshot_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_snapshot", "p_worker_id"
    },
    "append_run_event_guarded": {
        "p_attempt", "p_event_type", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "append_usage_ledger_guarded": {
        "p_attempt", "p_entry", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "bind_work_scope_batch_run": {
        "p_batch_id", "p_bound_by", "p_expected_digest", "p_expected_revision",
        "p_run_id"
    },
    "catalog_candidate_manufacturers": {"p_snapshot_id"},
    "catalog_candidate_model_years": {"p_commercial_model", "p_manufacturer", "p_snapshot_id"},
    "catalog_candidate_models": {"p_manufacturer", "p_snapshot_id"},
    "catalog_candidate_variant_page": {"p_snapshot_id"},
    "catalog_canonical_manufacturer_coverage": {"p_manufacturers"},
    "catalog_raw_record_by_upstream_id": {"p_snapshot_id", "p_upstream_record_id"},
    "catalog_run_pending_promotions": {"p_run_id", "p_tool_operation"},
    "catalog_snapshot_candidate_diff": {"p_previous_snapshot_id", "p_snapshot_id"},
    "claim_current_verdict_states": {"p_run_id"},
    "claim_run_lease": {"p_run_id", "p_worker_id"},
    "create_agent_message_guarded": {
        "p_attempt", "p_lease_token", "p_message", "p_run_id", "p_worker_id"
    },
    "create_claim_with_source_guarded": {
        "p_attempt", "p_claim", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_conflict_guarded": {
        "p_attempt", "p_conflict", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_message_and_run_v3": {
        "p_content", "p_conversation_id", "p_idempotency_key", "p_metadata",
        "p_request_fingerprint", "p_requested_by", "p_run_id", "p_run_identity"
    },
    "create_project_from_proposal_with_owner_v2": {
        "p_configuration", "p_description", "p_name", "p_owner", "p_proposal_id",
        "p_slug"
    },
    "create_supervisor_decision_guarded": {
        "p_attempt", "p_decision", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_tool_access_request_guarded": {
        "p_attempt", "p_lease_token", "p_request", "p_run_id", "p_worker_id"
    },
    "create_tool_grant_guarded": {
        "p_attempt", "p_grant", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "create_tool_usage_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_usage", "p_worker_id"
    },
    "create_work_scope": {"p_conversation_id", "p_created_by", "p_revision"},
    "create_work_scope_batch_run": {
        "p_batch_id", "p_content", "p_expected_digest", "p_expected_revision",
        "p_idempotency_key", "p_metadata", "p_request_fingerprint", "p_requested_by",
        "p_run_id", "p_run_identity", "p_work_scope_id"
    },
    "finalize_run_guarded": {
        "p_attempt", "p_expected_status", "p_lease_token", "p_run_id", "p_status",
        "p_worker_id"
    },
    "heartbeat_run_guarded": {"p_attempt", "p_lease_token", "p_run_id", "p_worker_id"},
    "link_catalog_candidate_evidence_guarded": {
        "p_attempt", "p_lease_token", "p_link", "p_run_id", "p_worker_id"
    },
    "patch_run_blackboard_evidence_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_summary", "p_worker_id"
    },
    "promote_catalog_variant_guarded": {
        "p_attempt", "p_lease_token", "p_promotion", "p_run_id", "p_worker_id"
    },
    "prepare_work_scope_queue": {
        "p_attempt", "p_lease_token", "p_preparation", "p_run_id", "p_worker_id"
    },
    "record_catalog_candidates_batch_guarded": {
        "p_attempt", "p_candidates", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "record_catalog_candidate_guarded": {
        "p_attempt", "p_candidate", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "record_catalog_raw_record_guarded": {
        "p_attempt", "p_lease_token", "p_record", "p_run_id", "p_worker_id"
    },
    "record_catalog_raw_records_batch_guarded": {
        "p_attempt", "p_lease_token", "p_records", "p_run_id", "p_worker_id"
    },
    "record_catalog_snapshot_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_snapshot", "p_worker_id"
    },
    "record_claim_verdict_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_verdict", "p_worker_id"
    },
    "record_conflict_resolution_guarded": {
        "p_attempt", "p_lease_token", "p_resolution", "p_run_id", "p_worker_id"
    },
    "record_evidence_fragment_guarded": {
        "p_attempt", "p_fragment", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "record_run_usage_guarded": {
        "p_attempt", "p_lease_token", "p_ledger", "p_run_id", "p_worker_id"
    },
    "reserve_daily_project_budget": {"p_amount", "p_daily_limit", "p_project_id", "p_run_id"},
    "reserve_daily_user_budget": {"p_amount", "p_daily_limit", "p_run_id", "p_user_id"},
    "reserve_model_call_budget_guarded": {
        "p_attempt", "p_call_seq", "p_daily_project_limit", "p_daily_user_limit",
        "p_estimated_cost", "p_lease_token", "p_project_id", "p_run_id",
        "p_user_id", "p_worker_id"
    },
    "reserve_model_call_budget_v2": {
        "p_call_seq", "p_daily_project_limit", "p_daily_user_limit",
        "p_estimated_cost", "p_project_id", "p_run_id", "p_user_id"
    },
    "revise_work_scope": {
        "p_created_by", "p_expected_digest", "p_expected_revision", "p_revision",
        "p_work_scope_id"
    },
    "save_checkpoint_guarded": {
        "p_attempt", "p_engine_version", "p_lease_token", "p_phase", "p_run_id",
        "p_worker_id", "p_workflow_key"
    },
    "set_work_scope_paused": {"p_paused", "p_requested_by", "p_work_scope_id"},
    "settle_model_call_budget": {"p_actual_cost", "p_reservation_id"},
    "settle_model_call_budget_guarded": {
        "p_actual_cost", "p_attempt", "p_lease_token", "p_reservation_id",
        "p_run_id", "p_worker_id"
    },
    "settle_model_call_budget_v2": {"p_actual_cost", "p_reservation_id"},
    "transition_run_worker_guarded": {
        "p_attempt", "p_expected_status", "p_lease_token", "p_run_id", "p_status",
        "p_worker_id"
    },
    "update_run_usage_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_usage", "p_worker_id"
    },
    "upsert_run_blackboard_guarded": {
        "p_attempt", "p_blackboard", "p_lease_token", "p_run_id", "p_worker_id"
    },
    "upsert_source_guarded": {
        "p_attempt", "p_lease_token", "p_run_id", "p_source", "p_worker_id"
    },
    "work_scope_batch_for_run": {"p_run_id"},
    "work_scope_progress": {"p_work_scope_id"},
}
