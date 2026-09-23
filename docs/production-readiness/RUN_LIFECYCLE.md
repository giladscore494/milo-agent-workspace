# Run lifecycle

All `COMPLETED_IN_CODE` unless noted.

## States

Run status (migration `002`): `queued → starting → running ⇄ waiting →
completed | partial_success | failed`, plus `cancellation_requested →
cancelled`. Launch state (migrations `009`/`012`): `none → pending →
launching → launched | launch_failed | launch_unknown`.

## Idempotent creation

Run creation requires an `idempotency_key` (8–128 chars,
`backend/schemas.py`); `create_message_and_run` (migration `012`) inserts
message+run atomically and a unique index on
`(requested_by, conversation_id, idempotency_key)` makes retries return the
existing run instead of duplicating work. Proposal creation is idempotent
the same way.

## Launch lifecycle and reconciliation

The API launches the worker job through `backend/job_launcher.py`
(`JOB_LAUNCHER=disabled` by default). Launch transitions use compare-and-set
so concurrent launchers cannot double-launch. When the launch response is
uncertain (timeout, ambiguous error), the run is parked as
`launch_unknown` and **never automatically relaunched** — an operator
verifies against Cloud Run execution logs and resolves it with
`scripts/release/reconcile-launch-unknown.sh` (list-only by default;
mutations require full protected apply mode and are idempotent). The UI
surfaces `launch_state`, `launch_error_class` and
`launch_reconciliation_required` (`backend/main.py`); the lease token is
stripped from browser responses.

A **lost launch** is a run left at `queued` + `launching`: the API took launch
ownership with its compare-and-set and its process died before it recorded
`launched`, `launch_failed` or `launch_unknown`. It is an unresolved launch,
like `launch_unknown`: `launching` never means "not launched", so nothing
relaunches it, nothing treats it as dead, and the Mapping Plan offers no
Cancel for it (nothing would finalize the cancellation). The same tool
resolves it: `reconcile-launch-unknown.sh` lists lost launches quiet for at
least `--min-quiet-seconds` (default 1800, never under 900), and after the
operator has checked Cloud Run for an execution of the run, it applies
`confirmed-launched`, `confirmed-not-launched` or `leave-unresolved` under the
same protected apply mode and audit. The two mutating decisions go through
`public.reconcile_lost_launch` (migration `20260924000100`), which proves
under the run's row lock that the run is still queued, that no worker ever
claimed it (no worker, no lease, never started) and that it has been quiet
for the threshold; `confirmed-not-launched` also requires that nothing but
the API ever wrote about the run. It then moves the run to `launch_failed`,
the existing requeue path: the same run is launched again only through the
launch compare-and-set, by a person (the Mapping Plan's "Launch batch", or a
replay of the original request) or after the tool's `requeue`. At most one
worker ever executes a run: `claim_run_lease` grants one live lease.

A **never-launched run** (`pending` or `launch_failed`: no worker was ever
started) can be launched again as the same run. When it cannot -- its Mapping
Plan was revised past its batch, and a stale revision never launches -- it
would hold the plan and its requester's run slot for good. The same tool's
`retire-not-launched` decision releases it: `public.retire_unlaunched_run`
proves under the run's row lock that the launch is known not to have started a
worker (never `launching` or `launch_unknown`: those are reconciled first),
that no worker ever claimed it and no lease is held, that no execution or paid
work exists for it, and that it has been quiet for the threshold. It then ends
the run the way the canonical finalizer ends a cancellation: `cancelled`
(`RUN_NOT_LAUNCHED`) and its `run_cancelled` event in one transaction. Nothing
is relaunched; a batch it held becomes `interrupted`.

## Cancellation

