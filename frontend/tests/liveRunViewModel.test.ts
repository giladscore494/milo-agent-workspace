import { describe, expect, it } from 'vitest';
import { buildLiveRunViewModel, parseRunLimits } from '../lib/liveRunViewModel';
import { initialWorkspaceState, reconstructRun } from '../lib/runReducer';
import { buildSwarmRunViewModel } from '../lib/swarmViewModel';
import { Run, RunEvent } from '../lib/types';
import { identityFor } from './fixtures/runIdentity';

const RUN_ID = '2c9e2c11-58c8-4b46-b7d5-3d8de9f4b7aa';
const CONVERSATION = '1f90f4ce-7844-4031-91d6-b74e40e1884e';

function run(workflow: string, over: Partial<Run> = {}): Run {
  return { id: RUN_ID, conversation_id: CONVERSATION, status: 'running', run_identity: identityFor(workflow, RUN_ID), ...over };
}

function event(id: number, event_type: string, extra: Partial<RunEvent> = {}): RunEvent {
  return { id: String(id), run_id: RUN_ID, event_type, ...extra } as RunEvent;
}

function live(r: Run, events: RunEvent[]) {
  const state = reconstructRun(r, events);
  const swarm = buildSwarmRunViewModel({ run: r, swarm: state.swarm, workflowKey: r.run_identity?.workflow_key });
  return buildLiveRunViewModel({ runId: RUN_ID, state, swarm });
}

