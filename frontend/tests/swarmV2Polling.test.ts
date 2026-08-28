import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRunRealtime } from '../lib/useRunRealtime';
import { SMOKE_USAGE } from './swarmV2Fixture';

const apiMocks = vi.hoisted(() => ({
  run: vi.fn(),
  events: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: apiMocks }));

const RUN_A = '11111111-1111-4111-8111-00000000000a';
const RUN_B = '11111111-1111-4111-8111-00000000000b';

function runRow(id: string, status = 'running', usage?: unknown) {
  return { id, conversation_id: 'c', status, usage };
}

function swarmEvent(id: string, runId: string, eventType: string, payload: Record<string, unknown> = {}) {
  return { id, run_id: runId, event_type: eventType, message: eventType, payload };
}

describe('M/N. Swarm V2 state is isolated per run', () => {
  beforeEach(() => {
    apiMocks.run.mockReset();
    apiMocks.events.mockReset();
  });

  it('M. switching run A -> run B clears every V2-derived value', async () => {
    apiMocks.run.mockImplementation(async (id: string) =>
      id === RUN_A ? runRow(RUN_A, 'running', SMOKE_USAGE) : runRow(RUN_B),
    );
    apiMocks.events.mockImplementation(async (id: string) =>
      id === RUN_A
        ? [
            swarmEvent('1', RUN_A, 'commander_plan_created', { graph_revision: 1 }),
            swarmEvent('2', RUN_A, 'task_started', { task_id: 'env_check' }),
            swarmEvent('3', RUN_A, 'task_completed', { task_id: 'env_check', status: 'completed' }),
            swarmEvent('4', RUN_A, 'evidence_added', { claim_id: 'c1', source_id: 's1', task_id: 'env_check' }),
            swarmEvent('5', RUN_A, 'conflict_found', { claim_id: 'c1' }),
            swarmEvent('6', RUN_A, 'verification_batch_completed', { batch_index: 1, batch_count: 1, claim_count: 1 }),
          ]
        : [],
    );

    const { result, rerender } = renderHook(
      ({ runId }) => useRunRealtime(runId, 'swarm_v2'),
      { initialProps: { runId: RUN_A } },
    );

    await waitFor(() => expect(result.current.swarm.tasks).toHaveLength(1));
    expect(result.current.swarm.plan.graphRevision).toBe(1);
    expect(result.current.swarm.usage.modelCalls).toBe(7);
    expect(result.current.swarm.conflictClaimCount).toBe(1);
    expect(result.current.swarm.evidenceClaimCount).toBe(1);
    expect(result.current.swarm.verification.completedBatches).toBe(1);

    rerender({ runId: RUN_B });
    await waitFor(() => expect(result.current.state.run?.id).toBe(RUN_B));

    expect(result.current.swarm.tasks).toEqual([]);
    expect(result.current.swarm.taskCounts.total).toBe(0);
    expect(result.current.swarm.plan).toEqual({
      planCreated: false,
      graphRevision: 0,
      replanCount: 0,
    });
    expect(result.current.swarm.verification.completedBatches).toBe(0);
    expect(result.current.swarm.verification.started).toBe(false);
    expect(result.current.swarm.conflictClaimCount).toBe(0);
    expect(result.current.swarm.evidenceClaimCount).toBe(0);
    expect(result.current.swarm.activity).toEqual([]);
    // Run A's authoritative usage must not survive into run B.
    expect(result.current.swarm.usage.present).toBe(false);
    expect(result.current.swarm.usage.modelCalls).toBeUndefined();
    expect(result.current.state.lastEventId).toBeUndefined();
  });

  it('N. a late run A response cannot mutate run B state or usage', async () => {
    let releaseRunA: (value: unknown) => void = () => {};
    const pendingRunA = new Promise((resolve) => {
      releaseRunA = resolve;
    });

    apiMocks.run.mockImplementation(async (id: string) => {
      if (id === RUN_A) {
        await pendingRunA;
        return runRow(RUN_A, 'completed', SMOKE_USAGE);
      }
      return runRow(RUN_B, 'running');
    });
    apiMocks.events.mockImplementation(async (id: string) =>
      id === RUN_A ? [swarmEvent('99', RUN_A, 'task_started', { task_id: 'late_task' })] : [],
    );

    const { result, rerender } = renderHook(
      ({ runId }) => useRunRealtime(runId, 'swarm_v2'),
      { initialProps: { runId: RUN_A } },
    );
    rerender({ runId: RUN_B });
    await waitFor(() => expect(result.current.state.run?.id).toBe(RUN_B));

    releaseRunA(undefined);
    await new Promise((resolve) => setTimeout(resolve, 25));

    expect(result.current.state.run?.id).toBe(RUN_B);
    expect(result.current.swarm.tasks).toEqual([]);
    expect(result.current.swarm.usage.present).toBe(false);
    expect(result.current.swarm.terminal).toBe(false);
    expect(result.current.mode).not.toBe('terminal');
  });
});

