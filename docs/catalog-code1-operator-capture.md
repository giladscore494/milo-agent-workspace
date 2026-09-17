# CODE-1 — the guarded operator Government capture entrypoint

**Status as of 2026-09-17:** the entrypoint is **implemented in code and
tested**. **No live capture has been executed**, from this repository or
anywhere else that this repository can observe, and **no capture run has been
prepared** in any real environment. Nothing here claims a durable production
snapshot, a deployment, a migration, an activation or an enabled flag.

| Fact | State |
| --- | --- |
| CODE-1 implemented in code | **yes** — `backend/catalog/operator_capture.py`, `tests/test_catalog_operator_capture.py` |
| A supported way to prepare a capture run exists | **yes** — `--prepare`, in the same entrypoint; no manual SQL, dashboard edit or migration |
| A capture run has been prepared anywhere real | **no** |
| A live Government capture has been executed | **no** |
| OPERATOR-0 (read-only schema inspection) | **still required before execution** |
| AUTH-1 (authorization for the first live capture) | **still required** |
| A durable production catalog snapshot exists | **not claimed either way** — only OPERATOR-0 settles it (Gap Audit S3-02b) |
| `MILO_ENABLE_CATALOG_EXECUTION` enabled anywhere | **no** — this change enables it in no environment |
| Deployment, migration, activation, paid call | **none** |

