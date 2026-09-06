'use client';
import { safeText } from '@/lib/sanitize';
import { LaunchState } from '@/lib/types';
import { PollingMode } from '@/lib/useRunRealtime';

export type CurrentRunPanelProps = {
  executionUi: boolean;
  hasConversation: boolean;
  runId?: string;
  runStatus?: string;
  phase: string;
  connection: PollingMode;
  isTerminal: boolean;
  isPartialSuccess: boolean;
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
 * Current run surface: durable status, launch state, cancellation and the
 * terminal verdict. Status strings are rendered exactly as the backend reports
 * them — no lifecycle is inferred and no progress is invented here.
 */
export function CurrentRunPanel({
  executionUi,
  hasConversation,
  runId,
  runStatus,
  phase,
  connection,
  isTerminal,
  isPartialSuccess,
  launchState,
  launchReconciliationRequired,
  confirmingCancel,
  cancelReason,
  cancelError,
  onCancelReasonChange,
  onRequestCancel,
  onConfirmCancel,
  onKeepRunning,
}: CurrentRunPanelProps) {
  if (!executionUi || !hasConversation) {
    return (
      <section className="panel panel--quiet">
        <h3 className="panel-title">Live run</h3>
        <p className="muted">
          {executionUi
            ? 'Select or create a conversation to start a run.'
            : 'No active run. Run creation and execution control are disabled until a separately approved execution stage.'}
        </p>
      </section>
    );
  }

  if (!runId) {
    return (
      <section className="panel panel--quiet">
        <h3 className="panel-title">Live run</h3>
        <p className="muted">Nothing is running in this conversation yet. Send a task to start a run.</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <h3 className="panel-title">Live run</h3>
      <dl className="run-facts">
        <div className="run-fact"><dt>Run</dt><dd className="identifier">{safeText(runId)}</dd></div>
        <div className="run-fact"><dt>Status</dt><dd>{safeText(runStatus ?? 'loading…')}</dd></div>
        <div className="run-fact"><dt>Phase</dt><dd>{safeText(phase)}</dd></div>
        <div className="run-fact"><dt>Connection</dt><dd>{connection === 'reconnecting' ? 'reconnecting…' : connection}</dd></div>
      </dl>
      {!isTerminal && !confirmingCancel && (
        <button type="button" className="button button--quiet" onClick={onRequestCancel}>Cancel run</button>
      )}
      {confirmingCancel && (
        <div className="cancel-form">
          <div className="field">
            <label className="field-label sr-only" htmlFor="cancel-reason">Cancellation reason</label>
            <input id="cancel-reason" value={cancelReason} onChange={(event) => onCancelReasonChange(event.target.value)} placeholder="Reason (optional)" />
          </div>
          <div className="button-row">
            <button type="button" className="button button--danger" onClick={onConfirmCancel}>Confirm cancellation</button>
            <button type="button" className="button button--quiet" onClick={onKeepRunning}>Keep running</button>
          </div>
        </div>
      )}
      {cancelError && <p className="alert" role="alert">{safeText(cancelError)}</p>}
      {isTerminal && (
        <p className="run-verdict">
          Run finished with status <b>{safeText(runStatus)}</b>.
          {isPartialSuccess && ' Partial success is not a completed run: some tasks, coverage gaps, conflicts or verdicts remain outstanding.'}
        </p>
      )}
      {launchState && (
        <p className="muted">
          Launch state <b>{safeText(launchState)}</b>
          {launchReconciliationRequired ? ' — reconciliation required.' : '.'}
        </p>
      )}
    </section>
  );
}
