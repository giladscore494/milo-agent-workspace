import { describe, expect, it } from 'vitest';
import { initialWorkspaceState, reconstructRun, reduceRunEvent } from '../lib/runReducer';
import {
  TERMINAL_RUN_STATUSES,
  isFullySuccessfulRunStatus,
  isPartialSuccessRunStatus,
  isTerminalRunStatus,
  isUnsuccessfulTerminalRunStatus,
} from '../lib/runStatus';
import { normalizeRunUsage } from '../lib/runUsage';
import { reduceSwarmEvents } from '../lib/swarmReducer';
import { isSwarmV2Workflow } from '../lib/swarmTypes';
import { buildSwarmRunViewModel, summarizeSwarmRun } from '../lib/swarmViewModel';
import { Run, RunEvent } from '../lib/types';
import {
  SMOKE_RUN_ID,
  SMOKE_TASK_IDS,
  SMOKE_USAGE,
  smokeEventStream,
  swarmEvent,
} from './swarmV2Fixture';

function run(status: string, usage?: Run['usage']): Run {
  return { id: SMOKE_RUN_ID, conversation_id: 'c', status, usage };
}

describe('terminal run contract', () => {
  it('recognizes all six durable terminal statuses and nothing else', () => {
    expect([...TERMINAL_RUN_STATUSES]).toEqual([
      'completed',
      'partial_success',
      'failed',
      'cancelled',
      'timed_out',
      'budget_exhausted',
    ]);
    for (const status of TERMINAL_RUN_STATUSES) expect(isTerminalRunStatus(status)).toBe(true);
    for (const status of ['queued', 'launching', 'starting', 'running', 'waiting', 'cancellation_requested']) {
      expect(isTerminalRunStatus(status)).toBe(false);
    }
    expect(isTerminalRunStatus(undefined)).toBe(false);
    expect(isTerminalRunStatus(null)).toBe(false);
    expect(isTerminalRunStatus('')).toBe(false);
  });

  it('G. completed is terminal and fully successful', () => {
    const vm = buildSwarmRunViewModel({ run: run('completed'), workflowKey: 'swarm_v2' });
    expect(vm.terminal).toBe(true);
    expect(vm.terminalStatus).toBe('completed');
    expect(vm.fullySuccessful).toBe(true);
    expect(vm.partialSuccess).toBe(false);
    expect(vm.lifecycle).toBe('completed');
    expect(vm.lifecycleLabel).toBe('Completed');
  });

  it('H. partial_success is terminal but NOT equivalent to completed', () => {
    const vm = buildSwarmRunViewModel({ run: run('partial_success'), workflowKey: 'swarm_v2' });
    expect(vm.terminal).toBe(true);
    expect(vm.terminalStatus).toBe('partial_success');
    expect(vm.fullySuccessful).toBe(false);
    expect(vm.partialSuccess).toBe(true);
    expect(vm.lifecycle).toBe('partial_success');
    expect(vm.lifecycleLabel).toBe('Partial success');
    expect(isFullySuccessfulRunStatus('partial_success')).toBe(false);
    expect(isPartialSuccessRunStatus('partial_success')).toBe(true);
    expect(isUnsuccessfulTerminalRunStatus('partial_success')).toBe(true);

    const completed = buildSwarmRunViewModel({ run: run('completed'), workflowKey: 'swarm_v2' });
    expect(vm.lifecycle).not.toBe(completed.lifecycle);
    expect(vm.fullySuccessful).not.toBe(completed.fullySuccessful);
  });

  it.each([
    ['I. failed', 'failed', 'Failed'],
    ['J. cancelled', 'cancelled', 'Cancelled'],
    ['K. timed_out', 'timed_out', 'Timed out'],
    ['L. budget_exhausted', 'budget_exhausted', 'Budget exhausted'],
  ])('%s is terminal and never fully successful', (_label, status, expectedLabel) => {
    const vm = buildSwarmRunViewModel({ run: run(status), workflowKey: 'swarm_v2' });
    expect(vm.terminal).toBe(true);
    expect(vm.terminalStatus).toBe(status);
    expect(vm.fullySuccessful).toBe(false);
    expect(vm.lifecycle).toBe(status);
    expect(vm.lifecycleLabel).toBe(expectedLabel);
  });

  it('never leaves timed_out or budget_exhausted rendering as running', () => {
    // Events stop mid-execution: the last one the run ever appended says tasks
    // are running. The durable status is what the UI must believe.
    const swarm = reduceSwarmEvents([
      swarmEvent('run_started', {}),
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_started', { task_id: 'env_check' }),
    ]);
    expect(swarm.lifecycle).toBe('running_tasks');
    for (const status of ['timed_out', 'budget_exhausted'] as const) {
      const vm = buildSwarmRunViewModel({ run: run(status), swarm, workflowKey: 'swarm_v2' });
      expect(vm.lifecycle).toBe(status);
      expect(vm.terminal).toBe(true);
    }
  });

  it('a non-terminal run keeps the event-derived lifecycle', () => {
    const swarm = reduceSwarmEvents([
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_started', { task_id: 'env_check' }),
    ]);
    const vm = buildSwarmRunViewModel({ run: run('running'), swarm, workflowKey: 'swarm_v2' });
    expect(vm.terminal).toBe(false);
    expect(vm.terminalStatus).toBeUndefined();
    expect(vm.lifecycle).toBe('running_tasks');
    expect(vm.lifecycleLabel).toBe('Running tasks');
  });
});

