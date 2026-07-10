'use client';
import { useEffect, useReducer, useRef, useState } from 'react';
import { api } from './api';
import { initialWorkspaceState, reconstructRun, reduceRunEvent } from './runReducer';

export function useRunRealtime(runId?: string) {
  const [state, dispatch] = useReducer(reduceRunEvent, initialWorkspaceState);
  const [mode, setMode] = useState<'realtime'|'polling'|'idle'>('idle');
  const last = useRef<number | undefined>();
  useEffect(() => { last.current = state.lastEventId; }, [state.lastEventId]);
  useEffect(() => {
    if (!runId) return;
    let closed = false;
    setMode('polling');
    api.run(runId)
      .then(run => api.events(runId).then(events => {
        if (!closed) events.forEach(e => dispatch(e));
        return { run, events };
      }).then(({ run, events }) => reconstructRun(run, events)))
      .catch(() => {});

    // Realtime is intentionally disabled in this auth-hardening PR until the
    // Supabase Realtime channel can be opened with the same authenticated
    // browser client/session used by API requests.
    const timer = setInterval(async () => {
      if (closed) return;
      try {
        const events = await api.events(runId, last.current);
        events.forEach(dispatch);
        setMode('polling');
      } catch {
        setMode('polling');
      }
    }, 4000);
    return () => { closed = true; clearInterval(timer); };
  }, [runId]);
  return { state, mode };
}
