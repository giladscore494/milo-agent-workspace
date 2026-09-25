# Reasoning-aware model contract and worst-case cost reservation (PR-R)

Source: `MILO_V2_REASONING_BUDGET_PR_SPEC.md` (PR-R), base `main@f641658`.
Scope: Swarm V2 cost control and model contract. V1 behaviour is unchanged
except the kimi-k2.6 price correction.

## Why

Run `4761a8ce` failed with `COMMANDER_COMPLETION_SHAPE_INVALID`. The Commander
had a 4,000-token output cap, V2 sent no thinking setting, and kimi-k2.6
reasoned through the whole cap. It returned an empty answer with
`finish_reason=length`. That code was not repairable, so the run died after
10,578 tokens (6,578 in, 4,000 out).

The cost controls assumed every output token was answer. For a reasoning
model that is false, and a small per-call cap hurts quality without
protecting money. PR-R moves the hard bound to money per run and per day.
It reserves the **worst case** of each call before sending it, so each call
can have a large cap.

## Verified provider facts (2026-09-25)

| Fact | Finding | Source |
|---|---|---|
| K3 output-cap field | `max_completion_tokens` (default 131072, max 1048576). `max_tokens` is deprecated with the same meaning | Kimi K3 Quickstart; Create Chat Completion |
| Reasoning counts toward the cap | Yes: `reasoning_content` + `content` must fit inside the cap. Hitting it gives `finish_reason="length"` | Thinking Models |
| Cached-token usage field | `usage.prompt_tokens_details.cached_tokens`, also sent at the top level as `usage.cached_tokens`; plus `prompt_tokens_details.cache_write_tokens` | Context Caching |
| Reasoning-token usage field | **Not documented officially.** `completion_tokens_details.reasoning_tokens` is read when present. Otherwise the reasoning share is estimated and the diagnostic `USAGE_REASONING_FIELD_ABSENT` is emitted once per run | Third-party guide only |
| `json_schema` + `strict` with thinking | Supported on K3. It constrains `message.content` only | Kimi K3 Quickstart |
| K3 prices / 1M | miss 3.00, hit 0.30, cache write 3.00 (5-min TTL) / 6.00 (1-hour TTL), output 15.00 | operator-verified pricing page, 2026-09-25 |
| k2.6 prices / 1M | 0.95 / 0.16 / 4.00, no separate cache-write charge | operator-verified pricing page, 2026-09-25 |
| K3 multi-turn | K3 expects `reasoning_content` passed back in multi-turn calls. V2 is single-turn, and a repair carries only the static reason code, so nothing is passed back | Kimi-K3 README section 6 |

## Components

| Concern | Where |
|---|---|
| Model profiles (fail-closed) | `backend/model_profiles.py` |
| Usage breakdown (counts only) | `backend/model_usage.py` |
| Request builder | `backend/engines/swarm_v2/request_builder.py` |
| Role policies, call contract | `backend/engines/swarm_v2/model_gateway.py` (`ROLE_POLICIES`), `backend/budget.py` (`CallContract`) |
| Worst-case reservation | `backend/budget.py` (`BudgetTracker.open_call`) |
| Completion classification and repair | `backend/engines/swarm_v2/completion.py` |
| Per-call ledger columns | `supabase/migrations/20260925000100_reasoning_aware_usage.sql` |

### Role policies

| Role | Effort | `max_output` | `min_answer_reserve` | Structured output |
|---|---|---|---|---|
| commander:planning | high | 32,000 | 6,000 | json_object (strict json_schema gated, see below) |
| commander:replanning | high | 16,000 | 3,000 | json_object |
| worker:execute | low | 12,000 | 3,000 | json_object (strict json_schema gated, see below) |
| verifier:verification | high | 24,000 | 4,000 | json_object (strict json_schema gated, see below) |

Structured output is `json_object` for every role today. The strict
`json_schema` path is still in the builder, but a request uses it only when
all three hold: the model's profile lists `json_schema`, the profile's
`strict_json_schema_verified` is set (strict output proven against the live
provider), and the role's schema passes `is_strict_compatible` (every property
required and `additionalProperties: false` at every depth). kimi-k3 is
`json_object` only. The CommanderPlan and verifier-batch schemas are not yet
strict-compatible, so even a verified profile falls back to `json_object` for
them.

These are starting values. Calibrate them from `reasoning_tokens` (or
`reasoning_tokens_estimated`) after 2–3 runs.

### Worst-case reservation

```
reserve = input_upper_bound × max(price_input_miss, price_cache_write_5m)
        + granted_output_cap × price_output          (reasoning included)
```

`input_upper_bound` is the byte-based conservative bound, never an average.
The reserve is checked against every remaining dollar ceiling before anything
is sent:

- the run: `max_cost_per_run − actual − in-flight reserves`;
- the user's day;
- the project's day.

It is also the amount held by the atomic daily reservation and written as the
per-call ledger `reserved` row. At settlement the actual cost replaces it.
V1 keeps its flat `estimated_cost_per_call` reservation.

## Reason codes

