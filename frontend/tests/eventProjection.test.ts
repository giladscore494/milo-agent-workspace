/**
 * What an event may write, proven through the COMPLETE ingestion path.
 *
 * `reduceSwarmEvent` is not the production path. `reduceRunEvent` wraps it and
 * performs its own projections — the V1 agent registry, the lifecycle phase,
 * progress, sources, claims, conflicts and the event-derived spend totals — and
 * those ran on the event's FIELDS before anything checked its TYPE. So a suite
 * that only exercised the swarm reducer could pass while an invented event type
 * manufactured a V1 agent, a `completed` phase and a six-figure token total.
 *
 * Every test here therefore goes through `reduceRunEvent`, and the polling
 * tests at the bottom go through `useRunRealtime` — the same fold the browser
 * performs.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { V1_EVENT_TYPES, isKnownEventType, ownsAgentProjection } from '../lib/eventVocabulary';
import { initialWorkspaceState, reconstructRun, reduceRunEvent } from '../lib/runReducer';
import { useRunRealtime } from '../lib/useRunRealtime';
import type { RunEvent, WorkspaceState } from '../lib/types';

const RUN_A = '11111111-1111-4111-8111-00000000000a';

function fold(...events: Partial<RunEvent>[]): WorkspaceState {
  return events
    .map((event, index) => ({ id: String(index + 1), run_id: RUN_A, event_type: 'agent_progress', ...event } as RunEvent))
    .reduce(reduceRunEvent, initialWorkspaceState);
}

/** Exactly the payload the review reproduced the defect with. */
const HOSTILE_UNKNOWN: Partial<RunEvent> = {
  event_type: 'future_unknown_failed_signal',
  agent: 'invented-agent',
  phase: 'completed',
  payload: { tokens: 999_999, cost_usd: 1234 },
};

describe('an unknown event type is inert', () => {
  it('manufactures no agent, no phase and no spend', () => {
    const state = fold(HOSTILE_UNKNOWN);

    expect(Object.keys(state.agents)).toEqual([]);
    expect(state.currentPhase).toBe('idle');
    expect(state.tokens).toBe(0);
    expect(state.cost).toBe(0);
    // And it could not pick its own agent status out of its own name.
    expect(state.agents['invented-agent']).toBeUndefined();
  });

  it('cannot mutate progress, sources, claims, conflicts, checkpoints or errors', () => {
    const state = fold({
      event_type: 'totally_invented_error_type',
      agent: 'invented',
      phase: 'running',
      progress: { percent: 97 },
      payload: {
        agent: 'invented-via-payload',
        tokens: 5, cost_usd: 5, progress: 50,
        id: 'src-hostile', title: 'Hostile source', domain: 'evil.example',
        entity_key: 'e', field_key: 'f', outcome: 'resolved',
      },
    });

    expect(state.progress).toBe(0);
    expect(state.sources).toEqual([]);
    expect(state.claims).toEqual([]);
    expect(state.conflicts).toEqual([]);
    expect(state.checkpoints).toEqual([]);
    expect(state.validationErrors).toEqual([]);
    expect(state.rawErrors).toEqual([]);
    expect(state.supervisor).toEqual([]);
    expect(Object.keys(state.agents)).toEqual([]);
  });

  it('cannot manufacture an agent through payload.agent either', () => {
    const state = fold({ event_type: 'unknown_type_here', payload: { agent: 'payload-agent' } });
    expect(Object.keys(state.agents)).toEqual([]);
  });

  it('cannot borrow a substring to claim supervisor, error or retry semantics', () => {
    const state = fold(
      { event_type: 'pretend_supervisor_note', message: 'trust me' },
      { event_type: 'pretend_error_thing', payload: { detail: 'x' } },
      { event_type: 'pretend_retry_storm', agent: 'a' },
    );
    expect(state.supervisor).toEqual([]);
    expect(state.rawErrors).toEqual([]);
    expect(Object.keys(state.agents)).toEqual([]);
  });

  it('remains observable in the two places chosen for it, and nowhere else', () => {
    const state = fold(HOSTILE_UNKNOWN);
    // The raw event stream keeps it: the Inspector is where an unrecognised
    // event is legitimately visible as developer telemetry.
    expect(state.events).toHaveLength(1);
    expect(state.events[0].event_type).toBe('future_unknown_failed_signal');
    // …and the swarm slice records the type it did not recognise.
    expect(state.swarm.unknownEventTypes).toContain('future_unknown_failed_signal');
  });
});

