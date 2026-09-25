# PR-S — reasoning-safe transport (runbook)

Every Swarm V2 provider call is **streamed**, bounded by a per-role **total
deadline** and a short **inactivity** window. Transport failures carry their
own static codes, and every provider attempt writes one sanitized JSON line to
stdout, which Cloud Logging ingests. This page is the operator's reference for
the new values and for reclaiming a lease a failed call left quarantined.

Code: `backend/provider_streaming.py`, `backend/provider_transport.py`
(`request_timing`), `backend/engines/swarm_v2/model_gateway.py`
(`ROLE_POLICIES`), `backend/provider_quota.py` (`REVIEWED_LEASE_TTL_SECONDS`).
Tests: `tests/test_provider_streaming.py`, which includes the replay of run
5145ca65.

## Why: run 5145ca65

Commander planning on `kimi-k3` (effort `high`, `max_completion_tokens`
32000, `json_object`) was sent **non-streaming**. A reasoning model that is
not streaming sends nothing until the whole answer exists. The request
deadline was 90 s (derived from the 120 s lease window). The deadline
transport gives the silent header wait whatever the setup phases leave:
`90 − (4.5 pool + 9 connect + 9 write) = 67.5 s`. At about 70 s after send,
httpx raised `ReadTimeout` with **no provider response object**, so:

| event (production, 2026-09-25) | at |
| --- | --- |
| `agent_started` commander/planning | 14:06:04.58 |
| `provider_lease_ownership_lost` `PROVIDER_LEASE_PROBE_FAILED` (first probe, 30 s) | 14:06:36.2 |
| `provider_lease_quarantined` `PROVIDER_REQUEST_OUTCOME_UNKNOWN`, lease `34d6697f…` | 14:07:15.6 |
| `run_failed` `COMMANDER_COMPLETION_FAILED`, 0 tokens | 14:07:16.3 |

The reason was `OUTCOME_UNKNOWN`, not `PROVIDER_REQUEST_DEADLINE_EXCEEDED`.
That is the signature of the header read timeout, not of the 90 s total.

## What changed

| | before | now |
| --- | --- | --- |
| Swarm V2 request | non-streaming | `stream: true`. `stream_options` is **not** sent: it is not documented for the Kimi models in any source MILO could verify (the Kimi K3 README does not mention it) |
| answer assembly | `message.content` | `delta.content` only; **`reasoning_content` is never read or stored** |
| `finish_reason`, `usage` | response body | final chunk: usage on the final choice (Moonshot, `choices[0].usage`) or a usage-only chunk. Reading stops once the `finish_reason` and a usage block at or after it have both arrived; MILO does not wait for `[DONE]` |
| a finished stream with no usage | n/a | charged its **whole reservation** (input upper bound + full cap), never 0 |
| sent, outcome unknown (mid-stream drop, inactivity or total deadline) | 0 tokens, $0 | charged its **whole worst-case reservation** to the run budget and the daily settlement; reasoning and answer counts recorded as null |
| lease released when | the call returned | the stream **ended with a `finish_reason`**; a dropped stream stays `OUTCOME_UNKNOWN` (quarantined) |
| silence bound | header read = deadline − setup (67.5 s) | **60 s** between chunks, header wait included (`STREAM_INACTIVITY_SECONDS`) |
| total bound | 90 s for every call | per role, below |
| V1 chat, standalone search | 90 s, non-streaming | **unchanged**: 90 s, non-streaming (`NON_STREAMING_REQUEST_DEADLINE_SECONDS`) |

### Per-role total deadlines (`RolePolicy.total_deadline_seconds`)

| role | total | worst-case wall time (total + one 60 s inactivity window) |
| --- | --- | --- |
| commander / planning | 600 s | 660 s |
| commander / replanning | 300 s | 360 s |
| verifier / verification | 480 s | 540 s |
| worker / execute | 300 s | 360 s |

The transport checks the total on every chunk. A stream that goes silent just
before its deadline is stopped by the inactivity window instead, which is why
the worst case is the total plus one window.

## Environment values

**No environment change is required, and none was made.** The production
worker job (`milo-agent-worker`, checked by variable **name** only on
2026-09-25) sets neither variable below, so the code defaults apply on the
next deploy.

| variable | reviewed default | what it does |
| --- | --- | --- |
| `MILO_PROVIDER_LEASE_TTL_SECONDS` | **800** (was 120) | Nominal lease window. The request-deadline **ceiling** is derived from it: `800 − max(15, 0.25 × 800) = 600 s`. That equals the largest role deadline, and `assert_request_deadline_safe(600, 800)` holds exactly (600 + 200 ≤ 800). It reclaims nothing: no lease is ever freed on this clock. The ownership probe cadence becomes 800 / 4 = 200 s. |
| `MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS` | unset (derived: 600) | Explicit ceiling override. Leave it unset. |
| `MILO_MAX_RUN_DURATION_SECONDS` | 1800 (**unchanged**) | Checked before each call, so the last admitted call may run up to 660 s past it: 1800 + 660 = 2460 s, inside the 3600 s job timeout. This is now **enforced**: `runtime_policy` refuses a duration cap where cap + 660 ≥ 3600 (so ≥ 2940 s), and refuses a release whose reviewed lease window does not admit the longest role. The policy fingerprint is unchanged. |

**Do not set `MILO_PROVIDER_LEASE_TTL_SECONDS` below 800** or
`MILO_PROVIDER_REQUEST_TIMEOUT_SECONDS` below 600. The transport would clamp
every role deadline to the smaller ceiling (at a TTL of 120 that is 90 s, the
deadline that killed run 5145ca65). So a paid Swarm V2 run whose ceiling is
below the longest role deadline is **refused at worker boot** with
`PROVIDER_DEADLINE_BELOW_ROLE_POLICY`, before any provider call.

