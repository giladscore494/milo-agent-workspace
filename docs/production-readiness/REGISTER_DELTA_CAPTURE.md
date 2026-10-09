# Register delta capture (PR-DELTA-0: design and decision)

**Status:** design only. This PR changes no runtime code and adds no migration.
PR-DELTA-1 implements the recommendation below, but only after the owner
approves this document.

**Measured:** 9.10.2026. Production was read with SELECT only. The churn figures
come from a local ephemeral PostgreSQL 16 (CI runs the same test). Nothing was
written anywhere in production.

Citations use the form `file:line`. Migrations live in `supabase/migrations/`
and are cited by their timestamp prefix.

## 0. The problem in one paragraph

Every tozar has been captured at least once, but 17 tozars drifted after their
capture: 79 net rows are missing. The sync (PR-SYNC-1, `backend/catalog/register/sync.py:96-108`
`plan`) can only re-capture a drifted tozar WHOLE. That means 54,543 rows
re-written to add 79. The capacity guard prices this as 388.5 MB + 54,543 ×
3,000 B = 552.1 MB, which is above its 400 MB limit, so auto sync paused
itself (`SYNC_PAUSED_CAPACITY`, 8.10 01:14 IDT, `stop=capacity`). VACUUM FULL
cannot make room either: `register-vacuum.sh` needs DB + table × 1.1 ≤ 450 MB
(`scripts/ops/register-vacuum.sh:43`), and even the smallest table fails that
(388.5 + 64.0 × 1.1 = 458.9 MB). Any one-row change upstream in a large tozar
forces a whole re-capture, so a 500 MB database cannot maintain itself on the
current path. This is structural.

## 1. Read-only measurements

### 1.1 The 17 drifted tozars (production, SELECT only)

Directory version `9d0df774`: 101,782 rows. The active snapshots cover 101,703
rows (99.92%).

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

- **Measured bytes** is `catalog_register_snapshot_compactions.bytes_after`, i.e.
  `catalog_register_measured_bytes`: heap only, raw + candidates + variants +
  two ledger levels.
- Every Δ is net growth. Hash detection (§3) can only tell how many of those
  rows are additions and how many are changes once the fresh rows are fetched.

**Change detection is feasible from data already stored, for every tozar, not
only these 17.** Across all 138 active rank-1 snapshots:

- stored rows = variant rows = 101,703;
- 101,703 distinct `(snapshot, content_sha256)` pairs, so no content repeats
  inside any snapshot today;
- 0 null hashes;
- 0 snapshots without a complete current-mapper hash set;
- 0 uncompacted active snapshots.

Every snapshot is compacted, so the hash lives on the variant row, not on the
payload (`catalog_variants.content_sha256`, `20260930000100:145`). Readers
reach it through `catalog_raw_record_content_sha256` (`20261002000100:200-212`).

The brief counts 137 tozars. The current directory version has **138** units,
and all 138 have an active snapshot. None of the 138 has 0 rows.

### 1.2 Database state (production, SELECT only)

| | |
|---|---|
| `pg_database_size` | 388,492,435 B (388.5 MB) |
| autovacuum | on; `autovacuum_vacuum_scale_factor` 0.2, threshold 50, naptime 60 s; no per-table reloptions; PostgreSQL 17.6 |

| Table | Heap | Live tuple bytes (`sum(pg_column_size(t.*))`) | Heap − live (reusable free and dead space) | Index + TOAST | Lifetime ins / upd / del | Dead now |
|---|---|---|---|---|---|---|
| `catalog_raw_records` | 53.2 MB | 34.6 MB (340 B/row) | 18.6 MB | 34.0 MB | 134,122 / 115,028 / 32,385 | 14,271 |
| `catalog_candidate_variants` | 18.1 MB | 14.6 MB (144 B/row) | 3.5 MB | 45.9 MB | 131,307 / 115,002 / 29,596 | 14,280 |
| `catalog_variants` | 71.3 MB | 64.1 MB (630 B/row) | 7.2 MB | 37.7 MB | 115,006 / 0 / 13,303 | 6,924 |
| `catalog_variant_coverage` | 62.9 MB | 59.4 MB (292 B/row) | 3.5 MB | 40.2 MB | 203,380 / 26,613 / 0 | 13,846 |

The update counts on raw records and candidates (~115k each) are the PR-L2
compaction: one UPDATE per row, which leaves one dead tuple per row
(`20261002000100:643-650`). The heaps hold about **32.8 MB** of reusable space.
Index free space cannot be measured without `pgstattuple`, which would be an
extension install, i.e. a write.

### 1.3 MVCC churn: one whole re-capture vs. one delta (local PostgreSQL 16)

`tests/test_register_delta_churn_postgres.py` follows the PR-L2 pattern
(`tests/test_register_compaction_postgres.py:814-885`):

- every migration is applied;
- rows are written by `_bulk_snapshot`;
- the build goes through `record_catalog_variants`, compaction through
  `compact_register_snapshot`;
- autovacuum is off, so the dead-tuple counts are exact.

The test tozar has 1,000 rows and drifts by 8 added rows, 1 changed row and 1
removed row, so the fresh count is 1,007.

Output (`pytest -q -rs tests/test_register_delta_churn_postgres.py`, 49 s):

