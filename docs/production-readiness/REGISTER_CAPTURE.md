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
| Directory | API (`POST /projects/{id}/register/directory`) then capture job (`--register-directory`) | One refresh at a time: while one is live (`catalog_register_group_stale`, the same liveness rule as a capture), a second press answers 200 with its `group_id` / `run_id` and executes nothing; otherwise 202. One bounded scan of the tozar column only (`fields=tozar`, `sort=_id`, 1000-row pages, offset paging until the offset reaches the scan's `total`), counting rows per exact tozar locally, then one count per tozar (`limit=0`, `filters={"tozar": <exact>}`). CKAN `distinct=true` is not read for values: on data.gov.il its `total` counts the distinct values but its `records` are truncated (1 of 137 sorted, 26 unsorted, 2026-09-30), so one `distinct=true, limit=0` request is made and its `total` reported as `distinct_total`, a cross-check only. The whole directory is refused, no version written, unless every scan page but the last is full, the rows counted (every tozar, unfilterable values included) add up to the scan's `total`, and each unit's scan count equals its filtered count (`GOV_DIRECTORY_RESULT_INVALID`); a `total` that moves between scan pages refuses with `GOV_DIRECTORY_REGISTER_CHANGED`. No row payload is fetched (only the `tozar` column), under hard caps (`MILO_REGISTER_DIRECTORY_MAX_REQUESTS`, default 6000, ~240 requests at the current ~102k rows and 137 tozars; `MILO_REGISTER_DIRECTORY_MAX_SECONDS`, default 3000). A new directory version only when the content differs from the CURRENT (newest) one (a register that reverts A → B → A records A again), and only capturable values become units (a value `CaptureScope` refuses -- null, empty, padded, over-long, control/format characters -- is counted as unfilterable): `register_version` = SHA-256 of `gov.register.directory.1`, the resource id, and the sorted `(tozar, count)` list. |
| Capture request | API (`POST /projects/{id}/register/captures`) | Release refusal first (both jobs on the current release, `prepare_trigger.release_refusal_for`); then ONE database call (`request_register_capture`) that is idempotent per `(tozar, register_version)`, refuses a group over `MILO_REGISTER_GROUP_MAX_ROWS` expected rows (a single larger tozar is captured alone and never split), and refuses a capture that would take the database above the threshold -- before anything is written. Then an operator capture run and the capture job with the register invocation. |
| Capture | capture job (`--register-group-id`) | Per tozar: the scoped capture Prepare uses (same client, bounds, snapshot key and content hash), rows written in bounded batches, then **before activation**: the stored count must equal a FRESH, independent count of that exact tozar taken after every row is written (the directory's bounded `limit=0` request with `filters={"tozar": <exact>}` -- never the capture's own reported total; it is the unit's `api_total`), and the archive object must be written and recorded. A snapshot that is already ACTIVE with the same key (e.g. Prepare captured the same content, with no archive) gets the same two checks after the ingest: its archive is written and verified (idempotent: a recorded archive is trusted) before the unit is marked captured. Otherwise the snapshot stays inactive (never used by Prepare). |
| Page | API (`GET /projects/{id}/register`) | Every tozar with expected rows and state (not captured / capturing / captured with snapshot key, rows and verified / failed with its code), totals vs the directory, measured bytes per row, and the capacity bar. 404 while the flag is off. |

### Capacity

`projected = pg_database_size + (expected_rows + rows still in flight) x MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE`
(rows of earlier requests still `requested` / `capturing` count too, but only
while their work is LIVE by `catalog_register_group_stale` -- a failed trigger,
a claim with no run, a run that ended, a run no worker claimed or a lease that
expired, each past the 15-minute grace, reserves nothing: a killed job never
holds capacity, whichever directory version it was for),
refused when `projected > MILO_DB_CAPACITY_BYTES x MILO_DB_CAPACITY_THRESHOLD`
with `CATALOG_CAPACITY_THRESHOLD_EXCEEDED` and the three numbers (current,
projected, limit, in bytes) in the response body (`error.capacity`) and on the
page. Measured bytes per snapshot are recorded when a unit finishes:
`sum(pg_column_size(row))` over the snapshot's raw records and candidates
(`measurement_method = pg_column_size(raw_records+candidates)`), shown on the
page as bytes per row. PR-L1b: the measurement covers the raw records,
candidates, variants and the ledger rows at `identity` / `government_fields`
that name the snapshot (`measurement_method =
pg_column_size(raw_records+candidates+variants+ledger)`,
`catalog_register_measured_bytes`): taken again whenever the snapshot's
variant build completes, and at once for a unit that reuses an already built
snapshot. A re-measurement sets `measured_at`, never `updated_at`: it never
makes an older unit the tozar's newest. Heap sizes only (a floor): the
estimate below includes indexes.

The estimate (PR-L1b, 6,000 B/row) is the measured total per register row,
tables + TOAST + indexes, rounded up to the next 500: raw record + candidate
3,512 B (production, 25,495 rows), variant 975-1,039 B, ledger at two levels
~996 B (`tests/test_catalog_variants_postgres.py`, L1-6): 5,547 B at the
upper reading.

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
capture identity holds `roles/storage.objectCreator` and `roles/storage.objectViewer`
on that bucket only: it can create an object and read its metadata back (so an
upload whose answer was lost, or a crash before the database record, is
verified by its recorded SHA-256 instead of wedging the snapshot), and can
never overwrite or delete one. `objectViewer` also lets that identity read
the archived objects' content (public register data) on that bucket; the
owner accepted this over a custom metadata-only role. A unit that fails ends its run `failed`, so the
next capture of that tozar -- or Prepare's -- adopts the pending snapshot once
that run's lease has lapsed (at most `MILO_WORKER_LEASE_SECONDS`, 300 s;
earlier, the retry answers `GOV_SNAPSHOT_OWNED_BY_ANOTHER_RUN` and can simply
be repeated).

### Retention (O22)

Always kept: per tozar the active snapshot and the one before it; the
snapshot of the latest captured register unit of each tozar; the snapshot of
each tozar's CURRENT variant build (the one the discovery tree serves); every
snapshot referenced by evidence, claims (field provenance), runs (adoptions,
checkpoints), work-scope units / batches / queue items, a `register`-level
coverage-ledger row, or whose candidates are referenced; every snapshot whose
writer run is live. Only scoped (per-tozar) Government snapshots are
candidates. Prune deletes database rows only -- never an archive object --
and only the exact list the dry-run's digest names (SHA-256 of the sorted
items, one per line).