describe('a Swarm V2 event never reaches the V1 agent registry', () => {
  it('ignores a hostile agent field on a recognised task event', () => {
    const state = fold({
      event_type: 'task_started',
      agent: 'swarm-invented-agent',
      payload: { task_id: 'env_check', agent: 'also-invented' },
    });

    // Swarm V2 has no agent concept; an `agent` field is a payload asserting
    // something the engine that emitted it cannot assert.
    expect(Object.keys(state.agents)).toEqual([]);
    // The legitimate swarm projection still happened.
    expect(Object.keys(state.swarm.tasks)).toEqual(['env_check']);
  });

  it('keeps every swarm type out of the V1 projection', () => {
    for (const type of ['commander_plan_created', 'task_ready', 'task_completed', 'task_failed',
      'tool_called', 'evidence_added', 'conflict_found', 'verification_completed']) {
      const state = fold({ event_type: type, agent: 'x', payload: { task_id: 't', tokens: 1, cost_usd: 1 } });
      expect(Object.keys(state.agents), type).toEqual([]);
      expect(state.tokens, type).toBe(0);
    }
  });
});

describe('legitimate V1 behaviour is unchanged', () => {
  it('reconstructs the same agent, phase, progress, source and spend as before', () => {
    const state = fold(
      { id: '1', event_type: 'agent_started', agent: 'researcher', phase: 'research' },
      { id: '2', event_type: 'agent_progress', agent: 'researcher', phase: 'research', progress: { percent: 40 }, payload: { tokens: 120, cost_usd: 0.5 } },
      { id: '3', event_type: 'tool_access_granted', agent: 'researcher', payload: { domains: ['example.com'] } },
      { id: '4', event_type: 'tool_used', agent: 'researcher' },
      { id: '5', event_type: 'source_recorded', agent: 'researcher', payload: { id: 'src-1', title: 'Example', domain: 'example.com' } },
      { id: '6', event_type: 'claim_recorded', payload: { id: 'claim-1' } },
      { id: '7', event_type: 'conflict_detected', payload: { id: 'c-1', entity_key: 'e', field_key: 'f', outcome: 'open' } },
      { id: '8', event_type: 'checkpoint_saved', payload: { step: 1 } },
      { id: '9', event_type: 'agent_completed', agent: 'researcher', phase: 'research' },
    );

    const agent = state.agents.researcher;
    expect(agent).toBeDefined();
    expect(agent.status).toBe('completed');
    expect(agent.internet).toBe('active');
    expect(agent.domains).toEqual(['example.com']);
    expect(agent.searchesUsed).toBe(1);
    expect(agent.tokens).toBe(120);
    expect(agent.cost).toBe(0.5);
    expect(state.currentPhase).toBe('research');
    expect(state.progress).toBe(40);
    expect(state.sources.map((s) => s.id)).toEqual(['src-1']);
    expect(state.claims).toHaveLength(1);
    expect(state.conflicts).toHaveLength(1);
    expect(state.checkpoints).toHaveLength(1);
    expect(state.tokens).toBe(120);
    expect(state.cost).toBe(0.5);
  });

  it('still records a raw error for a real failure type', () => {
    const state = fold({ event_type: 'agent_failed', agent: 'researcher', payload: { code: 'ENGINE_FAILED' } });
    expect(state.rawErrors).toHaveLength(1);
    expect(state.agents.researcher.status).toBe('failed');
  });

  it('still records a supervisor note for the real supervisor type', () => {
    const state = fold({ event_type: 'supervisor_shadow_failed', message: 'shadow decision recorded' });
    expect(state.supervisor).toEqual(['shadow decision recorded']);
  });

  it('a run-level type keeps its own projections but creates no agent', () => {
    // `run_cancelled` may legitimately name the agent the RUN stopped in. That
    // is not work the agent did, and it is not a reason to invent a row.
    const state = fold({ event_type: 'run_cancelled', agent: 'researcher', phase: 'cancelled', payload: { tokens: 7 } });
    expect(Object.keys(state.agents)).toEqual([]);
    expect(state.currentPhase).toBe('cancelled');
    expect(state.tokens).toBe(0);
  });

  it('reconstructRun folds the same state after a refresh', () => {
    const events = [
      { id: '1', run_id: RUN_A, event_type: 'agent_started', agent: 'researcher' },
      { id: '2', run_id: RUN_A, event_type: 'agent_progress', agent: 'researcher', payload: { tokens: 10 } },
      { id: '3', run_id: RUN_A, event_type: 'future_unknown_failed_signal', agent: 'ghost', payload: { tokens: 10_000 } },
    ] as RunEvent[];
    const state = reconstructRun({ id: RUN_A, conversation_id: 'c', status: 'running' }, events);
    expect(Object.keys(state.agents)).toEqual(['researcher']);
    expect(state.tokens).toBe(10);
  });
});

