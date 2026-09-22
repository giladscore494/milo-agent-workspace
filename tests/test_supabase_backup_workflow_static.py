from pathlib import Path


WORKFLOW = Path(".github/workflows/backup-supabase-production.yml")
BACKUP_DOC = Path("docs/production-readiness/SUPABASE_BACKUP.md")


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
    upload = text.split("uses: actions/upload-artifact@v6", 1)[1].split(
        "- name: Cleanup backup workspace", 1
    )[0]

    assert "*.tar.gz.enc" in upload
    assert "manifest.json" in upload
    assert "retention-days: 7" in upload
    assert "schema.sql" not in upload
    assert "data.sql" not in upload
    assert "roles.sql" not in upload
    assert "SUPABASE_BACKUP_PASSPHRASE" not in upload


def test_manifest_digest_covers_the_encrypted_file_not_the_artifact_zip():
    """The workflow hashes the `.enc` bundle -- never a ZIP.

    GitHub builds the artifact ZIP *after* this step, around the files it
    uploads, so `encrypted_sha256` can only ever describe the encrypted
    backup. This pins the source of that value so the documentation's
    distinction cannot drift away from what the workflow actually records.
    """
    text = WORKFLOW.read_text()

    assert 'encrypted_sha="$(sha256sum "${encrypted_bundle}" | awk \'{print $1}\')"' in text
    assert 'encrypted_size="$(stat -c \'%s\' "${encrypted_bundle}")"' in text
    assert '"encrypted_sha256": digest,' in text
    assert '"encrypted_size_bytes": int(size),' in text
    assert '"source_sha": source_sha,' in text
    assert '"workflow_run_id": int(run_id),' in text
    # The workflow computes no digest over a zip: the only archive it hashes
    # is the encrypted bundle.
    assert "sha256sum" in text and ".zip" not in text


def test_backup_doc_distinguishes_the_zip_digest_from_the_encrypted_digest():
    doc = BACKUP_DOC.read_text()

    # Both digests are named, and named as DIFFERENT things.
    assert "Artifact ZIP digest" in doc
    assert "encrypted_sha256" in doc
    assert "not expected to be equal" in doc
    assert "transport evidence" in doc

    # The superseded instruction -- "compare the artifact's SHA-256 to
    # manifest.json" -- must not come back.
    assert "compare its SHA-256 to `manifest.json`" not in doc

    # The operator verifies the digest of the EXTRACTED encrypted file.
    assert "sha256sum backup/*.tar.gz.enc" in doc
    assert "encrypted_size_bytes" in doc
    assert "source_sha" in doc
    assert "workflow_run_id" in doc


def test_backup_doc_states_the_full_ordered_verification_procedure():
    doc = BACKUP_DOC.read_text()
    procedure = doc.split("## Verify a backup before a migration", 1)[1]

    for step in range(1, 11):
        assert f"\n{step}. " in procedure, f"step {step} is missing from the procedure"

    # The procedure ends at the passphrase, and never asks for its value.
    assert "approved operator secret store" in procedure
    assert "Never print, echo, log, paste or commit the passphrase" in procedure
    assert "SUPABASE_BACKUP_PASSPHRASE=" not in doc


def test_backup_doc_contains_no_real_artifact_or_secret_material():
    doc = BACKUP_DOC.read_text()

    # Placeholders only -- no captured run id, no real digest, no project ref.
    assert "<run_id>" in doc
    import re

    assert not re.search(r"\b[0-9a-f]{64}\b", doc), "no concrete SHA-256 may be pasted here"
    assert not re.search(r"\b[a-z]{20}\.supabase\.co\b", doc)