```
PR-DELTA-0 churn of one drifted 1,000-row tozar (+8 added, 1 changed, -1 removed); per table: inserted/updated/deleted/dead tuples
  whole re-capture (today): tuples written 7,049, dead 7,012, WAL 12,813,368 B, file growth after plain VACUUM 6,504,448 B, live growth 16,384 B [raw_records 1007/1007/1000/2007; candidate_variants 1007/1007/1000/2007; variants 1007/0/1000/1000; variant_coverage 16/1998/0/1998]
  delta (option A): tuples written 65, dead 20, WAL 612,320 B, file growth after plain VACUUM 262,144 B, live growth 40,960 B [raw_records 9/9/0/9; candidate_variants 9/9/0/9; variants 9/0/0/0; variant_coverage 16/2/0/2; delta_retirements_probe 2/0/0/0]
  option C, 4 whole re-captures of a 1,000-row tozar with a plain VACUUM after each, file bytes (first and last after VACUUM FULL): 9,248,768 -> 16,056,320 -> 17,809,408 -> 18,161,664 -> 18,219,008 -> 9,248,768
```

**What one whole re-capture costs, per row of the tozar:**

| Table | What happens to each row | Tuples | Dead tuples |
|---|---|---|---|
| raw records | new row inserted; compaction UPDATE; old row DELETEd by superseded compaction (`20261002000100:628-650`) | 3 | 2 |
| candidates | same as raw records | 3 | 2 |
| variants | new row inserted; old row deleted | 2 | 1 |
| coverage | 2 UPDATEs (one per level), because `snapshot_key` changed; "the same rank refreshes its facts" (`20260930000100:440-454`) | 2 | 2 |

Totals:

- 7.0 tuples written per row, and 7.0 dead tuples;
- **6.5 KB of space left to reclaim per row** (file growth after a plain VACUUM);
- 12.7 KB of WAL per row.

Live growth is only 16 KB, which is the 7 net new rows.

**What the delta costs:** 65 tuples written and 20 dead, for 9 changed rows
and 2 retirements:

- 2 dead tuples per changed row come from compacting the delta's own raw and
  candidate rows;
- 2 more come from the coverage row of the changed key.

The file growth (256 KB) and the live growth (40 KB) are page granularity: a
handful of new 8 KB pages across five tables and their indexes.

**What repeated whole re-captures do to the file (option C):** the file
settles once autovacuum reuses the space. Over four cycles, the growth per
cycle was +6.8, +1.8, +0.35 and +0.06 MB. The plateau is **+8.97 MB per 1,000
rows re-captured inside one vacuum window, i.e. ~9.0 KB per row**. That is the
transient of one capture: new rows uncompacted (5,256 B) next to the old
compacted rows (2,931 B), plus index pages.

The test also checks Q1 (§3):

- the multiset difference between the stored hashes and the fresh rows'
  hashes is exactly 9 added and 2 retired;
- renumbering every `_id` gives the same answer;
- a row duplicated with only `_id` different is counted twice.

## 2. Facts in the code that this design relies on (verified)

| Fact | Where |
|---|---|
| `catalog_variants.content_sha256` is the record's content with `_id` removed: `sha256((payload - '_id')::text)`. It is immutable and storage-local. | `20260927000100:163-170`; build `20260930000100:371`; the "storage-local" rule `backend/catalog/digest.py:1-27` |
| `catalog_raw_records.payload_sha256` covers the full payload, `_id` included. | `20260914200000:140`; archive check `catalog_raw_record_payload_matches` `20261002000100:745-753` |
| Snapshot identity = content. `snapshot_content_sha256` hashes the query, page size, reported total, schema fingerprint and every page body's checksum (so `_id` and row order are included). An identical capture is the same snapshot. | `backend/catalog/government/snapshot.py:17-25, 86-111`; `catalog_source_snapshots_key_uidx` `20260914200000:116`; replay `backend/catalog/government/ingest.py:41-66` |
| An active snapshot is frozen, and activation is gated on stored = declared. | `20260914200000:109-112, 446-470`; `activate_catalog_snapshot_guarded` `20260924000200:330-387` |
| "Active" means newest `activated_at`. No constraint enforces one active snapshot per tozar; every rank-1 reader orders by `activated_at desc, id`. | `catalog_variant_current_snapshot` `20261001000100:472-482`; `compact_register_snapshot` `20261002000100:475-483`; retention's `ranked` CTE `20261001000100:641-702` |
| After its build, the rank-1 snapshot is compacted: payload NULL, candidate keys only. Superseded snapshots keep only referenced rows, as skeletons. | `20261002000100:410-668`; `catalog_candidate_referenced` `:673-686` |
| Count verification compares stored rows with a FRESH `count_tozar` taken after every row is written. | `backend/catalog/register/capture.py:188-193, 222-232`; `record_register_unit_status` `20260929000100:575-582` |
| Archives are create-only, one per snapshot, with a recorded line count and sha256; line = `source_locator.capture_index + 1`. | `20260929000100:272-310`; `backend/catalog/register/capture.py:143-185`; `backend/catalog/register/archive.py` |
| `catalog_variant_coverage` is keyed by `(variant_identity_key, level)`, not by snapshot, and a newer-activated snapshot's row is never taken back by an older one. | `20260927000100:215-250`; `20260930000100:427-457` |
| Capacity guard: `projected = pg_database_size + (new + in-flight rows) × bytes_per_row`. | `20260929000100:428-443`; price `backend/catalog/register/service.py:252-259`, `config.py:40,54` |
| A unit costs `1 + ceil(rows/1000) + 1` requests, plus 1 retry headroom, within an 80-request sync. | `backend/catalog/register/sync.py:53-57, 91-93` |
| The variant FK is `(snapshot_id, upstream_record_id)` → raw records, RESTRICT; the build FK is `(snapshot_id, mapper_version)`; both are unique per snapshot; variants are append-only. | `20260930000100:219-246`; `catalog_raw_records_snapshot_upstream_uidx` `20260914200000:162` |
| A snapshot with NO `capture_scope` is read as the whole register's snapshot by refresh and Prepare. | `backend/catalog/government/refresh.py:228-236, 311-321`; `capture_scope.declared_scope` `:157-182` |

