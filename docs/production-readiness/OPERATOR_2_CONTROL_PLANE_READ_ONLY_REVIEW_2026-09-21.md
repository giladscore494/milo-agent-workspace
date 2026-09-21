# Operator 2 — Control-plane Production readiness, read-only review

**Date:** 2026-09-21
**Scope:** Production Supabase project, inspected through the read-only
connector. **Every statement below comes from a `SELECT`.** No migration was
applied, no row was written, no flag was changed, no image was built or
deployed, and no paid provider call was made.

This is a fresh review built from CURRENT main after Consoles 1–5, not a
re-reading of the Stage C/D list. It exists because that list had fallen behind
the runtime: the preflight required six RPCs while the runtime depended on
forty-six.

---

## 1. Required migration / RPC inventory

The inventory is no longer hand-written. `scripts/release/release_inventory.py`
derives it from two facts about the repository as it is: every PostgREST RPC
name the runtime actually calls, and every function the migrations create with
the arguments each one requires.

**Derived from current main:** 46 required RPCs across 19 migrations.
**Recorded as applied in Production:** migrations `001` … `20260916120000`.

### BLOCKING — four migrations the runtime depends on are not applied

| Migration | Console | Runtime objects Production is missing |
|---|---|---|
| `20260920000100_execution_usage_ledger` | 2 | `run_execution_usage` table, `merge_execution_usage`, `record_run_usage_guarded` |
| `20260920000200_atomic_run_finalization` | 3 | `finalize_run_guarded` |
| `20260921000100_current_verdict_authority` | 4 | `claim_current_verdict_state`, `claim_current_verdict_states` |
| `20260921000200_immutable_run_identity` | 6 (this PR) | `runs.run_identity`, `bind_run_identity`, `create_tool_access_request_guarded`, `create_tool_grant_guarded`, `append_usage_ledger_guarded` |

Verified absent by direct `pg_proc` / `information_schema` reads, not inferred
from the migration history alone.

This is exactly the failure mode the rebuilt inventory exists to catch. A paid
run against Production as it stands today would reach `record_run_usage_guarded`
on its first recorded consumption and `finalize_run_guarded` on its terminal
write, and find neither — **after** the money was spent. The Stage D preflight
now refuses before the authorized run is created rather than discovering it
during one.

**Not a mutation this review may perform.** Applying migrations to Production is
a separate authorized action.

### Present and correctly scoped

Every guarded RPC that IS deployed carries the expected service-only ACL —
`anon` and `authenticated` hold no EXECUTE, `service_role` does:
`assert_worker_lease`, `claim_run_lease`, `heartbeat_run_guarded`,
`transition_run_worker_guarded`, `append_run_event_guarded`,
`save_checkpoint_guarded`, `update_run_usage_guarded`,
`record_claim_verdict_guarded`, `record_conflict_resolution_guarded`,
`record_evidence_fragment_guarded`, `settle_model_call_budget`.

---

## 2. Service-role / browser-role ACLs and RLS posture

All 36 public tables have RLS enabled. `anon` holds **no** SELECT and **no**
write privilege on any table. `authenticated` holds write privileges on exactly
two tables — `conversations` and `workflow_proposals` — and both are
policy-guarded (2 and 3 policies respectively). Every service-only table has
RLS on and zero policies, which is the intended "no browser access at all"
posture rather than an oversight.

### Generic Supabase advisories — separated deliberately

Two findings are the standard advisory shapes, and neither is an exploitable
path here:

* **`stuck_runs` is a SECURITY DEFINER view** (no `security_invoker=true`).
  `anon` and `authenticated` hold no SELECT on it; only `service_role` does. It
  aggregates `runs` for operator diagnostics and exposes nothing a browser role
  could reach.
* **Seven SECURITY DEFINER functions exist.** Every one of them pins
  `search_path` — the query for functions with a mutable search path returned
  **zero rows** — and all but one are `service_role`-only. The exception,
  `rls_auto_enable`, is executable by browser roles but returns `event_trigger`
  and is bound to an event trigger: PostgREST cannot expose it and it cannot be
  invoked with `SELECT`.

**Concrete exploitable paths found: none.**

---

## 3. Execution flags and default-off posture

Execution flags live in the Cloud Run environment, not in Supabase, so this
review verifies the repository side and the durable consequences:

