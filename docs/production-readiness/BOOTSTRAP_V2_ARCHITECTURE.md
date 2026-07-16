# MILO Production Bootstrap v2 — Transactional Architecture

Status: architecture of record for `scripts/release/bootstrap_v2/`.

## 1. Relationship to the previous bootstrap (PR #34)

**The architecture used by PR #34 (`claude/bootstrap-production-oneshot`) is
abandoned.** This implementation is a clean-room replacement built from the
clean `claude/production-readiness-j0hhni` base. No code from the PR #34
branch is copied, sourced, patched, imported, extended, or ported line by
line. The old implementation attempted to harden a monolithic Bash workflow
through scattered flags, counters, `|| true`, late audits, and local gates.
That approach is rejected as a category, not repaired.

Concepts that remain valid — exact Upstash database ID binding, exact live
Cloud Run validation, exact WIF validation, adoption of existing Secret
Manager resources, exact Redis secret-version pinning, Vercel project
identity verification, isolation from local `.env` overrides, atomic local
metadata writing, secret-leak tests, execution flags remaining `false`, and
distinct API / worker / gateway identities — are reimplemented inside the new
architecture. They are requirements, not code to transplant.

## 2. Design principles

1. **Typed state.** All run state lives in frozen (`frozen=True`,
   `slots=True`) dataclasses and enums defined in `model.py`. There are no
   mutable module-level failure flags and no competing blocker counters.
2. **A formal state machine.** `state_machine.py` owns the only legal stage
   order. Stages are monotonic; any critical failure transitions to
   `BLOCKED`, from which no later stage can start in the same run.
3. **Complete read-only global discovery.** Every provider (local identity,
   GCP, Upstash, Vercel) is fully inspected read-only *before* the first
   external mutation. No mutation is planned from partial evidence.
4. **A frozen mutation plan.** Discovery evidence feeds a pure planner that
   emits an immutable `MutationPlan`, serialized to canonical JSON and bound
   by `MILO_PLAN_DIGEST` (full lowercase SHA-256). Before apply, discovery is
   re-run, the plan is regenerated, and the digests must match exactly.
5. **Explicit stage boundaries.** Each stage ends in a `StageResult` passed
   through the single gate `require_stage_clean()`. Apply mode has no soft
   critical findings: MANUAL, UNKNOWN, WARN-critical, and UNVERIFIED all
   block.
6. **One authoritative result object.** A single immutable `RunResult`
   determines the process exit code, the human summary, the JSON report,
   metadata eligibility, and the workflow conclusion. Nothing else does.
7. **Zero later mutations after the first blocker.** The mutation ledger and
   the state machine jointly guarantee that once a blocker is recorded, no
   further mutation can execute. This is proven by a fault-injection test
   matrix asserting on ledger sequence numbers, not on the absence of one
   obvious command.
8. **Post-write verification after every individual write.** Every mutation
   is followed by an exact re-read of the mutated resource. A failed or
   unavailable re-read blocks before the next mutation.
9. **Honest resumability, not false atomicity.** Cross-provider rollback is
   never claimed. A failed run reports exactly which resources were created,
   marks the run `PARTIAL`, withholds metadata, and emits recovery steps. A
   rerun re-discovers and adopts correct partial state idempotently.

## 3. Component layout

```text
scripts/release/bootstrap_v2/
    __init__.py
    cli.py                  entry point; wires config, engine, report
    model.py                enums + frozen domain models
    result.py               RunResult construction and status derivation
    state_machine.py        monotonic stage machine + require_stage_clean
    planner.py              pure: evidence in -> desired state + MutationPlan
    policy.py               validated configuration, allowlists, bounds
    report.py               human summary + JSON report from one RunResult
    subprocess_runner.py    executes declared operations only; ledger
    adapters/
        gcp.py              gcloud-backed reads/writes, outcome classification
        upstash.py          Upstash REST API (in-memory auth header)
        vercel.py           Vercel REST API (in-memory auth header)
        github_provenance.py  artifact provenance verification
    validators/
        cloud_run.py        container selection, env, flags, budgets, pins
        iam.py              exact role/member/condition policy equality
        metadata.py         metadata v3 closed schema + atomic write
        redis.py            full-fingerprint Redis identity coherence
        wif.py              exact WIF pool/provider/issuer/audience/condition

scripts/release/bootstrap-production-v2.py   thin module launcher
scripts/release/bootstrap-production-v2.sh   exec-only Bash wrapper
```

Rules enforced by construction and by static tests:

