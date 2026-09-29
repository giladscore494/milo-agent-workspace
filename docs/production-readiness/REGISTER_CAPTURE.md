# Register capture (PR-D1)

Capture the Government WLTP register from the website, one EXACT `tozar` at a
time or in bounded groups, with a capacity guard, count verification, an
immutable archive in Cloud Storage and digest-bound retention.

Capture is **$0 and is not a product run**. It is gated by its own website
stage flag (`MILO_ENABLE_REGISTER_CAPTURE`, default off) and nothing else:
not `MILO_ENABLE_RUN_CREATION`, not `GATEWAY_ALLOW_RUN_START_ROUTES`, not Arm.
No new Vercel environment variable is needed: the page's read is always
proxied, and its two writes are execution routes, so they use the existing
`GATEWAY_ALLOW_EXECUTION_ROUTES` that Stage P / E' already turned on.

## What happens

| Step | Where | What |
|---|---|---|
| Directory | capture job (`--register-directory`) | One distinct read (`fields=tozar`, `distinct=true`) and one count per tozar (`limit=0`, `filters={"tozar": <exact>}`). Metadata only, no row payload, under hard caps (`MILO_REGISTER_DIRECTORY_MAX_REQUESTS`, default 1000; `MILO_REGISTER_DIRECTORY_MAX_SECONDS`, default 900). A new directory version only when the content changed: `register_version` = SHA-256 of `gov.register.directory.1`, the resource id, and the sorted `(tozar, count)` list. |
| Capture request | API (`POST /projects/{id}/register/captures`) | Release refusal first (both jobs on the current release, `prepare_trigger.release_refusal_for`); then ONE database call (`request_register_capture`) that is idempotent per `(tozar, register_version)`, refuses a group over `MILO_REGISTER_GROUP_MAX_ROWS` expected rows (a single larger tozar is captured alone and never split), and refuses a capture that would take the database above the threshold -- before anything is written. Then an operator capture run and the capture job with the register invocation. |
| Capture | capture job (`--register-group-id`) | Per tozar: the scoped capture Prepare uses (same client, bounds, snapshot key and content hash), rows written in bounded batches, then **before activation**: the stored count must equal the source's total for that tozar, and the archive object must be written and recorded. Otherwise the snapshot stays inactive (never used by Prepare). |
| Page | API (`GET /projects/{id}/register`) | Every tozar with expected rows and state (not captured / capturing / captured with snapshot key, rows and verified / failed with its code), totals vs the directory, measured bytes per row, and the capacity bar. 404 while the flag is off. |

### Capacity

`projected = pg_database_size + expected_rows x MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE`,
refused when `projected > MILO_DB_CAPACITY_BYTES x MILO_DB_CAPACITY_THRESHOLD`
with `CATALOG_CAPACITY_THRESHOLD_EXCEEDED` and the three numbers (current,
projected, limit, in bytes) in the response body (`error.capacity`) and on the
page. Measured bytes per snapshot are recorded when a unit finishes:
`sum(pg_column_size(row))` over the snapshot's raw records and candidates
(`measurement_method = pg_column_size(raw_records+candidates)`), shown on the
page as bytes per row.

### Archive

`gs://<MILO_REGISTER_ARCHIVE_BUCKET>/register/<resource_id>/<sha256(tozar)[:16]>/<snapshot_key>.jsonl.gz`:
every upstream record of the snapshot, one canonical JSON line each (sorted
keys, compact, UTF-8), in capture order, gzip with a pinned mtime (the same
records make the same bytes). Written create-only (`ifGenerationMatch=0`); an
existing object counts only when its recorded SHA-256 matches. The database
records `gcs_uri`, byte size and SHA-256 (`catalog_register_snapshot_archives`);
a raw record's line is `source_locator.capture_index + 1`
(`catalog_register_archive_lines`). No recorded archive, no activation
(`CATALOG_ARCHIVE_WRITE_FAILED` / `CATALOG_ARCHIVE_NOT_CONFIGURED`). The
capture identity holds `roles/storage.objectCreator` on that bucket only: it
can create, never read back, overwrite or delete.

### Retention (O22)

Always kept: per tozar the active snapshot and the one before it; every
snapshot referenced by evidence, claims (field provenance), runs (adoptions,
checkpoints), work-scope units / batches / queue items, the coverage ledger,
or whose candidates are referenced; every snapshot whose writer run is live.
Only scoped (per-tozar) Government snapshots are candidates. Prune deletes
database rows only -- never an archive object -- and only the exact list the
dry-run's digest names (SHA-256 of the sorted keys, one per line).

## Configuration (API unless noted)

