# Register delta capture (PR-DELTA-0: design and decision)

**Status:** design only. This PR changes no runtime code and adds no migration.
PR-DELTA-1 implements the recommendation below, and only after the owner
approves this document.

**Measured:** 9.10.2026. Production was read with SELECT only; nothing was
written anywhere in production. The churn figures come from a local ephemeral
PostgreSQL 16, and CI runs the same test.

Citations are `file:line`. Migrations are under `supabase/migrations/` and are
cited by their timestamp prefix. Where a function was redefined, the citation
is its CURRENT definition (the last migration that restates it).

## 0. The problem in one paragraph

Every tozar has been captured at least once, but 17 tozars drifted after their
capture, and 79 net rows are now missing. The sync can only re-capture a
drifted tozar WHOLE (PR-SYNC-1, `plan` in `backend/catalog/register/sync.py:96-108`).
That is 54,543 rows to add 79.

The capacity guard prices each claim group as
`pg_database_size + (group rows + rows in flight) × 3,000 B`
(`20260929000100:428-443`; groups are claimed one at a time,
`sync.py:181-200`). At 388.5 MB, any group above **3,835 rows** is refused:

| Group | Priced at |
|---|---|
| מרצדס alone | 428.9 MB |
| ב מ וו | 419.1 MB |
| טויוטה | 407.7 MB |
| פולקסווגן | 406.8 MB |

So the sync stops at the first such group, and auto sync paused itself
(`SYNC_PAUSED_CAPACITY`, 8.10 01:14 IDT, `stop=capacity`).

VACUUM FULL cannot make room either. `register-vacuum.sh` needs
DB + table × 1.1 ≤ 450 MB (`scripts/ops/register-vacuum.sh:43`), and even the
smallest table fails: 388.5 + 64.0 × 1.1 = 458.9 MB.

Any upstream change to a large tozar, even one row, forces a whole re-capture.
On a 500 MB database the current path cannot maintain itself. The problem is
structural.

## 1. Read-only measurements

### 1.1 The 17 drifted tozars (production, SELECT only)

Directory version `9d0df774` holds 101,782 rows. The active snapshots cover
101,703 of them (99.92%).

| Tozar | Directory | Stored (active) | Δ | Active snapshot | Activated | Variants | Distinct content hashes | Null hashes | Compacted | Archive lines = rows | Measured bytes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| מרצדס | 13,475 | 13,448 | +27 | `cs1.4c38f7e764…` | 30.9 | 13,448 | 13,448 | 0 | yes | yes | 23,292,383 |
| ב מ וו | 10,186 | 10,176 | +10 | `cs1.e540904974…` | 30.9 | 10,176 | 10,176 | 0 | yes | yes | 17,393,288 |
| טויוטה | 6,389 | 6,381 | +8 | `cs1.60de34cf84…` | 30.9 | 6,381 | 6,381 | 0 | yes | yes | 11,002,280 |
| פולקסווגן | 6,099 | 6,097 | +2 | `cs1.bceb5bb6e6…` | 1.10 | 6,097 | 6,097 | 0 | yes | yes | 10,313,832 |
| פורד | 2,610 | 2,607 | +3 | `cs1.6d6308c1cb…` | 1.10 | 2,607 | 2,607 | 0 | yes | yes | 4,385,434 |
| שברולט | 2,272 | 2,270 | +2 | `cs1.67d659b51c…` | 1.10 | 2,270 | 2,270 | 0 | yes | yes | 3,884,359 |
| טסלה | 2,139 | 2,138 | +1 | `cs1.e58077f49a…` | 1.10 | 2,138 | 2,138 | 0 | yes | yes | 3,625,562 |
| סובארו | 2,111 | 2,110 | +1 | `cs1.195904c285…` | 1.10 | 2,110 | 2,110 | 0 | yes | yes | 3,481,624 |
| ניסאן | 1,913 | 1,912 | +1 | `cs1.658fdf8008…` | 1.10 | 1,912 | 1,912 | 0 | yes | yes | 3,211,061 |
| לנדרובר | 1,635 | 1,620 | +15 | `cs1.6171bf71b1…` | 1.10 | 1,620 | 1,620 | 0 | yes | yes | 2,858,294 |
| ג'יפ | 1,423 | 1,422 | +1 | `cs1.37c76e5c0d…` | 1.10 | 1,422 | 1,422 | 0 | yes | yes | 2,428,296 |
| מזדה | 1,365 | 1,364 | +1 | `cs1.93158e1f5e…` | 1.10 | 1,364 | 1,364 | 0 | yes | yes | 2,281,408 |
| לקסוס | 1,000 | 999 | +1 | `cs1.313dfd6cc5…` | 1.10 | 999 | 999 | 0 | yes | yes | 1,710,484 |
| סוזוקי | 923 | 922 | +1 | `cs1.57097cb9ff…` | 1.10 | 922 | 922 | 0 | yes | yes | 1,544,525 |
| קאדילאק | 624 | 622 | +2 | `cs1.c57da52250…` | 30.9 | 622 | 622 | 0 | yes | yes | 1,065,712 |
| ג'י.אמ.סי | 192 | 190 | +2 | `cs1.083bef721d…` | 1.10 | 190 | 190 | 0 | yes | yes | 323,838 |
| מקסוס | 187 | 186 | +1 | `cs1.d16e0854bf…` | 1.10 | 186 | 186 | 0 | yes | yes | 315,563 |
| **17 tozars** | **54,543** | **54,464** | **+79** | | | **54,464** | **54,464** | **0** | **17/17** | **17/17** | **93.1 MB** |

- **Measured bytes** is `catalog_register_snapshot_compactions.bytes_after`
  (`catalog_register_measured_bytes`). It counts heap only: raw records,
  candidates, variants and two ledger levels.
- **Δ** is net growth in every row. Which of those rows are additions and which
  are changes is known only after the fetch and the hash comparison (§3).

**Change detection is feasible from data already stored, for every tozar, not
only these 17.** Across all 138 active rank-1 snapshots:

- stored rows = variant rows = 101,703;
- there are 101,703 distinct `(snapshot, content_sha256)` pairs, so no
  content repeats inside any snapshot today;
- there are no null hashes;
- no snapshot lacks a complete current-mapper hash set;
- no active snapshot is uncompacted.

Every snapshot is compacted, so the hash lives on the variant row
(`catalog_variants.content_sha256`, `20260930000100:145`). The readers reach it
through `catalog_raw_record_content_sha256` (`20261002000100:200-212`).

The brief counts 137 tozars. The current directory version has **138** units,
all 138 have an active snapshot, and none has 0 rows.

### 1.2 Database state (production, SELECT only)

- `pg_database_size`: 388,492,435 B (388.5 MB).
- autovacuum: on, with `autovacuum_vacuum_scale_factor` 0.2, threshold 50 and
  naptime 60 s; no per-table reloptions. PostgreSQL 17.6.