This closes the CODE half of Gap Audit row **S3-02a** (`MISSING` — "no
supported, guarded live capture entrypoint exists"). It closes none of the
state rows, which are external and unobserved.

## What it is

One operator-invoked controller with **two modes** — `--prepare`, which makes a
capture run nothing else will ever launch, and `--execute`, which performs the
capture against it. Neither implies the other. The capture connects the
reviewed components in the reviewed order and adds no capture logic of its
own:

```
HttpsDataGovTransport  ->  DataGovClient(page_limit=1000)
                       ->  GovernmentCatalogRefresh.sync_if_changed()
                       ->  GovernmentCatalogIngestor.ingest_resource()
                       ->  the real repository, under an authentic WorkerLease
```

`GovernmentCatalogRefresh` is used rather than the ingestor directly because it
**delegates** the capture to `GovernmentCatalogIngestor.ingest_resource`, so
every first-capture guarantee is preserved unchanged, and it adds two
properties a first capture wants anyway: an unchanged source costs one
`package_show` and zero durable writes, and a changed source yields the
database-side bounded diff.

It is not a route, a UI, a tool, a job, a schedule or a model caller.

## Five separate things, and none implies another

This is the distinction the whole design rests on. Doing any one of these does
**not** do, authorize or imply any other:

| # | Act | What it does | What it does **not** do |
| --: | --- | --- | --- |
| 1 | **Enabling `MILO_ENABLE_CATALOG_EXECUTION`** | opens the catalog path for the worker, and lets this entrypoint be invoked at all | starts nothing; prepares nothing; captures nothing |
| 2 | **Preparing an operator capture run** (`--prepare`) | creates one run and takes operator ownership of its launch, so no model worker can ever run it | sends no request; claims no lease; writes no catalog row; captures nothing |
| 3 | **Authorizing a live capture** (AUTH-1) | a human decision, recorded outside this repository | changes no code, no flag and no run |
| 4 | **Executing the capture** (`--execute`) | performs one bounded live capture against the prepared run | promotes nothing to canonical; enables nothing |
| 5 | **Running MILO against the result** | ordinary Swarm V2 work over a snapshot that now exists | is a separate, separately authorized activity |

A snapshot existing is not permission to use it. Preparation existing is not
permission to capture. The flag being on is not a capture.

## 1 & 2 — preparing an operator capture run

Every durable catalog write is lease-guarded and a lease belongs to a run, so
the capture needs one — and it must be a run **no model worker will ever
execute**. Every ordinary creation path (`backend/main.py`
`_create_and_launch_run`) hands its new run straight to `JobLauncher.launch()`,
so an ordinary run is exactly the wrong thing.

`--prepare` is the supported way to make the right thing. It is the same
module, the same CLI, and it uses only existing repository methods — no SQL, no
direct table write, no dashboard edit, no migration, and `JobLauncher` is never
imported, constructed or called.

```
python -m backend.catalog.operator_capture \
  --prepare \
  --acknowledge-schema-report-reviewed "I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED" \
  --project-ref <the project reference this process is configured for> \
  --conversation-id <an existing conversation you are a member of> \
  --requested-by <your own user id> \
  --idempotency-key <optional; replaying it returns the same run>
```

It prints exactly one thing you need:

```json
{
  "entrypoint": "catalog.government.capture",
  "status": "prepared",
  "reason_code": "",
  "reason": "",
  "preparation": {
    "run_id": "…", "already_prepared": false,
    "launch_owner": "operator", "captured": false
  }
}
```

The conversation id, the user id, the project reference and the idempotency key
are **not** echoed back.

### What it does, in order — and why the order is the safety property

1. **`get_conversation(conversation_id, requested_by)`** — membership, enforced
   by the repository exactly as it is for a browser request. An operator who is
   not a member of the conversation's project gets the same not-found a browser
   user would, and **nothing is created**.
2. **`create_user_message` + `create_queued_run`** — the ordinary creation
   pair, so the run is a real run with real ownership, a real idempotency key
   and the operator marker. It is born `queued`/`pending`, which is
   **launchable**, and stays that way for exactly as long as step 3 takes.
3. **`try_acquire_launch`** — **the atomic boundary.** This is the same
   single-statement compare-and-set `backend/main.py` uses, so the operator and
   the ordinary launch path compete at one authoritative transition and exactly
   one can win. Losing it is a refusal (`CAPTURE_LAUNCH_OWNERSHIP_LOST`), never
   a retry, and the run is left alone.
4. **`set_launch_state('none')`** — rest the run in a launch state
   `try_acquire_launch` can never acquire from. This is safe as a plain UPDATE
   *only because* step 3 already established exclusivity; doing it without step
   3 would be the defect, since `set_launch_state` is unconditional and would
   happily overwrite a launcher's `launching`.

### Why `launch_state = 'none'`

It is migration 009's own default and is already inside the
`runs_launch_state_check` constraint, so nothing is invented and no migration
is added. Two repository facts make it the right value:

- **Unacquirable.** `try_acquire_launch` moves a run only from `pending` or
  `launch_failed`. Its single caller is `backend/main.py`, and
  `set_launch_state`'s single caller only ever writes
  `launching`/`launched`/`launch_failed`/`launch_unknown` — so nothing in the
  product can move a run *into* `none`, or back *out* of it.
- **Truthful.** It asserts the *absence* of a launch, which is exactly what an
  operator capture run is. `launching` and `launched` would each claim a launch
  that never happened, and `launch_unknown` would additionally park the run for
  a reconciliation nobody owes.

A real-PostgreSQL test
(`test_012_operator_owned_launch_state_is_unacquirable_by_the_launch_cas`)
holds the database itself to the first property.

### Replay, and interruption

- **Replay** — re-running `--prepare` with the same `--idempotency-key`,
  conversation and user returns **the same run**, reports
  `already_prepared: true`, and creates no second run.
- **Interruption** — a crash between steps 3 and 4 leaves the run at
  `launching`, which is fail-closed in both directions: no launcher can acquire
  it, and the capture refuses it (`CAPTURE_RUN_NOT_ELIGIBLE`). It is inert, and
  it is not a model run.
- **Refusal before creation** — a membership failure creates no run and no
  message at all.

## 4 — executing the capture

```
python -m backend.catalog.operator_capture \
  --execute \
  --acknowledge-live-government-egress "I ACKNOWLEDGE LIVE GOVERNMENT EGRESS" \
  --acknowledge-schema-report-reviewed "I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED" \
  --project-ref <the project reference this process is configured for> \
  --run-id <the run id `--prepare` printed> \
  --package-id degem-rechev-wltp \
  --resource-id 142afde2-6228-49f9-8a29-9b6c3a0cbe40 \
  --page-limit 1000 \
  --report-path <optional path for the sanitized report>
```

`--plan` prints what an execution would construct and performs none of it.
`--help`, a bare invocation, and any two modes together all refuse.

### Every execution prerequisite

All nine must hold together. Any one missing, malformed, contradictory or
unrecognised is a refusal with a static reason code and a non-zero exit,
**before** the repository or the transport is constructed.

1. `--execute`. Absent — including under `--plan` — is `CAPTURE_NOT_AUTHORIZED`.
2. `--acknowledge-live-government-egress` **exactly** equal to
   `I ACKNOWLEDGE LIVE GOVERNMENT EGRESS`. Not a boolean: a boolean is what a
   shell alias or a copied command supplies without anybody deciding anything.
   `--prepare` does **not** ask for this one, because it performs no egress.
3. `--acknowledge-schema-report-reviewed` **exactly** equal to
   `I ACKNOWLEDGE OPERATOR-0 SCHEMA REPORT REVIEWED`. Separate from (2) because
   they are separate facts. Both `--prepare` and `--execute` require it.
4. `--project-ref` equal to the first host label of this process's
   `SUPABASE_URL`. Neither value is ever printed.
5. `--run-id` — the run `--prepare` produced and still owns.
6. `MILO_ENABLE_CATALOG_EXECUTION` enabled, read through CODE-2's
   `catalog_execution_enabled()`. Unset, empty, `false` and any unrecognised
   value are all off. `--prepare` is gated on it too.
7. `MILO_ENABLE_PAID_EXECUTION` **disabled**, for both modes.
8. `--package-id degem-rechev-wltp` and
   `--resource-id 142afde2-6228-49f9-8a29-9b6c3a0cbe40` (the pinned WLTP
   resource). The quantity resource is allowlisted in `source.py` and is **not**
   reachable from this entrypoint.
9. `--page-limit 1000`, stated explicitly. `--max-pages` and `--max-records`
   are optional and may only restate `200` and `120000`.

There is no `--url`, `--host`, `--action`, `--query`, `--filters`, `--offset`
or `--limit`: an unrecognised argument is `CAPTURE_ARGUMENT_NOT_SUPPORTED`.

### The run the capture will accept

| Field | Required value | Established by |
| --- | --- | --- |
| `status` | `queued` | the run was never started |
| `launch_state` | `none` | `--prepare` winning `try_acquire_launch`, then resting the run |
| `input.metadata.milo_operation` | `catalog.government.capture` | `--prepare` |

The marker **alone buys nothing**: a browser request's `metadata` reaches
`input.metadata`, so a user can put that string on an ordinary run — and a test
proves such a run is still refused and stays launchable. What a user cannot do
is give a run the operator-owned launch state.

Ownership is verified **on the row `claim_run` itself returned**, not on the
row read before it. `claim_run_lease` is `returning *`, so that row is the run
as it existed at the instant the lease was taken — the check and the claim are
one step, rather than a read followed by a hopeful claim. If ownership does not
hold there, the capture refuses (`CAPTURE_LAUNCH_OWNERSHIP_LOST`), sends no
Government request and writes no catalog row.


### Stop conditions

Do not run it until **all** of these hold:

1. **AUTH-1**: explicit, current authorization for one live Government capture.
2. **OPERATOR-0** has run and its read-only schema report has been read. An
   enabled catalog over an unverified schema is the posture the flag exists to
   prevent.
3. `MILO_ENABLE_CATALOG_EXECUTION` has been enabled under its own separate
   authorization ([STAGED_ACTIVATION.md](production-readiness/STAGED_ACTIVATION.md)
   §"Catalog execution").
4. `MILO_ENABLE_PAID_EXECUTION` is off. A capture requires no model spend and
   must not be bundled with one.
5. A capture run has been prepared with `--prepare`, and its run id is in
   hand. Preparation is itself gated on (3) and on the OPERATOR-0
   acknowledgement, so it cannot be done ahead of those.
6. The rollback is understood: it prevents the **next** capture and undoes
   nothing (see below).

## The bounds, and the page size

`--page-limit 1000` is `MAX_PAGE_LIMIT` itself — **not** an increase of it.
The WLTP resource is expected to hold roughly 101 000 rows:

| Page size | Pages implied | Against `MAX_PAGES_PER_CAPTURE = 200` |
| ---: | ---: | --- |
| 100 (`DEFAULT_PAGE_LIMIT`) | `ceil(101000/100) = 1010` | refused: `GOV_PAGE_BUDGET_EXCEEDED` |
| **1000** (`MAX_PAGE_LIMIT`) | `ceil(101000/1000) = 101` | inside the existing ceiling |

Nothing else moves. `MAX_PAGE_LIMIT=1000`, `MAX_PAGES_PER_CAPTURE=200`,
`MAX_RECORDS_PER_CAPTURE=120_000`, `MAX_RESPONSE_BYTES=8 MiB`, the per-record
bound, the exact-total and pagination checks, the schema-fingerprint check, the
duplicate `_id` check and activation-only-after-complete-persistence are all
unchanged. **A live page exceeding the byte limit fails closed**, and this
change does not raise that limit to make a future capture succeed.

The capture is the whole resource: no `q`, no `filters`, no arbitrary URL,
hostname, action, package, resource or paging key. HTTPS only, redirects still
forbidden, no credential sent to `data.gov.il`.

## The sanitized report

One JSON document to stdout, and the same document to `--report-path` when one
is given — the only file this entrypoint writes. Failures carry a static reason
code and a non-zero exit.

```json
{
  "entrypoint": "catalog.government.capture",
  "status": "succeeded",
  "reason_code": "",
  "reason": "",
  "capture": {
    "outcome": "changed",
    "resource_id": "...", "upstream_version": "...", "upstream_version_kind": "...",
    "active_snapshot_key": "...", "no_op": false,
    "diff_unavailable": false, "research_required": true,
    "snapshot": {
      "snapshot_id": "...", "snapshot_key": "...", "content_sha256": "...",
      "schema_fingerprint": "...", "resource_id": "...",
      "upstream_version": "...", "upstream_version_kind": "...",
      "declared_record_count": 0, "stored_record_count": 0, "page_count": 0,
      "candidate_count": 0, "candidate_status_counts": {},
      "normalization_contract": "...", "normalized_record_count": 0,
      "normalization_issue_count": 0, "normalization_issues": {},
      "normalization_issue_records": [], "rejected_record_count": 0,
      "activated": true, "reused_existing": false
    },
    "diff": {
      "previous_snapshot_key": "", "added_count": 0, "changed_count": 0,
      "removed_count": 0, "bounded": false
    }
  }
}
```

`outcome` is one of `changed`, `unchanged`, `replayed` or `reused`, derived
from what the refresh and the snapshot state rather than from what the process
happened to observe. `snapshot` is absent for `unchanged`. `diff` is present
only when the refresh computed one; it carries **counts only**, because the
delta items quote manufacturer and model text read out of the register.

**What it never carries:** a raw record, a response body, a page body, an
exception message, SQL text, a URL, a hostname, a project reference, a
credential, a lease token, a worker id or model text. Every string is
truncated, every list is clipped, and the schema is closed — an unexpected
boundary failure is reduced to `CAPTURE_UNEXPECTED_FAILURE`.

### Exit statuses

| Status | Meaning |
| ---: | --- |
| `0` | the capture succeeded, `--prepare` prepared a run, or `--plan` printed a plan |
| `2` | refused — nothing was captured, claimed or mutated |
| `1` | started and stopped — a capture, ingestion, lease or repository failure |

The nine prerequisites are evaluated **before** the repository or the transport
exists, so a refusal on any of them opens no socket and no connection. Two
refusals happen one step later, because they cannot be answered without looking
at the run: `CAPTURE_RUN_UNAVAILABLE` and `CAPTURE_RUN_NOT_ELIGIBLE` are
decided after a database connection and a single **read**, and still before any
claim, any lease, any network egress and any write.

The project identity is verified twice: once against the mapping the invocation
was given, and again against `os.environ` — the mapping the repository itself
reads — immediately before the connection is opened. In production the two are
the same and the second check is a no-op; it exists so the first can never be
satisfied by one mapping while the connection goes somewhere named by another.

## Rollback

Setting `MILO_ENABLE_CATALOG_EXECUTION=false` **prevents another capture** from
starting: prerequisite 6 fails, so no transport is constructed, no database is
connected to, no run is claimed and nothing is captured, ingested or activated.

It **deletes nothing and deactivates nothing.** A snapshot that already landed
stays exactly as it is, active or not; raw records, candidates, evidence links
and canonical variants are untouched. Re-enabling the flag resumes from the
same durable state. The command and its verification evidence are in
[ROLLBACK.md](production-readiness/ROLLBACK.md) §"Catalog execution", which
remains truthful for this entrypoint without amendment.

An interrupted capture needs no rollback at all: activation is the last step
and is gated on complete persistence, so an interruption leaves a non-active
snapshot that no reader reads, and the previous usable snapshot keeps
answering.

## What this does not authorize

Preparing a run is not authorization to capture, and capturing is not
authorization to run MILO against the result.


Capturing and activating a Government snapshot is **not** authorization to run
MILO against it or to promote canonical facts. This entrypoint enables and
depends on none of: paid execution, run-creation routes, browser execution UI,
Commander, Swarm workers, provider credentials or catalog promotion. It creates
no schedule and no automatic refresh; `GovernmentCatalogRefresh` still has no
scheduler, and `tests/test_catalog_pr3_swarm_promotion.py` still fails if one
appears.