PR-L1b (20261001000100): a pruned snapshot's variants and variant builds are
deleted with it, in the same transaction (the variants' append-only trigger
is suspended only inside `prune_register_snapshots` and re-enabled before it
returns). Ledger rows at `identity` / `government_fields` that name it never
block the prune and are never deleted: where the tozar's CURRENT build
states the same key (a newer snapshot's partial build wrote the row, then was
superseded), the prune re-points the row to that build by the build's own
rule (`ledger_repointed` in its answer); any other row is kept unchanged as
history -- the register no longer states that key, and a captured snapshot's
archive keeps its key citable. Also prunable,
listed as `PRUNABLE-VARIANTS <snapshot_key> mapper_version=<v>`: the variant
rows of a mapper version other than the current one, once the snapshot's
build under the current mapper version is complete. Their digest item is
`<snapshot_key> <mapper_version>`.

## Configuration (API unless noted)

| Key | Default | |
|---|---|---|
| `MILO_ENABLE_REGISTER_CAPTURE` | off | the website stage flag |
| `MILO_DB_CAPACITY_BYTES` | `500000000` (500 MB) | |
| `MILO_DB_CAPACITY_THRESHOLD` | `0.80` | |
| `MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE` | `6000` | raw + candidate + variant + two ledger levels, per row (PR-L1b) |
| `MILO_REGISTER_GROUP_MAX_ROWS` | `10000` | one request's cap |
| `MILO_REGISTER_ARCHIVE_BUCKET` | none | capture job; from the operator key `REGISTER_ARCHIVE_BUCKET` |
| `MILO_REGISTER_DIRECTORY_MAX_REQUESTS` / `_MAX_SECONDS` | `6000` / `3000` | capture job |
| `MILO_RATE_LIMIT_REGISTER_ACTIONS_USER` | `10` per 60 s | both register writes, per user; its own bucket (never `run_creation_user`) |
| `MILO_ENABLE_REGISTER_CAPTURE_JOB` | off | capture job; set only by the API's invocation, per execution |

## Operator steps, in order

1. **Apply the migration** `20260929000100_catalog_register_capture.sql` with
   the release (additive). It grants SELECT on the register tables and view
   and EXECUTE on the read functions only -- nothing that writes -- to the
   read-only roles the gates and retention connect as: the release role
   `milo_release_readonly_<suffix>` (LOGIN, BYPASSRLS, not a member of
   `pg_read_all_data`; matched by that prefix, never a hard-coded suffix),
   `supabase_read_only_user`, and any other BYPASSRLS login role in
   `pg_read_all_data`. Never a superuser or a platform role. The grant is
   made when the migration runs: a release read-only role created or
   rotated LATER gets no EXECUTE (the read functions are revoked from
   PUBLIC), and `REGISTER_COVERAGE` / retention then read "not available" /
   refuse. Grant it the same way (SQL editor, as `postgres`):

   ```sql
   grant select on public.catalog_register_directory_versions, public.catalog_register_directory_units,
     public.catalog_register_capture_groups, public.catalog_register_capture_units,
     public.catalog_register_snapshot_archives, public.catalog_register_archive_lines to <role>;
   grant execute on function public.catalog_register_version(text,jsonb),
     public.catalog_register_database_bytes(), public.catalog_register_snapshot_bytes(uuid),
     public.catalog_register_prunable_snapshots(), public.catalog_register_prune_digest(text[]),
     public.catalog_register_coverage(), public.catalog_register_latest_directory(),
     public.catalog_register_unit_states(), public.catalog_register_prunable_list(),
     public.catalog_register_group_stale(uuid,interval) to <role>;
   ```

   Retention's list also reads the snapshot-referencing tables (runs,
   evidence, claims, work scopes, ...) as that role: the migration-state check's
   (`check-migration-state.sh`) `READONLY_ROLE_LACKS_SELECT` covers its SELECT on them.
