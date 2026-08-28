/**
 * The authoritative aggregate usage contract for a run.
 *
 * `run.usage` mirrors `BudgetTracker.snapshot()` (backend/budget.py) and is the
 * ONLY authority for model calls, tokens, cost, retries and backpressure. It is
 * written to `runs.usage` (migration 010) after every settled provider call.
 *
 * Usage is NEVER reconstructed by summing events. One logical Swarm task can
 * produce an initial worker model call, one bounded worker-output repair (B3)
 * and one or more verifier batch calls (B4/B5), so
 *
 *     task count != model call count != agent count
 *
 * and any event-derived total would be wrong by construction.
 *
 * CONTRACT GAP (documented, not worked around): `GET /runs/{id}` currently
 * returns `backend.schemas.Run`, which does not declare `usage`. Pydantic drops
 * undeclared keys, so the browser receives no `usage` field today even though
 * the column is populated. Every field below is therefore optional and the
 * normalized view reports `present: false` rather than inventing a zero.
 */

export type RunUsage = {
  model_calls?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  estimated_cost?: number | null;
  actual_cost?: number | null;
  retries?: number | null;
  provider_backpressure_events?: number | null;
  /** Backend-counted model-backed steps; NOT a count of UI agents. */
  agent_steps?: number | null;
  elapsed_seconds?: number | null;
};

export type NormalizedRunUsage = {
  present: boolean;
  modelCalls?: number;
  inputTokens?: number;
  outputTokens?: number;
  totalTokens?: number;
  estimatedCost?: number;
  actualCost?: number;
  retries?: number;
  providerBackpressureEvents?: number;
  agentSteps?: number;
  elapsedSeconds?: number;
};

export const EMPTY_RUN_USAGE: NormalizedRunUsage = { present: false };

function finiteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * Read `run.usage` defensively: a missing object, a missing field, `null` and a
 * non-numeric value all become `undefined`. Absent is never rendered as zero,
 * because "no usage reported yet" and "zero model calls" are different facts.
 */
export function normalizeRunUsage(usage?: RunUsage | null): NormalizedRunUsage {
  if (!usage || typeof usage !== 'object') return EMPTY_RUN_USAGE;
  const normalized: NormalizedRunUsage = {
    present: true,
    modelCalls: finiteNumber(usage.model_calls),
    inputTokens: finiteNumber(usage.input_tokens),
    outputTokens: finiteNumber(usage.output_tokens),
    totalTokens: finiteNumber(usage.total_tokens),
    estimatedCost: finiteNumber(usage.estimated_cost),
    actualCost: finiteNumber(usage.actual_cost),
    retries: finiteNumber(usage.retries),
    providerBackpressureEvents: finiteNumber(usage.provider_backpressure_events),
    agentSteps: finiteNumber(usage.agent_steps),
    elapsedSeconds: finiteNumber(usage.elapsed_seconds),
  };
  return normalized;
}
