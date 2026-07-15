'use client';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { api } from './api';
import { initialWorkspaceState, reduceRunEvent } from './runReducer';
import { Run, RunEvent, WorkspaceState } from './types';

const TERMINAL_RUN_STATES = new Set([
  'completed',
  'partial_success',
  'failed',
  'cancelled',
  'timed_out',
  'budget_exhausted',
]);

const BASE_INTERVAL_MS = 3_000;
const MAX_BACKOFF_MS = 30_000;

export type PollingMode = 'idle' | 'polling' | 'reconnecting' | 'terminal';

type RunAction =
  | { kind: 'event'; event: RunEvent }
  | { kind: 'run'; run: Run }
  | { kind: 'reset' };

function workspaceReducer(state: WorkspaceState, action: RunAction): WorkspaceState {
  if (action.kind === 'reset') {
    // Full isolation between runs: events, agents, sources, claims,
    // conflicts, errors, cost and token totals all restart from zero.
    return initialWorkspaceState;
  }
  if (action.kind === 'run') {
    return { ...state, run: action.run, currentPhase: TERMINAL_RUN_STATES.has(action.run.status) ? action.run.status : state.currentPhase };
  }
  // reduceRunEvent already de-duplicates by event id.
  return reduceRunEvent(state, action.event);
}

/**
 * Authenticated run polling.
 *
 * - fetches the run and the initial events, then continues incrementally
 *   with after_event_id so no event is processed twice;
 * - never overlaps requests (an in-flight guard skips ticks);
 * - stops on terminal run states;
 * - exponential backoff with a visible 'reconnecting' mode on temporary
 *   failures, resetting once a poll succeeds;
 * - safe across browser refresh: callers persist the run id and the hook
 *   reconstructs state from the authenticated read endpoints;
 * - cleans up its timer on unmount. Tokens are re-read per request by the
 *   api layer, so Supabase session refreshes are transparent.
 *
 * Supabase Realtime remains intentionally disabled until it can join with
 * the same authenticated browser session; polling is the supported path.
 */
export function useRunRealtime(runId?: string) {
  const [state, dispatch] = useReducer(workspaceReducer, initialWorkspaceState);
  const [mode, setMode] = useState<PollingMode>('idle');
  const lastEventId = useRef<number | undefined>(undefined);
  const inFlight = useRef(false);
  const failures = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const stopped = useRef(false);
  // Monotonic generation: bumps whenever the active run (or user/session)
  // changes, so a late response from run A can never mutate run B's state.
  const generation = useRef(0);

  useEffect(() => {
    lastEventId.current = state.lastEventId;
  }, [state.lastEventId]);

  const poll = useCallback(async (id: string, myGeneration: number) => {
    if (inFlight.current || stopped.current) return;
    inFlight.current = true;
    try {
      const run = await api.run(id);
      if (generation.current !== myGeneration) return; // stale response
      dispatch({ kind: 'run', run });
      const events = await api.events(id, lastEventId.current);
      if (generation.current !== myGeneration) return; // stale response
      for (const event of events) dispatch({ kind: 'event', event });
      failures.current = 0;
      if (TERMINAL_RUN_STATES.has(run.status)) {
        stopped.current = true;
        setMode('terminal');
      } else {
        setMode('polling');
      }
    } catch {
      if (generation.current !== myGeneration) return;
      failures.current += 1;
      setMode('reconnecting');
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    // Every run switch starts from a clean slate and invalidates any
    // in-flight request from the previous run.
    generation.current += 1;
    const myGeneration = generation.current;
    dispatch({ kind: 'reset' });
    stopped.current = false;
    failures.current = 0;
    lastEventId.current = undefined;
    inFlight.current = false;

    if (!runId) {
      setMode('idle');
      return;
    }
    setMode('polling');

    let cancelled = false;
    const tick = async () => {
      if (cancelled || stopped.current || generation.current !== myGeneration) return;
      await poll(runId, myGeneration);
      if (cancelled || stopped.current || generation.current !== myGeneration) return;
      const backoff = Math.min(
        BASE_INTERVAL_MS * 2 ** failures.current,
        MAX_BACKOFF_MS,
      );
      timer.current = setTimeout(tick, backoff);
    };
    void tick();

    return () => {
      cancelled = true;
      stopped.current = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [runId, poll]);

  return { state, mode };
}