2. **Archive bucket** (Cloud Shell, idempotent): set
   `REGISTER_ARCHIVE_BUCKET=<new globally unique name>` in the operator
   configuration (and the `MILO_OPERATOR_CONFIG` repository variable), then

   ```bash
   bash scripts/ops/setup-register-archive.sh --plan
   bash scripts/ops/setup-register-archive.sh --apply   # ends with PASS bucket ...
   ```

   The deploy preflight reports `storage:register-archive` (WARN until this
   has run; BLOCKED for a public bucket or an app identity with a
   delete-capable role; as the deployer, WARN `PARTIAL` -- see step 3).
3. **Turn the Register page on**: Actions -> **Website stage** -> `stage =
   register-capture` (or `bash scripts/ops/website-stage.sh --stage
   register-capture`). It ensures the capture job on the release image (which
   now carries the bucket), checks the archive, binds the API identity on the
   capture job exactly as E' does, and sets `MILO_ENABLE_REGISTER_CAPTURE` on
   the API, read back. A Stage A deploy pins it off again: dispatch **Deploy**
   with `restore_website_stage = register-capture` (or `all` = `both` +
   register-capture) to turn it back on after the deploy, or re-run this
   stage. Either refuses up front without `REGISTER_ARCHIVE_BUCKET`. The kill
   switch closes it.

   **The archive gate, as the deployer.** The workflow runs as the deployer,
   which may describe the bucket but holds NO IAM read on the bucket or the
   project (by design; none is granted for this). `setup-register-archive.sh
   --check` then verifies what a describe proves -- the bucket exists, is in
   us-central1, has uniform access and public access prevention ENFORCED --
   and answers `PARTIAL ...`: the gate passes with
   `WARN: the bucket posture is verified; its IAM is not readable by this identity.`
   A missing bucket, public access prevention off, or the wrong location /
   access still refuses. The IAM half (the capture identity's grant, no
   delete-capable role for an application identity) is the operator's: in
   Cloud Shell, `bash scripts/ops/setup-register-archive.sh --check` must
   print `PASS` (the full check, unchanged).
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
   (the gate then fails). Only an exit 1 WITH a `REGISTER_COVERAGE=FAIL` line
   means that; any other failure of the report reads
   `INFO not available (the coverage report did not run: exit <n>)`.

## Error codes

API: `CATALOG_REGISTER_DISABLED` (404), `CATALOG_REGISTER_UNAVAILABLE`,
`CATALOG_REGISTER_NO_DIRECTORY`, `CATALOG_REGISTER_VERSION_STALE`,
`CATALOG_REGISTER_UNIT_UNKNOWN`, `CATALOG_REGISTER_REQUEST_INVALID`,
`CATALOG_REGISTER_GROUP_TOO_LARGE`, `CATALOG_CAPACITY_THRESHOLD_EXCEEDED`,
`CATALOG_REGISTER_JOB_NOT_RELEASE`, `CATALOG_REGISTER_JOB_UNREADABLE`,
`CATALOG_REGISTER_TRIGGER_FAILED`, `CATALOG_REGISTER_CONVERSATION_UNAVAILABLE`,
`CATALOG_REGISTER_WORKFLOW_UNSUPPORTED` (the page exists only in projects
whose engine reads the catalog, `swarm_v2`).
Capture job, per unit: `CATALOG_CAPTURE_COUNT_MISMATCH`,
`CATALOG_ARCHIVE_WRITE_FAILED`, `CATALOG_ARCHIVE_NOT_CONFIGURED`,
`CATALOG_REGISTER_UNIT_NOT_THIS_RUN`, `CATALOG_REGISTER_CAPTURE_FAILED`,
`CATALOG_REGISTER_CAPTURE_INTERRUPTED`, `CATALOG_REGISTER_CAPTURE_STALLED` (page: the
database would take a new request -- a claim with no run, a run no worker
claimed, or an expired lease, each past the 15-minute grace); entrypoint:
`CAPTURE_REGISTER_ARGUMENTS_INVALID`, `CAPTURE_REGISTER_CAPTURE_DISABLED`;
directory: `GOV_DIRECTORY_REQUEST_BUDGET_EXCEEDED`,
`GOV_DIRECTORY_TIME_BUDGET_EXCEEDED`, `GOV_DIRECTORY_RESULT_INVALID`,
`GOV_DIRECTORY_REGISTER_CHANGED`;
database: `CATALOG_REGISTER_CAPTURE_UNVERIFIED`, `CATALOG_ARCHIVE_CONFLICT`;
retention: `CATALOG_PRUNE_NOT_CONFIRMED`, `CATALOG_PRUNE_REQUEST_INVALID`,
`CATALOG_PRUNE_DIGEST_MISMATCH`, `CATALOG_PRUNE_FAILED`.
