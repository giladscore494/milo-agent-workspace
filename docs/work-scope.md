# Mapping Plan: the canonical WorkScope

Scoped catalog PR1 added ONE server-owned contract that states what MILO
intends to map, and a Mapping Plan surface to state and inspect it. Scoped
catalog PR2 added its preparation; see "Preparation" below. Scoped catalog PR3
adds batch runs: a person starts ONE prepared batch at a time from the Mapping
Plan, and watches the plan's progress; see "Batch runs" below.

**A plan itself executes nothing.** A batch runs only when a person starts it,
one batch per request, through the one run creation and launch path every run
uses; nothing starts the next batch. The API and the Mapping Plan reach no
Government read at all; only the operator capture job prepares a plan, and
only when an operator runs it explicitly.

- Code: `backend/catalog/scope/` (`contract.py`, `directory.py`, `interpret.py`,
  `coverage.py`, `service.py`).
- Schema: `supabase/migrations/20260922000100_catalog_work_scopes.sql`.
- UI: `frontend/components/scope/MappingPlanPanel.tsx` and `frontend/lib/workScope.ts`.
- Gates: `MILO_ENABLE_WORK_SCOPE_MUTATIONS` (plan writes) and
  `MILO_ENABLE_WORK_SCOPE_BATCHES` (starting a batch, with
  `MILO_ENABLE_RUN_CREATION`; pause / resume). Both default off, pinned off
  everywhere.

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
| `GET /projects/{id}/work-scope/capabilities` | membership | `available` = `swarm_v2` project AND the mutations flag. `can_start_batches` = `swarm_v2` AND `MILO_ENABLE_WORK_SCOPE_BATCHES` AND `MILO_ENABLE_RUN_CREATION`. `can_prepare` is always false: only the operator capture job prepares. |
| `GET /projects/{id}/work-scope/directory` | membership | The directory, with coverage per entry. |
| `GET /conversations/{id}/work-scopes/open` | membership | The conversation's open plan, or `{"work_scope": null}`. |
| `GET /work-scopes/{id}` | membership | One plan. Absent and not-a-member are the same 404. |
| `POST /conversations/{id}/work-scopes` | `MILO_ENABLE_WORK_SCOPE_MUTATIONS` | Revision 1. `{instruction}` OR `{edit}`, never both. One open plan per conversation. |
| `POST /work-scopes/{id}/revisions` | `MILO_ENABLE_WORK_SCOPE_MUTATIONS` | Revision n+1. Must name `expected_revision` AND `expected_digest`; a stale head is `409 WORK_SCOPE_STALE`, and nothing is written. |
| `GET /work-scopes/{id}/progress` | membership | The plan's progress, derived from durable state, and what the server allows next (`controls`). |
| `POST /work-scopes/{id}/runs` | `MILO_ENABLE_RUN_CREATION` AND `MILO_ENABLE_WORK_SCOPE_BATCHES` | Starts ONE batch. Names `expected_revision`, `expected_digest`, `batch_id` and an `idempotency_key`. `202` with the run; see "Batch runs". |
| `POST /work-scopes/{id}/pause` / `resume` | `MILO_ENABLE_WORK_SCOPE_BATCHES` | Holds / releases the plan's next batch. Idempotent (`changed: false`). |

A write that is understood and changes nothing writes nothing (`applied:
false`, `WORK_SCOPE_NOTE_NO_CHANGE`).

The gateway proxies the four reads the UI uses as SAFE routes, and the five
writes as execution routes behind `GATEWAY_ALLOW_EXECUTION_ROUTES`; a batch
start is also a run-creation route there, rate limited as one. The UI renders
the surface only when the execution UI flag is on AND the capability read says
the plan is available, and the Batches section only where `can_start_batches`
is true.

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

## Preparation (scoped catalog PR2)

Scoped catalog PR2 turns ONE exact plan revision into durable, bounded work.
Nothing in it starts a run: binding a batch to a run and launching it is PR3.

- Code: `backend/catalog/scope/preparation.py`,
  `backend/catalog/government/capture_scope.py`, and a scoped mode of
  `backend/catalog/operator_capture.py`.
- Schema: `supabase/migrations/20260923000100_catalog_work_scope_preparation.sql`.
- Gate: `MILO_ENABLE_WORK_SCOPE_PREPARATION`. It is read only by the operator
  capture job. The job definition pins it `false`, and
  `government-production-capture.sh --prepare-work-scope
  --enable-work-scope-preparation` turns it on for one execution.

### Where it runs

Only in the operator capture Cloud Run job, under the lease of a prepared
`operator_capture` run. That job is the one place a Government transport
exists. The database refuses the preparation write from any other kind of run,
so a paid Swarm V2 run can never prepare. Government preparation therefore sits
outside the paid batch-run clock by construction.

### What one preparation does

1. **Checks the head.** It reads the plan and the exact revision named, and
   refuses unless that revision is still the head with that digest.
2. **Checks the directory.** It parses the stored canonical text strictly and
   refuses a plan made under another directory version.