A cancellation is a request the run's worker finalizes. `POST
/runs/{id}/cancel` accepts it only for a run a worker will finalize: one a
worker has claimed (`starting`, `running`, `waiting`), or a `queued` run whose
launch is recorded as `launched`. Anything else is refused with nothing
written -- `RUN_NOT_LAUNCHED` (no worker was ever started: launch it again, or
an operator retires it) or `RUN_LAUNCH_UNRESOLVED` (an operator reconciles the
launch first). The route checks it, and the database write itself re-checks it:
`request_cancellation` is a compare-and-set on the status read and, for an
unclaimed run, on `launch_state = 'launched'`, evaluated in the same statement
as the write (`backend/runtime.py::cancellation_refusal` is the one rule; the
Mapping Plan's Cancel control uses it too).

## Worker leases

`claim_run_lease` (migration `012`) atomically assigns worker id, attempt
and a fresh `lease_token` nonce, setting `lease_expires_at`. A heartbeat
thread extends the lease (`MILO_WORKER_LEASE_SECONDS`,
`MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS`). Every worker-originated
durable write presents the matching worker id + attempt + lease token and
is validated **atomically in the database**, not by an application-side
read-then-write check: run transitions, heartbeats and usage snapshots
via conditional `UPDATE` predicates (migration `012`,
`backend/repository/supabase.py`), and event appends, checkpoints,
blackboard upserts, agent messages, supervisor decisions and budget
reservations/settlements via the lease-guarded RPCs of migration
`20260810000300` (`assert_worker_lease` takes `FOR SHARE` on the runs row
so a concurrent reclaim serializes against the write). A stale or
superseded worker is rejected with `STALE_WORKER_WRITE`/zero-row updates
on every one of these paths — proven end-to-end by
`tests/test_migrations_postgres.py::test_stale_worker_full_scenario_every_mutation_rejected`.
Lease loss also short-circuits execution (`backend/worker/main.py`).

The heartbeat thread distinguishes a DEFINITIVE loss -- the database
answering that the lease, attempt or token is no longer current, or that the
run is gone -- from a transient failure (transport, 5xx). A transient
failure is retried sooner than the normal interval and the lease is treated
as lost only once the last proven extension has lapsed; ownership is never
widened by this, because every durable write is still fenced at the
database boundary and `holds_lease` re-reads the run row.

Usage consumed under a lease is recorded in the ExecutionUsageLedger
(`run_execution_usage`, migration `20260920000100`) after every consumption
and restored -- as the maximum of every durable record -- by the next
worker, so a resume, a replay or a replacement never regains capacity. See
`BUDGETS_AND_COSTS.md`.

## Checkpoints and events

`run_checkpoints` and `run_events` (migration `002`) persist progress;
events power UI polling (`GET /runs/{id}/events`). Checkpoint writes are
lease-guarded like every other worker mutation.

## Terminal decisions

Every terminal path — V1 and V2 success, partial success, engine failure,
pre-execution refusal, cancellation, timeout and budget exhaustion, and the
summary-checkpoint resume — converges on ONE finalizer
(`backend/finalization.py`), and no branch writes a durable status itself.
The status is derived from a terminal REASON plus the canonical
`ProductOutcome` (`backend/product_outcome.py`); terminal statuses are
ordered by authority so a late `completed` can never overwrite a cancellation
or a safety-rail stop; terminalization is idempotent, and a terminal state
another legitimate path already won is adopted rather than rewritten. The
terminal transition and the terminal event commit in ONE transaction
(`finalize_run_guarded`, migration `20260920000200`), so a terminal event
exists only for the decision that durably won. See `RUN_FINALIZATION.md`.

## Cancellation

`POST /runs/{id}/cancel` is membership-authorized, rate-limited, gated by
`MILO_ENABLE_RUN_CANCELLATION`, and idempotent — repeating it on an
already-cancelled run is a no-op. A run cancelled before start emits no
`run_started` event and never calls the engine
(`tests/test_corrective_blockers.py`). The worker observes
`cancellation_requested` at its next heartbeat/step boundary and transitions
to `cancelled` under its lease. A product the engine finished anyway is kept
as the run's output, and the run is still recorded as `cancelled`: the
decision to stop outranks a late result (`RUN_CANCELLED_AFTER_RESULT`).

## Retries and duration

Retries are capped by `MILO_MAX_RETRIES` and run duration by
`MILO_MAX_RUN_DURATION_SECONDS` (mandatory for paid execution). Retry
attempts increment `attempt`, which invalidates older leases.