* `scripts/check_unsafe_defaults.py` passes: no execution flag is committed as
  enabled anywhere in tracked source, Docker, CI or deployment files.
* `bash scripts/release/production-readiness.sh` → `RESULT: OK`, 31 PASS,
  2 WARN, **0 BLOCKED**, 18 MANUAL.

---

## 4. Catalog promotion remains OFF

Verified by durable state, not only by configuration:

| Table | Rows |
|---|---|
| `catalog_source_snapshots` | 0 |
| `catalog_raw_records` | 0 |
| `catalog_candidate_variants` | 0 |
| `catalog_canonical_variant_current` | 0 |
| `catalog_canonical_field_current` | 0 |
| `catalog_canonical_field_provenance` | 0 |

Canonical promotion has never run in Production. The catalog schema exists and
holds nothing, so there is no snapshot for a chat run to read even if the flag
were armed.

---

## 5. Canonical RuntimePolicy alignment

The reviewed policy document's fingerprint is
`7ffc0f6d36220ecdd773955ef4e89289d804fa86e2e279ddd93a6bd6c37ed52b`, unchanged
by this console: `backend/runtime_policy.py` is untouched, and
`policy_envelope.PINNED_POLICY_FINGERPRINT` still matches the checkout.

---

## 6. Immutable run identity

`runs.run_identity` does **not** exist in Production (migration not applied).
All 8 existing runs are therefore unpinned, which is the state this release
treats as "created before identities existed": readable and resumable through
the legacy project route, **not** exportable and **not** release-authorizable.
Nothing defaults them to V1.

---

## 7. Fenced mutation paths

Production currently satisfies the fencing contract for every guarded RPC it
has. The three writers this console adds — tool access requests, tool grants
and the per-call usage ledger row — are still unfenced in Production because
their migration is not applied. Their current durable footprint is zero:

| Table | Rows |
|---|---|
| `tool_access_requests` | 0 |
| `tool_grants` | 0 |
| `sources` | 0 |
| `claim_verdicts` | 0 |
| `run_usage_ledger` | 465 |

The ledger rows are the recorded spend of the eight historical runs.

---

## 8. Run baseline and terminal state

Eight runs, **all terminal** (zero non-terminal). 179 run events.

### BLOCKING for Stage D — the pinned prior-run baseline is stale

`stage-d-env.sh` pins `STAGE_D_EXPECTED_PRIOR_RUNS=7`; Production holds **8**.
The eighth is `3772fc84-420c-4a66-9e79-d58649d4e9b4`
(`stage-d-expansion-1-20260918-01`, `timed_out`), created after the pin.

The preflight will refuse until the pin is updated in a reviewed commit. That
refusal is correct fail-closed behaviour and **this PR deliberately does not
change it**: the prior-run baseline is an authorization decision, not an
inventory fact, and re-pinning it is part of authorizing a paid run.

The prepared Government capture run `555101dc-46f6-4048-bd67-efccbc98f528` is
`cancelled` / `launch_state=none` — the "retired" posture the probe accepts —
and carries no worker trace.

---

## 9. Accepted release / image binding

Not re-verified here: `verify_images.py` needs `gcloud`, which this review does
not have, and the audit correctly degrades those checks to MANUAL rather than
reporting them as passed. Image-digest verification is **unchanged and not
weakened** by this console; what is added is the missing final link — the
authorized run's persisted identity must carry the accepted release SHA and the
reviewed policy fingerprint, or the evidence gate refuses.

`MILO_RELEASE_SHA` must now be present and correct on **both** runtime surfaces
before a run is created; `verify_caps.py` refuses a deployment that states no
release or the wrong one.

---

## Verdict

| Question | Answer |
|---|---|
| Required migration/RPC inventory satisfied? | **NO** — four runtime-required migrations unapplied |
| ACL / RLS posture sound? | YES |
| Concrete exploitable path found? | NO |
| Catalog promotion off? | YES (and never run) |
| Policy alignment? | YES |
| Run identity present? | NO — migration unapplied; all 8 runs unpinned |
| Stage D prior-run baseline valid? | **NO** — pinned 7, actual 8 |
| Safe for a paid run today? | **NO** |

A green CI run says the code is consistent with itself. It does not say the
deployed database carries the objects that code calls, and here it provably
does not.
