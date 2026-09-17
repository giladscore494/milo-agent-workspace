# Encrypted Supabase production backup

Status: `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`.

This workflow creates a short-lived, encrypted logical safety backup before a
reviewed production migration. It does not apply migrations and it never
uploads plaintext database output.

## Scope and limitations

The backup contains the `public` schema, its data, database roles, and the
remote migration-history listing. Supabase-managed `auth` and `storage`
schemas are excluded by the Supabase CLI. This is a rollback aid for MILO's
`public`-schema migration, not a full Supabase project clone.

## Required secret

Configure `SUPABASE_BACKUP_PASSPHRASE` in the GitHub `production`
environment. Store the same generated value in the approved operator secret
store. Never print, commit, paste into a workflow input, or place it in an
artifact. Existing Supabase production secrets are reused.

## Create and verify

Dispatch `.github/workflows/backup-supabase-production.yml` from the exact
reviewed `main` SHA. Supply the full SHA as `expected_sha` and type
`CREATE_ENCRYPTED_PRODUCTION_BACKUP` as the confirmation.

The workflow refuses mismatched authorization, creates the dumps, records
component hashes, encrypts with AES-256-CBC and PBKDF2-SHA256 (600,000
iterations), decrypts into an ephemeral verification directory, validates
every hash, uploads only the encrypted archive plus a non-sensitive manifest,
and deletes runner plaintext on every exit path.

Artifact retention is seven days.

## Two different digests — do not confuse them

There are **two** SHA-256 values in play, over **two different files**, and
they are not expected to be equal:

| Digest | Covers | Where it comes from |
| --- | --- | --- |
| **Artifact ZIP digest** | the `.zip` GitHub Actions wraps the upload in | the Actions UI / API (`digest` on the artifact), and the run log line `SHA256 digest of uploaded artifact zip is …` |
| **`encrypted_sha256`** | the `*.tar.gz.enc` file **inside** that ZIP | `manifest.json`, computed by the workflow over the encrypted bundle before upload |

The ZIP is a transport container that GitHub builds around the two uploaded
files, so its digest covers different bytes than the encrypted backup it
carries. Comparing the ZIP digest against `manifest.json.encrypted_sha256`
will always disagree, and treating that disagreement as corruption — or,
worse, treating a coincidental match as proof — is a misreading of both
values. The ZIP digest may be recorded as **transport evidence** that the
download arrived intact; only the digest of the extracted `.tar.gz.enc` file
answers whether the encrypted backup itself is the one the workflow made.

## Verify a backup before a migration

Verify the workflow run is green, then:

1. Download the GitHub Actions artifact ZIP.
2. Extract it locally.
3. Verify the extracted contents are exactly two files: one `manifest.json`
   and one encrypted `*.tar.gz.enc` backup. Anything else — a plaintext
   `.sql`, a second archive, a missing manifest — fails the check.
4. Inspect `manifest.json`.
5. Verify its `source_sha` is the exact reviewed SHA being migrated, and its
   `workflow_run_id` is the run you dispatched.
6. Compute the SHA-256 of the extracted `*.tar.gz.enc` file.
7. Compare **that** hash to `manifest.json`'s `encrypted_sha256`. They must
   match exactly.
8. Compare the extracted `*.tar.gz.enc` file's size in bytes to
   `manifest.json`'s `encrypted_size_bytes`. They must match exactly.
9. Independently confirm the matching backup passphrase is still available in
   the approved operator secret store.
10. Never print, echo, log, paste or commit the passphrase — confirming it
    exists is not the same as reading it out, and no step above requires its
    value.

```sh
unzip -o supabase-production-backup-<run_id>.zip -d backup/
ls backup/                                    # step 3: manifest.json + one .enc
cat backup/manifest.json                      # steps 4-5
sha256sum backup/*.tar.gz.enc                 # step 6, compare to encrypted_sha256
stat -c '%s' backup/*.tar.gz.enc              # step 8, compare to encrypted_size_bytes
```

A mismatch at step 7 or 8 means the artifact is not the backup the manifest
describes: stop, and do not treat it as a recovery point for a migration.

For recovery, first restore into a new isolated Supabase project. Do not
restore directly over production without a separately reviewed recovery plan.
Production schema changes roll forward through a corrective migration.
