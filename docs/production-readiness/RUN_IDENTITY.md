# Run identity, persistence fencing and the event registry

This document covers the three control-plane authorities that close the
release boundary: what a run IS, who may change a run's durable state, and
which events are legitimate.

---

## 1. Immutable run identity

### The defect

A run had no identity of its own. Every surface that needed to know what a run
WAS re-derived the answer later, from whatever was to hand:

* **`backend/worker/engine.py`** resolved the engine from the PROJECT's current
  `workflow_key`, at worker claim time. A project switched from
  `vehicle_catalog_v1` to `swarm_v2` between a run's creation and its launch —
  or between its first attempt and a Cloud Run retry — changed what that run
  was. The same run row could execute as V1 on attempt 1 and as V2 on attempt
  2, with the second attempt resuming the first's checkpoint into a different
  engine.
* **`backend/export_envelope.py`** decided the engine like this:

  ```python
  engine = str((run.get("input") or {}).get("workflow_key")
               or run.get("workflow_key") or "vehicle_catalog_v1")
  ```

  The first source is the run's own `input` — the request metadata the CALLER
  supplied — so a caller who put a `workflow_key` in their metadata chose what
  the exported run claimed to be. The second names a column that does not
  exist. The third meant a Swarm V2 run whose input did not happen to name its
  workflow exported as V1, and was then classified by V1's outcome rules.
* **The browser** selected V1 or V2 presentation from `project.workflow_key`,
  so a historical run re-rendered as whatever the project is TODAY.
* **Nothing recorded** which runtime policy or which release admitted the run,
  so "which envelope was this paid run authorized against?" had no durable
  answer at all.

### The rule

> A run's identity is established BEFORE execution, from server-owned relations
> only, and is immutable for the life of the run. Resume, worker retry,
> checkpoint restore, export and finalization READ it. None of them may infer
> it from current defaults, engine availability, checkpoint contents, output
> shape, event history or caller input, and none of them may rewrite it.

A run created as V2 can never later look like V1, or the reverse.

### The schema

`backend/run_identity.py` owns it; `runs.run_identity jsonb` stores it.

| Field | Meaning |
| --- | --- |
| `identity_version` | `milo-run-identity/1` — the record's own schema |
| `run_id` | the run this identity belongs to, carried INSIDE the record so a record lifted out of its row cannot be read as another run's |
| `workflow_key` | the engine allowlist key, resolved once from the trusted run → conversation → project relation |
| `engine_version` | the reviewed engine contract version, from `ENGINE_VERSIONS` — the ONE place either engine's version is written down |
| `policy_version` | `POLICY_SCHEMA_VERSION` |
| `policy_fingerprint` | the digest of the REVIEWED runtime-policy envelope this image declares |
| `release_sha` | Full 40-character `MILO_RELEASE_SHA` of the immutable release that created the run. New persisted identities cannot use an empty release; an unpinned historical identity is not executable or exportable. |
| `event_registry_version` | the event vocabulary this run's durable stream speaks |

The **reviewed** policy fingerprint is bound, not a deployment-resolved one:
caps go on both the API and the Worker while provider and engine limits belong
to the Worker alone, so a deployment-resolved fingerprint would differ by
surface for one run. The reviewed envelope is a property of the runtime
SOURCE, which is exactly what makes it bindable to a release.

### Where it is enforced

* **Application** — the API builds the identity before durable creation and
  calls only the atomic `create_message_and_run_v3` path. It refuses a
  missing/stale release, policy, event registry or engine identity before
  launch. The product worker consumes the persisted identity before taking a
  lease.
* **Database** — `20260921000200_immutable_run_identity.sql`:
  * every NEW run must be INSERTed with a complete identity;
  * `create_message_and_run_v3` commits the message, run and immutable
    identity in ONE transaction and re-checks the trusted project workflow;
  * the superseded `bind_run_identity`, `create_message_and_run` and
    `create_message_and_run_v2` primitives are removed;
  * `runs_forbid_identity_rewrite` refuses every post-INSERT identity change,
    including legacy `NULL → value` retrofits and erasure;
  * `claim_run_lease` refuses identity-less legacy rows;
  * the shape constraint requires a full release SHA and the reviewed
    workflow/engine identity shapes.

### Legacy runs

The column is **nullable on purpose**. Production already holds runs created
before this release. `None` means exactly one thing — "created before
identities existed" — and it is never a fallback for a record that is present
and wrong, which raises like any other untrusted identity.

An unpinned legacy run remains **readable history only**. It is not executable,
not resumable, not exportable and not release-authorizable. Neither the API,
the product worker nor the lease RPC may infer an engine from the project's
current workflow, and nothing may retrofit an identity afterwards: doing so
would invent history.

---

## 2. Fenced persistence

Identity answers *is this a worker?* The lease answers *is this **the** worker
of **this** attempt of **this** run?* Only the second stops a replaced worker
whose credentials are still valid.

### What was unfenced

| Path | What a stale worker could do |
| --- | --- |
| `tool_access_requests` | open tool access on a run it no longer owned |
| `tool_grants` | grant a tool AND mark the referenced request granted — two tables, two unfenced statements |
| `run_usage_ledger` | keep charging a run it no longer owned against the live worker's DAILY budget |
| nine worker HTTP routes | append events, open tool access, and — through `/complete` and `/fail` — **terminalize a run another worker was executing** |

Four of those routes (`/tool-usage`, `/sources`, `/claims`, `/conflicts`) were
additionally dead on arrival: they called repository methods whose lease
arguments had become required when the evidence writes were fenced, and never
passed them.