| Table | Heap | Live tuple bytes (`sum(pg_column_size(t.*))`) | Heap − live (reusable free + dead) | Index + TOAST | Lifetime ins / upd / del | Dead now |
|---|---|---|---|---|---|---|
| `catalog_raw_records` | 53.2 MB | 34.6 MB (340 B/row) | 18.6 MB | 34.0 MB | 134,122 / 115,028 / 32,385 | 14,271 |
| `catalog_candidate_variants` | 18.1 MB | 14.6 MB (144 B/row) | 3.5 MB | 45.9 MB | 131,307 / 115,002 / 29,596 | 14,280 |
| `catalog_variants` | 71.3 MB | 64.1 MB (630 B/row) | 7.2 MB | 37.7 MB | 115,006 / 0 / 13,303 | 6,924 |
| `catalog_variant_coverage` | 62.9 MB | 59.4 MB (292 B/row) | 3.5 MB | 40.2 MB | 203,380 / 26,613 / 0 | 13,846 |

- **The ~115k updates on raw records and candidates are the PR-L2 compaction.**
  It writes one UPDATE per row, which leaves one dead tuple per row
  (`20261002000100:643-650`).
- **Reusable space.** The heaps hold about **32.8 MB** of it. Index free space
  is not measured: that needs `pgstattuple`, and installing an extension is a
  write.

### 1.3 MVCC churn: one whole re-capture vs. one delta (local PostgreSQL 16)

`tests/test_register_delta_churn_postgres.py` follows the PR-L2 pattern
(`tests/test_register_compaction_postgres.py:814-885`):

- every migration is applied;
- rows are written with `_bulk_snapshot`;
- the build runs through `record_catalog_variants`, and compaction through
  `compact_register_snapshot`;
- autovacuum is off, so the dead-tuple counts are exact;
- statistics are read until two reads agree.

The test tozar has 1,000 rows. Its drift is 8 added rows, 1 changed row and 1
removed row, so the fresh count is 1,007.

Three runs agree on every tuple count. Bytes vary slightly between runs. One
output (`pytest -q -rs tests/test_register_delta_churn_postgres.py`, 57 s):

```
PR-DELTA-0 churn of one drifted 1,000-row tozar (+8 added, 1 changed, -1 removed); per table: inserted/updated/deleted/dead tuples
  whole re-capture (today): tuples written 7,049, dead 7,012, WAL 13,069,208 B, file growth after plain VACUUM 6,627,328 B, live growth 16,384 B [raw_records 1007/1007/1000/2007; candidate_variants 1007/1007/1000/2007; variants 1007/0/1000/1000; variant_coverage 16/1998/0/1998]
  delta (option A): tuples written 65, dead 20, WAL 654,272 B, file growth after plain VACUUM 262,144 B, live growth 40,960 B [raw_records 9/9/0/9; candidate_variants 9/9/0/9; variants 9/0/0/0; variant_coverage 16/2/0/2; delta_retirements_probe 2/0/0/0]
  option C, 6 whole re-captures of a 1,000-row tozar with a plain VACUUM after each, file bytes (first and last after VACUUM FULL): 9,248,768 -> 16,072,704 -> 17,883,136 -> 18,210,816 -> 18,235,392 -> 18,276,352 -> 18,341,888 -> 9,248,768
```

Ranges over the three runs:

| Path | WAL | File growth after plain VACUUM |
|---|---|---|
| Whole re-capture | 12.79–13.07 MB | 6.47–6.63 MB |
| Delta | 0.61–0.65 MB | 256 KB |

**One whole re-capture, per row of the tozar.** The coverage UPDATEs happen
because `snapshot_key` changed: when two rows have the same rank, the newer
row's facts replace the older ones (`20261001000100:428-446`).

| Table | What happens to each row | Tuples written (ins + upd) | Deleted | Dead |
|---|---|---|---|---|
| raw records | inserted; UPDATEd by the compaction; the old row is DELETEd by the superseded compaction (`20261002000100:628-650`) | 2 | 1 | 2 |
| candidates | the same | 2 | 1 | 2 |
| variants | inserted; the old row deleted | 1 | 1 | 1 |
| coverage | 2 UPDATEs | 2 | 0 | 2 |

Per row of the tozar, that comes to:

- **7.0 tuples written** and 7.0 dead tuples;
- **6.4–6.6 KB of space left to reclaim** (file growth after a plain VACUUM);
- 12.7–13.0 KB of WAL.

Live growth is only 16 KB, which is the 7 net new rows.

**The delta.** For 9 changed rows and 2 retirements it writes 65 tuples and
leaves 20 dead:

- 2 dead per changed row come from compacting the delta's raw and candidate
  rows;
- 2 more come from the coverage rows of the one changed key.

The file growth (256 KB) and live growth (40 KB) are page granularity: a
handful of new 8 KB pages across five tables and their indexes.

What the delta measurement does and does not cover:

- **Covered: the delta's WRITES.** Its rows go through today's build and
  compaction as a separate, activated scope. The never-activated path is what
  PR-DELTA-1 builds.
- **Not counted: per-delta constant rows,** about 10 small tuples per delta
  (registry, build, snapshot, unit and compaction rows).

**Repeated whole re-captures (option C).** Plain VACUUM between cycles lets the
file reuse its space. Growth per cycle:

| Cycle | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Growth | +6.82 MB | +1.81 MB | +0.33 MB | +0.02 MB | +0.04 MB | +0.07 MB |

The file sits at **about +9.1 MB per 1,000 rows re-captured inside one vacuum
window (~9.1 KB per row)**. That is the transient of one capture: new rows
uncompacted (5,256 B) next to the old rows compacted (2,931 B), plus index
pages. A small residual creep remains, 0.02–0.07 MB per cycle in this run (the
other run measured +0.14 MB at its fourth cycle). Six cycles cannot show
whether the creep stops.

**Q1 checks.** The test also checks the change detection of §3:

- the multiset difference between stored hashes and fresh hashes is exactly
  9 added and 2 retired;
- renumbering every `_id` gives the same answer;
- a row duplicated with only `_id` changed counts twice.

## 2. Facts in the code this design relies on (verified)