- The Bash wrapper contains no provider command, no mutation, no parsing, no
  failure aggregation, no planning, no audit logic, and no credential
  handling. It only `exec`s the Python CLI.
- Provider adapters never import or call one another. Only the engine driven
  by the state machine coordinates provider operations.
- Validators are pure functions over typed inputs wherever possible.
- The planner is pure: evidence in, desired state and mutation plan out.

## 4. Domain model

Enums: `Mode` (PLAN / APPLY / AUDIT), `Severity` (PASS / INFO / WARN /
BLOCKED / UNVERIFIED), `RunStatus` (PLANNED / BLOCKED / PARTIAL / APPLIED /
AUDITED), `Stage` (see §5), `ProbeOutcome` (see §6).

Frozen models: `ResourceIdentity`, `Finding`, `Evidence`, `ReadOperation`,
`MutationOperation`, `MutationPlan`, `MutationRecord`,
`PostWriteVerification`, `StageResult`, `RunResult`, `RecoveryStep`,
`MetadataV3`, and provider-specific discovered-state models
(`UpstashDatabaseState`, `GcpServiceAccountState`, `SecretState`,
`IamPolicyState`, `WifState`, `CloudRunContainerState`,
`CloudRunResourceState`, `VercelProjectState`, `VercelEnvVarState`,
`RedisIdentity`, `LocalIdentityState`).

## 5. State machine

```text
INITIAL
  -> LOCAL_GUARD_VERIFIED
  -> GLOBAL_DISCOVERY_COMPLETE
  -> PLAN_FROZEN
  -> APPLY_AUTHORIZED
  -> UPSTASH_STAGE_VERIFIED
  -> GCP_IDENTITY_SECRET_STAGE_VERIFIED
  -> IAM_STAGE_VERIFIED
  -> CLOUD_RUN_STAGE_VERIFIED
  -> VERCEL_STAGE_VERIFIED
  -> FINAL_AUDIT_VERIFIED
  -> METADATA_COMMITTED
  -> COMPLETE
```

Any critical failure transitions immediately to `BLOCKED`. Once blocked, no
later stage can start, and the machine cannot recover from `BLOCKED` during
the same run. Recovery happens only through a fresh run with fresh
discovery.

`require_stage_clean(stage_result)` is the single mandatory gate. It blocks
when any of the following holds:

- a critical read failed;
- a required source was unavailable;
- evidence was missing, malformed, ambiguous, or stale;
- identity was not exact;
- a critical finding is WARN, MANUAL, UNKNOWN, or UNVERIFIED during apply;
- a mutation failed;
- post-write verification failed;
- live state drifted from the frozen plan;
- the mutation ledger contains an undeclared write.

## 6. Global read-only preflight

The complete preflight finishes before the first external mutation. Upstash
is never created before GCP and Vercel are inspected; no GCP service account
is created before Upstash and Vercel identity are known; WIF is inspected
before any IAM or Cloud Run mutation; Vercel forbidden-variable inventory is
taken before any GCP change.

Every provider read classifies its outcome as exactly one `ProbeOutcome`:
`PRESENT`, `CLEANLY_ABSENT`, `PERMISSION_DENIED`, `AUTH_FAILURE`,
`API_DISABLED`, `RATE_LIMITED`, `NETWORK_FAILURE`, `MALFORMED_OUTPUT`,
`TIMEOUT`, `UNKNOWN_ERROR`. **Only a positively identified not-found state
is absence.** Permission errors, network errors, and parser failures are
never treated as absence.

Preflight scope: local identity (repository, clean worktree, full 40-char
SHA, approved ref, environment, confirmation input, tooling, no deprecated
metadata keys, no `.env` influence); GCP (project, operator, APIs, Cloud Run
service and job state, Artifact Registry, three service accounts, Secret
Manager resources and enabled versions, per-secret IAM, project IAM red
flags, Cloud Run IAM, WIF pool/provider/issuer/audience/mapping/condition,
workloadIdentityUser principal set, run.invoker policy, write capability);
Upstash (auth, documented list response, exact DB ID or exact case-sensitive
name, no ambiguous duplicate, list/detail ID equality, production-safe name,
active, TLS true, region, canonical endpoint, create capability); Vercel
(exact project/org IDs and name, production environment, reused-variable
inventory, forbidden-variable inventory, managed-variable state, mutation
mechanism, isolation from caller environment and `.env` files).

