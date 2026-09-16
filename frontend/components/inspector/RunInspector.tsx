'use client';
import { KeyboardEvent, useRef } from 'react';
import { INTERNET_POLICIES, InternetBadge } from '@/components/common/InternetBadge';
import { redactSecrets, safeText } from '@/lib/sanitize';
import { normalizeRunUsage } from '@/lib/runUsage';
import { catalogRefusalLabel } from '@/lib/catalogStatus';
import { SwarmCatalogStatus, SwarmRunViewModel, summarizeSwarmRun } from '@/lib/swarmViewModel';
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

      {/* CODE-2: the operator-facing catalog status. It renders only for a
          project whose TRUSTED workflow_key is swarm_v2 — `swarm.isSwarmV2` is
          derived from the project row, never from an event payload — and only
          once a catalog event has actually been observed. */}
      {swarm.isSwarmV2 && swarm.catalog.observed && <CatalogStatusPanel catalog={swarm.catalog} />}

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

/**
 * Catalog status: what this run wrote into the canonical catalog, or why not.
 *
 * Deliberately small and deliberately not a redesign — it sits beside the
 * existing event stream in the rail that already answers operational
 * questions.
 *
 * Three rules it holds:
 *
 *  - **state is text and structure, never colour alone.** Every outcome is
 *    spelled out in words ("Promoted", "Refused") and in a count, so the panel
 *    reads identically to someone who cannot distinguish the badge colours;
 *  - **keyboard reachable.** It is a labelled region with a heading and a
 *    definition list; there is no control to trap focus and nothing that is
 *    reachable only by pointer;
 *  - **static text only.** A refusal renders through `catalogRefusalLabel`,
 *    which answers from a closed allowlist; keys are already bounded and
 *    sanitized in the projection and pass through `safeText` again here. No
 *    payload object is ever rendered.
 *
 * A refusal is NOT presented as a failed run. It is a legitimate outcome of a
 * research run — a field with no verified evidence, an unresolved conflict, a
 * candidate the ingestion left ambiguous — and the wording says so.
 */
function CatalogStatusPanel({ catalog }: { catalog: SwarmCatalogStatus }) {
  return (
    <section className="catalog-status" aria-labelledby="catalog-status-title">
      <h3 className="section-title" id="catalog-status-title">Catalog status</h3>
      <p className="muted">
        Canonical catalog outcomes for this run. A refusal is an operational outcome, not a failed run.
      </p>
      <dl className="catalog-status-counts">
        <div>
          <dt>Promoted</dt>
          <dd>{catalog.promotedCount}</dd>
        </div>
        <div>
          <dt>Refused</dt>
          <dd>{catalog.refusedCount}</dd>
        </div>
        <div>
          <dt>Replayed</dt>
          <dd>{catalog.replayedCount}</dd>
        </div>
      </dl>
      {catalog.lastRefusalLabel && (
        <p className="catalog-status-reason">
          <b>Latest refusal:</b> {safeText(catalog.lastRefusalLabel)}
        </p>
      )}
      <ul className="catalog-status-actions">
        {catalog.actions.map((action) => (
          <li key={String(action.eventId)} className={`catalog-action is-${action.outcome}`}>
            <span className="catalog-action-outcome">
              {action.outcome === 'promoted' ? 'Promoted' : 'Refused'}
              {action.replayed ? ' (replay)' : ''}
            </span>
            <span className="catalog-action-key">{safeText(action.candidateKey ?? 'unnamed candidate')}</span>
            {action.outcome === 'promoted' ? (
              <small className="catalog-action-detail">
                {action.promotedFieldCount} field{action.promotedFieldCount === 1 ? '' : 's'} promoted
                {action.unsupportedFieldCount > 0 ? `, ${action.unsupportedFieldCount} unsupported` : ''}
              </small>
            ) : (
              <small className="catalog-action-detail">
                {/* An absent code resolves to the same static fallback as an
                    unrecognised one — the label never comes from the payload. */}
                {safeText(catalogRefusalLabel(action.reasonCode ?? ''))}
              </small>
            )}
          </li>
        ))}
      </ul>
    </section>
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
