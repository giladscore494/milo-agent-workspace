# The canonical runtime policy

**Authority:** `backend/runtime_policy.py`
**Schema:** `milo-runtime-policy/1`
**Print it:** `python3 scripts/release/stage-d/policy_envelope.py document`

## What problem this solves

A run's enforceable limits used to be written down in five places that were
maintained by hand and were free to disagree:

| Surface | What it said |
|---|---|
| `backend/tier2_profile.py` | 23 tasks, 56 agent steps, 24 tool calls, 1 replan, $3.00 — and enforced nothing |
| `backend/budget.py` | enforced model calls, tokens, cost, duration; required **five** of them for paid execution, not including `max_agent_steps` or the recorded-cost cap |
| `backend/engines/swarm_v2/validation.py` | admitted plans against `PlanLimits` defaults of **64 tasks, 3 replans, 100 tool calls** |
| `scripts/release/stage-d/stage-d-env.sh` | its own transcription of the whole envelope, including `MILO_PROVIDER_RPM_LIMIT=350` |
| `scripts/release/swarm-v2-smoke/parse_env_contract.py` | the spent smoke envelope: 200 calls, $4.00, 900,000 tokens, 8 workers |

Two of those disagreements were not theoretical. The plan firewall really did
admit 3 replans and 100 tool calls into a profile that advertised 1 and 24.
And `ProviderLimitsConfig.assert_within_organization_ceiling` really does
refuse `MILO_PROVIDER_RPM_LIMIT=350` against MILO's 80 RPM ceiling, so the
pinned Stage D posture could not have started a worker at all.

That is one architectural defect, not five bugs: **documented policy, profile,
deployment configuration and engine limits could describe different effective
safety envelopes.**

## The rule

> A deployment may **tighten** any reviewed limit.
> It may never **widen** one silently.
> In the **paid** posture, absent, unparseable, non-positive and
> wider-than-reviewed all **fail closed**.

The rule binds exactly where money can be spent. An unpaid deployment cannot
make a provider call at all — `BudgetTracker`'s kill switch refuses every one
while `MILO_ENABLE_PAID_EXECUTION` is off — so a wider value there is
**recorded** on the resolved policy (`relaxed`, and in its document) rather
than refused, and the same configuration is refused the moment paid execution
is armed. The zero-cost staging stack really does carry a $5.00 daily budget
and a $0.001 per-call reservation rate; narrowing it would protect nothing
and break something.

## Scope: the deployment, not the run's engine

One worker image serves both engines and the environment is per-deployment, so
a Cloud Run job carrying a Swarm V2 width wider than the reviewed one is
misconfigured even while it happens to be executing a V1 run — the next run on
the same job may be V2. **Every dimension is therefore resolved and validated
for every deployment**, the mandatory-for-paid set is engine-independent, and
no dimension carries engine-scope metadata. What a dimension binds is stated by
the surface that enforces it (`enforced_by`) and by its own name
(`v1_technical_parallelism` / `v2_max_active_workers`).

## How a dimension is declared

Each dimension is declared exactly once, in `POLICY_DIMENSIONS`, with:

* **`reviewed`** — the value the controlled first paid run was authorized at.
  A *ceiling* on what a deployment may configure, never a default it inherits.
* **`runtime_default`** — what the code really uses when nothing is set. This
  is what makes the mandatory set **derived** rather than declared: a
  dimension must be configured explicitly for paid execution exactly when
  leaving it out would let the runtime operate wider than the reviewed value.
  `PlanLimits.max_replans` defaults to 3 against a reviewed 1, so it is
  mandatory. `MILO_V1_TECHNICAL_PARALLELISM` defaults to 1 against a reviewed
  4, so it is not. Nobody maintains that list, so it cannot fall behind the
  profile again.
* **`direction`** — which way "tighter" runs. Most dimensions are upper
  bounds, so lower is tighter. `estimated_cost_per_call` is not: a *smaller*
  reservation rate admits *more* calls before the estimated-cost cap trips.
  The provider backoff values are declared but not bounded in either
  direction — the number of attempts and the maximum stall are already
  bounded by other dimensions, so a shorter backoff cannot widen anything.

