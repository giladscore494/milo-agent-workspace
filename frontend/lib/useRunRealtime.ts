'use client';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api } from './api';
import { EventId, maxEventId, normalizeEventId } from './eventId';
import { initialWorkspaceState, reduceRunEvent } from './runReducer';
import { isTerminalRunStatus } from './runStatus';
import { SwarmRunViewModel, buildSwarmRunViewModel } from './swarmViewModel';
import { Run, RunEvent, WorkspaceState } from './types';

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
    return { ...state, run: action.run, currentPhase: isTerminalRunStatus(action.run.status) ? action.run.status : state.currentPhase };
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
export function useRunRealtime(runId?: string, workflowKey?: string) {
  const [state, dispatch] = useReducer(workspaceReducer, initialWorkspaceState);
  const [mode, setMode] = useState<PollingMode>('idle');
  const lastEventId = useRef<EventId | undefined>(undefined);
  const inFlight = useRef(false);
  const failures = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const stopped = useRef(false);
  // Monotonic generation: bumps whenever the active run (or user/session)
  // changes, so a late response from run A can never mutate run B's state.
  const generation = useRef(0);

  // The cursor has exactly one owner: `poll` advances it from the events it
  // just folded, and the run-switch effect below clears it. Deriving it from
  // rendered state as well would give it a second, racier writer.

  const poll = useCallback(async (id: string, myGeneration: number) => {
    if (inFlight.current || stopped.current) return;
    inFlight.current = true;
    try {
      const run = await api.run(id);
      if (generation.current !== myGeneration) return; // stale response
      dispatch({ kind: 'run', run });
      const events = await api.events(id, lastEventId.current);
      if (generation.current !== myGeneration) return; // stale response
      for (const raw of events) {
        // The event-id contract is enforced at ingest, before the reducer runs,
        // so a malformed id degrades to "this one event is not rendered"
        // instead of throwing inside a React reducer. The backend types the
        // column as a NOT NULL bigint, so this branch is defensive only.
        let id: EventId;
        try {
          id = normalizeEventId(raw.id);
        } catch {
          continue;
        }
        dispatch({ kind: 'event', event: { ...raw, id } });
        // Advance the cursor from the response itself rather than from rendered
        // state, and by numeric maximum rather than array position, so the next
        // after_event_id can never move backwards or repeat an event.
        lastEventId.current = maxEventId(lastEventId.current, id);
      }
      failures.current = 0;
      if (isTerminalRunStatus(run.status)) {
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

  // The Swarm V2 view model is derived, never stored, so it resets with the
  // workspace state on every run switch and can never outlive its run.
  const swarm: SwarmRunViewModel = useMemo(
    () => buildSwarmRunViewModel({ run: state.run, swarm: state.swarm, workflowKey }),
    [state.run, state.swarm, workflowKey],
  );

  return { state, mode, swarm };
}
