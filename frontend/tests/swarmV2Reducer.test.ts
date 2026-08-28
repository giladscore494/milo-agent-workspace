import { describe, expect, it } from 'vitest';
import { initialWorkspaceState, reduceRunEvent } from '../lib/runReducer';
import { reduceSwarmEvent, reduceSwarmEvents } from '../lib/swarmReducer';
import { humanizeTaskId, initialSwarmRunState } from '../lib/swarmTypes';
import { selectRunningSwarmTaskIds, selectSwarmTasks } from '../lib/swarmViewModel';
import { RunEvent } from '../lib/types';
import { SMOKE_TASK_IDS, smokeEventStream, swarmEvent } from './swarmV2Fixture';

describe('Swarm V2 reducer: logical task identity', () => {
  it('A. a normal V2 event sequence reconstructs exactly 5 logical tasks', () => {
    const state = reduceSwarmEvents(smokeEventStream());
    expect(state.taskOrder).toHaveLength(5);
    expect([...state.taskOrder].sort()).toEqual([...SMOKE_TASK_IDS]);
    expect(Object.keys(state.tasks)).toHaveLength(5);
    for (const task of selectSwarmTasks(state)) {
      expect(task.status).toBe('completed');
    }
  });

  it('B. two different task_started events can coexist as running', () => {
    const state = reduceSwarmEvents([
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('task_started', { task_id: 'list_catalog' }),
    ]);
    expect(selectRunningSwarmTaskIds(state).sort()).toEqual(['env_check', 'list_catalog']);
    expect(state.taskOrder).toHaveLength(2);
  });

  it('C. task identity comes from payload.task_id, never event.agent', () => {
    // Two events for ONE logical task that carry two different (and for Swarm
    // V2, never actually populated) agent labels.
    const first: RunEvent = {
      ...swarmEvent('task_started', { task_id: 'catalog_health' }),
      agent: 'worker-a',
    };
    const second: RunEvent = {
      ...swarmEvent('task_completed', { task_id: 'catalog_health', status: 'completed' }),
      agent: 'worker-b',
    };
    const state = reduceSwarmEvents([first, second]);
    expect(state.taskOrder).toEqual(['catalog_health']);
    expect(state.tasks.catalog_health.status).toBe('completed');

    // An event with an agent but no task_id creates no logical task at all.
    const orphan = reduceSwarmEvent(state, { ...swarmEvent('task_started', {}), agent: 'ghost' });
    expect(orphan.taskOrder).toEqual(['catalog_health']);
    expect(orphan.tasks.ghost).toBeUndefined();
  });

  it('humanizeTaskId is deterministic formatting only', () => {
    expect(humanizeTaskId('catalog_health')).toBe('Catalog health');
    expect(humanizeTaskId('compile_report')).toBe('Compile report');
    expect(humanizeTaskId('env-check')).toBe('Env check');
    expect(humanizeTaskId('list__catalog')).toBe('List catalog');
    expect(humanizeTaskId('x')).toBe('X');
    expect(humanizeTaskId('_')).toBe('_');
  });
});

describe('Swarm V2 reducer: plan and Commander state', () => {
  it('D. commander_replanned updates plan revision and decision deterministically', () => {
    const state = reduceSwarmEvents([
      swarmEvent('commander_plan_created', { graph_revision: 1 }),
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('commander_replanned', { decision: 'ADD_TASKS', graph_revision: 2 }),
    ]);
    expect(state.plan.planCreated).toBe(true);
    expect(state.plan.graphRevision).toBe(2);
    expect(state.plan.replanCount).toBe(1);
    expect(state.plan.lastReplanDecision).toBe('ADD_TASKS');
    expect(state.lifecycle).toBe('replanning');

    const second = reduceSwarmEvent(
      state,
      swarmEvent('commander_replanned', { decision: 'REVISE_TASK', graph_revision: 3 }),
    );
    expect(second.plan.graphRevision).toBe(3);
    expect(second.plan.replanCount).toBe(2);
    expect(second.plan.lastReplanDecision).toBe('REVISE_TASK');
  });

  it('rejects a decision value outside the backend allowlist', () => {
    const state = reduceSwarmEvents([
      swarmEvent('commander_replanned', { decision: 'THINK_HARDER', graph_revision: 2 }),
    ]);
    expect(state.plan.lastReplanDecision).toBeUndefined();
    expect(state.plan.graphRevision).toBe(2);
  });

  it('exposes no Commander reasoning field', () => {
    const state = reduceSwarmEvents([
      swarmEvent('commander_replanned', {
        decision: 'ADD_TASKS',
        graph_revision: 2,
        reason: 'coverage gap on list_catalog',
      }),
    ]);
    expect(JSON.stringify(state.plan)).not.toContain('coverage gap');
    expect('reason' in state.plan).toBe(false);
  });
});