The last row matters for §4: a delta snapshot must never look like a
whole-register snapshot, and must never look like a whole-tozar active
snapshot either.

## 3. Q1: how to detect the change

**Rule:** compare the multiset of content hashes, never `_id`.

- `S` = the multiset of `content_sha256` over the tozar's SERVED rows. Today
  that is the rank-1 snapshot's variant rows; with option A it is base ⊕ deltas.
- `F` = the multiset of `catalog_variant_content_sha256(row)` over a fresh,
  complete fetch of the tozar.
- **added** = F − S (each hash with its surplus count)
- **retired** = S − F
- **kept** = min(F, S) per hash

Duplicates count, because two upstream rows can differ only in `_id`.

- **A changed row** is one retirement plus one addition (§7).
- **A renumbered `_id`** changes nothing, since `_id` is outside the hash.
- **A changed field schema** changes every hash. That is detected as "everything
  changed" and falls back to a whole re-capture (§10.2).

**Where the hash is computed: in the database, not in Python.** The content
hash is storage-local: it is taken over PostgreSQL's `jsonb::text` rendering,
which Python must not try to reproduce (`backend/catalog/digest.py:1-27`).
PR-DELTA-1 therefore adds one read-only RPC that takes one page of fresh rows
(≤ 1,000 payloads, ~1.5 MB) and returns their content hashes in order (it
writes nothing). It also adds one read that returns the served hashes of a
tozar with each row's `(snapshot_id, upstream_record_id)`, paged. The diff
itself is pure Python over those two lists. The measurement test does the same
in one SQL statement (`_diff` in `tests/test_register_delta_churn_postgres.py`).

**Which served row a retirement names.** When a hash is retired `k` times, the
`k` served rows with that hash are chosen by `(snapshot activated/applied
order, upstream_record_id collate "C")`. This is deterministic, so a replay
retires the same rows.

**Requests.** The fetch is unchanged: the same `capture_resource` of the exact
tozar, `1 + ceil(rows/1000) + 1` requests. Only storage and the build become
incremental.

| Case | Requests |
|---|---|
| מרצדס, 13,475 rows | 1 + 14 + 1 = **16**, plus 1 retry headroom = 17 of the 80-request sync |
| All 17 drifted tozars (Σ `ceil(n/1000)` = 64) | 64 + 2 × 17 = **98** |
| Check + light directory | ~13 |

So the current backlog needs **two syncs**, the same number of requests as
today's whole re-capture. Detection adds 0 data.gov.il requests. It adds about
`ceil(rows/1000)` database round trips for the hashes, plus one paged read of
the served hashes.

## 4. Q2: how to store the change

### Option A: delta snapshot (the base ⊕ deltas)

**What a delta is.** A delta is a `catalog_source_snapshots` row that:

- holds ONLY the added rows as raw records, candidates and (after its build)
  variants;
- has its own create-only archive of exactly those lines;
- is accompanied by an append-only retirement list naming served rows by
  `(snapshot_id, upstream_record_id, content_sha256)`;
- is registered in an append-only table of applied deltas.

The tozar's served state is:

> the base snapshot's variants (the tozar's current complete build,
> `catalog_variant_current_snapshot`, unchanged)
> ∪ the variants of every APPLIED delta of that base
> − every row retired by an applied delta.

**Three properties keep it apart from every existing reader.**

1. **It is never activated.** `activated_at` stays NULL. Every existing reader
   only sees `activated_at is not null`:
   - rank-1 resolution, compaction, retention rank, the superseded list;
   - refresh/Prepare (`resolve_active_snapshot`);
   - the batch preparation gate.

   So none of them can mistake a 9-row delta for the tozar. The base stays the
   tozar's active, rank-1, compacted snapshot.