Preflight produces one coherent intended Redis identity: database ID, exact
name, canonical REST URL, full token SHA-256 fingerprint (64 hex chars),
Secret Manager resource name, current exact enabled version, API and worker
secret pins, Vercel fingerprint, logical environment `production`. No field
may originate from a different database than the others.

## 7. Frozen mutation plan

Each `MutationOperation` carries: sequence number, provider, operation type,
exact resource identity, expected pre-state digest, intended post-state
digest, reason, idempotency key, cost flag, safe-compensation flag, and the
required post-write read. The plan serializes to canonical JSON;
`MILO_PLAN_DIGEST` is its full lowercase SHA-256. Before mutation, discovery
re-runs and the regenerated digest must equal the approved digest; any
security-sensitive drift blocks before mutation. The runner rejects any
mutating operation not declared in the frozen plan.

## 8. Stage ordering and per-stage rules

- **Stage A — Upstash.** At most one create, only when the frozen plan
  proves clean absence. No delete/reset/rename/token-reset/arbitrary
  selection. Post-create re-read must return the created ID exactly and
  verify name, state, TLS, region, endpoint, token presence, and canonical
  URL. On post-create verification failure: run is `PARTIAL`, the created ID
  is recorded, zero later mutations occur, no metadata is generated, rerun
  recovery instructions are emitted, and rollback is never claimed. A rerun
  adopts the created resource.
- **Stage B — GCP identities and secrets.** Create the three distinct
  service accounts (API, worker, gateway) and explicitly named secrets only
  when cleanly absent; add versions only when declared and required. Never
  create a service-account key. Worker identity and prerequisites complete
  before API configuration. Every create/version-add is re-read exactly and
  failure stops before the next mutation.
- **Stage C — IAM.** Only per-secret `roles/secretmanager.secretAccessor`
  for intended consumers, gateway `roles/iam.workloadIdentityUser` principal
  set, and API `roles/run.invoker` for the gateway service account. Policy
  equality covers exact role, exact member set, exact condition, expected
  binding count, and absence of broad or unrelated principals. Same-role
  bindings with different conditions are never merged. An unexpected
  accessor is blocking, not a warning. Writes are etag-aware; every write is
  re-read and drift blocks.
- **Stage D — Cloud Run.** Worker job first, then API service.
  Configuration update only: no image deploy/change, no job execution, no
  unauthenticated access, no traffic promotion. Verification is
  container-specific: the intended application container is identified
  explicitly and env vars are never flattened across containers. Blocks on
  ambiguous containers, duplicate env names, plain+secret conflicts, secret
  configured as plain text, wrong service account, wrong secret resource,
  nonnumeric or `latest` Redis pin, incorrect execution flags, and
  missing/invalid budgets. `MILO_RELEASE_SHA` is removed and verified absent
  from both resources. `MILO_BOOTSTRAP_SHA` is bootstrap reconciliation
  identity only and never claimed to prove the running image; image
  identity, when required, is verified independently via immutable digest or
  trusted provenance.
- **Stage E — Vercel.** Starts only after Upstash, GCP identity/secret,
  IAM, worker, and API stages are verified with zero global blockers.
  Before the first write: all required reused variables exist, no forbidden
  variable exists, exact project/org/name and production environment are
  proven. Managed variables are written one by one, each re-read and
  verified before the next. `deploy`, `promote`, `redeploy`, `link`,
  `unlink`, environment removal, and `--prod` are forbidden.

## 9. Metadata v3

`MILO_METADATA_SCHEMA_VERSION=3` is a closed schema: unknown, duplicate,
deprecated, and secret-looking keys are rejected, as are malformed UTF-8,
control characters, NUL, excessive size, symlinks, and non-regular files.
Metadata is non-secret, written to a private output directory as a 0600
temporary file, fsynced, atomically renamed, and written only after final
audit success. On any failure the candidate is removed, no success artifact
is uploaded, and the report records `metadata_status=withheld`.

## 10. Workflow trust

Production apply is not runnable from this development PR branch; it is
enabled only after merge to an explicit protected trusted ref. During PR
development, only offline/mocked CI runs. The workflow separates plan, apply
and audit jobs; checks the trusted ref before authentication; gates apply
behind a GitHub Environment; gives the plan job no write-capable production
credentials; uses job-level least permissions (`id-token: write` only where
GCP auth genuinely happens, `actions: read` only for provenance retrieval);
never uses `pull_request_target`; exposes no production secrets to PR jobs;
serializes apply runs; never lets audit cancel apply; pins all third-party
actions to immutable full commit SHAs with human version comments; and
strictly validates all inputs. Artifact provenance verifies trusted workflow
identity, code/ref, release ref, apply mode, successful conclusion, expected
repository, non-fork source, exact head SHA, plan digest, unique / non-
expired / expected-size artifact, safe extraction, exactly one regular
metadata file, no symlinks, and no extra files. The provenance verifier is
never controlled solely by the candidate workflow code it validates.