describe('Swarm V2 reducer: idempotency and unknown events', () => {
  it('E. a duplicate event id is idempotently ignored', () => {
    const event = swarmEvent('task_started', { task_id: 'env_check' });
    const once = reduceSwarmEvent(initialSwarmRunState, event);
    const twice = reduceSwarmEvent(once, event);
    expect(twice).toBe(once);
    expect(twice.activity).toHaveLength(1);

    // ...and through the workspace reducer, which owns the event list.
    const w1 = reduceRunEvent(initialWorkspaceState, event);
    const w2 = reduceRunEvent(w1, event);
    expect(w2.events).toHaveLength(1);
    expect(w2.swarm.activity).toHaveLength(1);
    expect(w2.swarm.taskOrder).toEqual(['env_check']);
  });

  it('a duplicated whole stream reconstructs identical task state', () => {
    const events = smokeEventStream();
    const once = events.reduce(reduceRunEvent, initialWorkspaceState);
    const twice = [...events, ...events].reduce(reduceRunEvent, initialWorkspaceState);
    expect(twice.events).toHaveLength(once.events.length);
    expect(twice.swarm.taskOrder).toEqual(once.swarm.taskOrder);
    expect(twice.swarm.verification).toEqual(once.swarm.verification);
    expect(twice.swarm.plan).toEqual(once.swarm.plan);
  });

  it('X. an unknown safe event does not corrupt existing state', () => {
    const before = reduceSwarmEvents(smokeEventStream());
    const after = reduceSwarmEvent(
      before,
      swarmEvent('future_swarm_telemetry_v9', { task_id: 'env_check', novel_field: 42 }, '9001'),
    );
    expect(after.taskOrder).toEqual(before.taskOrder);
    expect(after.tasks).toEqual(before.tasks);
    expect(after.plan).toEqual(before.plan);
    expect(after.verification).toEqual(before.verification);
    expect(after.unknownEventTypes).toEqual(['future_swarm_telemetry_v9']);
  });

  it('survives an event with a missing, null or non-object payload', () => {
    const state = reduceSwarmEvents([
      { id: '1', run_id: 'r', event_type: 'task_started' },
      { id: '2', run_id: 'r', event_type: 'task_started', payload: null },
      { id: '3', run_id: 'r', event_type: 'task_started', payload: 'not-an-object' },
      { id: '4', run_id: 'r', event_type: 'evidence_added', payload: [] },
    ]);
    expect(state.taskOrder).toEqual([]);
    expect(state.evidenceClaimIds).toEqual([]);
  });
});