## Failure codes

A provider transport outcome no longer becomes `COMMANDER_COMPLETION_FAILED`,
`TASK_FAILED` or `SWARM_V2_EXECUTION_FAILED`. It reaches `run.error.code` and
the `run_failed` event, whose payload is `{code, role}` with `role` =
`<agent kind>:<phase>`. For a worker task, the code goes on the failed task's
result.

| code | meaning | lease |
| --- | --- | --- |
| `PROVIDER_REQUEST_DEADLINE_EXCEEDED` | the role's total deadline fired | quarantined |
| `PROVIDER_STREAM_INACTIVITY_TIMEOUT` | no chunk (or no headers) for 60 s | quarantined |
| `PROVIDER_STREAM_INTERRUPTED` | connection broke mid-stream, or the stream ended with no `finish_reason` | quarantined |
| `PROVIDER_CONNECTION_FAILED` | nothing was ever sent | released |
| `PROVIDER_HTTP_<status>` | the provider answered with an error (numeric status only, **never the body**) | released |

Scheduler verdicts keep their existing codes (`PROVIDER_BACKPRESSURE_EXCEEDED`,
quota exhaustion).

## Structured logs (Cloud Logging)

Each provider attempt writes one JSON line to stdout with
`"event":"provider_call"`. Query:

```
resource.type="cloud_run_job"
resource.labels.job_name="milo-agent-worker"
jsonPayload.event="provider_call"
```

Fields: `run_id`, `role`, `model`, `effort`, `cap`, `stream`,
`response_format`, `total_deadline_s`, `inactivity_s`,
`time_to_first_chunk_ms`, `total_ms`, `chunk_count`, `finish_reason`,
`prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`,
`usage_reported`, `outcome`, `completion_proven`, `code`, `exception_class`,
`cause_class`.

The line never carries a prompt, a completion, reasoning, a URL, a header, an
API key or exception **text**; exceptions are logged by class name only. A
worker agent's task id is dropped: `role` is `worker:execute`.

## The ownership probe (`provider_lease_probe_failed`)

It fired at the first probe of every run. Diagnosis: the probe script
`_LUA_VERIFY` was the **only** store script that parsed a held lease's score
in Lua, `tonumber(score)`. A held lease's score is `+inf`, which `ZSCORE`
returns as the string `"inf"`. Where the store's Lua `tonumber` does not parse
`"inf"`, the comparison is a script error on every probe of every held lease.
Acquire, held and recover leave scores to Redis's own range engine, and those
work in production.

Stock Redis 7 parses `"inf"`: both the old and new scripts were checked
against a local `redis-server` 7.0.15 and return 1 for a held lease. So this
diagnosis is the leading hypothesis for Upstash, **not a reproduction**.
The fix removes the dependency either way: the probe uses `ZRANGEBYSCORE key
(now +inf` and compares members, as `_LUA_HELD` does. It stays read-only and
observability-only.

To confirm on the next run, the probe now records **why** it failed. Both
`provider_lease_ownership_lost` and a stdout `"event":"provider_lease_probe"`
line carry `exception_class`, `cause_class` and a static `failure_kind`:

| `failure_kind` | meaning |
| --- | --- |
| `store_error_response` | the store refused the command, e.g. a Lua runtime error (text not carried) |
| `store_http_<status>` | the REST call returned an HTTP error |
| `store_transport` | the REST call failed in transport |

If it still fires after this release, `failure_kind` names the next thing to
look at.

## Reclaiming a quarantined lease

A quarantined lease is held **until a human returns it**; nothing reclaims it
on a clock. Each one is a Kimi concurrency slot MILO will not use. The one
tool that returns a lease is `scripts/release/provider_quota_leases.py`.
Background: [KIMI_TIER2_LIMITS.md](KIMI_TIER2_LIMITS.md), §8a.

1. **Find it.** The run's `provider_lease_quarantined` event carries the
   `lease_id` and `reason`. For run 5145ca65 that is `34d6697f5936441082384bae76b91276`,
   reason `PROVIDER_REQUEST_OUTCOME_UNKNOWN`. Then list what the shared store
   holds (read-only):

   ```
   UPSTASH_REDIS_REST_URL=… UPSTASH_REDIS_REST_TOKEN=… \
     python3 scripts/release/provider_quota_leases.py list
   ```

   Each row shows age and `recovery_eligible`.
2. **Wait for the floor.** A lease younger than 4500 s (the 3600 s worker task
   timeout plus margin) is refused (`LEASE_TOO_YOUNG`), because its process
   may still be alive.
3. **Verify provider-side completion.** Check the Kimi console for in-flight
   requests. MILO cannot check this, and the attestation flag records that you did.
4. **Recover one lease, by id:**

   ```
   python3 scripts/release/provider_quota_leases.py recover \
       --lease-id 34d6697f5936441082384bae76b91276 \
       --justification "run 5145ca65 planning read-timeout 2026-09-25 14:07Z; console shows 0 in-flight" \
       --i-have-verified-provider-side-completion
   ```

   Exit 0 = removed. Exit 2 = refused (`ATTESTATION_REQUIRED`,
   `REASON_REQUIRED`, `LEASE_TOO_YOUNG`, `LEASE_NOT_HELD`). Exit 1 = store
   unreachable. The store token is never printed.

PR-S does not change this procedure. It changes how often it is needed: a
reasoning call that is still producing chunks no longer ends in an unknown
outcome, and a streamed call that finishes releases its lease on proof.