| Key | Default | |
|---|---|---|
| `MILO_ENABLE_REGISTER_CAPTURE` | off | the website stage flag |
| `MILO_DB_CAPACITY_BYTES` | `500000000` (500 MB) | |
| `MILO_DB_CAPACITY_THRESHOLD` | `0.80` | |
| `MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE` | `3500` | |
| `MILO_REGISTER_GROUP_MAX_ROWS` | `10000` | one request's cap |
| `MILO_REGISTER_ARCHIVE_BUCKET` | none | capture job; from the operator key `REGISTER_ARCHIVE_BUCKET` |
| `MILO_REGISTER_DIRECTORY_MAX_REQUESTS` / `_MAX_SECONDS` | `1000` / `900` | capture job |
| `MILO_ENABLE_REGISTER_CAPTURE_JOB` | off | capture job; set only by the API's invocation, per execution |

## Operator steps, in order

1. **Apply the migration** `20260929000100_catalog_register_capture.sql` with
   the release (additive; grants SELECT / EXECUTE on the read functions to the
   read-only role).
2. **Archive bucket** (Cloud Shell, idempotent): set
   `REGISTER_ARCHIVE_BUCKET=<new globally unique name>` in the operator
   configuration (and the `MILO_OPERATOR_CONFIG` repository variable), then

   ```bash
   bash scripts/ops/setup-register-archive.sh --plan
   bash scripts/ops/setup-register-archive.sh --apply   # ends with PASS bucket ...
   ```

   The deploy preflight reports `storage:register-archive` (WARN until this
   has run; BLOCKED for a public bucket or an app identity with a
   delete-capable role).
3. **Turn the Register page on**: Actions -> **Website stage** -> `stage =
   register-capture` (or `bash scripts/ops/website-stage.sh --stage
   register-capture`). It ensures the capture job on the release image (which
   now carries the bucket), checks the archive, binds the API identity on the
   capture job exactly as E' does, and sets `MILO_ENABLE_REGISTER_CAPTURE` on
   the API, read back. A Stage A deploy pins it off again; re-run this stage
   after a deploy (it is not part of `both`). The kill switch closes it.
4. **Use it**: open a conversation in the project, open **Register**, press
   **Refresh directory**, then **Capture** one tozar or a selected group.
5. **Retention, dry-run first**: Actions -> **Register retention** with
   `mode = dry-run`: it lists every prunable snapshot, rows, estimated bytes
   and `DIGEST <hex>`. To prune exactly that list: `mode = apply`,
   `confirm = PRUNE`, `digest = <that hex>` (the `production` environment's
   reviewer approves the run). A list that changed refuses.
6. **Read REGISTER_COVERAGE**: Actions -> **Production gates**; the summary
   row `REGISTER_COVERAGE` reads
   `INFO directory <version>; units a/b; rows c/d; database x/y bytes (...); unverified snapshots n`.
   Informational, except `FAIL` when the database is above the threshold
   (the gate then fails).

## Error codes

API: `CATALOG_REGISTER_DISABLED` (404), `CATALOG_REGISTER_UNAVAILABLE`,
`CATALOG_REGISTER_NO_DIRECTORY`, `CATALOG_REGISTER_VERSION_STALE`,
`CATALOG_REGISTER_UNIT_UNKNOWN`, `CATALOG_REGISTER_REQUEST_INVALID`,
`CATALOG_REGISTER_GROUP_TOO_LARGE`, `CATALOG_CAPACITY_THRESHOLD_EXCEEDED`,
`CATALOG_REGISTER_JOB_NOT_RELEASE`, `CATALOG_REGISTER_JOB_UNREADABLE`,
`CATALOG_REGISTER_TRIGGER_FAILED`, `CATALOG_REGISTER_CONVERSATION_UNAVAILABLE`.
Capture job, per unit: `CATALOG_CAPTURE_COUNT_MISMATCH`,
`CATALOG_ARCHIVE_WRITE_FAILED`, `CATALOG_ARCHIVE_NOT_CONFIGURED`,
`CATALOG_REGISTER_UNIT_NOT_THIS_RUN`, `CATALOG_REGISTER_CAPTURE_FAILED`,
`CATALOG_REGISTER_CAPTURE_INTERRUPTED` (page); entrypoint:
`CAPTURE_REGISTER_ARGUMENTS_INVALID`, `CAPTURE_REGISTER_CAPTURE_DISABLED`;
directory: `GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED`,
`GOV_DIRECTORY_TIME_BUDGET_EXCEEDED`, `GOV_DIRECTORY_RESULT_INVALID`;
database: `CATALOG_REGISTER_CAPTURE_UNVERIFIED`, `CATALOG_ARCHIVE_CONFLICT`;
retention: `CATALOG_PRUNE_NOT_CONFIRMED`, `CATALOG_PRUNE_REQUEST_INVALID`,
`CATALOG_PRUNE_DIGEST_MISMATCH`, `CATALOG_PRUNE_FAILED`.