describe('run.usage is authoritative', () => {
  it('U. run.usage wins over anything an event stream might suggest', () => {
    // A stream that carries token-shaped payload keys the V1 projection sums.
    const events: RunEvent[] = [
      ...smokeEventStream(),
      { id: '900', run_id: SMOKE_RUN_ID, event_type: 'budget_warning', payload: { tokens: 999_999, cost_usd: 5 } },
    ];
    const state = events.reduce(reduceRunEvent, initialWorkspaceState);
    const vm = buildSwarmRunViewModel({
      run: run('completed', SMOKE_USAGE),
      swarm: state.swarm,
      workflowKey: 'swarm_v2',
    });
    // The V1 event-derived totals exist but are not the run's usage.
    expect(state.tokens).toBe(999_999);
    expect(vm.usage.totalTokens).toBe(10_370);
    expect(vm.usage.actualCost).toBeCloseTo(0.019178, 6);
    expect(vm.usage.modelCalls).toBe(7);
    expect(vm.scale.modelCalls).toBe(7);
  });

  it('V. missing and partial optional usage fields are safe', () => {
    expect(normalizeRunUsage(undefined)).toEqual({ present: false });
    expect(normalizeRunUsage(null)).toEqual({ present: false });

    const partial = normalizeRunUsage({ model_calls: 7, actual_cost: null });
    expect(partial.present).toBe(true);
    expect(partial.modelCalls).toBe(7);
    expect(partial.actualCost).toBeUndefined();
    expect(partial.totalTokens).toBeUndefined();
    // Absent is never rendered as zero.
    expect(partial.retries).not.toBe(0);

    const noisy = normalizeRunUsage({
      total_tokens: Number.NaN,
      retries: Number.POSITIVE_INFINITY,
      input_tokens: 12,
    });
    expect(noisy.totalTokens).toBeUndefined();
    expect(noisy.retries).toBeUndefined();
    expect(noisy.inputTokens).toBe(12);

    const vm = buildSwarmRunViewModel({ run: run('running'), workflowKey: 'swarm_v2' });
    expect(vm.usage.present).toBe(false);
    expect(vm.usage.modelCalls).toBeUndefined();
    expect(vm.scale.modelCalls).toBeUndefined();
    expect(vm.scale.logicalTasks).toBe(0);
  });
});