## Who consumes it

```
                     backend/runtime_policy.py
                               │
   ┌──────────┬────────────┬───┴────┬─────────────┬──────────────┐
   │          │            │        │             │              │
BudgetConfig  PlanLimits  Provider  production_   tier2_profile  stage-d/
(budget.py)   (swarm_v2)  LimitsCfg config.py     (the document) policy_envelope.py
   │          │    │                                             │
BudgetTracker │  ModelGateway (what the model is told)      stage-d-env.sh
              │  PlanValidator (what the firewall enforces)  verify_caps.py
              └─ feasibility (what the run can pay for)
```

The Swarm V2 requirement is literal: **one** `PlanLimits` instance, built by
`policy.plan_limits()`, is passed to `ModelGateway` (provider-visible planning
policy), `PlanValidator` (deterministic firewall) and the feasibility gate. A
policy of `max_replans = 1` makes two replans impossible; a policy of
`max_tool_calls = 24` makes a 25-call plan impossible.

## Where configuration is refused

| When | What refuses | Code |
|---|---|---|
| Startup / operator check | `backend/production_config.py` | `POLICY_DIMENSION_ABSENT`, `POLICY_WIDER_THAN_REVIEWED`, `POLICY_VALUE_INVALID`, `POLICY_INVARIANT_VIOLATED`, `POLICY_CATALOG_POSTURE_CONTRADICTORY` |
| Worker, before a run executes | `backend/worker/main.py` | `BUDGET_CONFIG_INVALID`, `PROVIDER_LIMITS_CONFIG_INVALID`, `RUNTIME_POLICY_INVALID` |
| Release toolkit, before a run is created | `scripts/release/stage-d/verify_caps.py` | refuses on any disagreement with the policy, including the policy fingerprint |

Catalog posture is part of the policy: arming canonical promotion for data a
deployment is not allowed to **read** is a contradiction, and it is now
refused during configuration validation rather than at worker construction,
after a run has been created and a lease acquired.

## Stage D, and the release binding

Generating the envelope from the policy removes the second transcription but
introduces a different way to be wrong: the generator reads the **local
checkout**, while Stage D verifies separately pinned release **image digests**.
Comparing a checkout against itself proves nothing about what the running
images enforce. So the policy is bound to the release twice, and both bindings
fail closed:

1. `PINNED_POLICY_FINGERPRINT` in `policy_envelope.py` is a **literal reviewed
   constant**, changed in a reviewed commit exactly like an image digest. Every
   selector refuses unless the checkout's policy digest matches it, so a
   drifted checkout cannot even print a pin. CI fails if the two diverge.
2. `release_binding_problems()` proves the checkout **is** the accepted
   release: `HEAD` equals `STAGE_D_RELEASE_SHA` and the policy source is
   unmodified. `verify_caps.py` and both step scripts run it before any run is
   created. Being unable to prove it — no git metadata, a shallow clone, an
   unreadable tree — is a refusal, never a pass.

With `STAGE_D_RELEASE_SHA` still naming the pre-policy release, Stage D now
**refuses**, which is the correct executable form of the supersession
`STAGE_D_AUTHORIZATION.md` already documents in prose.

The number of new paid worker executions Stage D will accept is the policy's
`first_paid_run_execution_cap`, read by `verify_executions.py --baseline`;
the shell no longer computes `baseline + 1` on its own.

`stage-d-env.sh` no longer transcribes anything. It generates
`STAGE_D_CAPS`, `STAGE_D_WORKER_PROVIDER_LIMITS`,
`STAGE_D_WORKER_ENGINE_LIMITS` and `STAGE_D_POLICY_FINGERPRINT` from the
policy, and `verify_caps.py` re-derives all of them and refuses the run if
what it was handed disagrees. Verifying the live environment against a
transcription only ever proved that the deployment matched the transcription.

## What this does NOT do

It does not track consumption. It answers *"what is the authoritative
policy?"*. How usage is accounted for durably against that policy, and how a
resume may never refund it, is a separate concern.