## 11. Credential and secret handling

No secret in argv, logs, report details, metadata, artifacts, or exception
messages. No raw application secret is duplicated into GitHub when Secret
Manager already owns it. No generated curl config with unescaped
credentials; no full token-bearing JSON through child-process environments.
API response sizes are capped. Secret input uses stdin or private 0600
temporary files inside a 0700 private temp directory with cleanup on
success, error, and signals. Token variables are cleared from long-lived
state after use. Redaction exists only as a backup safeguard. Upstash and
Vercel use a Python HTTP client with in-memory authorization headers.
Human-formatted CLI text is never parsed as authoritative identity when a
structured API or JSON output exists.

## 12. Explicitly forbidden architecture

The implementation must never contain or reproduce any of the following.
Machine-detectable items are enforced by static tests in
`tests/test_bootstrap_v2_workflow.py` and the bootstrap_v2 test suite.

1. A monolithic Bash script controlling discovery, planning, mutation,
   auditing and reporting.
2. `|| true` around mutation-capable functions.
3. `|| true` around critical evidence reads.
4. Continuing after a blocker.
5. Beginning Upstash creation before the global preflight is complete.
6. Running Vercel after a GCP blocker.
7. Running later Vercel writes after one Vercel write fails.
8. Performing IAM mutation after missing, partial or invalid WIF evidence.
9. Treating a permission error as not found.
10. Treating API/network/parser failure as absence.
11. Recording a critical apply prerequisite as MANUAL and continuing.
12. Multiple competing sources of failure truth.
13. Human CLI-output regex as the sole source of project identity.
14. Flattening environment variables from all Cloud Run containers.
15. Last-write-wins behavior for duplicate env names.
16. Accepting plain and secret definitions for the same variable.
17. Ignoring IAM conditions.
18. Accepting unexpected secret accessors as warnings.
19. Truncated Redis fingerprints.
20. `MILO_RELEASE_SHA` as bootstrap metadata.
21. Claiming bootstrap SHA proves the running image.
22. Open metadata schemas that accept unknown keys.
23. Trusting an artifact only from filename and numeric run ID.
24. Letting untrusted workflow code validate itself.
25. Mutable GitHub Action tags in a production credential path.
26. Writing `audited` before knowing the audit passed.
27. Writing `applied` before final live verification.
28. Generating metadata after a partial run.
29. Claiming cross-provider atomic rollback.
30. Deploying or promoting an image.
31. Executing the worker.
32. Calling Kimi or another model provider.
33. Enabling paid execution.
34. Enabling any execution flag.
35. Granting `allUsers` or `allAuthenticatedUsers`.
36. Project-wide Secret Manager accessor grants.
37. Service-account key creation.

## 13. One authoritative report

One immutable `RunResult` controls exit code, terminal output, JSON report,
metadata eligibility, and workflow success. Allowed final statuses:
`PLANNED`, `BLOCKED`, `PARTIAL`, `APPLIED`, `AUDITED`. A failed audit cannot
be `AUDITED`; a partial apply cannot be `APPLIED`; a metadata failure cannot
be `APPLIED`; unverified live state cannot be `APPLIED`; any blocker
produces a nonzero exit. The report includes mode, starting SHA, trusted
ref, plan digest, last completed stage, all findings, all reads, all
mutations with sequence numbers, post-write verification, resources created
before failure, recovery steps, metadata status, and final status.

## 14. Compatibility contract

Operator-facing names are preserved (GCP project, region, Cloud Run
service/job names, WIF pool/provider/issuer/audience/condition, Secret
Manager resource names, GitHub secrets and variables, script aliases, Cloud
Run plain env vars, execution flags, budget vars, Vercel reused / managed /
forbidden variables) exactly as documented in
`docs/production-readiness/MANUAL_SERVICE_CONNECTIONS.md` and `policy.py`.
Live values are never hardcoded into provider logic; they are validated
configuration with documented current values. The legacy runtime identity
(`id-kimi-agent-runner@…`) may be discovered and reported for migration but
is never silently accepted as both API and worker identity; three distinct
identities are required.
