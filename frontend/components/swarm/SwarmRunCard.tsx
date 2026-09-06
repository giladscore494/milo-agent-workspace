'use client';
import { safeText } from '@/lib/sanitize';
import { SwarmRunViewModel } from '@/lib/swarmViewModel';
import { LaunchState } from '@/lib/types';
import { PollingMode } from '@/lib/useRunRealtime';
import { SwarmStageTrack } from './SwarmStageTrack';
import { SwarmTaskList } from './SwarmTaskList';
import {
  describeSwarmCommander,
  describeSwarmLifecycle,
  describeSwarmUsage,
  describeSwarmVerification,
} from './swarmPresentation';

export type SwarmRunCardProps = {
  swarm: SwarmRunViewModel;
  runId: string;
  connection: PollingMode;
  launchState?: LaunchState;
  launchReconciliationRequired?: boolean;
  confirmingCancel: boolean;
  cancelReason: string;
  cancelError: string;
  onCancelReasonChange: (value: string) => void;
  onRequestCancel: () => void;
  onConfirmCancel: () => void;
  onKeepRunning: () => void;
};

/**
 * The Swarm V2 execution surface.
 *
 * It renders the `SwarmRunViewModel` and nothing else: it never reads the raw
 * event stream (that stays in the inspector), never derives usage by counting
 * events, and never moves the run into a terminal state before the backend
 * reports one — requesting cancellation is shown as a request, not as a result.
 *
 * The three execution quantities stay separate and separately labelled, because
 * they are genuinely different numbers: the accepted smoke run is 5 logical
 * tasks and 7 model calls, and no element here can turn either into "7 agents".
 * There is no agent concept in Swarm V2 and none is reconstructed.
 *
 * This card is not the final-result renderer. It reports the durable terminal
 * status; the sanitized output surface stays where it is until F4 defines
 * final-result semantics.
 */