describe('O/P. polling failure and recovery', () => {
  beforeEach(() => {
    apiMocks.run.mockReset();
    apiMocks.events.mockReset();
  });

  it('O. a temporary polling error preserves already-rendered state', async () => {
    let call = 0;
    apiMocks.run.mockImplementation(async () => {
      call += 1;
      if (call === 1) return runRow(RUN_A, 'running');
      throw new Error('network down');
    });
    apiMocks.events.mockResolvedValueOnce([
      swarmEvent('1', RUN_A, 'commander_plan_created', { graph_revision: 1 }),
      swarmEvent('2', RUN_A, 'task_started', { task_id: 'env_check' }),
    ]);

    const { result } = renderHook(() => useRunRealtime(RUN_A, 'swarm_v2'));
    await waitFor(() => expect(result.current.swarm.tasks).toHaveLength(1));

    await waitFor(() => expect(result.current.mode).toBe('reconnecting'), { timeout: 8_000 });

    // Nothing already rendered is erased by the failure.
    expect(result.current.swarm.tasks).toHaveLength(1);
    expect(result.current.swarm.tasks[0].taskId).toBe('env_check');
    expect(result.current.swarm.plan.graphRevision).toBe(1);
    expect(result.current.state.events).toHaveLength(2);
    expect(result.current.state.run?.id).toBe(RUN_A);
  }, 15_000);

  it('P. recovery continues from the existing cursor without replaying events', async () => {
    let runCall = 0;
    apiMocks.run.mockImplementation(async () => {
      runCall += 1;
      if (runCall === 2) throw new Error('temporary failure');
      return runRow(RUN_A, 'running');
    });

    const cursors: Array<string | undefined> = [];
    apiMocks.events.mockImplementation(async (_id: string, after?: string) => {
      cursors.push(after);
      if (after === undefined) {
        return [
          swarmEvent('1', RUN_A, 'task_started', { task_id: 'env_check' }),
          swarmEvent('2', RUN_A, 'task_completed', { task_id: 'env_check', status: 'completed' }),
        ];
      }
      if (after === '2') {
        return [swarmEvent('3', RUN_A, 'task_started', { task_id: 'list_catalog' })];
      }
      return [];
    });

    const { result } = renderHook(() => useRunRealtime(RUN_A, 'swarm_v2'));
    await waitFor(() => expect(result.current.state.events).toHaveLength(2));
    await waitFor(() => expect(result.current.mode).toBe('reconnecting'), { timeout: 8_000 });

    // The failed poll never reached the events endpoint, so the cursor is untouched.
    expect(cursors).toEqual([undefined]);

    await waitFor(() => expect(result.current.state.events).toHaveLength(3), { timeout: 12_000 });
    expect(result.current.mode).toBe('polling');
    // The recovery poll resumed from the last folded id; no event was replayed.
    expect(cursors).toEqual([undefined, '2']);
    expect(result.current.swarm.tasks.map((task) => task.taskId)).toEqual([
      'env_check',
      'list_catalog',
    ]);
    expect(result.current.swarm.taskCounts.completed).toBe(1);
    expect(result.current.swarm.taskCounts.running).toBe(1);
    expect(result.current.state.lastEventId).toBe('3');
  }, 25_000);
});
