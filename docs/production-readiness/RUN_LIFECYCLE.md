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

## Worker leases

`claim_run_lease` (migration `012`) atomically assigns worker id, attempt
and a fresh `lease_token` nonce, setting `lease_expires_at`. A heartbeat
thread extends the lease (`MILO_WORKER_LEASE_SECONDS`,
`MILO_WORKER_HEARTBEAT_INTERVAL_SECONDS`); every worker mutation
(heartbeat, transition, checkpoint, events, completion) must present the
matching worker id + attempt + lease token, so a stale or superseded worker
cannot mutate a reclaimed run. Lease loss short-circuits execution
(`backend/worker/main.py`).

## Checkpoints and events

`run_checkpoints` and `run_events` (migration `002`) persist progress;
events power UI polling (`GET /runs/{id}/events`). Checkpoint writes are
lease-guarded like every other worker mutation.

## Cancellation

`POST /runs/{id}/cancel` is membership-authorized, rate-limited, gated by
`MILO_ENABLE_RUN_CANCELLATION`, and idempotent — repeating it on an
already-cancelled run is a no-op. A run cancelled before start emits no
`run_started` event and never calls the engine
(`tests/test_corrective_blockers.py`). The worker observes
`cancellation_requested` at its next heartbeat/step boundary and transitions
to `cancelled` under its lease.

## Retries and duration

Retries are capped by `MILO_MAX_RETRIES` and run duration by
`MILO_MAX_RUN_DURATION_SECONDS` (mandatory for paid execution). Retry
attempts increment `attempt`, which invalidates older leases.