describe('the unified live-run view model', () => {
  it('reads the engine from the immutable identity only', () => {
    expect(live(run('vehicle_catalog_v1'), []).engine).toBe('vehicle_catalog_v1');
    expect(live(run('swarm_v2'), []).engine).toBe('swarm_v2');
    const untrusted = live(run('vehicle_catalog_v1', { run_identity: null }), []);
    expect(untrusted.engine).toBeUndefined();
    expect(untrusted.engineLabel).toBe('Engine not stated');
    expect(untrusted.work).toBeUndefined();
  });

  it('projects V1 phases, chunks, active agents and evidence from durable events', () => {
    const view = live(run('vehicle_catalog_v1'), [
      event(1, 'run_started'),
      event(2, 'phase_started', { phase: 'technical_enrichment' }),
      event(3, 'agent_started', { agent: 'trims_years_agent', message: 'Agent task started: trims_years_agent/technical_enrichment', phase: 'technical_enrichment' }),
      event(4, 'chunk_started', { agent: 'trims_years_agent', phase: 'technical_enrichment' }),
      event(5, 'chunk_started', { agent: 'engines_fuel_power_agent', phase: 'technical_enrichment' }),
      event(6, 'chunk_completed', { agent: 'engines_fuel_power_agent', phase: 'technical_enrichment' }),
      event(7, 'source_recorded', { agent: 'trims_years_agent', payload: { id: 's1', title: 'Register', domain: 'gov.il' } }),
      event(8, 'provider_backpressure_wait', { payload: { seconds: 2 } }),
    ]);
    expect(view.phaseLabel).toBe('Technical enrichment');
    expect(view.work).toEqual({ unit: 'chunks', total: 2, queued: 0, running: 1, completed: 1, failed: 0 });
    expect(view.active).toEqual([{ name: 'trims_years_agent', doing: 'Agent task started: trims_years_agent/technical_enrichment' }]);
    expect(view.evidence).toEqual({ sources: 1, claims: 0, conflicts: 0 });
    expect(view.provider.pacingEventsObserved).toBe(1);
    expect(view.finalization).toEqual({ state: 'live' });
  });

  it('projects V2 tasks from the swarm slice and never invents an agent', () => {
    const view = live(run('swarm_v2'), [
      event(1, 'commander_plan_created', { payload: { graph_revision: 1, task_count: 2 } }),
      event(2, 'task_ready', { payload: { task_id: 'task-a' } }),
      event(3, 'task_started', { payload: { task_id: 'task-a' } }),
      event(4, 'task_ready', { payload: { task_id: 'task-b' } }),
      event(5, 'tool_called', { payload: { task_id: 'task-a', tool: 'search' } }),
    ]);
    expect(view.engine).toBe('swarm_v2');
    expect(view.work?.unit).toBe('tasks');
    expect(view.work?.running).toBe(1);
    expect(view.work?.queued).toBe(1);
    expect(view.active).toHaveLength(1);
    expect(view.active[0].doing).toContain('tool call');
  });

  it('reads spend against the run ceilings and the canonical outcome from the run row', () => {
    const terminal = run('vehicle_catalog_v1', {
      status: 'completed',
      usage: { model_calls: 40, actual_cost: 0.25, total_tokens: 120_000, provider_backpressure_events: 2, retries: 1 },
      limits: { max_model_calls_per_run: 150, max_cost_per_run: 1.0, max_total_tokens_per_run: 600_000, max_run_duration_seconds: 1800 },
      product_outcome: {
        engine: 'vehicle_catalog_v1', semantic_status: 'complete', usability: 'usable', result_kind: 'usable_result',
        coverage: { produced: 2, outstanding: 0, ratio: 1 }, blocking: [], payload: { present: true },
      },
    });
    const view = live(terminal, []);
    expect(view.terminal).toBe(true);
    expect(view.spendRatio).toBeCloseTo(0.25);
    expect(view.limits).toEqual({ maxModelCalls: 150, maxCost: 1, maxTotalTokens: 600_000, maxDurationSeconds: 1800, maxAgentSteps: undefined });
    expect(view.provider).toEqual({ backpressureEvents: 2, retries: 1, pacingEventsObserved: 0 });
    expect(view.finalization.state).toBe('finalized');
    if (view.finalization.state === 'finalized') expect(view.finalization.outcome.semanticStatus).toBe('complete');
  });

  it('states a terminal run without a recorded verdict as exactly that', () => {
    const view = live(run('swarm_v2', { status: 'cancelled', product_outcome: null }), []);
    expect(view.finalization).toEqual({ state: 'terminal_without_outcome' });
    expect(view.active).toEqual([]);
  });

  it('never turns an absent quantity into zero', () => {
    const view = live(run('vehicle_catalog_v1'), []);
    expect(view.usage.present).toBe(false);
    expect(view.spendRatio).toBeUndefined();
    expect(parseRunLimits(null)).toEqual({});
    // Effective concurrency is read from the server's projection, never computed here.
    const withConcurrency = parseRunLimits({
      max_cost_per_run: 1,
      concurrency: {
        v1_technical_parallelism: 4, v2_max_active_workers: 3, provider_max_concurrency: 2,
        provider_organization_ceiling: 32, provider_effective_concurrency: 2,
        v1_provider_admitted: 2, v2_provider_admitted: 2,
        max_concurrent_runs_per_user: 1, max_concurrent_runs_per_project: 1,
        search_basic_qps: 1, search_pro_qps: 1, search_qps_verified: false, paid_posture: false,
      },
    });
    expect(withConcurrency.concurrency).toEqual({
      v1TechnicalParallelism: 4, v2MaxActiveWorkers: 3, providerMaxConcurrency: 2,
      providerOrganizationCeiling: 32, providerEffectiveConcurrency: 2,
      v1ProviderAdmitted: 2, v2ProviderAdmitted: 2,
      maxConcurrentRunsPerUser: 1, maxConcurrentRunsPerProject: 1,
      searchBasicQps: 1, searchProQps: 1, searchQpsVerified: false, paidPosture: false,
    });
    expect(parseRunLimits({ concurrency: null }).concurrency).toBeUndefined();
    expect(parseRunLimits({ concurrency: { v2_max_active_workers: -8 } }).concurrency?.v2MaxActiveWorkers).toBeUndefined();
    expect(parseRunLimits({ max_cost_per_run: -1 })).toEqual({ maxModelCalls: undefined, maxTotalTokens: undefined, maxCost: undefined, maxDurationSeconds: undefined, maxAgentSteps: undefined });
    expect(initialWorkspaceState.events).toEqual([]);
  });
});
