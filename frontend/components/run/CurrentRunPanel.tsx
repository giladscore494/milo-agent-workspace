'use client';
import { useRef } from 'react';
import { safeText } from '@/lib/sanitize';
import { LaunchState } from '@/lib/types';
import { PollingMode } from '@/lib/useRunRealtime';
import { CancelRunControl } from './CancelRunControl';
import { LaunchStateNote } from './LaunchStateNote';

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
  // Where focus lands if the cancellation control is withdrawn from under it
  // because the run reached a terminal state.
  const headingRef = useRef<HTMLHeadingElement | null>(null);

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
      <h3 className="panel-title" ref={headingRef} tabIndex={-1}>Live run</h3>
      <dl className="run-facts">
        <div className="run-fact"><dt>Run</dt><dd className="identifier">{safeText(runId)}</dd></div>
        <div className="run-fact"><dt>Status</dt><dd>{safeText(runStatus ?? 'loading…')}</dd></div>
        <div className="run-fact"><dt>Phase</dt><dd>{safeText(phase)}</dd></div>
        <div className="run-fact"><dt>Connection</dt><dd>{connection === 'reconnecting' ? 'reconnecting…' : connection}</dd></div>
      </dl>
      <CancelRunControl
        idPrefix="run"
        available={!isTerminal}
        confirming={confirmingCancel}
        reason={cancelReason}
        error={cancelError}
        onReasonChange={onCancelReasonChange}
        onRequestCancel={onRequestCancel}
        onConfirm={onConfirmCancel}
        onKeepRunning={onKeepRunning}
        focusFallbackRef={headingRef}
      />
      {isTerminal && (
        <p className="run-verdict">
          Run finished with status <b>{safeText(runStatus)}</b>.
          {isPartialSuccess && ' Partial success is not a completed run: some tasks, coverage gaps, conflicts or verdicts remain outstanding.'}
        </p>
      )}
      <LaunchStateNote
        launchState={launchState}
        launchReconciliationRequired={launchReconciliationRequired}
        tone="muted"
      />
    </section>
  );
}