describe('Swarm V2 reducer: B3 repair, B4 batching, B5 grounding', () => {
  it('R. a worker repair does not create another logical task or agent', () => {
    const state = reduceSwarmEvents([
      swarmEvent('task_started', { task_id: 'get_details' }),
      swarmEvent('worker_output_repair_started', {
        task_id: 'get_details',
        reason_code: 'SCHEMA_INVALID',
        attempt_number: 1,
      }),
      swarmEvent('task_completed', { task_id: 'get_details', status: 'completed' }),
    ]);
    expect(state.taskOrder).toEqual(['get_details']);
    expect(state.tasks.get_details.repairCount).toBe(1);
    expect(state.tasks.get_details.status).toBe('completed');
    // No agent registry exists to grow.
    expect('agents' in state).toBe(false);
    // The static repair reason code is telemetry, not rendered rationale.
    expect(JSON.stringify(state)).not.toContain('SCHEMA_INVALID');
  });

  it('the repair also creates no V1 agent in the workspace reducer', () => {
    const state = [
      swarmEvent('task_started', { task_id: 'get_details' }),
      swarmEvent('worker_output_repair_started', {
        task_id: 'get_details',
        reason_code: 'SCHEMA_INVALID',
        attempt_number: 1,
      }),
    ].reduce(reduceRunEvent, initialWorkspaceState);
    expect(Object.keys(state.agents)).toEqual([]);
    expect(state.swarm.taskOrder).toEqual(['get_details']);
  });

  it('S. multiple verifier batches do not create multiple logical tasks or agents', () => {
    const state = reduceSwarmEvents([
      swarmEvent('grounding_context_resolved', {
        claim_count: 6,
        source_count: 4,
        missing_context_count: 1,
      }),
      swarmEvent('verification_batch_completed', { batch_index: 1, batch_count: 3, claim_count: 2 }),
      swarmEvent('verification_batch_completed', { batch_index: 2, batch_count: 3, claim_count: 2 }),
      swarmEvent('verification_batch_completed', { batch_index: 3, batch_count: 3, claim_count: 2 }),
      swarmEvent('verification_completed', { status: 'completed' }),
    ]);
    expect(state.taskOrder).toEqual([]);
    expect(state.verification.completedBatches).toBe(3);
    expect(state.verification.completedBatchIndex).toBe(3);
    expect(state.verification.batchCount).toBe(3);
    expect(state.verification.completed).toBe(true);
    expect(state.lifecycle).toBe('verifying');
  });

  it('T. grounding_context_resolved exposes counts only and no source text state', () => {
    const state = reduceSwarmEvents([
      swarmEvent('grounding_context_resolved', {
        claim_count: 6,
        source_count: 4,
        missing_context_count: 1,
      }),
    ]);
    expect(state.verification).toEqual({
      started: true,
      completed: false,
      groundingResolved: true,
      claimCount: 6,
      groundingSourceCount: 4,
      missingContextCount: 1,
      completedBatchIndex: 0,
      completedBatches: 0,
    });
    expect(state.taskOrder).toEqual([]);
    // Exactly these keys exist; nothing that could hold text, a source id or
    // a content hash.
    expect(Object.keys(state.verification).sort()).toEqual([
      'claimCount',
      'completed',
      'completedBatchIndex',
      'completedBatches',
      'groundingResolved',
      'groundingSourceCount',
      'missingContextCount',
      'started',
    ]);
  });

  it('evidence_added records claim identity only, never fragment text', () => {
    const state = reduceSwarmEvents([
      swarmEvent('evidence_added', {
        claim_id: 'claim-1',
        source_id: 'source-1',
        task_id: 'list_catalog',
        // A future payload key carrying text must never be projected.
        fragment: 'the catalog lists 41 trims',
      }),
    ]);
    expect(state.evidenceClaimIds).toEqual(['claim-1']);
    expect(state.tasks.list_catalog.evidenceClaimIds).toEqual(['claim-1']);
    expect(JSON.stringify(state)).not.toContain('41 trims');
  });

  it('conflict_found records claim ids without duplication', () => {
    const state = reduceSwarmEvents([
      swarmEvent('conflict_found', { claim_id: 'claim-1' }),
      swarmEvent('conflict_found', { claim_id: 'claim-2' }),
      swarmEvent('conflict_found', { claim_id: 'claim-1' }),
    ]);
    expect(state.conflictClaimIds).toEqual(['claim-1', 'claim-2']);
  });
});

describe('Swarm V2 reducer: task status transitions', () => {
  it('advances pending -> ready -> running -> completed and never regresses', () => {
    const forward = reduceSwarmEvents([
      swarmEvent('task_ready', { task_id: 'env_check' }),
      swarmEvent('task_started', { task_id: 'env_check' }),
      swarmEvent('task_completed', { task_id: 'env_check', status: 'completed' }),
    ]);
    expect(forward.tasks.env_check.status).toBe('completed');
    const late = reduceSwarmEvent(forward, swarmEvent('task_ready', { task_id: 'env_check' }));
    expect(late.tasks.env_check.status).toBe('completed');
  });

  it('records a static failure code for a failed task', () => {
    const state = reduceSwarmEvents([
      swarmEvent('task_started', { task_id: 'get_details' }),
      swarmEvent('task_failed', {
        task_id: 'get_details',
        status: 'failed',
        code: 'DEPENDENCY_FAILED',
      }),
    ]);
    expect(state.tasks.get_details.status).toBe('failed');
    expect(state.tasks.get_details.failureCode).toBe('DEPENDENCY_FAILED');
  });
});