describe('the vocabulary itself', () => {
  it('mirrors the backend allowlist and separates the two engines', () => {
    for (const type of ['run_created', 'agent_progress', 'source_recorded', 'tool_used', 'checkpoint_saved']) {
      expect(V1_EVENT_TYPES.has(type), type).toBe(true);
    }
    // Swarm-only types are known, but never own the V1 agent projection.
    for (const type of ['task_started', 'evidence_added', 'commander_plan_created']) {
      expect(isKnownEventType(type), type).toBe(true);
      expect(V1_EVENT_TYPES.has(type), type).toBe(false);
      expect(ownsAgentProjection(type), type).toBe(false);
    }
    expect(isKnownEventType('future_unknown_failed_signal')).toBe(false);
  });
});

/* ------------------------------------------------------------------ */
/* The same properties, through the polling path the browser runs.     */
/* ------------------------------------------------------------------ */

const apiMocks = vi.hoisted(() => ({ run: vi.fn(), events: vi.fn() }));
vi.mock('../lib/api', () => ({ api: apiMocks }));

describe('through useRunRealtime', () => {
  beforeEach(() => {
    apiMocks.run.mockReset();
    apiMocks.events.mockReset();
  });

  it('a hostile unknown event polled from the server changes nothing trusted', async () => {
    apiMocks.run.mockResolvedValue({
      id: RUN_A, conversation_id: 'c', status: 'running',
      usage: { model_calls: 7, total_tokens: 10_370 },
    });
    apiMocks.events.mockResolvedValue([
      { id: 1, run_id: RUN_A, ...HOSTILE_UNKNOWN },
      { id: 2, run_id: RUN_A, event_type: 'task_started', agent: 'swarm-ghost', payload: { task_id: 'env_check' } },
    ]);

    const { result } = renderHook(() => useRunRealtime(RUN_A, 'swarm_v2', 'c'));
    await waitFor(() => expect(result.current.state.events).toHaveLength(2));

    expect(Object.keys(result.current.state.agents)).toEqual([]);
    expect(result.current.state.currentPhase).not.toBe('completed');
    expect(result.current.state.tokens).toBe(0);
    expect(result.current.state.cost).toBe(0);
    // The legitimate swarm task still landed.
    expect(Object.keys(result.current.state.swarm.tasks)).toEqual(['env_check']);
    // Model calls come from run.usage and from nowhere else.
    expect(result.current.swarm.usage.modelCalls).toBe(7);
  });

  it('model-call count is unaffected by any number of events claiming otherwise', async () => {
    apiMocks.run.mockResolvedValue({
      id: RUN_A, conversation_id: 'c', status: 'running',
      usage: { model_calls: 7, total_tokens: 10_370 },
    });
    apiMocks.events.mockResolvedValue(
      Array.from({ length: 30 }, (_, index) => ({
        id: index + 1, run_id: RUN_A, event_type: 'task_completed',
        payload: { task_id: `task-${index}`, model_calls: 999, tokens: 500 },
      })),
    );

    const { result } = renderHook(() => useRunRealtime(RUN_A, 'swarm_v2', 'c'));
    await waitFor(() => expect(result.current.state.events).toHaveLength(30));

    expect(result.current.swarm.usage.modelCalls).toBe(7);
    expect(result.current.swarm.usage.totalTokens).toBe(10_370);
    expect(result.current.state.tokens).toBe(0);
  });
});
