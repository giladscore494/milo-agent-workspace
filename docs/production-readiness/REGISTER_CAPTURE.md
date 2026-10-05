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
| Directory | API (`POST /projects/{id}/register/directory`) then capture job (`--register-directory`) | One refresh at a time: while one is live (`catalog_register_group_stale`, the same liveness rule as a capture), a second press answers 200 with its `group_id` / `run_id` and executes nothing; otherwise 202. One bounded scan of the tozar column only (`fields=tozar`, `sort=_id`, 10,000-row pages (the scan's own `SCAN_PAGE_LIMIT`; a scan row is only `_id` and `tozar`, and data.gov.il answered 10,000-record pages of ~260 KB with the exact total, 2026-09-30; capture pages stay at 1000), offset paging until the offset reaches the scan's `total`, each page sending `total_estimation_threshold=10000000` because an unfiltered search on data.gov.il otherwise answers `total_was_estimated: true`; a total still marked estimated is refused), counting rows per exact tozar locally, then one count per tozar (`limit=0`, `filters={"tozar": <exact>}`). CKAN `distinct=true` is not read for values: on data.gov.il its `total` counts the distinct values but its `records` are truncated (1 of 137 sorted, 26 unsorted, 2026-09-30), so one `distinct=true, limit=0` request is made and its `total` reported as `distinct_total`, a cross-check only. The whole directory is refused, no version written, unless every scan page but the last is full, the rows counted (every tozar, unfilterable values included) add up to the scan's `total`, and each unit's scan count equals its filtered count (`GOV_DIRECTORY_RESULT_INVALID`); a `total` that moves between scan pages refuses with `GOV_DIRECTORY_REGISTER_CHANGED`. No row payload is fetched (only the `tozar` column), under hard caps (`MILO_REGISTER_DIRECTORY_MAX_REQUESTS`, default 6000, ~150 requests at the current ~102k rows and 137 tozars: 1 distinct cross-check, 11 scan pages, 137 counts; `MILO_REGISTER_DIRECTORY_MAX_SECONDS`, default 3000). P51: data.gov.il's firewall blocks a burst (production: 92 back-to-back requests answered 200, the 93rd a 403 HTML block page), so every data.gov.il send of a client -- directory, capture and counts -- starts at least 1 s after the previous one (`MIN_REQUEST_INTERVAL_SECONDS`): a full directory takes ~2.5 minutes. The block page (403, HTML) or a 429 is retried after 60 s, 180 s and 300 s; every retry counts against the directory's request cap and its wait must end inside the time cap. A new directory version only when the content differs from the CURRENT (newest) one (a register that reverts A → B → A records A again), and only capturable values become units (a value `CaptureScope` refuses -- null, empty, padded, over-long, control/format characters -- is counted as unfilterable): `register_version` = SHA-256 of `gov.register.directory.1`, the resource id, and the sorted `(tozar, count)` list. |
| Capture request | API (`POST /projects/{id}/register/captures`) | Release refusal first (both jobs on the current release, `prepare_trigger.release_refusal_for`); then ONE database call (`request_register_capture`) that is idempotent per `(tozar, register_version)`, refuses a group over `MILO_REGISTER_GROUP_MAX_ROWS` expected rows (a single larger tozar is captured alone and never split), and refuses a capture that would take the database above the threshold -- before anything is written. Then an operator capture run and the capture job with the register invocation. |
| Capture | capture job (`--register-group-id`) | Per tozar: the scoped capture Prepare uses (same client, bounds, snapshot key and content hash), rows written in bounded batches, then **before activation**: the stored count must equal a FRESH, independent count of that exact tozar taken after every row is written (the directory's bounded `limit=0` request with `filters={"tozar": <exact>}` -- never the capture's own reported total; it is the unit's `api_total`), and the archive object must be written and recorded. A snapshot that is already ACTIVE with the same key (e.g. Prepare captured the same content, with no archive) gets the same two checks after the ingest: its archive is written and verified (idempotent: a recorded archive is trusted) before the unit is marked captured. Otherwise the snapshot stays inactive (never used by Prepare). |
| Page | API (`GET /projects/{id}/register`) | Every tozar with expected rows and state (not captured / capturing / captured with snapshot key, rows and verified / failed with its code), totals vs the directory, measured bytes per row, and the capacity bar. 404 while the flag is off. |

P52: every data.gov.il request (directory, capture, counts) sends a User-Agent containing `datagov-external-client`, the crawler token data.gov.il's API examples page requires of automated clients ("צריכת נתונים על ידי crawling"); `snapshot.CAPTURE_TOOL` (snapshot provenance) is unchanged.

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

The estimate (PR-L2, 3,000 B/row) is the measured total per register row
ONCE COMPACTED, tables + TOAST + indexes after VACUUM FULL, rounded up to the
next 500: raw record without its payload ~605 B, candidate keys ~431 B (its
identity is read from the variant row; the identity indexes skip it), variant
~993 B (with the reading index the compacted readers use), ledger at two
levels ~903 B = ~2,931 B (5,000 rows,
`tests/test_register_compaction_postgres.py`; 5,256 B before compaction).
That test asserts:

| | |
|---|---|
| bytes per row | <= 2,890 x 1.15 and <= the default |
| full register | 25 MB non-register base (`NON_REGISTER_BASE_BYTES`, production 144.5 - 121.0 MB) + 101,686 rows x ~2,931 B + the two superseded Toyota snapshots' 22 referenced skeletons = **~323.1 MB** (<= 360 MB) |
| after one refresh (two snapshots per tozar) | the active snapshot compacted, the one before it kept as its referenced rows (10% planned) = **~333.6 MB** (<= 400 MB) |

The capture gate stays `pg_database_size` + incoming rows x the estimate: the
plan limit counts the database's size on disk, dead space included. A
capture's rows hold their payload only until their build completes; the
space a compaction frees is reused by later captures, but `pg_database_size`
drops only after the two tables are rewritten (**Register retention**,
`operation = vacuum-full`; operator step 7). The estimate counts COMPACTED
rows, so while any finished register capture's snapshot is not compacted
(`catalog_register_uncompacted_captures`: a refused or failed compaction
leaves full-size rows) the gate prices incoming and in-flight rows as they
land, uncompacted (`UNCOMPACTED_BYTES_PER_ROW` = 5,500 B; 5,256 B measured).
Nothing is refused for it: a re-capture of that tozar supersedes and archives
the stuck snapshot.

**Readers after compaction** (`catalog_candidate_variants_resolved`): a
UNION ALL of the candidates still stating their identity (read as stored,
through the partial identity indexes), the compacted ones joined to their
variant row (through `catalog_variants_reading_idx`), and the skeletons whose
variant is gone (identity null). `catalog_work_scope_coverage_decisions` is an
inlinable SQL function (no `SET`; every name qualified), planned with its
caller's values; the two register-code helpers carry no `SET` either (no
per-call search_path switch; their sub-select keeps them from being
inlined). Since `20261002000200` the decisions read
`catalog_candidate_register_reading`, which joins a compacted row's variant
row ONCE for its identity, register codes and content hash (instead of the
view plus four correlated helper calls per row). Measured on 6,000 rows (ms,
before -> after compaction, every decision evaluated -- grouped by decision,
since a bare `count(*)` lets the planner skip the decision entirely): coverage
decisions 3,524 -> 2,616 (the earlier definition: 3,452 -> 3,515; most of the
cost is the per-row `catalog_variant_identity_key` of the ledger join, which
predates PR-L2), the candidate page by model 16 -> 21, an identity lookup
0.6 -> 2.7. A model filter never scans the snapshot, and the compacted rows
are reached through `catalog_variants_reading_idx` -- in the view and inside
the candidate page (asserted with EXPLAIN and auto_explain).

### Compaction (PR-L2, 20261002000100)

`public.compact_register_snapshot` has two modes.

**Every apply reads the archive back first.** The dry-run runs first; only a
READY snapshot has its archive object downloaded, its length, sha256 and
line count checked against `catalog_register_snapshot_archives`, and the
apply names the sha256 it computed (`p_verified_sha256`): the database
refuses any other (`CATALOG_COMPACTION_ARCHIVE_UNVERIFIED`), and so does the
job on a read failure or a mismatch -- nothing is removed on the word of an
upload answer. The upload itself sends `md5Hash`, so Cloud Storage rejects a
corrupted write.

**Active** (the tozar's rank-1 snapshot). When nothing live reads it (as
below, except the caller's own run: a capture job compacts in its own run),
and its variants are completely
built under the current mapper version AND its archive is recorded, its raw
payloads leave the database and every candidate keeps only its keys (id,
snapshot, raw record, `candidate_key`, status): its identity columns are
NULL and read from the variant row through `catalog_candidate_variants_resolved`
(the table's own columns; every candidate reader reads it). The capture job
does it right after the build (the unit's document reports
`compaction.status`: `compacted`, `unchanged`, `refused` with a code,
`skipped` or `failed`; never failing the unit), and the **Register variants**
workflow does it for an already built snapshot (`compact = dry-run`, then
`apply`). Every row stays, with its id, keys, `payload_sha256` and
`source_locator` (the archive line); every foreign key, evidence link, queue
item and reservation stays valid.

**Superseded** (any older activated snapshot of the tozar). Once the active
one is built and nothing live can read the old one -- no live run on it (its
writer, an adopter, a preparation, a batch run, a run whose checkpoint names
it, a reservation a live run holds, and any capture-job run that is not a
register capture: a Prepare may be reading it for reuse), no batch its open
plan can still start, no reservation at all
(`CATALOG_COMPACTION_SNAPSHOT_IN_USE` otherwise) -- it keeps only the rows
something references (a candidate named by a queue item, evidence link, field
provenance, promotion or reservation, and its raw record) as skeletons, and
drops every other row and all its variants. `catalog_readable_snapshot` then
refuses it (`CATALOG_SNAPSHOT_ARCHIVED`), and so do the readers that do not go
through it -- `catalog_work_scope_coverage_decisions`,
`catalog_variant_coverage_for_batch`, `work_scope_batch_for_run` and
`catalog_run_pending_promotions` (`catalog_snapshot_not_archived`): a
skeleton candidate has no identity left. Its archive is the record (the
replay export reads it there, row or no row). The
capture job does it for the tozar's older snapshots right after it compacts
the new one (the unit's document lists them under `compaction.superseded`);
the operator path does it by key. This includes the two superseded Toyota
Prepare snapshots: their batches are completed (no run can start again), so
only their 10 + 12 queued rows stay.

The append-only triggers are suspended only inside that one security-definer
function (search_path pinned, service_role only). Refusals, each writing
nothing:
`CATALOG_COMPACTION_SNAPSHOT_UNKNOWN`, `CATALOG_COMPACTION_SNAPSHOT_INELIGIBLE`
(not an activated whole-tozar Government snapshot),
`CATALOG_COMPACTION_COUNT_UNVERIFIED` (the newest register unit that captured
it is not count-verified, or stored rows differ from declared rows),
`CATALOG_COMPACTION_BUILD_INCOMPLETE` (no complete current-mapper build
covering every raw row), `CATALOG_COMPACTION_TYPED_MISMATCH` (a row's typed
variant does not read exactly as its payload -- e.g. a zero-padded code;
checked per row by `catalog_variant_reads_as_payload` -- or a candidate's
identity is not its variant's, `catalog_candidate_reads_as_variant`),
`CATALOG_COMPACTION_SNAPSHOT_IN_USE` (either mode), then
`CATALOG_COMPACTION_ARCHIVE_MISSING` (so a dry-run naming it passed every other
check) and, for an apply, `CATALOG_COMPACTION_ARCHIVE_UNVERIFIED`. The locks
wait at most 5 s (`lock_timeout`); the checks then run under them (16 s per
6,000 rows locally), so every reader of the six tables waits that long.

After compaction every reader takes the register codes, the content hash and
the identity reading from the snapshot's variant rows
(`catalog_raw_record_code`, `catalog_raw_record_content_sha256`,
`catalog_compacted_record_reading`); REGISTER_FIELD_ABSENT reads "the
register states nothing" as the typed column null AND no parse issue for the
field (an unparseable value stays a hard gap). The original record is read
from the archive only: `python -m backend.catalog.register.compaction
--snapshot-key <key> --show-record <upstream id>` (and the replay export)
fetch the object and check its SHA-256 against the recorded one; for an active
snapshot the database also checks the line against the row's
`payload_sha256`. A compacted snapshot is not rebuilt under a new mapper
version: the database refuses any variant row for it
(`CATALOG_VARIANT_SNAPSHOT_COMPACTED`), CI refuses a migration that redefines
`catalog_variant_mapper_version()` after PR-L2, and the deployed gate is NOT
ready while a live compaction names another mapper (`CATALOG_COMPACTION_MAPPER`
in `production-verify.sh`). No path rebuilds a compacted snapshot yet: a
rebuild-from-archive must land before any variant mapper bump.

A snapshot with no archive (a Prepare snapshot captured before PR-D1) gets it
written from its stored rows first, only once every other precondition holds:
the database checks the rows are exactly an archive's lines
(`catalog_register_snapshot_archivable`), every line is checked against its
row's `payload_sha256` (`catalog_raw_record_lines_mismatched`), then PR-D1's
create-only writer uploads it (the same object a register capture of the
same rows writes) and it is recorded
(`record_register_snapshot_archive_from_database`).

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

## Sync register (PR-SYNC-1)

The **Sync register** button (`POST /projects/{id}/register/sync`) is the one
way to keep the register complete and current: no tozar to pick, nothing to
configure. It executes the capture job once with `--register-sync`
(`backend/catalog/register/sync.py`). One run lease owns the whole sync, and
the groups it captures one after another.

What one sync does, in order, inside a hard budget of **80 data.gov.il
requests** (`SYNC_MAX_REQUESTS`; every send and every retry counts):

1. **Check** (2 requests): `package_show` and the register's exact total. If
   both match what the last sync recorded for the current directory version,
   nothing changed.
2. **Light directory** (~11 requests), only on a change or when a captured
   tozar's count differs from the directory: the same tozar-column scan as
   **Refresh directory**, then one count only for each tozar whose count moved
   or that is new. A new directory version is recorded only on change.
3. **Backlog**: tozars never captured, then those whose last capture failed or
   was interrupted, then those whose captured count differs from the directory,
   each in byte order of the tozar. It is recomputed from the database on every
   run, so a sync that stops loses nothing and the next one starts where it
   stopped. A tozar is never given up.
4. **Rolling refresh**: only once the backlog is empty, the 2 tozars captured
   longest ago under an older directory version are captured again. If the
   resource's published version has not changed, an unchanged tozar lands on
   its SAME snapshot and adds no rows. A tozar already captured under the
   current version is not requested again.
5. **Capture**, through the same request, capacity guard, verification,
   archive, activation, variants and compaction as **Capture selected**. A
   unit whose requests (`1 + pages + 1`, 3 for a small tozar) do not fit what
   is left of the budget is not started. It is left for the next sync.

The sync never waits on the firewall. Its client has no 60/180/300 s
schedule: the first 403 block page or 429 ends the sync at once
(`GOV_SYNC_THROTTLED`), and nothing more is sent. The unit it hit is recorded
failed (retryable), exactly like any failed capture. **A throttled sync is
normal; run it again later.** Wait at least the capture lease (~5 minutes)
so the next sync can adopt a snapshot the stopped one left pending.

Only one register job runs at a time. A sync is refused (`409
CATALOG_REGISTER_BUSY`, no job started) while a capture, a directory refresh
or another sync is live. A group capture is refused the same way while a
directory refresh or a sync is live.

Every sync prints one line (stdout, and the run's output). The Register page
shows the last one:

`SYNC_SUMMARY|changed=<bool>|directory_version=<12 hex>|work=<n>|captured=<n>|reused=<n>|failed=<n>|deferred=<n>|requests=<used>/80|stop=<complete|budget|throttled|capacity>|backlog=<tozars>/<rows>|coverage=<captured_rows>/<directory_rows>`

| Field | Meaning |
|---|---|
| `changed` | the check saw a change (or no earlier sync), so the light directory ran |
| `work` | tozars planned this run: the backlog, else the rolling refresh |
| `captured` / `reused` | captured into a new snapshot / re-captured into the snapshot it already had |
| `failed` / `deferred` | failed this run (retried by the next) / not reached this run (budget, throttle or capacity) |
| `stop` | `complete`, `budget` (80 requests), `throttled` (firewall), `capacity` (the capacity guard refused; nothing partial) |
| `backlog` | tozars and directory rows still to capture after this run; it shrinks run by run |
| `coverage` | rows of captured tozars / rows in the directory |

At the 1.10 numbers (93 small tozars, 3,740 rows missing), the first sync
spends 15 requests on the check and light directory, then captures 21 tozars
(78/80). About 5 syncs empty the backlog.

## Configuration (API unless noted)

| Key | Default | |
|---|---|---|
| `MILO_ENABLE_REGISTER_CAPTURE` | off | the website stage flag |
| `MILO_DB_CAPACITY_BYTES` | `500000000` (500 MB) | |
| `MILO_DB_CAPACITY_THRESHOLD` | `0.80` | |
| `MILO_CAPTURE_BYTES_PER_ROW_ESTIMATE` | `3000` | compacted raw record + candidate keys + variant + two ledger levels, per row (PR-L2) |
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
7. **Compaction (PR-L2)**: apply `20261002000100_catalog_register_compaction.sql`
   with the release (it also grants the release read-only role MAINTAIN on
   the two compacted tables). A new capture compacts by itself, then its
   tozar's older snapshots. For the existing snapshots, each by key: Actions
   -> **Register variants** with its `snapshot_key` and `compact = dry-run`,
   then `compact = apply`:
   - Audit `cs1.6fb07b73d273aa8b8a2a10a1a61f38f2` (archived by its capture;
     expect `READY ... mode=active`) and the active Toyota
     `cs1.ae06a5c5f8585e420e10eced830e2719` (a Prepare snapshot; expect
     `READY ... mode=active archive=from-database`), then `COMPACTED ... mode=active`;
   - then the superseded Toyota `cs1.e335295707fa8d6935b113bb6a0165f4` and
     `cs1.41cdbb31f18f96429eb62651bc71131d` (expect `READY ... mode=superseded
     archive=from-database`, then `COMPACTED ... mode=superseded raw_rows=6374
     kept_rows=12` and `kept_rows=10`).
   The capture job's bucket (`REGISTER_ARCHIVE_BUCKET`) must be set: an
   archive written from the stored rows needs it. Then, with nothing live,
   hand the space back: Actions -> **Register retention** with
   `operation = vacuum-full`, `mode = dry-run` (the two tables' sizes,
   `pg_database_size`, what is live, and `register-vacuum owner <table>`
   PASS/FAIL: the owner connection owns each table), then `mode = apply`,
   `confirm = VACUUM`. The rewrite runs as the tables' OWNER with the
   migrations' own credential (the `production` environment's
   `SUPABASE_DB_PASSWORD` and `SUPABASE_PROJECT_ID`, given to that one step,
   passed as `PGPASSWORD`, never printed); the read-only role holds no
   MAINTAIN. The owner connects to `MILO_READONLY_DB_URL`'s host: a Supabase
   pooler in session mode (port 5432, `postgres.<project ref>`), or the direct
   host (`postgres`). The read-only connection (sizes, liveness, headroom) is
   made from `MILO_READONLY_DB_URL` read from the environment and split into
   libpq's variables, its password as `PGPASSWORD`: neither URL nor password
   is ever on psql's argv. Refused `CATALOG_VACUUM_NOT_PERMITTED` unless the owner
   read-back is PASS for both tables; the two are rewritten in ascending
   order of their current total size (`register-vacuum order` prints it: the
   smaller table's rewrite shrinks `pg_database_size` first, which can be what
   makes room for the larger one's copy); then BEFORE EACH TABLE,
   `CATALOG_VACUUM_BLOCKED` while any run or register capture is not
   terminal, and `CATALOG_VACUUM_NO_HEADROOM` when `pg_database_size` + the
   table's size x 1.1 > 450 MB (the rewrite copies the table first; the
   numbers are printed). VACUUM (FULL, ANALYZE) holds ACCESS EXCLUSIVE on
   each table for the seconds it rewrites it, waiting at most 5 s for the
   lock (set on the connection itself). Check `pg_database_size` in its
   summary (or on the Register page).
   A superseded compaction also waits for every capture-job run that is not
   a register capture (a Prepare) to end.

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
