# Catalog variants (PR-L1)

Every register row of an ACTIVE, count-verified, whole-tozar Government
snapshot becomes ONE typed catalog variant: level 1 (identity) and level 1.5
(every mapped Government field), its vehicle category, and the discovery
tree. Deterministic: no model, no Government request, $0.

| Piece | Where |
|---|---|
| Mapper (`gov.wltp.variant-mapper.1`), category table, bounded build, backfill entrypoint | `backend/catalog/register/variants.py` |
| Tables, build RPC, discovery reads, retention keep-set | `supabase/migrations/20260930000100_catalog_variants.sql` |
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

A new mapper version writes new rows beside the old ones and never changes
them. The discovery tree serves, for each tozar, the newest-activated
snapshot whose build under the current mapper version is complete.

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
   `20260929000100_catalog_register_capture.sql`, in that order.
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
  public.catalog_browser_facets(text) to <role>;
```