| Code | When | Terminal? |
|---|---|---|
| `MODEL_PROFILE_UNKNOWN` | A model with no registered profile: at paid worker boot, in configuration validation, at the gateway, or at the guarded client. Refused before any HTTP request | run fails |
| `MODEL_NOT_ALLOWLISTED` | Commander or worker model not on `MILO_COMMANDER_MODEL_ALLOWLIST` | run refused at boot |
| `SWARM_MODEL_CONFIG_INVALID` | Swarm V2 model env incomplete | run refused at boot |
| `MODEL_PARAM_FORBIDDEN` | A parameter the model forbids (K3: temperature, top_p, n, presence_penalty, frequency_penalty, thinking) or any parameter the builder does not own | run fails |
| `MODEL_EFFORT_UNSUPPORTED` | Effort not supported by the model (for example `none` on K3) | run fails |
| `MODEL_MESSAGE_INVALID` | A message carries a field other than role/content (for example `reasoning_content`) | run fails |
| `MODEL_RESPONSE_FORMAT_UNSUPPORTED` / `MODEL_OUTPUT_CAP_INVALID` | Builder contract violations | run fails |
| `BUDGET_INSUFFICIENT_FOR_ROLE` | The remaining token budget cannot grant the role's `min_answer_reserve`. Refused, never silently shrunk | budget_exhausted |
| `OUTPUT_CAP_REDUCED_BY_BUDGET` | Log line only: the cap was reduced but stayed at or above the reserve | no |
| `COST_RESERVATION_EXCEEDED` | The call's worst-case cost exceeds a remaining run or daily ceiling | budget_exhausted |
| `MODEL_REASONING_EXHAUSTED_OUTPUT` | `finish_reason=length`, empty answer. Repaired once by escalation | after the repair |
| `MODEL_OUTPUT_TRUNCATED` | `finish_reason=length`, partial answer. Repaired once by escalation | after the repair |
| `MODEL_EMPTY_COMPLETION` | Normal finish, empty answer. Not repaired | yes (commander) / task fails (worker) |
| `USAGE_REASONING_FIELD_ABSENT` | Diagnostic (`model_usage_diagnostic` event), once per run | no |

Escalation doubles the cap, up to `max_output`, when the failed call ran below
it. Otherwise it lowers the effort one notch: K3 goes high → low; k2.6 goes
thinking → disabled. With nothing to escalate there is no repair call. The
repair is an ordinary guarded call, so its own worst-case reservation must
pass.

## Worker environment (Stage E)

The model contract is set on the **worker** job only:

```
MILO_COMMANDER_MODEL=kimi-k3
MILO_COMMANDER_MODEL_ALLOWLIST=kimi-k3,kimi-k2.6
MILO_SWARM_WORKER_MODEL=kimi-k2.6
```

The reviewed RuntimePolicy values that change come from
`backend/runtime_policy.py`, like every other cap:

```
MILO_MAX_COST_PER_RUN=3.00
MILO_DAILY_USER_BUDGET=10.00
MILO_DAILY_PROJECT_BUDGET=10.00
MILO_MAX_OUTPUT_TOKENS_PER_RUN=400000
MILO_MAX_TOTAL_TOKENS_PER_RUN=900000
```

V1 is unchanged: its engine pins `kimi-k2.6` with thinking disabled.
Verify the posture with
`bash scripts/release/check-production-config.sh --env-file <worker.env>`
(check `model-contract`).

## Rollback

1. Revert the PR-R merge commit and deploy the previous release.
2. Restore the worker env: `MILO_COMMANDER_MODEL=kimi-k2.6`,
   `MILO_COMMANDER_MODEL_ALLOWLIST=kimi-k2.6`,
   `MILO_SWARM_WORKER_MODEL=kimi-k2.6`, and the previous caps
   (`MILO_MAX_COST_PER_RUN=1.00`, `MILO_DAILY_*_BUDGET=4.00`,
   `MILO_MAX_OUTPUT_TOKENS_PER_RUN=120000`, `MILO_MAX_TOTAL_TOKENS_PER_RUN=600000`).
3. The migration is additive and stays applied. The restated
   `append_usage_ledger_guarded` accepts the reverted release's entries, which
   carry none of the new keys, so the new nullable columns simply stay NULL.
4. A run's identity pins the policy and event-registry fingerprints, so the
   reverted release will not claim a run created under PR-R. Let in-flight
   runs reach a terminal state before rolling back.

## Work plan — Revision 2.3

The work plan document itself is not kept in this repository. Revision 2.3
is recorded here so the operator can transcribe it.

- **2.3-a (PR-R, this change):** the reasoning-aware model contract and
  worst-case cost reservation, as above. No deploy and no env change are part
  of the PR.
- **2.3-b:** deploy the worker with the Stage E env above, then pass the
  gates `deployed` → `armed` → `active`.
- **2.3-c:** retry batch 1. On the first live run, read
  `model_usage_diagnostic`: if `USAGE_REASONING_FIELD_ABSENT` appears,
  reasoning is being estimated, so calibrate against
  `reasoning_tokens_estimated`.
- **2.3-d:** after 2–3 runs, recalibrate `ROLE_POLICIES` (caps and reserves)
  and the run and daily dollar ceilings from the recorded breakdown.
  Consider K3 for the worker only after measurements exist.
