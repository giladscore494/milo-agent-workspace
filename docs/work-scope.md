# Mapping Plan: the canonical WorkScope

Scoped catalog PR1. This adds ONE server-owned contract that states what MILO
intends to map, and a Mapping Plan surface to state and inspect it. **Nothing
executes from a plan in this release.** No Government read, queue, batch, run or
provider call is reachable from any of it. A plan is a durable draft.

- Code: `backend/catalog/scope/` (`contract.py`, `directory.py`, `interpret.py`,
  `coverage.py`, `service.py`).
- Schema: `supabase/migrations/20260922000100_catalog_work_scopes.sql`.
- UI: `frontend/components/scope/MappingPlanPanel.tsx` and `frontend/lib/workScope.ts`.
- Gate: `MILO_ENABLE_WORK_SCOPE_MUTATIONS` (default off, pinned off everywhere).

## The contract (`milo-work-scope/1`)

```json
{"batch_size": 10,
 "contract": "milo-work-scope/1",
 "directory_version": "milo-manufacturer-directory/1",
 "max_items": 800,
 "model_years": {"from": 2018, "to": null},
 "source": {"family": "government", "package_id": "degem-rechev-wltp",
            "resource_id": "142afde2-6228-49f9-8a29-9b6c3a0cbe40"},
 "units": ["toyota", "lexus"]}
```

| Field | Rule |
| --- | --- |
| `units` | Directory keys, 1 to every entry, each once. **Order is priority.** |
| `model_years` | Whole years 1900–2100 or `null`; `from` not after `to`. |
| `max_items` | 1–2000 candidates across the whole plan. New plans default to 100, always with a note. |
| `batch_size` | 1–20 candidates per batch run. Default 10, hard server maximum 20. |
| `directory_version` / `source` / `contract` | Server-owned. An edit cannot state them. |

`scope_from_fields` is the ONLY constructor. It refuses a value outside the
contract, naming the field, and never clamps, trims, sorts or de-duplicates.
Types are strict: `"10"` is not a limit and `true` is not a batch size.

**The digest.** The record is rendered as canonical text (sorted keys, compact
separators, ASCII). The digest is the SHA-256 of exactly that text. The
database stores the text and derives the digest itself: a CHECK constraint
holds `digest = sha256(scope_text)` on every row. The digest is portable on
purpose. It travels to the browser and back, and every revision must name the
digest of the head it was made against.

## One plan, two inputs

A person can **type** what they want or **build** it in the Mapping Plan. Both
are only inputs. Each is reduced to the same four fields and validated by the
same function, so both produce the same canonical record and digest
(`tests/test_work_scope.py::test_chat_and_clicks_produce_the_same_stored_plan_and_digest`,
and `e2e/enabled.mapping-plan.spec.ts` M1 in a real browser).

The instruction reader (`interpret.py`) is deterministic. It is **not a model**:
no provider is called and nothing is sampled. It reads English and Hebrew:

| Typed | Plan |
| --- | --- |
| "Map Toyota and Lexus, starting with 2018+, up to 800 variants." / "מפה את טויוטה ולקסוס, החל מ-2018, עד 800 דגמים." | `[toyota, lexus]`, 2018 onward, 800 |
| "Continue with Japanese manufacturers we have not mapped yet." / "המשך עם יצרנים יפניים שעוד לא מיפינו." | Every Japanese entry, minus any the canonical catalog is **known** to hold |
| "Do Toyota first, then Mazda. Stop after 500 vehicles." / "קודם טויוטה, אחר כך מאזדה. עצור אחרי 500 רכבים." | `[toyota, mazda]`, 500 |

On an existing plan, an instruction changes only the fields it states. The
readers are:

- **SET** replaces the list ("map", "only", "continue with").
- **ADD** appends ("add", "also", "גם").
- **REMOVE** drops ("without", "do not map", "בלי").
- **FIRST** reorders ("Mazda first"). An instruction whose only mentions are
  FIRST mentions reorders the plan rather than replacing it.

Words the reader cannot place are returned verbatim, bounded, as an
`UNRECOGNIZED` note, so a misspelt marque is visible rather than silently
dropped. An instruction in which nothing is recognized is refused.

