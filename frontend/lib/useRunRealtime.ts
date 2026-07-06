'use client';
import { useEffect, useReducer, useRef, useState } from 'react';
import { api, supabaseBrowser } from './api';
import { initialWorkspaceState, reconstructRun, reduceRunEvent } from './runReducer';
import type { RunEvent } from './types';

type RunEventRealtimePayload = {
  new: RunEvent;
};

export function useRunRealtime(runId?: string) {
  const [state, dispatch] = useReducer(reduceRunEvent, initialWorkspaceState);
  const [mode, setMode] = useState<'realtime'|'polling'|'idle'>('idle');
  const last = useRef<number | undefined>();
  useEffect(() => { last.current = state.lastEventId; }, [state.lastEventId]);
  useEffect(() => {
    if (!runId) return;
    let closed = false;
    api.run(runId).then(run => api.events(runId).then(events => { if (!closed) events.forEach(e => dispatch(e)); return {run, events}; }).then(({run,events}) => reconstructRun(run,events))).catch(()=>{});
    let sb: any; let channel: any;
    supabaseBrowser().then(client => {
      sb = client;
      channel = sb?.channel(`run:${runId}`).on('postgres_changes', { event:'INSERT', schema:'public', table:'run_events', filter:`run_id=eq.${runId}` }, (payload: RunEventRealtimePayload) => {
        dispatch(payload.new);
      }).subscribe((status: string) => setMode(status === 'SUBSCRIBED' ? 'realtime' : 'polling'));
    });
    const timer = setInterval(async () => { if (closed) return; try { const events = await api.events(runId, last.current); events.forEach(dispatch); if (!channel) setMode('polling'); } catch { setMode('polling'); } }, 4000);
    return () => { closed = true; clearInterval(timer); if (channel) sb?.removeChannel(channel); };
  }, [runId]);
  return { state, mode };
}