3. **Captures each unit, in priority order.**
   - A unit with no verified register spelling is `register_unverified`.
     Nothing is captured for it, because filtering the register by a guessed
     spelling would be a query the register never answers.
   - Otherwise it runs a scoped refresh: the pinned WLTP resource filtered to
     `{"tozar": "<verified spelling>"}`.
   - The refresh is query-aware. An unchanged register reuses the unit's last
     scoped snapshot; a changed one captures a new immutable snapshot through
     the existing ingestion path, with the same bounds, completeness gate,
     normalization and activation as every other capture.
4. **Writes it all at once.** The database re-derives the plan's units, years,
   limit and batch size, counts every unit from its snapshot's own rows, and
   materializes the queue in one transaction.

A capture that fails (transport, completeness, schema or lease) fails the whole
preparation. A revision is prepared exactly once, so it is never prepared from
a partial read.

### Scoped snapshots are never the register

A scoped snapshot declares itself in `retrieval_metadata.capture_scope`, and a
database CHECK holds that declaration to the query the snapshot recorded. Every
unpinned reader skips it, in the database: the projection, the tool's reader,
the review surface, the worker's preparation and the whole-register refresh.

So a run of per-manufacturer snapshots can never become "the catalog", and can
never push the register out of the bounded listing window. A scoped refresh
compares its version and its diff only with the same scope.

### The queue and its batches

The queue is deterministic:

- **Unit order.** Units are taken in plan priority.
- **Row selection.** Within a unit it takes the snapshot's `candidate` rows
  inside the plan's model years, in the catalog's one canonical order, until
  `max_items` is spent.
- **Batch cutting.** Each unit's items are cut into batches of the plan's
  `batch_size`, at most 20.
- **Batch boundaries.** A batch never spans two units, so a batch run pins
  exactly one snapshot.

**The normalization gate.** A unit whose in-range rows are mostly `ambiguous`
is recorded `vocabulary_insufficient` and queues nothing. "Mostly" means
ambiguous rows outnumber readable ones; `ambiguous` rows are the ones the
reviewed vocabulary cannot read. Nothing in normalization is loosened.

With today's vocabulary, which was built from the committed `q=RAV4` capture
only, that gate is expected to hold back most of the real register. This is the
vocabulary-evidence blocker, and it is stated per unit rather than hidden.

### Batch runs (the binding PR3 calls)

`bind_work_scope_batch_run` is the compare-and-set every batch run passes
through. It refuses:

- a batch of a revision that is not the head (a stale revision never launches);
- a second live batch run in the plan (one batch at a time, and nothing starts
  the next one);
- a batch whose run already completed;
- a run already bound to another batch;
- a run of another conversation or another engine;
- a binder who is not a member.

Binding the same run to the same batch again is the same binding.

A run that the binding table binds to a batch is prepared by the worker from
exactly that batch: its one scoped snapshot, pinned by key, and its items in
batch order. It is never prepared from "the newest snapshot" or "the first N
candidates". The run's preparation record carries the batch identity, and a
resumed attempt refuses unless the binding still names the same batch.

## Batch runs (scoped catalog PR3)

A prepared revision's batches become runs one at a time, and only when a person
starts them from the Mapping Plan.

- Code: `backend/catalog/scope/batches.py`, the routes in `backend/main.py`,
  `frontend/components/scope/MappingPlanProgress.tsx`.
- Schema: `supabase/migrations/20260924000100_catalog_work_scope_batch_runs.sql`.
- Gate: `MILO_ENABLE_WORK_SCOPE_BATCHES`, with `MILO_ENABLE_RUN_CREATION` for a
  start. Both default off, pinned off everywhere.

### One start, one run, one launch path

`POST /work-scopes/{id}/runs` names three PRECONDITIONS: the head revision and
digest the progress was read against, and the batch the person confirmed. None
of them chooses anything:

1. The API authorizes membership, refuses a stale head or a batch that is not
   the next one, and binds the run's immutable identity (the same identity gate
   as every other run; a runtime that cannot bind one creates nothing).
2. `create_work_scope_batch_run` runs ONE transaction under the plan's row
   lock: an idempotent replay answers first -- unless the API would LAUNCH
   the replayed run (its launch never happened or definitely failed), which is
   a start and is refused when its batch is stale or the plan is paused or
   closed; then a stale head
   (`WORK_SCOPE_STALE`), a paused plan (`WORK_SCOPE_PAUSED`), a second live
   batch (`WORK_SCOPE_BATCH_IN_PROGRESS`), a settled batch and any batch but the
   NEXT one (`WORK_SCOPE_BATCH_NOT_NEXT`) are refused, and a refusal writes
   nothing; then `create_message_and_run_v3` writes the message, the queued run
   and its identity, with its own concurrency ceilings, and
   `bind_work_scope_batch_run` binds that run to the batch.
3. The API's existing launch step launches it: the launch compare-and-set, the
   launcher, and the same `launch_failed` / `launch_unknown` handling.