## The manufacturer directory and what it cannot yet do

`directory.py` is a reviewed, closed table. Each entry has four fields:

- an ASCII key, which is what a plan stores;
- English and Hebrew display names;
- an origin, used for "Japanese manufacturers";
- recognition aliases.

**Register spellings.** The Government register filters a marque by the exact
`tozar` text, which the register publishes no code for. The directory records a
register spelling only where committed register evidence states it. Today that
is **one** entry:

- `toyota → טויוטה`: every one of the 233 rows of the R5 capture states it.

Every other entry's `register_marque` is null and says so on every read
(`register_marque_verified: false`). The Hebrew display names are how Israeli
readers commonly write the marques. They are **not** register spellings and
are never used as filter values.

A plan may still name an unverified marque, because the plan is the person's
intent. Preparing it needs verified register evidence, which is scoped catalog
PR2's normalization/evidence work. `data.gov.il` was not reachable from the
environment that built PR1: `curl` got a 403 at the egress proxy CONNECT, and
the web fetch tool reported `EGRESS_BLOCKED`.

## Coverage

`catalog_canonical_manufacturer_coverage` returns exact canonical variant counts
for the verified register spellings, plus the catalog-wide total. Each directory
entry is in one of three states, and they are never merged:

| State | Meaning |
| --- | --- |
| `known` | An exact count, possibly zero. |
| `unverifiable` | No verified register spelling, so no count can be attributed. This is not zero. |
| `unavailable` | The read failed. Nothing is stated. |

The "not mapped yet" reader drops only marques **known** to be mapped. It keeps
unverifiable ones, with a `COVERAGE_UNKNOWN` note: a marque is never skipped
for what is not known about it.

## API

| Route | Gate | Notes |
| --- | --- | --- |
| `GET /projects/{id}/work-scope/capabilities` | membership | `available` = `swarm_v2` project AND the flag. `can_prepare` / `can_start_batches` are always false in this release. |
| `GET /projects/{id}/work-scope/directory` | membership | The directory, with coverage per entry. |
| `GET /conversations/{id}/work-scopes/open` | membership | The conversation's open plan, or `{"work_scope": null}`. |
| `GET /work-scopes/{id}` | membership | One plan. Absent and not-a-member are the same 404. |
| `POST /conversations/{id}/work-scopes` | `MILO_ENABLE_WORK_SCOPE_MUTATIONS` | Revision 1. `{instruction}` OR `{edit}`, never both. One open plan per conversation. |
| `POST /work-scopes/{id}/revisions` | `MILO_ENABLE_WORK_SCOPE_MUTATIONS` | Revision n+1. Must name `expected_revision` AND `expected_digest`; a stale head is `409 WORK_SCOPE_STALE`, and nothing is written. |

A write that is understood and changes nothing writes nothing (`applied:
false`, `WORK_SCOPE_NOTE_NO_CHANGE`).

The gateway proxies the three reads the UI uses as SAFE routes, and the two
writes as execution routes behind `GATEWAY_ALLOW_EXECUTION_ROUTES`. The UI
renders the surface only when the execution UI flag is on AND the capability
read says the plan is available.

## Database guarantees

The database holds these on every path, not only the API's:

- **Integrity of each revision.** The digest is derived from the text, the
  jsonb equals the text, and the record's shape and hard bounds are checked
  (batch size ≤ 20, limit ≤ 2000).
- **Append-only history.** Revisions are append-only and numbered 1, 2, 3 …
  without a gap. A plan's identity is immutable and it is never deleted. Its
  head advances one revision at a time and always names a revision that exists
  with that digest (a deferred constraint trigger).
- **Concurrency.** There is at most one open plan per conversation (a partial
  unique index), and a stale head is refused under a row lock.
- **Authorization.** The writers re-check membership and the trusted
  `swarm_v2` workflow themselves.
- **Service-path only.** RLS is on with no policies; `anon` and
  `authenticated` have nothing.

## Rollback

Leave `MILO_ENABLE_WORK_SCOPE_MUTATIONS` off, which is the default. The plan
reads keep answering, and nothing executes from a plan in this release. No other
relation depends on the two new tables. A forward migration could drop them.
