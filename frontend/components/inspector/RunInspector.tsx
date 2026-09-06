'use client';
import { KeyboardEvent, useRef } from 'react';
import { INTERNET_POLICIES, InternetBadge } from '@/components/common/InternetBadge';
import { redactSecrets, safeText } from '@/lib/sanitize';
import { normalizeRunUsage } from '@/lib/runUsage';
import { SwarmRunViewModel, summarizeSwarmRun } from '@/lib/swarmViewModel';
import { AgentState, WorkspaceState } from '@/lib/types';

export const INSPECTOR_TABS = ['Agents', 'Workflow', 'Sources', 'Claims', 'Conflicts', 'Costs', 'Developer'] as const;
export type InspectorTab = (typeof INSPECTOR_TABS)[number];

export type RunInspectorProps = {
  executionUi: boolean;
  tab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  agents: AgentState[];
  state: WorkspaceState;
  swarm: SwarmRunViewModel;
};

/**
 * Right rail: operational and developer detail, including the raw event
 * stream that used to sit in the centre column. Everything here is secondary
 * to the conversation and is derived from durable backend facts only.
 */
export function RunInspector({ executionUi, tab, onTabChange, agents, state, swarm }: RunInspectorProps) {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const current = INSPECTOR_TABS.indexOf(tab);
    const last = INSPECTOR_TABS.length - 1;
    let next = current;
    if (event.key === 'ArrowRight') next = current === last ? 0 : current + 1;
    else if (event.key === 'ArrowLeft') next = current === 0 ? last : current - 1;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = last;
    else return;
    event.preventDefault();
    onTabChange(INSPECTOR_TABS[next]);
    tabRefs.current[next]?.focus();
  }

  return (
    <div className="inspector">
      <div className="inspector-head">
        <h2 className="section-title">Run inspector</h2>
        <p className="eyebrow">Operational detail</p>
      </div>

      <div className="inspector-tabs" role="tablist" aria-label="Inspector sections" onKeyDown={onTabKeyDown}>
        {INSPECTOR_TABS.map((item, index) => (
          <button
            key={item}
            type="button"
            role="tab"
            id={`inspector-tab-${item}`}
            aria-controls="inspector-panel"
            aria-selected={tab === item}
            tabIndex={tab === item ? 0 : -1}
            ref={(node) => { tabRefs.current[index] = node; }}
            className="tab"
            onClick={() => onTabChange(item)}
          >
            {item}
          </button>
        ))}
      </div>

      <div className="inspector-panel" id="inspector-panel" role="tabpanel" aria-labelledby={`inspector-tab-${tab}`} tabIndex={0}>
        <InspectorPanel tab={tab} agents={agents} state={state} swarm={swarm} />
      </div>

      <section className="event-stream">
        <h3 className="section-title">Live event stream</h3>
        {state.events.length === 0 && (
          <p className="muted">
            {executionUi ? 'No events yet.' : 'No events. Realtime and polling stay disabled while execution surfaces are off.'}
          </p>
        )}
        {state.events.slice(-50).map((event) => (
          <div className="event" key={event.id}>
            <small className="event-type">{safeText(event.event_type)}</small>
            <span className="event-agent">{safeText(event.agent ?? '')}</span>
            <small className="event-phase">{safeText(event.phase ?? '')}</small>
            <p className="event-message">{safeText(event.message ?? '')}</p>
          </div>
        ))}
      </section>
    </div>
  );
}

function InspectorPanel({ tab, agents, state, swarm }: { tab: InspectorTab; agents: AgentState[]; state: WorkspaceState; swarm: SwarmRunViewModel }) {
  if (tab === 'Agents') {
    // Swarm V2 has no agent concept at all: its logical tasks are not agents
    // and must never be listed as if they were. Saying the legacy view does not
    // apply is the smallest honest adjustment; the tab itself stays put.
    if (swarm.isSwarmV2) {
      return (
        <p className="muted">
          Not applicable to Swarm V2. This run has no legacy agents — its logical tasks are shown in the
          run card and under Workflow.
        </p>
      );
    }
    return (
      <>
        {agents.length === 0 && <p className="muted">No agents are running.</p>}
        {agents.map((agent) => (
          <div className="agent-card" key={agent.name}>
            <div className="agent-card-head">
              <b>{safeText(agent.name)}</b>
              <span className={`badge is-status-${agent.status}`}>{safeText(agent.status)}</span>
            </div>
            <p>{safeText(agent.currentTask ?? agent.responsibility)}</p>
            <div><InternetBadge policy={agent.internet} reason={agent.internetReason} /></div>
          </div>
        ))}
        {agents.length === 0 && <div className="policy-legend">{INTERNET_POLICIES.map((policy) => <InternetBadge key={policy} policy={policy} />)}</div>}
      </>
    );
  }
  // Swarm V2 shows logical tasks, plan revision and verification progress.
  // Logical tasks, model calls and verifier batches stay separate quantities:
  // there is no agent count here and none is derivable from them.
  if (tab === 'Workflow') {
    return state.events.length === 0
      ? <p className="muted">No workflow activity yet.</p>
      : <pre className="code-block">{JSON.stringify(redactSecrets(swarm.isSwarmV2 ? summarizeSwarmRun(swarm) : { phase: state.currentPhase, progress: state.progress, checkpoints: state.checkpoints.length }), null, 2)}</pre>;
  }
  if (tab === 'Sources') {
    return state.sources.length === 0
      ? <p className="muted">No sources recorded.</p>
      : <>{state.sources.map((source) => (
          <div className="source" key={source.id}>
            <b>{safeText(source.title)}</b>
            <small> {safeText(source.domain)} • {safeText(source.source_strength)}</small>
          </div>
        ))}</>;
  }
  if (tab === 'Claims') return <pre className="code-block">{JSON.stringify(redactSecrets(state.claims), null, 2)}</pre>;
  if (tab === 'Conflicts') return <pre className="code-block">{JSON.stringify(redactSecrets(state.conflicts), null, 2)}</pre>;
  // run.usage is the authoritative aggregate; the event-derived totals stay
  // labelled as such and are never presented as the run's model-call count.
  if (tab === 'Costs') return <pre className="code-block">{JSON.stringify({ event_derived_tokens: state.tokens, event_derived_cost: state.cost, usage: normalizeRunUsage(state.run?.usage) }, null, 2)}</pre>;
  return <pre className="code-block">{JSON.stringify(redactSecrets({ events: state.events.length, checkpoints: state.checkpoints, validationErrors: state.validationErrors, rawErrors: state.rawErrors }), null, 2)}</pre>;
}
