# Catalog variants (PR-L1)

Every register row of an ACTIVE, count-verified, whole-tozar Government
snapshot becomes ONE typed catalog variant: level 1 (identity) and level 1.5
(every mapped Government field), its vehicle category, and the discovery
tree. Deterministic: no model, no Government request, $0.

| Piece | Where |
|---|---|
| Mapper (`gov.wltp.variant-mapper.1`), category table, bounded build, backfill entrypoint | `backend/catalog/register/variants.py` |
| Tables, build RPC, discovery reads, retention keep-set | `supabase/migrations/20260930000100_catalog_variants.sql` |
| Retention of superseded snapshots and old mapper versions, compact equipment, rank-1 builds, snapshot-first tree reads | `supabase/migrations/20261001000100_catalog_variant_retention.sql` (PR-L1b) |
| Read API (`GET /projects/{id}/catalog/browser/{manufacturers,models,years,variants,facets}`) | `backend/catalog/register/browser.py` |
| Backfill of one snapshot | `scripts/ops/register-variants.sh`, workflow **Register variants** |

## When variants are built

* **After a register capture.** When the capture job marks a unit
  `captured`, it builds that snapshot's variants in batches of at most 500
  rows. A failed build is reported in the unit's document
  (`variants.status = failed` and a static code). The unit stays captured.
* **Backfill** (a snapshot that was already active, or a failed build):
  Actions → **Register variants** → `snapshot_key`. Or run
  `bash scripts/ops/register-variants.sh --snapshot-key <key>`. It runs
  `python -m backend.catalog.register.variants --snapshot-key <key>` on the
  capture job's image. It is idempotent: an already built snapshot answers
  `unchanged` without reading a row.

Only the ACTIVE snapshot of a tozar (its latest activation) is built, by a
capture or a backfill; a superseded one is refused with
`CATALOG_VARIANT_SNAPSHOT_SUPERSEDED` (backfill: `REFUSED`, exit 2). A Prepare
snapshot that no register unit captured is eligible: its stored rows equal
its declared rows (the existing gate) and no capture unit recorded it
count-unverified.

A new mapper version writes new rows beside the old ones and never changes
them; once its build of a snapshot is complete, the old version's rows are
prunable (Register retention). The discovery tree serves, for each tozar, the
newest-activated snapshot whose build under the current mapper version is
complete: each read resolves that snapshot first and reads by snapshot id
(the tree index), never the whole table.

## Storage

The driver-assistance equipment is stored as two 19-bit masks
(`equipment_stated`, `equipment_on`) and `equipment_sources`, the five
`*_makor_hatkana` texts in `EQUIPMENT_FIELDS` order (null when none is
stated); the build still validates the mapper's document against the closed
key list (`CATALOG_VARIANT_EQUIPMENT_KEY_UNKNOWN`), and the browser returns
the same `equipment` document (`catalog_variant_equipment`). Measured
(L1-6, 5,000 rows): 975 B per variant row including TOAST and indexes (was
2,345 B with the jsonb document), and 996 B per row for the two ledger
levels. A superseded snapshot's variants are pruned with it (see
REGISTER_CAPTURE.md, Retention).

## Coverage ledger

For every built variant with an identity key (the row's stored candidate,
`milo-variant-identity/2`), the build writes `catalog_variant_coverage` at
levels `identity` and `government_fields`:

* `enriched` when the variant has one content in the snapshot;
* `failed` with `CATALOG_COVERAGE_KEY_COLLISION` when rows of the snapshot
  share the key with different content.

A changed content replaces the row (decision 19), but a row recorded from a
newer-activated snapshot is never taken back by an older one. `register`
rows are never touched. `last_run_id` is the run that wrote the snapshot.

## The flag

`MILO_ENABLE_CATALOG_BROWSER` (API only, default off, a Stage A flag: every
deploy pins it off). When it is off, every browser route answers 404. To turn
it on:

* Actions → **Website stage** → `stage = catalog-browser`, which runs
  `website-execution-activate.sh --apply-catalog-browser` behind the deployed
  gate and reads back run creation and paid execution as OFF; or
* Actions → **Deploy** with `restore_website_stage = catalog-browser`. `all`
  also includes it.

The kill switch closes it.

## Operator steps after merge

1. Apply `20260930000100_catalog_variants.sql` together with
   `20260929000100_catalog_register_capture.sql`, `20260930000200` and
   `20261001000100_catalog_variant_retention.sql`, in that order.
2. Backfill the active Toyota snapshot: **Register variants** with its
   snapshot key. Expect `BUILT ... status=built rows=N/N`.
3. Turn the browser on: **Website stage** → `catalog-browser`, or pick it as
   the deploy's restore stage.

A read-only role created after the migration gets no EXECUTE on the read
functions. Grant it the same way PR-D1's roles are granted:

```sql
grant select on public.catalog_variants, public.catalog_variant_builds,
  public.catalog_variants_current, public.catalog_variant_coverage to <role>;
grant execute on function public.catalog_variant_mapper_version(),
  public.catalog_variant_equipment_valid(jsonb), public.catalog_variant_parse_issues_valid(jsonb),
  public.catalog_variant_build_state(uuid,text),
  public.catalog_browser_page_valid(integer,integer),
  public.catalog_browser_manufacturers(text,integer,integer,integer,text,integer,integer),
  public.catalog_browser_models(text,text,integer,integer,integer,text,integer,integer),
  public.catalog_browser_years(text,text,text,integer,integer,integer,text,integer,integer),
  public.catalog_browser_variants(text,text,integer,text,integer,integer,integer,text,integer,integer),
  public.catalog_browser_facets(text),
  -- PR-L1b (20261001000100)
  public.catalog_variant_equipment_keys(), public.catalog_variant_equipment_mask(jsonb,boolean),
  public.catalog_variant_equipment_source_texts(jsonb), public.catalog_variant_equipment_sources_valid(text[]),
  public.catalog_variant_equipment(integer,integer,text[]), public.catalog_register_measured_bytes(uuid),
  public.catalog_variant_current_snapshot(text), public.catalog_register_prunable_variant_builds() to <role>;
```