| Fact | Where |
|---|---|
| `catalog_variants.content_sha256` is the record's content without `_id`: `sha256((payload - '_id')::text)`. Immutable, storage-local. | `20260927000100:163-170`; build `20261001000100:252-465`; storage-local rule `backend/catalog/digest.py:1-27` |
| `catalog_raw_records.payload_sha256` covers the full payload, `_id` included. | `20260914200000:140`; `catalog_raw_record_payload_matches` `20261002000100:745-753` |
| Snapshot identity = content. `snapshot_content_sha256` hashes query, page size, reported total, schema fingerprint and every page body's checksum, so `_id` and row order are included. An identical capture IS the same snapshot. The key also includes `upstream_version`. | `backend/catalog/government/snapshot.py:17-25, 86-111`; `backend/catalog/keys.py:107-118`; `catalog_source_snapshots_key_uidx` `20260914200000:116`; replay `ingest.py:41-66` |
| An active snapshot is frozen. Activation is gated on stored = declared. A pending snapshot advances its count and state, and is writable by its writer or adopter until it is failed or active. | `20260914200000:109-112, 446-470`; `activate_catalog_snapshot_guarded` `20260924000200:330-387`; `assert_snapshot_write_authority` `20260924000200:164-173` |
| "Active" is the newest `activated_at`. Nothing enforces one active per tozar; every rank-1 reader orders `activated_at desc, id`. | `compact_register_snapshot` `20261002000100:475-483`; retention `20261001000100:641-702` |
| "Served" is the newest COMPLETE `catalog_variant_builds` row per tozar. Every build reader ranks `distinct on (tozar) … order by activated_at desc, snapshot_id`. A build row requires a non-null `tozar` and `activated_at`, and every variant row needs one (FK). | `20260930000100:116-128, 221-222`; `catalog_variant_current_snapshot` `20261001000100:472-482`; view `:165-176` |
| After its build the rank-1 snapshot is compacted (payload NULL, candidate keys only). Superseded snapshots keep only referenced rows, as skeletons. | `20261002000100:410-668`; `catalog_candidate_referenced` `:673-686` |
| Count verification compares stored rows with a FRESH `count_tozar` taken after every write. | `backend/catalog/register/capture.py:188-193, 222-232`; `record_register_unit_status` `20260929000100:575-582` |
| Archives are create-only, one per snapshot, with a recorded line count and sha256. A line is `source_locator.capture_index + 1`. | `20260929000100:272-310`; `capture.py:143-185` |
| `catalog_variant_coverage` is keyed by `(variant_identity_key, level)`, not by snapshot. A row named by a newer-activated snapshot is never taken back by an older one. | `20260927000100:215-250`; `20261001000100:428-446` |
| The capacity guard is `projected = pg_database_size + (group + in-flight rows) × bytes_per_row`, per claim group. | `20260929000100:428-443`; `service.py:252-259`; `config.py:40,54` |
| A unit costs `1 + ceil(rows/1000) + 1` requests, plus 1 retry headroom, out of an 80-request sync. | `sync.py:53-57, 91-93` |
| A snapshot with NO `capture_scope` is read as the whole register's by refresh and Prepare. | `backend/catalog/government/refresh.py:228-236, 311-321`; `capture_scope.declared_scope` `:157-182` |
| `retrieval_metadata` is immutable once written. | `20260914200000:456-468` |
| The maintenance RPCs carry pinned `statement_timeout` / `lock_timeout`, and a `create or replace` resets them. | `20261004000100`; `tests/test_register_rpc_timeouts.py` |

## 3. Q1: how to detect the change

**Rule:** compare the multiset of content hashes, never `_id`. Define:

- `S`: the multiset of `content_sha256` over the tozar's SERVED rows (today the
  current build's variant rows; with option A, base ⊕ deltas);
- `F`: the multiset of `catalog_variant_content_sha256(row)` over a fresh,
  complete fetch of the tozar.

Then:

| Result | Definition |
|---|---|
| **added** | F − S, each hash with its surplus count |
| **retired** | S − F |
| **kept** | min(F, S) per hash |

Duplicates count: two upstream rows can differ only in `_id`.

- **A changed row** is one retirement plus one addition (§7).
- **A renumbered `_id`** changes nothing.
- **A changed field schema** changes every hash. That reads as "everything
  changed" and falls back to a whole re-capture (§10.2).

**The hashes are computed in the database, not in Python.** The content hash
and `payload_sha256` are storage-local: they are taken over PostgreSQL's
`jsonb::text` rendering, which Python must not reproduce
(`backend/catalog/digest.py:1-27`). PR-DELTA-1 adds two read-only functions:

- one takes a page of fresh rows (≤ 1,000 payloads, about 1.5 MB) and writes
  nothing. It returns, in order, each row's content hash (identity of content)
  and its `payload_sha256` (the delta's identity, §4);
- one returns the tozar's served hashes with each row's
  `(snapshot_id, upstream_record_id)`, paged.

The diff itself is pure Python over those lists. The measurement test does the
same thing in one SQL statement (`_diff` in
`tests/test_register_delta_churn_postgres.py`).

**Which served row a retirement names.** When a hash is retired `k` times, the
`k` served rows with that hash are chosen by `(serving order, upstream_record_id
collate "C")`, where serving order is the base first, then deltas by sequence.
The choice is deterministic, so a replay retires the same rows.

**Requests.** The fetch does not change: the same `capture_resource` of the
exact tozar, `1 + ceil(rows/1000) + 1` requests. Only storage and the build
become incremental.

- **מרצדס, 13,475 rows:** 1 + 14 + 1 = **16 requests**, plus 1 retry
  headroom = 17 of the 80-request sync.
- **All 17 drifted tozars:** Σ `ceil(n/1000)` = 64, so 64 + 2 × 17 = **98
  requests**, plus ~13 for the check and the light directory.
- So the current backlog takes **two syncs**, the same request count as
  today's whole re-capture.
- **Detection itself:** 0 data.gov.il requests, about `ceil(rows/1000)`
  database round trips for the hashes, and one paged read of the served
  hashes.

## 4. Q2: how to store the change

### Option A: delta snapshot (base ⊕ deltas)

**What a delta is.**

- **The snapshot.** A delta is a `catalog_source_snapshots` row that holds ONLY
  the added rows (raw records, candidates and, after its build, variants).
- **Its archive.** It has its own create-only archive of exactly those lines.
- **Its retirements.** It carries an append-only retirement list naming served
  rows by `(snapshot_id, upstream_record_id, content_sha256)`.
- **Its application.** Once all checks pass, it is applied by one application
  row.

**The served state of a tozar** is the union of:

- the variants of its current WHOLE build (the base, i.e.
  `catalog_variant_current_snapshot`, restated only to ignore delta builds);
- the variants of every APPLIED delta of that base;

minus every row retired by an applied delta.

**Three properties keep a delta apart from every existing reader. The database
enforces all three, not convention.**

1. **The delta is marked at birth and can never be activated.**
   - The snapshot is created with `retrieval_metadata.register_delta`:
     contract `gov.register.delta.1`, base key, previous delta key, sequence,
     added and retired counts, `retired_sha256` and `state_sha256`. All of it
     is known BEFORE the first write (§8.1 step 2), and `retrieval_metadata` is
     immutable afterwards.
   - A new BEFORE UPDATE trigger on `catalog_source_snapshots` refuses any
     `activated_at` on a marked row (`CATALOG_DELTA_NEVER_ACTIVATED`). So
     `activate_catalog_snapshot_guarded` cannot activate it, and the ingestor's
     "activate last" (`ingest.py:21-25`) cannot either.
   - As a result, every activated-only reader keeps seeing only the base:
     rank-1 resolution, compaction, the retention rank, the superseded list,
     refresh and Prepare (`resolve_active_snapshot`), the batch preparation
     gate (`20261002000100:1932-1937`), and Python's `find_active_catalog_snapshot`.
2. **Its build is marked too.** `catalog_variant_builds` gains
   `base_snapshot_id` (null for a whole build).
   - A delta's build row carries its base and the base's `activated_at`, which
     satisfies the NOT NULL column.
   - Every reader that ranks builds is restated with
     `base_snapshot_id is null`, as is the partial index
     `catalog_variant_builds_current_idx`. Those readers are
     `catalog_variant_current_snapshot`, `catalog_variants_current`,
     `catalog_browser_manufacturers`, retention's keep-set, `prune_register_snapshots`'
     `cur`, the superseded-compaction build check, `catalog_register_prunable_variant_builds`
     and `catalog_register_unit_measure`.
   - So a built but unapplied delta is invisible to every reader of served
     content.
3. **It declares the tozar's `capture_scope`.** No reader mistakes it for the
   whole register (`refresh.py:228-236`). It passes
   `catalog_capture_scope_consistent`: the query is the tozar's, and that is
   what was fetched.

**Applied means frozen.** `apply_register_delta` sets
`validation_state = 'complete'` in the same transaction (a pending snapshot may
advance it).

`assert_snapshot_write_authority` (`20260924000200:164-173`) is restated to
refuse every guarded write to a snapshot named in `catalog_register_deltas`:

- rows, candidates, failure marking, adoption;
- code `CATALOG_DELTA_APPLIED_IMMUTABLE`.

An applied delta therefore cannot gain rows, be failed, or be adopted, even
though it is never "active".

**Delta identity.**

- `content_sha256` is sha256 of a canonical manifest:
  - the contract;
  - the base `snapshot_key`;
  - the previous delta's key;
  - the sorted `payload_sha256` of the added rows;
  - the sorted retired `(snapshot_key, upstream_record_id)`.
- `snapshot_key` is derived from it as today (`keys.py:107-118`). The key also
  includes `upstream_version`.
- Within one register version, a re-run over the same state lands on the SAME
  delta key.
- `state_sha256` is the sha256 of the sorted multiset of served content hashes
  AFTER the delta. It is the `_id`-blind identity of the served state.
  `apply_register_delta` recomputes it and refuses a mismatch.

**Fold-back.**

- A whole re-capture through today's path IS the fold. The new full snapshot
  is activated, built (`base_snapshot_id` null), compacted, and becomes the
  current build. The old base's deltas then serve nothing.
- The old base is then superseded-compacted exactly as today. Retirements hold
  no foreign key to raw records (§9), so nothing blocks that DELETE.
- The sync takes the whole path when:
  - the delta would exceed 25% of the tozar;
  - the chain reaches 16 deltas;
  - or the schema fingerprint changed (§10.2).

  Always under the existing capacity guard.
- A fold is never needed for correctness, so a large tozar keeps its deltas
  until the database has room.

### Option B: carry-forward snapshot (a full new snapshot that reuses unchanged rows)

**The idea.** `S2` carries the full fetched content and its true
`snapshot_content_sha256`. Its unchanged rows reuse `S1`'s raw, candidate and
variant rows instead of being inserted again.

**The keys allow only one way to do this: MOVE the rows.**

- Every row is owned by one snapshot: `catalog_raw_records
  (snapshot_id, upstream_record_id)` is unique (`20260914200000:162`).
- `catalog_variants.snapshot_id` points to snapshots, RESTRICT.
- `(snapshot_id, upstream_record_id)` points to raw records, RESTRICT, and
  `(snapshot_id, mapper_version)` points to builds (`20260930000100:219-222`).

**What moving takes, per unchanged row.**

- Three UPDATEs:
  - `catalog_raw_records`: `snapshot_id = S2`, `record_key`, `source_locator`,
    and `upstream_record_id` if `_id` moved;
  - `catalog_candidate_variants`: `snapshot_id = S2`;
  - `catalog_variants`: `snapshot_id = S2`, `snapshot_key`, `archive_line`.
- The three must run in ONE statement (writable CTEs) or behind FKs made
  `DEFERRABLE`, because the composite FK is `NO ACTION` on update and is
  checked at the end of each statement.
- It all runs in a security-definer function that suspends three triggers:
  - `catalog_raw_records_append_only` (`20260914200000:432`);
  - `catalog_candidate_variants_identity_immutable` (`:511`);
  - `catalog_variants_append_only` (`20260930000100:244`).

**What breaks.**

- **Referenced candidates cannot move.** A candidate referenced by a queue
  item, an evidence link, field provenance, a promotion or a reservation has
  `S1` as its provenance, so it must be copied instead, which is a re-insert.
- **`S1` itself becomes inconsistent.** It loses rows: its
  `stored_record_count` no longer equals its archive `line_count`
  (`20261002000100:604-610`), while it is frozen as active
  (`20260914200000:452-453`).

**The MVCC churn that remains.** Every UPDATE writes a complete new tuple.
`snapshot_id` is indexed in all three tables, so none of these updates is HOT
and every index gets a new entry. Per unchanged row:

- 3 new tuples and 3 dead (raw, candidate, variant);
- 2 more if the coverage rows' `snapshot_key` is refreshed, as the ledger rule
  does today;
- space left to reclaim, from production widths (§1.2): heap 340 + 144 + 630 =
  1,114 B, plus about 1,156 B of index entries, ≈ **2.3 KB** (≈ 3.3 KB with
  coverage), against 6.4–6.6 KB today.

B saves the payload transient and the compaction rewrite, **but it is still
O(tozar) per drift.**

**B2 (membership table).** `S2` lists the variant ids it shares with `S1`:
about 150–200 B per row per snapshot, with no dead tuples. But every reader
must read through the membership. That is A's reader change plus an O(tozar)
write per drift.

### Option C: measure live data, not file size; keep whole re-capture

**The proposal.** Keep the write path. Make three measures read live bytes
(`sum(pg_column_size)` plus index estimates) instead of `pg_database_size`:

- the capacity guard;
- autosync rule 6 (`backend/catalog/register/autosync.py:121-124`);
- the FAIL of `REGISTER_COVERAGE`.

Then rely on plain autovacuum to make dead space reusable.

**Measured steady state (§1.3).** The file settles at **live + ~9.1 KB per row
re-captured inside one vacuum window**, with a small residual creep of
0.02–0.14 MB per 1,000-row cycle. At 56k–78k rows per week (§6), that creep
would be about 1–11 MB per week if it persists.

**The vacuum window.** A table is vacuumed after `50 + 0.2 × reltuples` dead
tuples, about 20,400 for raw records. Each re-captured row leaves 2 dead in raw
records, so autovacuum starts roughly every 10,000 re-captured rows. Space is
reusable only after it has run.

| What is re-captured in one window | High-water mark |
|---|---|
| מרצדס alone | live + 13,475 × 9.1 KB = **live + 123 MB** |
| A realistic 10,000–20,000-row window | **live + 91–182 MB** |

**Where that leads.** From today's 388.5 MB, with only 32.8 MB of measured
reusable heap space:

- re-capturing מרצדס takes `pg_database_size` to about
  388.5 − 32.8 + 123 ≈ **479 MB**;
- it goes higher if the window holds more than one tozar.

The plan limit counts size on disk, dead space included
(`REGISTER_CAPTURE.md:64-65`). So **C moves the guard off the number the plan
enforces**, while the file approaches 500 MB.

C is only viable with per-table autovacuum tuning (a scale factor around 0.01,
which is a migration of reloptions). Even then the floor is live + the largest
tozar's 123 MB.

### Other options considered

| Option | What it is | Verdict |
|---|---|---|
| D. Amend the active snapshot in place | Insert added rows into `S1`; delete retired ones | Refused. An active snapshot is frozen (`20260914200000:452-453`), and its identity, declared count, archive line count and compaction preconditions would describe content it no longer holds. |
| E. Detection only: skip unchanged tozars | Use the `_id`-blind hash to skip a re-capture whose content did not change even though `_id` or order moved. Today only an identical page chain is reused (`ingest.py:41-48`). | Folded into A as the zero-delta case: an unchanged multiset writes nothing. It does not help the 17 drifted tozars, which DID change. |
| F. A larger plan (paid) | | The owner's decision, not an engineering change. It postpones the problem. |
| G. Scheduled VACUUM FULL | | Blocked by the 450 MB headroom rule (§0), and it takes ACCESS EXCLUSIVE locks. |

## 5. Q3: invariants, option by option

Key: **K** = kept unchanged, **C** = changed (how), **B** = broken.

| Invariant | A (delta) | B (carry-forward, move) | C (live measure) |
|---|---|---|---|
| **Snapshot identity = content** (`snapshot.py:86-111`) | **C.** Full snapshots keep `snapshot_content_sha256`. A delta has its own manifest identity. The tozar's SERVED state is identified by `state_sha256` (`_id`-blind, order-blind), not by any page-chain hash. A full capture of the same state has a different snapshot identity than base ⊕ deltas, but the same `state_sha256`, and that is checked. | **B.** `S2` keeps its true identity, but `S1` stops holding the rows its identity describes. | **K** |
| **One active snapshot per tozar** (rank-1 by `activated_at`) | **K, enforced.** A delta can never be activated (trigger). The base stays rank-1. What is SERVED becomes base ⊕ applied deltas, through one function (§8.3). | **K** (`S2` becomes rank-1) | **K** |
| **Count verification against a fresh count** (`capture.py:188-193`) | **C.** The same fresh `count_tozar` after every write, compared with served-before − retired + added. `apply_register_delta` also checks `state_sha256`, which is stronger than a count. `record_register_unit_status` accepts a delta unit: snapshot = the delta, `captured_rows` = served rows. | **K** | **K** |
| **Create-only archive and line numbers** (`20260929000100:272-310`) | **K.** One create-only object per delta, with exactly its added lines. Line = `capture_index + 1`, the row's index inside the delta. The base's archive is untouched, so retired rows stay citable there. | **B.** Moved rows need new lines in `S2`'s archive, and `S1`'s `line_count` no longer equals its rows. | **K** |
| **Compaction preconditions** (`20261002000100:410-668`) | **C.** The base is untouched, and a fold supersedes it as today. A delta is compacted by a delta branch with the same checks over its own rows: archive read-back, build complete, typed rows read as payload, nothing live reading it. The superseded build check ignores delta builds. A superseded base's deltas keep their few rows until PR-DELTA-2 cleans them up. | **B.** It moves compacted rows and needs a fourth trigger suspension. | **K** |
| **`source_record` replay** (`compaction.py:236-266`, `scripts/export_replay_capture.py:97-145`) | **C.** A base row reads exactly as today. A delta row cannot go through `catalog_raw_record_by_upstream_id` (`catalog_readable_snapshot` refuses a non-active snapshot, `20261002000100:1346-1350`) or `find_active_catalog_snapshot` (`--show-record`, `compaction.py:314`). PR-DELTA-1 adds `catalog_register_delta_record(snapshot_key, upstream_record_id)` for an APPLIED delta, and a delta branch in `source_record` / `--show-record`. That branch reads the delta's archive line by `capture_index` and checks the line's sha256 against the row's `payload_sha256`. Run checkpoints and batches never pin a delta, so the replay export is unchanged. | **B.** A moved row's `snapshot_key` and line change under an existing citation. | **K** |
| **Variant build and mapper version** (`20261001000100:252-465`) | **C.** Delta rows are built under the current mapper by the same mapping. The build gate admits a snapshot that carries the delta marker, whose base is the tozar's current WHOLE build under that mapper, whose stored = declared = archive line count, and that is not applied; the writer's lease is checked. The count is verified at apply, not at the build. The delta build writes no coverage rows; apply does. A mapper bump stays refused (`CATALOG_COMPACTION_MAPPER`). A future rebuild-from-archive must rebuild deltas from their own archives. | **C.** Moved variants keep their mapper rows. | **K** |
| **Coverage rows** (`20260927000100:215-250`) | **C.** Written only for the keys a delta touches, in the apply transaction, under the delta's `snapshot_key`. The collision rule is evaluated over the SERVED rows (today's rule is scoped to one snapshot, `20261001000100:397-427`). The "never taken back" precedence (`:433-435`) is restated over a serving rank: (the base's `activated_at`, then delta sequence). A delta-named row is then protected from an older build, as an activated snapshot's row is today. A build that completes takes the same per-tozar advisory lock as apply, so the base cannot change under an apply. Unchanged keys are not rewritten: 2 updates per changed key instead of 2 per row. A removed row's ledger row stays, as today. | **C/K.** No saving unless the refresh rule changes. | **K** (2 updates per row stay) |
| **Catalog browser reads** (`20261001000100:472-640`, `20260930000100:494-533`) | **C.** `catalog_variants_current` and `catalog_browser_{manufacturers,models,years,variants,facets}` read the served set: `snapshot_id = any(base, applied deltas)` minus retired rows. The tree index `(snapshot_id, mapper_version, kinuy_mishari, shnat_yitzur)` serves `= any(...)`. The unfiltered manufacturer count becomes served rows, not `built_rows`. With no delta, or only an unapplied one, every answer is identical (golden tests). | **K** | **K** |
| **Deployed gate `REGISTER_COVERAGE`** (`scripts/ops/gates.sh:51-89`, `20260929000100:792-821`) | **K** (its SQL is unchanged). The newest captured unit's `captured_rows` is the served count. `unverified_snapshots` is unchanged. | **K** | **C.** FAIL on live bytes, not `pg_database_size`. |
| **Retention and prune** (`20261001000100:641-702`, `prune_register_snapshots` `20261002000100:2628`) | **C.** New keep rule: every snapshot named by an applied delta (base or delta) is kept, and the build rankers ignore delta builds. A pending, never-applied delta whose writer ended is prunable like any orphan pending scoped snapshot; its build and variants are deleted with it, as `prune_register_snapshots` already does for any snapshot. PR-DELTA-1 never prunes an applied delta or a base with applied deltas. | **C** | **K** |
| **Batch runs that pin a snapshot** (`20261002000100:1717-1745, 1747-2093`; `government/preparation.py:865-965`) | **K.** A batch pins an ACTIVATED snapshot through `prepare_work_scope_queue`, which refuses a non-activated one (`20261002000100:1932-1937`). Prepare captures its own snapshot (`refresh.py:281-285`). The swarm's Government reads are unchanged, so MILO swarm behavior is preserved. | **B.** A batch pinned on `S1` loses rows mid-plan unless its snapshot is excluded from the move. | **K** |
| **Capacity guard** (`20260929000100:428-443`) | **C.** Same formula. A delta unit reserves its expected delta rows, not the tozar's, and is re-checked with the real delta row count BEFORE the first write. | **C** | **C.** It measures live bytes, which is no longer what the plan counts. |

## 6. Q4: database cost

**Per-row costs used below.**

- **Whole re-capture (today and C), measured (§1.3), per row of the tozar:**
  - 7.0 tuples written, 7.0 dead;
  - 6.4–6.6 KB left to reclaim (file growth after plain VACUUM);
  - WAL 12.7–13.0 KB;
  - high-water: +9.1 KB per row of one vacuum window.
- **A, measured, per changed row:**
  - 7.2 tuples written (compaction and coverage included), plus 1 per
    retirement and about 10 constant per delta;
  - 2 dead, plus 2 per changed identity key;
  - live 2,931 B per added row (PR-L2,
    `tests/test_register_compaction_postgres.py:814-885`);
  - about 0.2 KB per retirement row;
  - about 2.3 KB per added row left to reclaim (5,256 − 2,931).
- **B, estimated (not measured)** from production widths: 2.3 KB (3.3 KB with
  coverage) left to reclaim per UNCHANGED row, on top of A's cost for the
  changed rows.

### (a) Absorbing the current 17-tozar drift

54,543 directory rows; at least 79 rows added. Each changed row would add one
addition and one retirement.

| Option | Tuples written | Dead tuples left | Bytes written / left to reclaim | Capacity guard at 388.5 MB (per claim group) |
|---|---|---|---|---|
| Today (whole) | ≈ 381,800 | ≈ 379,800 | ≈ 351–359 MB to reclaim; ≈ 0.70 GB of WAL | **Refused** for any group > 3,835 rows: מרצדס alone 428.9 MB, ב מ וו 419.1, טויוטה 407.7, פולקסווגן 406.8. Reserving all 17: 54,543 × 3,000 B = 163.6 MB. |
| A (delta) | ≈ 570 + retirements + ~170 constant (17 deltas) | ≈ 160 | live +0.23 MB; ≈ 0.18 MB to reclaim | 388.5 MB + 79 × 3,000 B = **388.7 MB: admitted** |
| B (carry-forward) | ≈ 163,000 (≈ 272,000 with coverage), plus A's | the same | ≈ 124 MB (≈ 177 MB with coverage) to reclaim | O(tozar) reservations; refused at today's pricing |
| C (live measure) | ≈ 381,800 | ≈ 379,800 | ≈ 351–359 MB of churn; the file rises toward ≈ **479 MB** or more | Admitted by a live-bytes guard, while the file nears the 500 MB plan |

### (b) Steady state: about 100 drifted rows per week across 10–20 large tozars

In production the 10 largest tozars hold 56,053 rows and the 20 largest hold
77,791.

| Option | Rows rewritten per week | Tuples written / dead per week | Growth |
|---|---|---|---|
| Today (whole) | 56,053–77,791 | ≈ 392k–545k / ≈ 390k–542k | 360–513 MB per week to reclaim. Refused by the guard, so the sync pauses (as now). |
| A (delta) | ~100, plus ~10–20 constant rows per delta | ≈ 720–900 / ≈ 200 | **+0.3 MB per week live (≈ 16 MB per year).** About 0.23 MB per week is reclaimed and reused. Retired rows stay in their base until a fold. |
| B (carry-forward) | 56,053–77,791 | ≈ 168k–389k / the same | 128–253 MB per week to reclaim. High-water: + 2.3–3.3 KB × the rows of one vacuum window (מרצדס ≈ 31–44 MB). |
| C (live measure) | 56,053–77,791 | as today | The plateau is **live + 9.1 KB × the rows of one vacuum window**: ≥ +123 MB for מרצדס, ≈ +91–182 MB per default autovacuum window. That is above the 400 MB threshold and near the 500 MB plan, plus a residual creep of up to ~1–11 MB per week if it persists. |

## 7. Q5: removals and changes upstream

| Upstream event | Hash detection sees | Option A writes |
|---|---|---|
| A row is added | +1 hash | 1 delta row (raw, candidate, variant; compacted) |
| A row is removed | −1 hash | 1 retirement row naming the served row. The base row stays as history, and so does its archive line. |
| A row changes (any field but `_id`) | −1 old hash, +1 new hash | 1 retirement + 1 delta row. Same identity key: its 2 coverage rows are updated in the apply transaction ("a changed content replaces"). The key moved: the new key's rows are inserted and the old key's rows stay, as today. |
| `_id` renumbered, content the same | nothing | nothing. The served row keeps its old `upstream_record_id`, which remains the correct citation into its archive. |
| Two rows that differ only in `_id` | one hash, count 2 | the multiplicity is kept; the count decides how many are added or retired |
| A row changes, then changes back | −1/+1, then −1/+1 | two deltas. `state_sha256` returns to its earlier value; a fold collapses them. |
| Every row changes (schema or format change) | −N/+N | above the 25% threshold, so the whole path (fold) runs, under the existing guard |

The collision rule (`CATALOG_COVERAGE_KEY_COLLISION`) is evaluated over the
served set after the delta. A changed row's old and new content therefore never
collide, because the old one is retired. Two served rows that share a key with
different content still collide, exactly as they do within one snapshot today.

## 8. Q6: safety

### 8.1 The order (PR-DELTA-1's capture job)

1. **Fetch the whole tozar.** This is the existing `capture_resource`, with the
   same completeness gates, and it writes nothing (`ingest.py:5-11`: "capture
   first, and completely"). A 403 or 429 ends the sync (`GOV_SYNC_THROTTLED`,
   `sync.py:80-88, 214-215`). **Nothing is written.**
2. **Diff in the database, read-only.** Hash the fetched rows, read the served
   hashes, and compute added and retired, the delta manifest, `retired_sha256`
   and `state_sha256`.
   - Both empty: record the unit `captured` (served rows = fresh count) with
     no new snapshot. This is option E.
   - The delta is above 25%, the chain is too long, or the schema moved: take
     today's whole path.
3. **Capacity re-check** with the real delta rows (`delta rows ×
   bytes_per_row`). A refusal is `stop=capacity`, and **nothing is written**.
4. **Open the delta snapshot** (pending, marked `register_delta` in its initial
   metadata). Write its rows and candidates under the lease, with the
   ingestor's batch writes.
5. **Archive the added lines.** Create-only, recorded and read back. This sets
   declared = stored = line count.
6. **Build the delta's variants** under the current mapper. The build row
   carries `base_snapshot_id`, and no coverage is written.
7. **Fresh `count_tozar`:** an independent count taken after every write.
8. **`apply_register_delta`.** One transaction under the lease, and under the
   per-tozar advisory lock that build completion also takes. It checks:
   - the base is still the tozar's current WHOLE build;
   - the chain head is the one the diff was computed against (optimistic
     concurrency);
   - the delta's rows = stored = archive `line_count` = built variants;
   - every retirement names a served, not yet retired row;
   - served-before − retired + added = the fresh count (`api_total`);
   - the recomputed `state_sha256` equals the job's.

   Then, in the same transaction, it inserts the application row and the
   retirements, sets `validation_state = 'complete'`, writes the coverage rows
   for the touched keys, and records the unit `captured`.
9. **Compact the delta** (the delta branch, with archive read-back). A failure
   is reported and the unit stays captured, as today (`capture.py:250-255`).

### 8.2 Failures halfway

| Failure | At | Result |
|---|---|---|
| 403 / 429 / network | step 1 | Nothing written. The unit is retryable. |
| 403 / 429 on the fresh count | step 7 | The delta is pending and built, but not applied, and invisible (§4 properties 1–2). The unit is `failed` and retryable. The served state is unchanged. |
| Lease lost or cancelled | steps 4–8 | Every write is lease-guarded (`assert_worker_lease`), so a stale writer writes nothing more. The pending delta is invisible. The next sync recomputes the same diff from the unchanged served state. Within the same register version it derives the SAME delta key and adopts the orphan under the existing rule: the previous writer ended `failed`, `cancelled` or `timed_out` with no live lease, and the adopter is a live `operator_capture` run (`20260924000200:194-219`). Otherwise, for example after a new register version, it creates a new delta and the orphan becomes prunable. |
| Capacity | step 3 | `stop=capacity`; nothing written. |
| The base is superseded meanwhile (another capture folded the tozar) | step 8 | `apply_register_delta` refuses (base or chain head moved). The delta is never served; the next sync diffs against the new base. |
| Someone tries to activate the delta | any | The trigger refuses (`CATALOG_DELTA_NEVER_ACTIVATED`). |
| Someone writes to, fails or adopts an applied delta | after 8 | `assert_snapshot_write_authority` refuses (`CATALOG_DELTA_APPLIED_IMMUTABLE`). |
| Crash inside step 8 | | Rollback: no application row, no retirement, no coverage row. |
| Compaction fails | step 9 | Served correctly, uncompacted (as today). The next claim is priced uncompacted through `catalog_register_uncompacted_captures`, extended to applied deltas. |

### 8.3 Why a failed delta never leaves the served state partially updated

The served state of a tozar is a pure function of three things:

- **(i) the base:** the current WHOLE build, `base_snapshot_id is null`;
- **(ii) the APPLIED deltas** of that base;
- **(iii) the retirements OF APPLIED deltas.**

The served-set function reaches delta rows and retirements only through
application rows. (ii), (iii) and the coverage rows that describe served
content are written ONLY by `apply_register_delta`, in one transaction, after
every check.

Before apply, a delta's snapshot, rows, archive record and build row can exist,
but no reader reaches them:

- the snapshot can never be activated (a trigger), so the rank-1, Prepare,
  batch and compaction readers never see it;
- its build row is marked, so every served-build reader skips it;
- no coverage row names it.

After apply, the delta can no longer be written to, failed or adopted.

At any instant, therefore, the served state is exactly the pre-delta state or
exactly the post-delta state. PR-DELTA-1 tests this in three ways:

- golden comparisons after every step, including built-but-unapplied: every
  reader gives the pre-delta answer;
- a crash injected inside apply;
- activation and write attempts on a delta, which are refused.

## 9. Q7: migration and rollback

**One migration**, `20261010000100_catalog_register_delta_capture.sql` (the
timestamp is set at implementation). It is additive and forward-only, and safe
to rerun (`create … if not exists`, `create or replace`, `add column if not
exists`).

**New objects:**

- **`catalog_register_deltas`**, append-only (`forbid_catalog_register_rewrite`).
  - `delta_snapshot_id` pk, FK to snapshots RESTRICT.
  - `base_snapshot_id`, FK to snapshots RESTRICT.
  - `tozar`, `sequence`, `previous_delta_snapshot_id`.
  - `added_rows`, `retired_rows`, `served_rows`.
  - `state_sha256`.
  - `applied_by_run_id`, FK to runs RESTRICT; `applied_at`.
  - Unique on `(base_snapshot_id, sequence)`.
- **`catalog_register_delta_retirements`**, append-only.
  - `delta_snapshot_id`, FK to `catalog_register_deltas`.
  - `snapshot_id`, `upstream_record_id`, `content_sha256`.
  - Primary key `(snapshot_id, upstream_record_id)`, so a served row is retired
    at most once.
  - **No FK to raw records.** A retirement is history, like an archive record
    (`20260929000100:272-279`), and must not block today's superseded
    compaction or prune of the base. Apply validates each row instead.
- **Columns:**
  - `catalog_variant_builds.base_snapshot_id uuid null`, FK to snapshots
    RESTRICT;
  - `catalog_register_capture_units.mode text default 'whole'
    check (mode in ('whole','delta'))`, so existing rows read `whole`;
  - `catalog_register_capture_units.reserved_rows integer`.
- **Index:** `catalog_variant_builds_current_idx` is recreated with
  `where completed_at is not null and base_snapshot_id is null`.
- **Trigger:** `forbid_register_delta_activation` on `catalog_source_snapshots`.
- **New functions (6):**
  - `catalog_register_content_hashes(jsonb)`: read-only;
  - `catalog_register_served_hashes(tozar, after, limit)`;
  - `catalog_register_served_snapshots(tozar)`;
  - `catalog_register_serving_rank(snapshot_key)`;
  - `catalog_register_delta_record(snapshot_key, upstream_record_id)`;
  - `apply_register_delta(...)`: lease-guarded, security definer, service_role
    only.

**Restated (17):**

- **Build rankers:**
  - `catalog_variant_current_snapshot`;
  - the `catalog_variants_current` view;
  - `catalog_browser_manufacturers`;
  - `catalog_register_prunable_snapshots`, which also gains the keep rule;
  - `catalog_register_prunable_variant_builds`;
  - `catalog_register_unit_measure`;
  - `prune_register_snapshots`.
- **Served-set readers:** `catalog_browser_models`, `_years`, `_variants`,
  `_facets`.
- **Writers and gates:**
  - `record_catalog_variants`: the delta gate, no ledger rows for a delta, the
    serving-rank precedence and the tozar lock;
  - `compact_register_snapshot`: the delta branch, and the superseded build
    check ignores delta builds;
  - `record_register_unit_status`: a delta unit;
  - `request_register_capture`: `reserved_rows`;
  - `assert_snapshot_write_authority`: an applied delta is immutable;
  - `catalog_register_uncompacted_captures`: also counts applied deltas.

Every restated maintenance function is re-pinned with the same
`statement_timeout` / `lock_timeout` as `20261004000100`.
`apply_register_delta` gets its own pin: `statement_timeout = 300s`,
`lock_timeout = 5s`, because it recomputes `state_sha256` over up to ~13.5k
rows. `tests/test_register_rpc_timeouts.py` is extended to cover it. Grants
follow the existing patterns: read functions to the release read-only role,
writers to `service_role` only.

**Why it is safe to apply before any code uses it.** While no row has
`register_delta` metadata:

- every restated reader returns exactly what it returns today (golden tests
  over the PR-L2 fixture world);
- every restated writer behaves exactly as today for `mode = 'whole'`.

So the migration changes no behavior until the capture job writes a delta.

**Rollback**, in increasing order:

1. **Turn the writer off.** Set `MILO_ENABLE_REGISTER_DELTA_CAPTURE` off on the
   capture job (it defaults to off). The sync then behaves exactly as today
   (whole re-capture), and applied deltas keep being served consistently.
2. **Roll back the image.** A previous release's code with the new migration is
   compatible: the old capture job only takes the whole path, and the restated
   SQL still serves applied deltas.
3. **Un-serve a tozar's deltas.** Fold it: one whole re-capture through the
   existing path, subject to the capacity guard. The new full snapshot becomes
   the current build, the old base is superseded-compacted as today, and its
   deltas stop being served. Nothing is deleted, because append-only history
   stays.

There is no down-migration, following the repository convention
(`docs/production-readiness/MIGRATIONS.md`).

## 10. Q8: recommendation

### 10.1 The recommendation in three sentences

Adopt **option A**. Re-fetch a drifted tozar whole as today, and detect the
change in the database as the multiset difference of `_id`-blind content
hashes. Store only the added rows in a marked, never-activatable delta snapshot
with its own create-only archive, plus an append-only retirement list.

The delta joins the served state only in one `apply_register_delta`
transaction, after a fresh count and a whole-state hash check. Its unapplied
rows and build are invisible to every reader by database rule, so a failed
delta leaves the served state untouched.

For the current backlog this turns 54,543 rewritten rows (≈ 380k dead tuples,
≈ 355 MB of churn, refused by the guard) into about 79 (≈ 160 dead tuples,
≈ 0.23 MB). Every swarm, Prepare, batch and archive path keeps reading the same
activated snapshots it reads today.

### 10.2 PR-DELTA-1 scope

| File | Change | Estimated non-test lines |
|---|---|---|
| `supabase/migrations/20261010000100_catalog_register_delta_capture.sql` | Everything in §9: two tables, three columns, one index, one trigger, six new functions, 17 restated, timeout pins | ~900 SQL |
| `backend/catalog/register/delta.py` (new) | The multiset diff, the choice of retired rows, the manifest and identity, `state_sha256`, and the threshold rule (≤ 25% of the tozar, chain < 16, same schema fingerprint) | ~220 |
| `backend/catalog/register/capture.py` | `capture_unit` takes the delta path when the flag is on and the tozar has a served base (§8.1 steps 2–9) | ~130 |
| `backend/catalog/register/sync.py` | Reserve delta rows for drifted tozars (`expected − captured` as the estimate; the real count is re-checked), and `delta=<n>` in `SYNC_SUMMARY` | ~40 |
| `backend/catalog/register/compaction.py` | The delta branch's archive read-back (reuses `verified_archive`); the delta branch of `source_record` and `--show-record` | ~60 |
| `backend/catalog/register/service.py` | The page's bytes per row counts `whole` units only (`service.py:173-177`) | ~10 |
| `backend/catalog/register/config.py` | `MILO_ENABLE_REGISTER_DELTA_CAPTURE` (default off) and the threshold constants | ~15 |
| `backend/repository/supabase.py`, `backend/testing/register_memory.py`, `variants_memory.py` | RPC wrappers and their in-memory mirrors | ~200 |
| `docs/production-readiness/REGISTER_CAPTURE.md` | The delta step, the new codes, the operator steps | docs |
| **Total** | | **~1,575 non-test lines** |

That is large for one review. The natural split is:

- **PR-DELTA-1a:** the migration and the readers, with golden tests showing
  "no delta, or an unapplied delta, gives identical answers". No writer, so no
  behavior change.
- **PR-DELTA-1b:** the writer, behind the flag.

**Tests:**

1. **Golden.** With no delta, and with a delta at every stage short of apply
   (opened, archived, built), every restated reader is identical. Built on the
   PR-L2 fixture world (`tests/test_register_compaction_postgres.py`).
2. **The database rules.** Activating a delta is refused. Writing to, failing
   or adopting an applied delta is refused. Superseded compaction of a base
   with retirements succeeds (no FK). The timeout pins are in place.
3. **A delta end to end on PostgreSQL:** added, changed and removed rows plus
   an `_id`-only duplicate. Then:
   - the browser serves exactly the fresh multiset;
   - `REGISTER_COVERAGE` reads served rows;
   - coverage changes only for touched keys, and an older build never takes
     back a delta-named ledger row;
   - a Prepare or batch on the base is unaffected;
   - `source_record` reads a delta row from its archive.
4. **Atomicity.** Every failure in §8.2 is injected. Each leaves every reader
   on the pre-delta answer, and the retry adopts or recreates the delta.
5. **Thresholds.** Above 25%, or after a schema change, the whole path runs.
   An unchanged multiset writes nothing.
6. **Retention.** Applied deltas and their base are kept, and an unapplied
   orphan is pruned.
7. **Churn.** `tests/test_register_delta_churn_postgres.py` measures the real
   delta path, asserting ≤ 10 dead tuples per changed row.
8. **Memory-double parity:** `tests/test_register_capture.py`,
   `tests/test_register_sync.py`.

**Release order:**

1. Merge PR-DELTA-1. Then the **Deploy Supabase Migrations** workflow
   (readers are unchanged while there is no delta).
2. Deploy production.
3. Run the deployed gate. `REGISTER_COVERAGE` is unchanged.
4. Set `MILO_ENABLE_REGISTER_DELTA_CAPTURE` on the capture job.
5. On the Register page, press **Resume**. 388.5 MB is under 400 MB, so it is
   admitted (`backend/catalog/register/autosync.py:223-226`).
6. Watch the next syncs. About two syncs (98 requests) absorb the 17 tozars,
   ending at `SYNC_SUMMARY … delta=17 … coverage=101782/101782`.
7. Later, PR-DELTA-2: cleanup and prune of superseded deltas, and an
   operator-triggered fold.

### 10.3 Invariants the recommendation changes, stated plainly

1. **Snapshot identity.** A tozar's served state is no longer one snapshot
   whose identity is its page chain. It is a base plus deltas, identified by
   `state_sha256`, an `_id`-blind, order-blind hash of the served content
   multiset. A full capture of a state and base ⊕ deltas of the same state
   have different snapshot keys and the same `state_sha256`.
2. **"Readable" no longer means "activated".**
   - Today `activated_at is not null` is the only thing that makes a snapshot
     readable (`backend/repository/supabase.py:1774`), and "nothing downstream
     reads a non-active snapshot" (`ingest.py:25`).
   - With deltas, an APPLIED delta is served content (browser, coverage,
     `source_record`) although it is never activated.
   - A pending, unapplied delta remains unread, as today.
3. **"Served" is not "rank-1 active".**
   - The browser and the coverage ledger read base ⊕ applied deltas.
   - The rank-1 active snapshot (what Prepare, batches, compaction and
     retention rank) stays the base. It keeps rows the register has since
     retired, until a fold.
   - So Prepare reuse, batch preparation and the replay of a base row still
     see those rows. This matches today: a Prepare already reads its own
     capture.
4. **A build row no longer implies an activated snapshot.** A
   `catalog_variant_builds` row with `base_snapshot_id` belongs to an
   unactivated delta and carries its base's `activated_at`.
5. **An applied delta stays `pending`-shaped.** `activated_at` is NULL and
   `validation_state` is `complete`, frozen by `assert_snapshot_write_authority`
   rather than by activation.
6. **Count verification** checks served-before − retired + added against the
   fresh count, plus `state_sha256`, instead of one snapshot's stored rows.
7. **A captured unit can name a delta.**
   - Its `snapshot_id` / `snapshot_key` is the delta.
   - `stored_record_count` ≠ `captured_rows`.
   - `measured_bytes` covers the delta only, so the page's bytes per row
     counts whole units only.
8. **The variant build** admits a marked, unactivated delta, and its coverage
   write moves into the apply transaction. Ledger precedence is by serving
   rank, not by `activated_at` alone.
9. **The capacity guard** keeps its formula, but a delta unit reserves its
   changed rows, not the tozar's rows.
10. **Retention** keeps every snapshot of an applied chain, and its base, until
    PR-DELTA-2.
    - A fold still reclaims the old base's variants and rows exactly as today.
    - The base's snapshot row and the deltas' few rows stay.
