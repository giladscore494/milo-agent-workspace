from pathlib import Path

MIGRATION = Path("supabase/migrations/008_workflow_proposal_ownership.sql").read_text().lower()


def test_proposal_ownership_migration_is_additive_and_idempotent():
    assert "add column if not exists created_by" in MIGRATION
    assert "add column if not exists project_id" in MIGRATION
    assert "create index if not exists workflow_proposals_created_by_idx" in MIGRATION
    assert "create index if not exists workflow_proposals_project_id_idx" in MIGRATION
    assert "drop table" not in MIGRATION
    assert "drop column" not in MIGRATION
    assert "delete from" not in MIGRATION
    assert "update public.workflow_proposals set created_by" not in MIGRATION.replace("--", "")


def test_proposal_ownership_migration_never_auto_assigns_legacy_rows():
    # The only update statement allowed is inside the commented operator
    # backfill template.
    executable_lines = [line for line in MIGRATION.splitlines() if not line.strip().startswith("--")]
    assert not any("update public.workflow_proposals" in line for line in executable_lines)


def test_proposal_ownership_migration_documents_operator_backfill_with_real_ids():
    assert "do not fabricate user ids" in MIGRATION
    assert "<real_auth_user_uuid>" in MIGRATION
    assert "<real_project_uuid>" in MIGRATION
    assert "exists (select 1 from auth.users" in MIGRATION


def test_proposal_ownership_migration_enables_rls_and_least_privilege():
    assert "alter table public.workflow_proposals enable row level security" in MIGRATION
    assert "grant select, insert on public.workflow_proposals to authenticated" in MIGRATION
    # UPDATE is column-scoped and must never include the ownership fields.
    assert "grant update (status, user_request" in MIGRATION
    assert "grant update (created_by" not in MIGRATION
    assert "delete on public.workflow_proposals" not in MIGRATION
    assert "created_by = auth.uid()" in MIGRATION