A second request for the batch that is already running -- a double click with a
fresh key, a second tab -- answers with THAT run (`created: false`) and is
launched only if no worker was ever started for it (its launch never happened,
or definitely failed), so such a batch is launched as the same run and an
uncertain launch is never relaunched. The Mapping Plan offers "Launch batch N"
for it, for the head revision only.

The run's instruction is composed by the server from the batch ("Mapping plan
batch 2 of 3 ... research the 10 Toyota vehicle variant candidates ..."); the
candidates reach the engine from the binding, through the worker's
server-owned work context. Nothing the browser sends reaches the run's work.

### One execution scope for the catalog

With the Government read on, every catalog-reading Swarm V2 run is
BATCH-BOUND: the worker refuses a run the database binds to no batch
(`GOVERNMENT_BATCH_REQUIRED`) before it reads any snapshot. There is no other
way for a paid run to choose catalog work -- no "newest snapshot, first N
candidates" -- so a chat request and the Mapping Plan can never be two execution
scopes. A chat instruction changes the PLAN (the same contract and digest as a
click); the plan's batches are the only catalog work that runs. With the read
off, a batch run is refused (`GOVERNMENT_READ_REQUIRED`) rather than run without
its candidates, and unbound runs behave exactly as before.

### Continuation

- **One batch at a time.** Nothing starts the next batch; the Mapping Plan
  offers "Continue with batch N" once the previous one has settled.
- **In order.** Only the lowest-numbered unsettled batch of the head revision
  may start, so no candidate is skipped.
- **Settled.** A batch whose run ended `completed` or `partial_success` has
  finished its work and is never run again (its unresolved candidates are
  counted as unresolved, not re-researched).
- **Interrupted.** A batch whose run failed, was cancelled, timed out or hit its
  budget stays the next batch; starting it again is its next attempt.
- **Pause / resume.** An append-only control history
  (`catalog_work_scope_controls`, alternating, gap-free) holds the plan: a
  paused plan starts nothing. A pause does not stop a running batch; the
  existing run cancellation does, and the Mapping Plan's "Cancel this batch"
  calls it.
- **Stale revisions never start.** A revision made while a batch runs leaves
  that batch running (it is shown as belonging to its revision); the new head
  starts nothing until the operator has prepared it.
- **A batch no worker was started for** (its launch never happened or
  definitely failed) is launched again as the same run, never cancelled: nothing
  would finalize the cancellation of a run no worker claims, so it would stay
  `cancellation_requested` and hold the plan. The generic run cancellation
  refuses it too (`RUN_NOT_LAUNCHED`), in the route and in the database write.
  When the plan was revised past such a batch it can no longer be launched
  (stale): an operator releases it with `reconcile-launch-unknown.sh
  --resolution retire-not-launched`, which proves no worker, no lease, no
  execution and no paid work, and ends the run `cancelled` (`RUN_NOT_LAUNCHED`)
  with its terminal event. The batch becomes `interrupted`, and the plan
  continues when a person starts the next batch of the prepared head revision.
- **An unresolved launch** is never cancelled from the Mapping Plan either.
  Cancel is offered only for a run a worker will finalize: one a worker has
  claimed, or a queued run whose launch is recorded as `launched`. A queued
  run whose launch is `launching` or `launch_unknown` may or may not have a
  worker, so the panel says the launch is unresolved and offers neither Cancel
  nor a relaunch.
- **A lost launch** -- the API took launch ownership (`launching`) and died
  before recording the outcome -- holds the plan too: nothing relaunches it,
  and `launching` never means "not launched". It is reconciled like
  `launch_unknown`, with the same tool (`reconcile-launch-unknown.sh`), after
  the operator has checked Cloud Run for an execution of the run. The tool's
  `confirmed-launched` and `confirmed-not-launched` go through
  `reconcile_lost_launch`, which proves under the run's row lock that no worker
  ever claimed the run and that it has been quiet for at least 15 minutes (and,
  for "not launched", that nothing but the API ever wrote about it).
  `confirmed-not-launched` moves the run to `launch_failed`: the Mapping Plan
  then offers "Launch batch N" for the head revision, which launches the SAME
  run through the launch compare-and-set, once, when a person confirms it.

### Progress

`work_scope_progress` derives everything from durable state and writes
nothing: each batch's state from its bound runs' statuses, and its promoted /
refused counts from those runs' own `catalog_variant_promoted` /
`catalog_promotion_refused` events matched to the queue by candidate key.
Unresolved = a settled batch's candidates that were neither promoted nor
refused. The API adds what the server allows next (`controls.start` with its
`blocked_by` reason, `pause`, `resume`, `cancel`), and the Mapping Plan polls
the read every few seconds only while a batch is running. The browser counts
nothing.

### Rollback

Turn `MILO_ENABLE_WORK_SCOPE_BATCHES` off: no batch starts, pauses or resumes;
the progress read keeps answering; a running batch is untouched and can still
be cancelled. Nothing is deleted or rewritten.