2. **It declares the tozar's `capture_scope`.** It is a valid scoped snapshot,
   so no reader can mistake it for the whole register
   (`refresh.py:228-236`). It also passes the `catalog_capture_scope_consistent`
   CHECK (the query is the tozar's query, which is what was fetched).
3. **"Served" is a separate, single-row fact.** The delta becomes part of the
   tozar's state only when `apply_register_delta` inserts its application row.
   That happens after the count, archive and build checks, and in ONE
   transaction with its retirements and its coverage rows (§8).

**Delta identity.**

- `content_sha256` = sha256 of the canonical manifest:
  - `contract = gov.register.delta.1`;
  - the base `snapshot_key`;
  - the previous delta's key (the chain);
  - the sorted added rows' `payload_sha256`;
  - the sorted retired `(snapshot_key, upstream_record_id)`.
- `snapshot_key` is derived from it as today (`backend/catalog/payloads.py`).
- A re-run over the same state therefore lands on the SAME delta snapshot, and
  adopts it under the existing orphan rule (`20260924000200`).

The delta also records `state_sha256`: the sha256 of the sorted multiset of
served content hashes AFTER the delta. This is the `_id`-blind identity of the
tozar's served state. `apply_register_delta` recomputes it from the database
and refuses on a mismatch, so the applied state is provably the fetched state.

**Fold-back.** A whole re-capture through today's path IS the fold:

- the new full snapshot is activated, built and compacted;
- it becomes `catalog_variant_current_snapshot`;
- the old base's deltas then serve nothing, because they belong to a base that
  is no longer current.

The sync chooses the whole path when the delta would exceed 25% of the tozar,
when the chain reaches 16 deltas, or when the schema fingerprint changed
(§10.2). It always does so under the existing capacity guard. Folding is never
needed for correctness, so a large tozar can keep its deltas until the
database has room.

### Option B: carry-forward snapshot (a full new snapshot that reuses unchanged rows)

The new snapshot `S2` carries the full fetched content and its true
`snapshot_content_sha256`. Its unchanged rows would reuse `S1`'s raw,
candidate and variant rows instead of inserting new ones.

**This can only be done by moving rows**, which is what the keys allow:

- every row is owned by exactly one snapshot:
  `catalog_raw_records (snapshot_id, upstream_record_id)` is unique
  (`20260914200000:162`);
- `catalog_variants.snapshot_id` → snapshots RESTRICT;
- `(snapshot_id, upstream_record_id)` → raw records RESTRICT
  (`20260930000100:219-222`);
- the build FK `(snapshot_id, mapper_version)` → `catalog_variant_builds`.

So each unchanged row needs:

- `UPDATE catalog_raw_records SET snapshot_id = S2, record_key, source_locator`
  (plus `upstream_record_id` if `_id` moved);
- `UPDATE catalog_candidate_variants SET snapshot_id = S2`;
- `UPDATE catalog_variants SET snapshot_id = S2, snapshot_key, archive_line`.

All three must run in ONE statement (writable CTEs) or behind FKs made
`DEFERRABLE`, because the composite FK is `NO ACTION` on update and is checked
at the end of each statement. This only runs in a security-definer function
that suspends three append-only/immutability triggers:

- `catalog_raw_records_append_only` (`20260914200000:432`);
- `catalog_candidate_variants_identity_immutable` (`:511`);
- `catalog_variants_append_only` (`20260930000100:244`).

**What breaks:**

- **Provenance.** A candidate referenced by a queue item, evidence link, field
  provenance, promotion or reservation must NOT move, because its provenance
  is `S1`. Those rows must be copied, which is a re-insert after all.
- **S1's own integrity.** `S1` loses rows. Its `stored_record_count` no longer
  equals its archive's `line_count` (`20261002000100:604-610`), yet it is
  frozen as an active snapshot (`20260914200000:452-453`).

**MVCC churn that remains:** every UPDATE writes a complete new tuple. Because
`snapshot_id` is in an index of all three tables, none is HOT, so every index
also gets a new entry. Per unchanged row:

- 3 new tuples and 3 dead tuples (raw, candidate, variant);
- 2 more if the coverage rows' `snapshot_key` is refreshed, as the ledger rule
  does today.

The bytes left to reclaim, from production widths (§1.2), are:

- heap: 340 + 144 + 630 = 1,114 B;
- index entries: ~1,156 B;
- total **≈ 2.3 KB per unchanged row**, or ≈ 3.3 KB with coverage.

Today it is 6.5 KB. B saves the payload transient and the compaction rewrite,
**but it is still O(tozar) per drift**.

**B2: a membership table.** `S2` lists the variant ids it shares with `S1`:
about 150–200 B per row per snapshot, with no dead tuples. But every reader
must read through the membership. That is A's reader change plus an O(tozar)
write per drift.

### Option C: measure live data, not file size; keep whole re-capture

Keep the current write path. Change only the capacity guard (and autosync rule
6, `backend/catalog/register/autosync.py:121-124`, and `REGISTER_COVERAGE`'s FAIL) to measure live
bytes (`sum(pg_column_size)` plus index estimates) instead of
`pg_database_size`, and let plain autovacuum make dead space reusable.

**Measured steady state** (§1.3): the file plateaus at **live + ~9.0 KB per
row re-captured inside one vacuum window**. After that it does not grow. The
window is set by autovacuum: a table is vacuumed after `50 + 0.2 × reltuples`
dead tuples (≈ 20,400 for raw records). Raw records gain 2 dead tuples per
re-captured row, so autovacuum starts roughly every 10,000 re-captured rows,
and only once it has run is the space reusable.

| Case | High-water mark |
|---|---|
| מרצדס alone in one window | live + 13,475 × 9.0 KB = **live + 121 MB** |
| A realistic window of 10,000–20,000 rows | **live + 90–180 MB** |

Starting from today's 388.5 MB, with only 32.8 MB of measured reusable heap
space, re-capturing מרצדס takes `pg_database_size` to about
388.5 − 32.8 + 121 ≈ **477 MB**, or more if the window holds more than one
tozar. The plan limit counts size on disk, dead space included
(`REGISTER_CAPTURE.md:64-65`), so **C moves the guard off the number the plan
enforces** while the file approaches 500 MB.

C stays an option only with per-table autovacuum tuning (scale factor ~0.01,
which is a migration of reloptions). Even then the floor is
**live + one largest tozar's 121 MB**.

### Other options considered

| Option | What it is | Verdict |
|---|---|---|
| D. Amend the active snapshot in place | Insert added rows into `S1`, delete retired rows | Refused. An active snapshot is frozen (`20260914200000:452-453`). Its identity, declared count, archive line count and compaction preconditions would all describe content it no longer holds. |
| E. Detection only, skip unchanged tozars | Use the `_id`-blind hash to skip a re-capture whose content is unchanged, even when `_id` or order moved (today only an identical page chain reuses, `ingest.py:41-48`) | Useful and cheap, but the 17 drifted tozars DID change. It is folded into A as the zero-delta case: a tozar whose multiset is unchanged writes nothing and records the unit as captured. |
| F. A larger plan (paid) | | Not an engineering change. It is the owner's decision, and it postpones the problem rather than removing it. |
| G. Scheduled VACUUM FULL | | Already blocked by the 450 MB headroom rule (§0), and needs ACCESS EXCLUSIVE. |

## 5. Q3: invariants, option by option

Key: **K** = kept unchanged, **C** = changed (how), **B** = broken.

| Invariant | A (delta) | B (carry-forward, move) | C (live measure) |
|---|---|---|---|
| Snapshot identity = content (`snapshot.py:86-111`) | **C.** Full snapshots keep `snapshot_content_sha256`. A delta has its own content identity (its manifest). The tozar's served state is identified by `state_sha256`, which is `_id`-blind and order-blind, NOT by any page-chain hash. A full capture of the same state therefore has a different snapshot identity than base ⊕ deltas. Its `state_sha256` is the same, and is checked. | **B.** `S2` keeps its true page-chain identity, but `S1` stops holding the rows its identity describes. | **K** |
| One active snapshot per tozar (rank-1 by `activated_at`) | **K.** A delta is never activated; the base stays rank-1. What is SERVED becomes base ⊕ applied deltas, read through one function (§8.3). | **K** (S2 becomes rank-1) | **K** |
| Count verification against a fresh count (`capture.py:188-193`) | **C.** The same fresh `count_tozar` after every write, compared with served-before − retired + added. `apply_register_delta` also checks `state_sha256` (stronger than a count). `record_register_unit_status` accepts a delta unit: snapshot = the delta, `captured_rows` = served rows. | **K** | **K** |
| Create-only archive and line numbers (`20260929000100:272-310`) | **K.** One create-only object per delta with exactly its added lines; line = `capture_index + 1`, where `capture_index` is the row's index inside the delta. The base's archive is untouched, so retired rows stay citable there. | **B.** Moved rows need new lines in `S2`'s archive, and `S1`'s `line_count` no longer equals its rows. | **K** |
| Compaction preconditions (`20261002000100:410-668`) | **C.** The base is untouched. A delta is compacted by a delta branch with the same checks over its own rows: archive verified by read-back, build complete, typed rows read as payload, nothing live reading it. Superseded handling: when the base is superseded, its deltas serve nothing and keep their rows until PR-DELTA-2 cleans them up (tiny). | **B.** It moves compacted rows and needs a fourth trigger suspension. | **K** |
| `source_record` replay (`compaction.py:236-266`, `scripts/export_replay_capture.py:97-145`) | **K.** A cited row is `(snapshot_key, upstream_record_id)`. For a base row, from the base archive; for a delta row, from the delta's archive by its `capture_index`. Both are sha256-checked, and run checkpoints keep pinning the base or a Prepare snapshot by key. | **B.** A moved row's `snapshot_key` and line change under an existing citation. | **K** |
| Variant build and mapper version (`20260930000100:265-460`) | **C.** Delta rows are built under the current mapper by the same mapping. The build gate admits a registered, count-verified, not-yet-applied delta whose base is the tozar's current complete build under that mapper. The coverage ledger write moves from the build into the apply transaction. The mapper bump stays refused (`CATALOG_COMPACTION_MAPPER`); a future rebuild-from-archive must rebuild deltas from their own archives. | **C.** Moved variants keep their mapper rows. | **K** |
| Coverage rows (`20260927000100:215-250`) | **C.** Rows are written for the keys a delta touches only, in the apply transaction, with the delta's `snapshot_key`. The collision rule is evaluated over the SERVED rows, not over one snapshot (`20260930000100:417-426` scopes it to one snapshot). Unchanged keys are not rewritten: 2 updates per changed key instead of 2 per row of the tozar. A removed row's ledger row stays, as today. | **C/K.** It saves nothing unless the refresh rule changes. | **K** (2 updates per row stay) |
| Catalog browser reads (`20261001000100:472-640`, `20260930000100:494-533`) | **C.** `catalog_variants_current`, `catalog_browser_{manufacturers,models,years,variants,facets}` read the served set: `snapshot_id = any(base, applied deltas)` minus retired. The tree index `(snapshot_id, mapper_version, kinuy_mishari, shnat_yitzur)` serves `= any(...)`. The unfiltered manufacturer count becomes served rows, not `built_rows`. With no delta, every answer is identical (a golden test). | **K** | **K** |
| Deployed gate `REGISTER_COVERAGE` (`scripts/ops/gates.sh:51-89`, `20260929000100:792-821`) | **K** (SQL unchanged). The newest captured unit's `captured_rows` is the served count. `unverified_snapshots` is unchanged. | **K** | **C.** FAIL on live bytes, not `pg_database_size`. |
| Retention and prune (`20261001000100:641-702`) | **C.** A keep rule: every snapshot named by an applied delta (base or delta) is kept. The RESTRICT FKs would refuse a prune anyway. PR-DELTA-1 never prunes a delta, so a pending, never-applied delta whose writer ended is not kept; it is prunable like any orphan pending scoped snapshot. | **C** | **K** |
| Batch runs that pin a snapshot (`20261002000100:1717-1745, 1747-2093`; `government/preparation.py:865-965`) | **K.** Batches pin an ACTIVATED snapshot through `prepare_work_scope_queue`, which never accepts a never-activated delta. Prepare captures its own snapshot (`refresh.py:281-285`). The swarm's Government reads are unchanged, so MILO swarm behavior is preserved. | **B.** A batch pinned on `S1` loses rows mid-plan, unless the batch's snapshot is excluded from the move. | **K** |
| Capacity guard (`20260929000100:428-443`) | **C.** The same formula, but a delta unit reserves its expected delta rows, not the tozar. It is re-checked with the real delta row count BEFORE the first write. | **C** | **C.** It measures live bytes and no longer matches what the plan counts. |

## 6. Q4: database cost

**Per-row costs used below.**

| Path | Source | Tuples written | Dead tuples | Bytes |
|---|---|---|---|---|
| Whole re-capture (today and C) | measured, §1.3 | 7.0 per tozar row | 7.0 per tozar row | 6.5 KB left to reclaim per row (file growth after a plain VACUUM); WAL 12.7 KB/row; high-water +9.0 KB per row of one vacuum window |
| A | measured | 7.2 per changed row (compaction and coverage included); 1 per retirement | 2 per changed row, +2 per changed identity key | live 2,931 B per added row (PR-L2 `test_register_compaction_postgres.py:814-885`); ~0.2 KB per retirement row; ~2.3 KB per added row left to reclaim (5,256 − 2,931) |
| B | NOT measured; estimated from production widths | | | 2.3 KB (3.3 KB with coverage) left to reclaim per unchanged row, on top of A's cost for the changed rows |

**(a) Absorbing the current 17-tozar drift.** That is 54,543 directory rows,
at least 79 rows added. Each changed row would add one addition and one
retirement.

| Option | Tuples written | Dead tuples left | Bytes written / left to reclaim | Capacity guard at 388.5 MB |
|---|---|---|---|---|
| Today (whole) | ≈ 381,800 | ≈ 379,800 | ≈ 352 MB to reclaim; ≈ 694 MB WAL | 388.5 + 54,543 × 3,000 B = **552.1 MB > 400: refused** (what happened) |
| A (delta) | ≈ 570 (+1 per retirement) | ≈ 160 | live +0.23 MB; ≈ 0.18 MB to reclaim | 388.5 + 79 × 3,000 B = **388.7 MB: admitted** |
| B (carry-forward) | ≈ 163,000 (≈ 272,000 with coverage) + A's | the same | ≈ 124 MB (≈ 177 MB with coverage) to reclaim | an O(tozar) reservation; refused at today's pricing |
| C (live measure) | ≈ 381,800 | ≈ 379,800 | ≈ 352 MB churn; the file rises toward 388.5 − 32.8 + 121 ≈ **477 MB** or more | admitted by a live-bytes guard, while the file nears the 500 MB plan |

**(b) Steady state: about 100 drifted rows per week across 10–20 large
tozars.** In production the 10 largest tozars hold 56,053 rows and the 20
largest hold 77,791.

| Option | Rows rewritten per week | Tuples written / dead per week | Growth |
|---|---|---|---|
| Today (whole) | 56,053–77,791 | ≈ 392k–545k / ≈ 390k–542k | 362–502 MB per week to reclaim; refused by the guard, so the sync pauses (as now) |
| A (delta) | ~100 | ≈ 720 / ≈ 200 | **+0.3 MB per week live (≈ 16 MB per year)**; ≈ 0.23 MB per week reclaimed and reused. Retired rows stay in their base until a fold. |
| B (carry-forward) | 56,053–77,791 | ≈ 168k–389k / the same | 128–253 MB per week to reclaim; high-water + 2.3–3.3 KB × the rows of one vacuum window (מרצדס ≈ 31–44 MB) |
| C (live measure) | 56,053–77,791 | as today | no net growth once at the plateau, but the plateau is **live + 9.0 KB × the rows of one vacuum window**: ≥ +121 MB for מרצדס, ≈ +90–180 MB per default autovacuum window. That is above the 400 MB threshold and near the 500 MB plan. |

## 7. Q5: removals and changes upstream

| Upstream event | Hash detection sees | Option A writes |
|---|---|---|
| Row added | +1 hash | 1 delta row (raw, candidate, variant; compacted) |
| Row removed | −1 hash | 1 retirement row naming the served row; the base row stays as history (its archive line too) |
| Row changed (any field but `_id`) | −1 old hash, +1 new hash | 1 retirement + 1 delta row. If the identity key is the same, its 2 coverage rows are updated in the apply transaction ("a changed content replaces"). If the key moved, the new key's rows are inserted and the old key's rows stay, as today. |
| `_id` renumbered, content same | nothing | nothing. The served row keeps its old `upstream_record_id`, which stays the correct citation into its archive. |
| Two rows differing only in `_id` | one hash with count 2 | multiplicity is kept: the count decides how many are added or retired |
| A row changes and changes back | −1/+1, then −1/+1 | two deltas. `state_sha256` returns to the earlier value; a fold collapses them. |
| Every row changes (schema or format change) | −N/+N | above the 25% threshold, so the whole path (fold) is used under the existing guard |

The coverage collision rule (`CATALOG_COVERAGE_KEY_COLLISION`) is evaluated
over the served set after the delta, so a changed row's old and new content
never collide with each other (the old one is retired). Two served rows
sharing a key with different content still collide, exactly as within one
snapshot today.

## 8. Q6: safety

### 8.1 The order (PR-DELTA-1's capture job)

1. **Fetch the whole tozar.** This is the existing `capture_resource`, with
   the same completeness gates, and nothing is written
   (`ingest.py:5-11`, "capture first, and completely").
   A 403 or 429 here ends the sync (`GOV_SYNC_THROTTLED`, `sync.py:80-88`, `:215-216`).
   **Nothing is written.**
2. **Hash the fetched rows in the database (read-only RPC), read the served
   hashes, and compute added/retired.**
   - If both are empty, record the unit `captured` (served rows = fresh count)
     with no new snapshot. That is option E.
   - If the delta exceeds 25% or the schema moved, use today's whole path.
3. **Capacity re-check** with the real delta rows (`delta rows ×
   bytes_per_row`). A refusal is `stop=capacity` and **nothing is written**.
4. **Open the delta snapshot** (pending, never activated) and write its rows
   and candidates under the lease (the ingestor's batch writes), then the
   retirement manifest's digest in its metadata.
5. **Archive the added lines.** Create-only, recorded, and read back.
6. **Build the delta's variants** under the current mapper. No coverage is
   written yet.
7. **Fresh `count_tozar`** (an independent count after every write).
8. **`apply_register_delta`**: ONE transaction under the lease, which:
   - takes the tozar's row lock (an advisory lock on the tozar), then checks:
     - the base is still the tozar's current complete build;
     - the chain head is the one the diff was computed against (optimistic
       concurrency);
     - the delta's rows = stored = archive `line_count` = built variants;
     - served-before − retired + added = the fresh count (`api_total`);
     - recomputed `state_sha256` = the job's;
   - then inserts the application row and the retirements, writes the coverage
     rows for the touched keys, and records the unit `captured`.
9. **Compact the delta** (delta branch, archive read-back). A failure here is
   reported and the unit stays captured, as today (`capture.py:250-255`).

### 8.2 Failures halfway

| Failure | Where | Result |
|---|---|---|
| 403 / 429 / network | step 1 | nothing written; the unit is retryable |
| 403 / 429 on the fresh count | step 7 | the delta is pending, never applied; the unit is `failed` (retryable); the served state is unchanged |
| Lease lost / cancelled | steps 4–8 | every write is lease-guarded (`assert_worker_lease`), so a stale writer writes nothing more. The pending delta is invisible: never activated, not applied. The next sync recomputes the same diff from the unchanged served state, derives the SAME delta `snapshot_key`, and adopts the orphan once the lease has lapsed (the existing adoption rule, `20260924000200`) or recreates it. A delta never applied is prunable. |
| Capacity | step 3 | `stop=capacity`, nothing written |
| Base superseded meanwhile (another capture folded the tozar) | step 8 | `apply_register_delta` refuses (base / chain head moved); the delta is never served; the next sync diffs against the new base |
| Crash inside step 8 | | the transaction rolls back: no application row, no retirement, no coverage row |
| Compaction fails | step 9 | served correctly, uncompacted (as today; the next claim is priced uncompacted by `catalog_register_uncompacted_captures`, extended to applied deltas) |

### 8.3 Why a failed delta never leaves the served state partially updated

The served state of a tozar is a pure function of three things:

- **(i)** the base, i.e. `catalog_variant_current_snapshot`, unchanged;
- **(ii)** the set of APPLIED deltas of that base;
- **(iii)** the retirements OF APPLIED deltas.

The served-set function joins retirements and delta rows only through the
application rows, so rows and retirements of an unapplied delta have no
effect.

(ii) and (iii), and the coverage rows that describe the served content, are
all written by `apply_register_delta`, in one transaction, after every check.
A delta's rows can exist in the database while the delta is unapplied, but no
reader reaches them:

- they are never activated, so the rank-1, Prepare, batch and compaction
  readers do not see them;
- their snapshot is not in the browser's served set;
- no coverage row names them yet.

So at any instant the served state is either exactly the state before the
delta or exactly the state after it. Two tests in PR-DELTA-1 assert this:

- a reader running between every step reads the pre-delta answer, by golden
  comparison;
- a crash injected inside the apply transaction leaves every reader
  unchanged.

## 9. Q7: migration and rollback

**One migration:** `20261010000100_catalog_register_delta_capture.sql`
(timestamp at implementation). It is additive and forward-only, and rerun-safe
(`create … if not exists`, `create or replace`).

**New tables:**

- `catalog_register_deltas`, append-only (`forbid_catalog_register_rewrite`).
  Columns:
  - `delta_snapshot_id` pk → snapshots RESTRICT;
  - `base_snapshot_id` → snapshots RESTRICT;
  - `tozar`, `sequence`;
  - `previous_delta_snapshot_id`;
  - `added_rows`, `retired_rows`, `served_rows`;
  - `state_sha256`, `applied_by_run_id` → runs RESTRICT, `applied_at`.
  - Unique `(base_snapshot_id, sequence)`.
- `catalog_register_delta_retirements`, append-only. Columns:
  - `delta_snapshot_id` → `catalog_register_deltas`;
  - `snapshot_id`, `upstream_record_id` → raw records `(snapshot_id, upstream_record_id)` RESTRICT;
  - `content_sha256`.
  - Primary key `(snapshot_id, upstream_record_id)`, so a served row is
    retired at most once.
- `catalog_register_capture_units.mode text default 'whole'
  check (mode in ('whole','delta'))` and `reserved_rows integer`. Existing rows
  read `whole`.

**New functions:**

- `catalog_register_content_hashes(jsonb)`: read-only, immutable.
- `catalog_register_served_hashes(tozar, after, limit)`: a paged read.
- `catalog_register_served_snapshots(tozar)`: base + applied deltas.
- `apply_register_delta(...)`: lease-guarded, security definer, service_role
  only.

**Restated functions:**

- `record_catalog_variants`: the gate admits a registered unapplied delta; the
  ledger write is skipped for a delta.
- `compact_register_snapshot`: a delta branch.
- `record_register_unit_status`: a delta unit is verified against served rows.
- `request_register_capture`: reserves `reserved_rows`.
- `catalog_variants_current` and the five `catalog_browser_*`: the served set.
- `catalog_register_prunable_snapshots`: the keep rule.
- `catalog_register_uncompacted_captures`: also counts applied deltas not yet compacted.

Grants follow the existing patterns: read functions to the release read-only
role; writers to `service_role` only.

**Why it is safe to apply before any code uses it.** With no row in
`catalog_register_deltas`:

- every restated reader returns exactly what it returns today (a golden test
  over the PR-L2 fixture world);
- every restated writer behaves exactly as today for `mode = 'whole'`.

The migration therefore changes no behavior until the capture job writes a
delta.

**Rollback**, in increasing order:

1. **Turn the writer off.** `MILO_ENABLE_REGISTER_DELTA_CAPTURE` (capture job,
   default off). The sync then behaves exactly as today (whole re-capture),
   and applied deltas keep being served consistently.
2. **Image rollback.** A previous release's code with the new migration is
   compatible: the old capture job only takes the whole path, and the
   restated SQL readers still serve applied deltas.
3. **Un-serve a tozar's deltas.** Fold it: one whole re-capture through the
   existing path (subject to the capacity guard). The new full snapshot
   becomes the current build, and the old base's deltas stop being served.
   Nothing is deleted, because append-only history stays. There is no
   down-migration, per the repository convention (`docs/production-readiness/MIGRATIONS.md`).

## 10. Q8: recommendation

### 10.1 The recommendation in three sentences

Adopt **option A**: re-fetch a drifted tozar whole as today, detect the change
in the database as the multiset difference of `_id`-blind content hashes, and
store only the added rows in a never-activated delta snapshot with its own
create-only archive, plus an append-only retirement list. The delta becomes
part of the served state only in one `apply_register_delta` transaction, after
a fresh count and a whole-state hash check, so a failed delta is invisible.
For the current backlog this turns 54,543 rows (≈ 380k dead tuples, ≈ 352 MB
of churn, refused by the guard) into about 79 rows (≈ 160 dead tuples, ≈ 0.23
MB), while every swarm, Prepare, batch, compaction and archive path keeps
reading the same activated snapshots it reads today.

### 10.2 PR-DELTA-1 scope

| File | Change | Estimated non-test lines |
|---|---|---|
| `supabase/migrations/20261010000100_catalog_register_delta_capture.sql` | §9: two tables, two columns, four new functions, ten restated | ~650 SQL |
| `backend/catalog/register/delta.py` (new) | diff (multiset), retirement choice, manifest/identity, `state_sha256`, the threshold rule (≤ 25% of the tozar, chain < 16, same schema fingerprint) | ~220 |
| `backend/catalog/register/capture.py` | `capture_unit` takes the delta path when the flag is on and the tozar has a served base: steps 2–9 of §8.1 | ~120 |
| `backend/catalog/register/sync.py` | reserve delta rows for drifted tozars (`expected − captured` as the estimate; the real count is re-checked), and report `delta=<n>` in `SYNC_SUMMARY` | ~40 |
| `backend/catalog/register/compaction.py` | the delta branch's archive read-back (reuses `verified_archive`) | ~30 |
| `backend/catalog/register/config.py` | `MILO_ENABLE_REGISTER_DELTA_CAPTURE` (default off), threshold constants | ~15 |
| `backend/repository/supabase.py` + `backend/testing/register_memory.py`, `variants_memory.py` | RPC wrappers and their in-memory mirrors | ~180 |
| `docs/production-readiness/REGISTER_CAPTURE.md` | the delta step, the new codes, operator steps | docs |
| **Total** | | **~1,275 non-test lines** |

If that is too large for one review, the natural split is:

- **PR-DELTA-1a:** the migration, the readers and the golden "no delta =
  identical" tests. No writer, so no behavior change.
- **PR-DELTA-1b:** the writer behind the flag.

**Tests:**

1. A golden comparison: with no delta, every restated reader is identical,
   using the PR-L2 fixture world
   (`tests/test_register_compaction_postgres.py`).
2. A delta end to end on PostgreSQL: added + changed + removed + an
   `_id`-only duplicate.
   - The browser serves exactly the fresh multiset.
   - `REGISTER_COVERAGE` reads served rows.
   - Coverage is updated only for touched keys.
   - A Prepare/batch on the base is unaffected.
3. Atomicity: every failure in §8.2 is injected (lease lost at each step, 403
   on the count, capacity, base moved, crash in apply). Each leaves every
   reader on the pre-delta answer, and the retry adopts or recreates the same
   delta key.
4. Thresholds: above 25%, or a schema change, takes the whole path; an
   unchanged multiset writes nothing.
5. Retention keeps applied deltas and their base, and prunes an unapplied
   orphan.
6. Every refusal code (static) and the read-only role's grants.
7. Churn: `tests/test_register_delta_churn_postgres.py` is turned into an
   assertion on the real delta path (≤ 10 dead tuples per changed row).
8. The memory-double parity tests (`tests/test_register_capture.py`,
   `tests/test_register_sync.py`).

**Release order:**

1. Merge PR-DELTA-1. The **Deploy Supabase Migrations** workflow applies the
   migration; with no delta, readers are unchanged.
2. Deploy production (the release image).
3. Check the deployed gate. `REGISTER_COVERAGE` is unchanged.
4. Set `MILO_ENABLE_REGISTER_DELTA_CAPTURE` on the capture job.
5. On the Register page, press **Resume**. Auto sync is paused only by the
   stale `stop=capacity`; 388.5 MB < 400 MB, so Resume is admitted
   (`backend/catalog/register/autosync.py:223-226`).
6. Watch the next sync. Expect about 2 syncs to absorb the 17 tozars
   (98 requests), with `SYNC_SUMMARY … delta=17 … coverage=101782/101782`.
7. Later: PR-DELTA-2 adds superseded-delta cleanup and prune, and an
   operator-triggered fold.

### 10.3 Invariants the recommendation changes, stated plainly

- **Snapshot identity.** A tozar's served state is no longer one snapshot
  whose identity is its page chain. It is a base plus deltas, identified by
  `state_sha256`: an `_id`-blind, order-blind hash of the served content
  multiset. A full capture and base ⊕ deltas of the same register state have
  different snapshot keys and the same `state_sha256`.
- **"Served" is not "rank-1 active".**
  - The browser and the coverage ledger read base ⊕ applied deltas.
  - The active rank-1 snapshot (what Prepare, batches, compaction and
    retention rank) stays the base.
  - Until a fold, the base keeps rows the register has since retired. Every
    reader that reads the base directly sees them: Prepare reuse, batch
    preparation, `source_record` replay.
  - This matches today: a Prepare already reads its own capture.
- **Count verification** checks served-before − retired + added against the
  fresh count, plus the `state_sha256` check, instead of one snapshot's stored
  rows.
- **The variant build** admits an unactivated, registered delta, and its
  coverage write moves into the apply transaction.
- **The capacity guard** keeps its formula, but a delta unit reserves its
  changed rows, not the tozar's rows.
- **Retention** keeps every snapshot of an applied chain. PR-DELTA-1 never
  prunes one.
