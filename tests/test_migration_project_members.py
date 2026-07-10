from pathlib import Path

MIGRATION = Path("supabase/migrations/007_project_members.sql").read_text().lower()


def test_project_members_migration_is_non_destructive_and_idempotent():
    assert "create table if not exists public.project_members" in MIGRATION
    assert "create index if not exists project_members_user_id_idx" in MIGRATION
    assert "drop table" not in MIGRATION
    assert "delete from" not in MIGRATION


def test_project_members_migration_documents_safe_backfill_without_fabricated_user():
    assert "do not fabricate user ids" in MIGRATION
    assert "where exists (select 1 from auth.users" in MIGRATION
    assert "<real_auth_user_uuid>" in MIGRATION


def test_project_members_migration_grants_authenticated_least_privilege():
    assert "grant select on public.project_members to authenticated" in MIGRATION
    assert "grant select on public.projects to authenticated" in MIGRATION
    assert "grant select, insert on public.conversations to authenticated" in MIGRATION
    assert "grant select on public.runs to authenticated" in MIGRATION
