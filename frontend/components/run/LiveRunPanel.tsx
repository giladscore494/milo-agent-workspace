'use client';
import { LiveRunViewModel } from '@/lib/liveRunViewModel';
import { safeText } from '@/lib/sanitize';
import { PollingMode } from '@/lib/useRunRealtime';

export type LiveRunPanelProps = {
  visible: boolean;
  live: LiveRunViewModel;
  connection: PollingMode;
};

function count(value: number | undefined, unit?: string): string {
  if (value === undefined) return 'not reported';
  return unit ? `${value} ${unit}` : String(value);
}

function money(value: number | undefined): string {
  return value === undefined ? 'not reported' : `$${value.toFixed(4)}`;
}

/**
 * Live run visualization for BOTH engines, from durable truth only.
 *
 * Run state, engine, phase, work counts, what each active worker is doing,
 * evidence progress, provider pacing, spend against the run's ceilings and the
 * finalization state — every value is a durable fact from the run row or a
 * bounded projection of durable events. Refresh and reconnect rebuild it from
 * the same authenticated reads. Nothing infrastructural is shown: no worker
 * identity, lease, provider key, host or endpoint.
 */
export function LiveRunPanel({ visible, live, connection }: LiveRunPanelProps) {
  if (!visible) return null;
  const { usage, limits } = live;
  return (
    <section className="panel live-run" aria-labelledby="live-run-title" data-terminal={live.terminal}>
      <header className="swarm-card-head">
        <div className="swarm-card-heading">
          <h3 className="panel-title" id="live-run-title">Live execution</h3>
          <p className="eyebrow">{live.engineLabel}{live.engineVersion ? ` · ${safeText(live.engineVersion)}` : ''}</p>
        </div>
        {connection === 'reconnecting' && <span className="badge swarm-reconnect">Reconnecting…</span>}
      </header>

      <dl className="run-facts">
        <div className="run-fact"><dt>Durable state</dt><dd>{live.status ? `${safeText(live.status)} · ${live.terminal ? 'terminal' : 'live'}` : 'loading…'}</dd></div>
        <div className="run-fact"><dt>Current phase</dt><dd>{safeText(live.phaseLabel)}</dd></div>
        <div className="run-fact"><dt>Finalization</dt><dd>{live.finalization.state === 'live' ? 'not yet finalized' : live.finalization.state === 'finalized' ? 'finalized with canonical outcome' : 'terminal, no canonical outcome recorded'}</dd></div>
      </dl>

      <section className="swarm-section" aria-labelledby="live-work-title">
        <h4 className="section-title" id="live-work-title">Work</h4>
        {live.work ? (
          <p className="live-work" data-testid="live-work">
            {live.work.total} {live.work.unit} · {live.work.queued} queued · {live.work.running} active · {live.work.completed} completed · {live.work.failed} failed
          </p>
        ) : (
          <p className="muted">No work items reported yet.</p>
        )}
        {live.active.length > 0 ? (
          <ul className="live-active" aria-label="Active workers">
            {live.active.map((worker, index) => (
              <li key={`${worker.name}-${index}`}><span className="identifier">{safeText(worker.name)}</span> — {safeText(worker.doing)}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">{live.terminal ? 'No active workers: the run is terminal.' : 'No worker is reported active right now.'}</p>
        )}
      </section>

      <section className="swarm-section" aria-labelledby="live-evidence-title">
        <h4 className="section-title" id="live-evidence-title">Research and evidence</h4>
        <dl className="run-facts">
          {live.evidence.sources !== undefined && <div className="run-fact"><dt>Sources</dt><dd>{live.evidence.sources}</dd></div>}
          {live.evidence.claims !== undefined && <div className="run-fact"><dt>Claims</dt><dd>{live.evidence.claims}</dd></div>}
          {live.evidence.conflicts !== undefined && <div className="run-fact"><dt>Conflicts</dt><dd>{live.evidence.conflicts}</dd></div>}
          {live.evidence.verifierBatches !== undefined && <div className="run-fact"><dt>Verifier batches</dt><dd>{live.evidence.verifierBatches}</dd></div>}
          {live.evidence.verificationStarted !== undefined && (
            <div className="run-fact"><dt>Verification</dt><dd>{live.evidence.verificationCompleted ? 'completed' : live.evidence.verificationStarted ? 'in progress' : 'not started'}</dd></div>
          )}
        </dl>
        {live.evidence.sources === undefined && live.evidence.claims === undefined && (
          <p className="muted">No evidence facts are stated for this run yet.</p>
        )}
      </section>

      <section className="swarm-section" aria-labelledby="live-provider-title">
        <h4 className="section-title" id="live-provider-title">Provider and budget</h4>
        <dl className="run-facts">
          <div className="run-fact"><dt>Backpressure events</dt><dd>{count(live.provider.backpressureEvents)}</dd></div>
          <div className="run-fact"><dt>Semantic retries</dt><dd>{count(live.provider.retries)}</dd></div>
          <div className="run-fact"><dt>Pacing events observed</dt><dd>{live.provider.pacingEventsObserved}</dd></div>
          <div className="run-fact"><dt>Model calls</dt><dd>{count(usage.modelCalls)}{limits.maxModelCalls !== undefined ? ` / ${limits.maxModelCalls}` : ''}</dd></div>
          <div className="run-fact"><dt>Tokens</dt><dd>{count(usage.totalTokens)}{limits.maxTotalTokens !== undefined ? ` / ${limits.maxTotalTokens}` : ''}</dd></div>
          <div className="run-fact"><dt>Spend</dt><dd>{money(usage.actualCost ?? usage.estimatedCost)}{limits.maxCost !== undefined ? ` / $${limits.maxCost.toFixed(2)}` : ''}</dd></div>
          <div className="run-fact"><dt>Elapsed</dt><dd>{usage.elapsedSeconds === undefined ? 'not reported' : `${Math.round(usage.elapsedSeconds)} s`}{limits.maxDurationSeconds !== undefined ? ` / ${limits.maxDurationSeconds} s` : ''}</dd></div>
        </dl>
        {live.spendRatio !== undefined && (
          <p className="note">Spend at {Math.round(live.spendRatio * 100)}% of the run's cost ceiling.</p>
        )}
        {!usage.present && <p className="muted">No usage has been recorded for this run yet.</p>}
      </section>

      <section className="swarm-section" aria-labelledby="live-concurrency-title">
        <h4 className="section-title" id="live-concurrency-title">Effective concurrency</h4>
        {limits.concurrency ? (
          <>
            <p className="muted">
              Server-derived from the canonical runtime policy. Logical engine width is what an engine may run at once; provider-admitted is what the provider profile actually lets through. The smaller binds.
            </p>
            <dl className="run-facts" data-testid="live-concurrency">
              {live.engine !== 'swarm_v2' && (
                <div className="run-fact"><dt>V1 technical parallelism (logical)</dt><dd>{count(limits.concurrency.v1TechnicalParallelism)}</dd></div>
              )}
              {live.engine !== 'swarm_v2' && (
                <div className="run-fact"><dt>V1 provider-admitted</dt><dd>{count(limits.concurrency.v1ProviderAdmitted)}</dd></div>
              )}
              {live.engine !== 'vehicle_catalog_v1' && (
                <div className="run-fact"><dt>V2 active workers (logical)</dt><dd>{count(limits.concurrency.v2MaxActiveWorkers)}</dd></div>
              )}
              {live.engine !== 'vehicle_catalog_v1' && (
                <div className="run-fact"><dt>V2 provider-admitted</dt><dd>{count(limits.concurrency.v2ProviderAdmitted)}</dd></div>
              )}
              <div className="run-fact"><dt>Provider concurrency (this process)</dt><dd>{count(limits.concurrency.providerMaxConcurrency)}{limits.concurrency.providerOrganizationCeiling !== undefined ? ` of ${limits.concurrency.providerOrganizationCeiling} organization ceiling` : ''}</dd></div>
              <div className="run-fact"><dt>Provider effective</dt><dd>{count(limits.concurrency.providerEffectiveConcurrency)}</dd></div>
              <div className="run-fact"><dt>Concurrent runs per user / project</dt><dd>{count(limits.concurrency.maxConcurrentRunsPerUser)} / {count(limits.concurrency.maxConcurrentRunsPerProject)}</dd></div>
              <div className="run-fact"><dt>Search QPS basic / pro</dt><dd>{count(limits.concurrency.searchBasicQps)} / {count(limits.concurrency.searchProQps)}{limits.concurrency.searchQpsVerified ? '' : ' (unverified fallback)'}</dd></div>
            </dl>
          </>
        ) : (
          <p className="muted">The server stated no resolvable concurrency for this deployment.</p>
        )}
      </section>

    </section>
  );
}
