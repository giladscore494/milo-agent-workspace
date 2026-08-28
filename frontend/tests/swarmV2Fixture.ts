/**
 * The accepted Swarm V2 production-smoke run, as a deterministic fixture.
 *
 * Five logical tasks, seven model calls. Those two numbers are different for a
 * real reason: one logical task can spend an initial worker model call, one
 * bounded repair call (B3) and share verifier batch calls (B4/B5). Any code
 * that renders "7 agents" or "7 tasks" from this fixture is a regression.
 */

import { RunEvent } from '../lib/types';
import { RunUsage } from '../lib/runUsage';

export const SMOKE_RUN_ID = 'a1b2c3d4-1111-4111-8111-000000000001';

export const SMOKE_TASK_IDS = [
  'catalog_health',
  'compile_report',
  'env_check',
  'get_details',
  'list_catalog',
] as const;

/** Exactly the aggregate the accepted smoke run reported. */
export const SMOKE_USAGE: RunUsage = {
  model_calls: 7,
  input_tokens: 7_120,
  output_tokens: 3_250,
  total_tokens: 10_370,
  estimated_cost: 0.02,
  actual_cost: 0.019178,
  retries: 0,
  provider_backpressure_events: 0,
  agent_steps: 7,
  elapsed_seconds: 41.5,
};

let sequence = 0;

export function resetSmokeSequence(start = 0) {
  sequence = start;
}

/**
 * Build an event exactly as the public path delivers it for Swarm V2: the
 * engine's `_emit` allowlist has no `agent` key, so `agent` is null and the
 * logical task can only be read from `payload.task_id`.
 */
export function swarmEvent(
  eventType: string,
  payload: Record<string, unknown> = {},
  id?: string,
): RunEvent {
  sequence += 1;
  return {
    id: id ?? String(sequence),
    run_id: SMOKE_RUN_ID,
    event_type: eventType,
    message: eventType,
    payload,
    agent: undefined,
    phase: undefined,
  };
}

/**
 * The full accepted smoke sequence: plan, five logical tasks executed with two
 * genuinely concurrent, one bounded worker repair, grounded verification in two
 * batches, and a completed run.
 */
export function smokeEventStream(): RunEvent[] {
  resetSmokeSequence();
  const events: RunEvent[] = [
    swarmEvent('run_created', { launcher: 'cloud_run_jobs' }),
    swarmEvent('run_started', { worker_id: 'worker-1', attempt: 1 }),
    swarmEvent('commander_plan_created', { graph_revision: 1 }),
  ];

  // env_check and list_catalog have no dependencies and run at the same time.
  events.push(swarmEvent('task_ready', { task_id: 'env_check' }));
  events.push(swarmEvent('task_started', { task_id: 'env_check' }));
  events.push(swarmEvent('task_ready', { task_id: 'list_catalog' }));
  events.push(swarmEvent('task_started', { task_id: 'list_catalog' }));
  events.push(swarmEvent('task_completed', { task_id: 'env_check', status: 'completed' }));
  events.push(
    swarmEvent('tool_called', { task_id: 'list_catalog', tool: 'catalog_lookup' }),
  );
  events.push(swarmEvent('task_completed', { task_id: 'list_catalog', status: 'completed' }));

  events.push(swarmEvent('task_ready', { task_id: 'get_details' }));
  events.push(swarmEvent('task_started', { task_id: 'get_details' }));
  // B3: one bounded repair of a structurally invalid completion. It is an
  // extra MODEL CALL on the same logical task, not a new task or agent.
  events.push(
    swarmEvent('worker_output_repair_started', {
      task_id: 'get_details',
      reason_code: 'SCHEMA_INVALID',
      attempt_number: 1,
    }),
  );
  events.push(swarmEvent('task_completed', { task_id: 'get_details', status: 'completed' }));

  events.push(swarmEvent('task_ready', { task_id: 'catalog_health' }));
  events.push(swarmEvent('task_started', { task_id: 'catalog_health' }));
  events.push(swarmEvent('task_completed', { task_id: 'catalog_health', status: 'completed' }));

  events.push(swarmEvent('task_ready', { task_id: 'compile_report' }));
  events.push(swarmEvent('task_started', { task_id: 'compile_report' }));
  events.push(swarmEvent('task_completed', { task_id: 'compile_report', status: 'completed' }));

  events.push(
    swarmEvent('evidence_added', {
      claim_id: 'claim-1',
      source_id: 'source-1',
      task_id: 'list_catalog',
    }),
  );
  events.push(
    swarmEvent('evidence_added', {
      claim_id: 'claim-2',
      source_id: 'source-2',
      task_id: 'get_details',
    }),
  );

  // B5 grounding, then B4 batching: two verifier calls, one verification.
  events.push(
    swarmEvent('grounding_context_resolved', {
      claim_count: 2,
      source_count: 2,
      missing_context_count: 0,
    }),
  );
  events.push(
    swarmEvent('verification_batch_completed', {
      batch_index: 1,
      batch_count: 2,
      claim_count: 1,
    }),
  );
  events.push(
    swarmEvent('verification_batch_completed', {
      batch_index: 2,
      batch_count: 2,
      claim_count: 1,
    }),
  );
  events.push(swarmEvent('verification_completed', { status: 'completed' }));
  events.push(swarmEvent('run_completed', { status: 'complete' }));
  return events;
}
