import { describe, expect, it } from 'vitest';
import { EMPTY_RUN_USAGE, normalizeRunUsage } from '../lib/runUsage';
import { buildSwarmRunViewModel, summarizeSwarmRun } from '../lib/swarmViewModel';
import { Run } from '../lib/types';
import contract from './fixtures/runResponseContract.json';

/**
 * H. The frontend RunUsage contract must accept the REAL backend response.
 *
 * `fixtures/runResponseContract.json` holds GET /runs/{id} bodies recorded from
 * the FastAPI app. `tests/test_run_usage_contract.py` re-renders them through
 * that app and asserts equality, so this fixture cannot silently drift from the
 * server; here it is parsed with the same code the workspace runs.
 */

const settled = contract.settled as unknown as Run;
const unsettled = contract.unsettled as unknown as Run;

describe('H. the real GET /runs/{id} response parses through lib/runUsage', () => {
  it('reads the authoritative aggregate from a settled run', () => {
    const usage = normalizeRunUsage(settled.usage);
    expect(usage).toEqual({
      present: true,
      modelCalls: 7,
      inputTokens: 7120,
      outputTokens: 3250,
      totalTokens: 10370,
      estimatedCost: 0.02,
      actualCost: 0.019178,
      retries: 0,
      providerBackpressureEvents: 0,
      agentSteps: 7,
      elapsedSeconds: 41.5,
    });
    // The accepted smoke aggregate, straight off the wire.
    expect(usage.inputTokens! + usage.outputTokens!).toBe(10370);
  });

  it('reports nothing — not zero spend — for a run that has settled no call', () => {
    expect(unsettled.usage).toBeNull();
    expect(normalizeRunUsage(unsettled.usage)).toEqual(EMPTY_RUN_USAGE);
    expect(normalizeRunUsage(unsettled.usage).modelCalls).toBeUndefined();
  });

  it('carries the aggregate into the Swarm V2 view model unchanged', () => {
    const vm = buildSwarmRunViewModel({ run: settled, workflowKey: 'swarm_v2' });
    expect(vm.usage.present).toBe(true);
    expect(vm.usage.modelCalls).toBe(7);
    expect(vm.usage.totalTokens).toBe(10370);
    expect(vm.usage.actualCost).toBe(0.019178);
    expect(vm.scale.modelCalls).toBe(7);
    // Still not a task count and still not an agent count: no event stream was
    // folded here, so the run reports 7 model calls across 0 known tasks.
    expect(vm.scale.logicalTasks).toBe(0);
    expect(Object.keys(vm).some((key) => key.toLowerCase().includes('agent'))).toBe(false);

    const summary = summarizeSwarmRun(vm);
    expect(summary.model_calls).toBe(7);
    expect(summary.usage_reported).toBe(true);
  });

  it('marks an unsettled run as not reporting usage', () => {
    const vm = buildSwarmRunViewModel({ run: unsettled, workflowKey: 'swarm_v2' });
    expect(vm.usage.present).toBe(false);
    expect(vm.scale.modelCalls).toBeUndefined();
    expect(summarizeSwarmRun(vm).usage_reported).toBe(false);
    expect(summarizeSwarmRun(vm).model_calls).toBeUndefined();
  });

  it('exposes exactly the public usage fields and nothing else', () => {
    expect(Object.keys(settled.usage as object).sort()).toEqual([
      'actual_cost',
      'agent_steps',
      'elapsed_seconds',
      'estimated_cost',
      'input_tokens',
      'model_calls',
      'output_tokens',
      'provider_backpressure_events',
      'retries',
      'total_tokens',
    ]);
    for (const value of Object.values(settled.usage as Record<string, unknown>)) {
      expect(typeof value).toBe('number');
    }
  });

  it('keeps the rest of the run contract the reducer relies on', () => {
    // Fields the workspace reads off the run row must survive the change.
    for (const key of ['id', 'conversation_id', 'status', 'launch_state', 'launch_reconciliation_required']) {
      expect(key in settled).toBe(true);
    }
    // Launch sanitization: the raw exception and lease never appear.
    expect('launch_error' in settled).toBe(false);
    expect('lease_token' in settled).toBe(false);
  });
});