### What holds now

Every worker-side durable mutation takes the canonical
`run + attempt + worker + lease` contract and travels through a guarded RPC
that settles it against the live lease **under the database clock**. No second
fencing implementation was introduced — the three new writers call the same
`assert_worker_lease` every other guarded write calls.

On the wire the contract is `WorkerLeaseFence` (`backend/schemas.py`).
`request.content()` is what gets written and never includes the fence: the
lease token is a credential, and the guarded evidence RPCs refuse any payload
carrying one outright.

`tests/test_worker_persistence_fencing.py` walks the Repository protocol and
fails when a NEW run-owned writer is added without a lease, so the audit does
not have to be remembered.

---

## 3. The canonical event registry

`backend/event_registry.py` is the ONE event vocabulary. Before it, four
independently maintained lists disagreed:

* the API accepted a set naming **no Swarm V2 type at all**, so it answered 422
  to `task_started` — an event the V2 engine emits on every task;
* `SupabaseEventSink` — the sink that actually writes — validated **nothing**,
  so 27 further types reached `run_events` unchecked;
* `InMemoryEventSink` validated against the API's narrower set, so three
  boundaries gave three answers for one event;
* the browser hand-maintained a Swarm V2 set with no backend counterpart, and
  five real engine types were missing from it.

### Groups, and what membership grants

Membership grants a PROJECTION, which is why the groups stay separate:

| Group | Owns |
| --- | --- |
| `v1` | the browser's agent, phase, progress, source, claim, conflict and spend projection |
| `swarm_v2` | the swarm slice — never the V1 projection, because V2 has no agent concept |
| `catalog` | exactly the bounded catalog status slice |
| `operational` | **nothing**. Durable operator signals whose emitters have no agent, task, phase or product concept |
| `capture_progress` | **not durable at all** — the Government capture keeps these in memory and writes none |

`EVENT_TYPES` is the union of the first four: the acceptance vocabulary, and
the only set a durable append is checked against.

### Anti-drift

`frontend/lib/eventRegistry.generated.json` is written by the Python module and
read by `frontend/lib/eventVocabulary.ts`, which declares no event names of its
own. `tests/test_event_registry.py` proves the manifest is exactly the Python
registry; `frontend/tests/eventVocabulary.test.ts` proves the TypeScript sets
are exactly the manifest. Neither side can move without the other.

A static AST scan (`_emitted_event_types`) additionally proves every literal
event type the backend emits is inside the registry — it sees emitters no test
happens to exercise, which is how 27 types stayed outside every
vocabulary while being written on every run.

---

## 4. Export

The engine comes from the immutable identity, with **no fallback**. A run whose
identity is absent or unreadable is REFUSED with a bounded reason: an export is
a document that will be read as authoritative, and a guessed engine in one is
worse than no document at all.

The envelope carries the whole identity, so an exported run states which engine
version, which reviewed policy envelope and which release admitted it. The
validator refuses an envelope whose `engine` and `run_identity` disagree.

---

## 5. Release binding

    accepted runtime source
      ==(bytes)==>      backend/runtime_policy.py at STAGE_D_RELEASE_SHA
      ==(digest)==>     the reviewed policy fingerprint
      ==(bound)==>      runs.run_identity of the authorized run
      ==(tag)==>        the built images
      ==(digest)==>     the images production serves and the job executes
      ==(gate)==>       Stage D authorization

Every link but one already had a check. The run identity is the one that did
not exist, so nothing tied the run that ACTUALLY EXECUTED to the release that
was authorized.

* `policy_envelope.py run-identity` prints the identity dimensions every run of
  this release must carry, generated from the same policy document;
* `stage-d-env.sh` pins it as `STAGE_D_EXPECTED_RUN_IDENTITY`;
* `probe_db.py`'s evidence gate compares the authorized run's PERSISTED
  identity against that pin and refuses a run that is not a run of this
  release, an unpinned run, or an unparseable expectation;
* `verify_caps.py` refuses, **before the run is created**, a deployment whose
  `MILO_RELEASE_SHA` is absent or is not the accepted release.

**PR #103's rule is preserved.** The binding is a statement about policy
CONTENT, not about which commit is checked out: a reviewed authorization commit
must be able to reference release R without being R. Image-digest verification
is unchanged and not weakened — a tag match is still never acceptance.

---

## 6. The release RPC / migration inventory

`scripts/release/release_inventory.py` DERIVES the preflight inventory from the
repository as it is: every RPC name the runtime calls (an AST scan of the
repository layer plus the probe's own `/rest/v1/rpc/` calls), and every function
the migrations create with the arguments each requires.

Stage D's preflight previously carried a hand-written list of six RPCs. The
runtime depends on **46 across 19 migrations**. A production database missing
`record_run_usage_guarded` or `finalize_run_guarded` passed every check and
would then have failed on the first paid model call, after the money was spent.

The inventory is kept as a reviewed literal,
`scripts/release/pins/required_rpc_args.py` (cleanup D8) — the same
arrangement as `PINNED_POLICY_FINGERPRINT`. `tests/test_release_inventory.py`
fails if that literal is not exactly what the current repository requires.
`probe_db.py` carries a byte-identical copy of it (it is transported into a
bare pinned image as one SHA-256-pinned file and cannot import anything) until
the Stage D toolkit is deleted.

Regenerate with:

```sh
python3 scripts/release/release_inventory.py rpcs
```
