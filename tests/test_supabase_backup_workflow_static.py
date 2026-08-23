from pathlib import Path


WORKFLOW = Path(".github/workflows/backup-supabase-production.yml")


def test_backup_workflow_is_manual_exact_and_never_applies_migrations():
    text = WORKFLOW.read_text()

    assert "workflow_dispatch:" in text
    assert "expected_sha:" in text
    assert "CREATE_ENCRYPTED_PRODUCTION_BACKUP" in text
    assert 'if [ "${GITHUB_SHA}" != "${EXPECTED_SHA_INPUT}" ]' in text
    assert "environment: production" in text
    assert "contents: read" in text
    assert "supabase db push" not in text
    assert "APPLY_PRODUCTION_MIGRATIONS" not in text


def test_backup_workflow_encrypts_and_verifies_all_public_backup_parts():
    text = WORKFLOW.read_text()

    assert "supabase db dump --linked --schema public" in text
    assert "--data-only --use-copy" in text
    assert "supabase db dump --linked --role-only" in text
    assert "supabase migration list --linked" in text
    assert "openssl enc -aes-256-cbc -salt -pbkdf2 -iter 600000" in text
    assert "openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000" in text
    assert "sha256sum -c checksums.sha256" in text
    assert "Encrypted backup created and decryptability/checksums verified." in text


def test_backup_workflow_uploads_no_plaintext_and_has_bounded_retention():
    text = WORKFLOW.read_text()
    upload = text.split("uses: actions/upload-artifact@v4", 1)[1].split(
        "- name: Cleanup backup workspace", 1
    )[0]

    assert "*.tar.gz.enc" in upload
    assert "manifest.json" in upload
    assert "retention-days: 7" in upload
    assert "schema.sql" not in upload
    assert "data.sql" not in upload
    assert "roles.sql" not in upload
    assert "SUPABASE_BACKUP_PASSPHRASE" not in upload