describe('Q. the accepted smoke fixture reconstructs without conflating concepts', () => {
  const state = reconstructRun(run('completed', SMOKE_USAGE), smokeEventStream());
  const vm = buildSwarmRunViewModel({
    run: run('completed', SMOKE_USAGE),
    swarm: state.swarm,
    workflowKey: 'swarm_v2',
  });

  it('reconstructs 5 logical tasks, 7 model calls, 10,370 tokens and $0.019178', () => {
    expect(vm.tasks).toHaveLength(5);
    expect(vm.taskCounts.total).toBe(5);
    expect(vm.taskCounts.completed).toBe(5);
    expect(vm.tasks.map((task) => task.taskId).sort()).toEqual([...SMOKE_TASK_IDS]);
    expect(vm.usage.modelCalls).toBe(7);
    expect(vm.usage.totalTokens).toBe(10_370);
    expect(vm.usage.actualCost).toBe(0.019178);
    expect(vm.usage.retries).toBe(0);
    expect(vm.usage.providerBackpressureEvents).toBe(0);
  });

  it('never renders 7 as a task or agent count', () => {
    expect(vm.tasks.length).not.toBe(vm.usage.modelCalls);
    expect(vm.taskCounts.total).toBe(5);
    // There is no agent concept in the Swarm V2 view model at all.
    expect('agents' in vm).toBe(false);
    expect('agentCount' in vm).toBe(false);
    expect(Object.keys(vm).some((key) => key.toLowerCase().includes('agent'))).toBe(false);
    // The V1 agent registry stays empty for a Swarm V2 run.
    expect(Object.keys(state.agents)).toEqual([]);
  });

  it('keeps tasks, model calls, verifier batches and repairs as separate quantities', () => {
    expect(vm.scale).toEqual({
      logicalTasks: 5,
      modelCalls: 7,
      verifierBatches: 2,
      workerRepairs: 1,
    });
    // 5 worker calls + 1 bounded repair + ... the aggregate stays the backend's.
    expect(vm.scale.logicalTasks + vm.scale.workerRepairs).not.toBe(vm.scale.modelCalls);
  });

  it('labels tasks with deterministic formatting only', () => {
    expect(vm.tasks.map((task) => task.label).sort()).toEqual([
      'Catalog health',
      'Compile report',
      'Env check',
      'Get details',
      'List catalog',
    ]);
  });

  it('summarizes safely for the inspector', () => {
    const summary = summarizeSwarmRun(vm);
    expect(summary.logical_tasks).toBe(5);
    expect(summary.model_calls).toBe(7);
    expect(summary.total_tokens).toBe(10_370);
    expect(summary.actual_cost).toBe(0.019178);
    expect(JSON.stringify(summary)).not.toMatch(/agent/i);
  });
});

describe('W. V1 compatibility', () => {
  const v1Events: RunEvent[] = [
    { id: '1', run_id: 'r', event_type: 'run_started', message: 'Run started' },
    {
      id: '2',
      run_id: 'r',
      event_type: 'agent_started',
      agent: 'Discovery',
      phase: 'discovery',
      message: 'Agent task started',
    },
    {
      id: '3',
      run_id: 'r',
      event_type: 'tool_access_granted',
      agent: 'Discovery',
      payload: { reason: 'freshness required', domains: ['nhtsa.gov'] },
    },
    {
      id: '4',
      run_id: 'r',
      event_type: 'source_recorded',
      agent: 'Discovery',
      payload: {
        id: 's1',
        title: 'NHTSA listing',
        domain: 'nhtsa.gov',
        source_type: 'primary',
        source_strength: 'strong',
        agent: 'Discovery',
      },
    },
    { id: '5', run_id: 'r', event_type: 'agent_completed', agent: 'Discovery' },
    { id: '6', run_id: 'r', event_type: 'run_completed', payload: { status: 'complete' } },
  ];

  it('a vehicle_catalog_v1 project still reconstructs safely', () => {
    const state = reconstructRun({ id: 'r', conversation_id: 'c', status: 'completed' }, v1Events);
    expect(Object.keys(state.agents)).toEqual(['Discovery']);
    expect(state.agents.Discovery.internet).toBe('approved');
    expect(state.agents.Discovery.sources).toHaveLength(1);
    expect(state.sources).toHaveLength(1);
    expect(state.currentPhase).toBe('discovery');
  });

  it('a V1 run produces no phantom Swarm tasks and is not presented as V2', () => {
    const state = reconstructRun({ id: 'r', conversation_id: 'c', status: 'completed' }, v1Events);
    const vm = buildSwarmRunViewModel({
      run: { id: 'r', conversation_id: 'c', status: 'completed' },
      swarm: state.swarm,
      workflowKey: 'vehicle_catalog_v1',
    });
    expect(vm.isSwarmV2).toBe(false);
    expect(vm.tasks).toEqual([]);
    expect(vm.taskCounts.total).toBe(0);
    expect(vm.plan.planCreated).toBe(false);
    expect(vm.verification.started).toBe(false);
    // V1 agents are NOT rewritten into fake Swarm tasks.
    expect(Object.keys(state.agents)).toEqual(['Discovery']);
  });

  it('workflow selection uses the trusted project workflow_key', () => {
    expect(isSwarmV2Workflow('swarm_v2')).toBe(true);
    expect(isSwarmV2Workflow('vehicle_catalog_v1')).toBe(false);
    expect(isSwarmV2Workflow(undefined)).toBe(false);
    expect(buildSwarmRunViewModel({ workflowKey: 'swarm_v2' }).isSwarmV2).toBe(true);
    expect(buildSwarmRunViewModel({}).isSwarmV2).toBe(false);
  });
});