export function SwarmRunCard({
  swarm,
  runId,
  connection,
  launchState,
  launchReconciliationRequired,
  confirmingCancel,
  cancelReason,
  cancelError,
  onCancelReasonChange,
  onRequestCancel,
  onConfirmCancel,
  onKeepRunning,
}: SwarmRunCardProps) {
  const lifecycle = describeSwarmLifecycle(swarm);
  const commander = describeSwarmCommander(swarm);
  const verification = describeSwarmVerification(swarm);
  const usage = describeSwarmUsage(swarm);
  const finished = lifecycle.finished;
  const cancellationRequested = swarm.runStatus === 'cancellation_requested';

  return (
    <section className="panel swarm-card" data-live={lifecycle.live} aria-labelledby="swarm-run-title">
      <header className="swarm-card-head">
        <div className="swarm-card-heading">
          <h3 className="panel-title" id="swarm-run-title">Swarm run</h3>
          <p className="eyebrow">Swarm V2 execution</p>
        </div>
        {/* Polling stays visually silent; only a failing poll says anything,
            and it never clears the task rows already on screen. */}
        {connection === 'reconnecting' && (
          <span className="badge swarm-reconnect">Reconnecting…</span>
        )}
      </header>

      {/* Only the four lifecycle stages and the terminal outcome are announced.
          Task rows, counters and usage change far too often to announce. */}
      <div className="swarm-lifecycle" aria-live="polite">
        <p className="swarm-headline">
          {lifecycle.live && <span className="swarm-pulse" aria-hidden="true" />}
          <span className="swarm-headline-text">{lifecycle.headline}</span>
        </p>
        {lifecycle.detail && <p className="swarm-detail">{lifecycle.detail}</p>}
        {lifecycle.planAdjusted && <p className="swarm-adjusted">Plan adjusted</p>}
      </div>

      <SwarmStageTrack stages={lifecycle.stages} />

      {cancellationRequested && (
        <p className="swarm-note-strong">
          Cancellation requested. The run stays active until the backend reports a terminal state.
        </p>
      )}

      <section className="swarm-section">
        <h4 className="section-title">Commander</h4>
        <p className="swarm-commander">
          {commander.status}
          {commander.revisionLabel && <span className="swarm-commander-meta"> · {commander.revisionLabel}</span>}
          {commander.replanLabel && <span className="swarm-commander-meta"> · {commander.replanLabel}</span>}
        </p>
        {/* A backend enum, shown verbatim and subdued. The UI does not translate
            it into a human explanation of why the plan changed. */}
        {commander.decisionCode && (
          <p className="swarm-decision">
            Last replan decision <span className="identifier">{safeText(commander.decisionCode)}</span>
          </p>
        )}
      </section>

      <section className="swarm-section">
        <h4 className="section-title">Logical tasks</h4>
        <SwarmTaskList tasks={swarm.tasks} runningCount={swarm.taskCounts.running} />
        <p className="note">
          Tool calls, repairs, evidence and verifier batches are activity inside these tasks, never extra tasks.
        </p>
      </section>

      {verification.length > 0 && (
        <section className="swarm-section">
          <h4 className="section-title">Verification</h4>
          <ul className="swarm-facts">
            {verification.map((entry) => (
              <li className="swarm-fact" key={entry.key}>
                <span className="swarm-fact-label">{entry.label}</span>
                {entry.value !== undefined && <span className="swarm-fact-value">{entry.value}</span>}
              </li>
            ))}
          </ul>
          <p className="note">Verifier batches are progress within one verification, not separate tasks.</p>
        </section>
      )}

      <section className="swarm-section">
        <h4 className="section-title">Usage and scale</h4>
        <dl className="swarm-usage">
          {usage.map((entry) => (
            <div className="swarm-usage-item" key={entry.key} data-known={entry.known}>
              <dt>{entry.label}</dt>
              <dd>{entry.value}</dd>
            </div>
          ))}
        </dl>
        <p className="note">
          Model calls come from the run&rsquo;s authoritative usage aggregate. They are not logical tasks and not agents.
        </p>
      </section>

      {/* Cancellation stays available only while the run is active, keeps the
          existing confirm-with-optional-reason interaction, and leaves the API
          call and the terminal verdict to the backend. */}
      {!finished && !confirmingCancel && (
        <button type="button" className="button button--quiet" onClick={onRequestCancel}>Cancel run</button>
      )}
      {!finished && confirmingCancel && (
        <div className="cancel-form">
          <div className="field">
            <label className="field-label sr-only" htmlFor="swarm-cancel-reason">Cancellation reason</label>
            <input
              id="swarm-cancel-reason"
              value={cancelReason}
              onChange={(event) => onCancelReasonChange(event.target.value)}
              placeholder="Reason (optional)"
            />
          </div>
          <div className="button-row">
            <button type="button" className="button button--danger" onClick={onConfirmCancel}>Confirm cancellation</button>
            <button type="button" className="button button--quiet" onClick={onKeepRunning}>Keep running</button>
          </div>
        </div>
      )}
      {cancelError && <p className="alert" role="alert">{safeText(cancelError)}</p>}

      {finished && (
        <p className="run-verdict">
          Run finished with status <b>{safeText(swarm.runStatus ?? swarm.lifecycleLabel)}</b>.
          {swarm.partialSuccess && ' Partial success is not a completed run: some tasks, coverage gaps, conflicts or verdicts remain outstanding.'}
        </p>
      )}

      {/* Launch and reconciliation are operational detail, kept subordinate to
          the execution story above. */}
      <footer className="swarm-card-foot">
        <span className="identifier">Run {safeText(runId)}</span>
        {swarm.runStatus && <span className="note">Status {safeText(swarm.runStatus)}</span>}
        {launchState && (
          <span className="note">
            Launch {safeText(launchState)}{launchReconciliationRequired ? ' · reconciliation required' : ''}
          </span>
        )}
      </footer>
    </section>
  );
}
