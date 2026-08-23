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

Artifact retention is seven days. Before a migration, verify the workflow is
green, download the artifact, compare its SHA-256 to `manifest.json`, and
confirm the matching passphrase is still available in the operator secret
store.

For recovery, first restore into a new isolated Supabase project. Do not
restore directly over production without a separately reviewed recovery plan.
Production schema changes roll forward through a corrective migration.
