'use client';
import { safeText } from '@/lib/sanitize';
import {
  BATCH_STATE_COPY,
  START_BLOCKER_COPY,
  UNIT_PROGRESS_COPY,
  UNIT_REASON_COPY,
  ProgressBatch,
  WorkScopeProgress,
  candidatesLabel,
} from '@/lib/workScope';

export type MappingPlanProgressProps = {
  /** `undefined` is "not read yet"; the error says why when a read failed. */
  progress?: WorkScopeProgress;
  loading: boolean;
  /** A start, pause, resume or cancel is in flight. */
  busy: boolean;
  /** The caller's own authored sentence; never upstream error text. */
  error: string;
  /** The start being confirmed, if any: nothing starts before confirmation. */
  confirming: boolean;
  unitName: (key: string) => string;
  onRequestStart: () => void;
  onConfirmStart: () => void;
  onCancelStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancelBatch: () => void;
  onOpenRun: (runId: string) => void;
  onRefresh: () => void;
};

function batchLabel(batch: Pick<ProgressBatch, 'batchNumber' | 'unitKey' | 'itemCount'>,
                    total: number | undefined, unitName: (key: string) => string): string {
  const of = total !== undefined && total > 0 ? ` of ${total}` : '';
  return `Batch ${batch.batchNumber}${of} — ${unitName(batch.unitKey)}, ${candidatesLabel(batch.itemCount)}`;
}

/**
 * The plan's batches: where it stands, and the ONE thing a person may do next.
 *
 * Everything shown is the server's progress read (`lib/workScope.ts`), which
 * the server derives from the bound runs' own durable status and their
 * promotion events — never counted in the browser. The buttons follow the
 * server's `controls`, and each start is confirmed first: a start creates ONE
 * paid run for ONE batch, and nothing ever starts the next batch by itself.
 */
export function MappingPlanProgress({
  progress,
  loading,
  busy,
  error,
  confirming,
  unitName,
  onRequestStart,
  onConfirmStart,
  onCancelStart,
  onPause,
  onResume,
  onCancelBatch,
  onOpenRun,
  onRefresh,
}: MappingPlanProgressProps) {
  const preparation = progress?.preparation;
  const totalBatches = preparation?.batches.total;
  const start = progress?.controls.start;
  const startBatch = start?.batch;
  const live = progress?.live;
  // No worker was ever started for the running batch: its launch never
  // happened, or definitely failed. Only launching it can move it on.
  const unlaunched = live !== undefined && live.runStatus === 'queued'
    && (live.launchState === 'pending' || live.launchState === 'launch_failed');

  return (
    <section className="mapping-plan-progress" aria-labelledby="mapping-plan-progress-title">
      <h4 className="section-title" id="mapping-plan-progress-title">Batches</h4>
      {error && <p className="alert" role="alert">{safeText(error)}</p>}
      {loading && progress === undefined && <p className="muted">Loading the plan’s progress…</p>}
      {progress !== undefined && (
        <>
          {progress.paused && (
            <p className="note" role="status">Paused — no batch will start until the plan is resumed. A running batch is not stopped by a pause.</p>
          )}

          {preparation === undefined ? (
            <p className="muted">
              The plan’s current revision ({progress.revision}) has not been prepared yet. An operator prepares a revision
              from the Government register; no batch can start until then.
            </p>
          ) : (
            <>
              <p className="muted">
                Prepared from revision {preparation.revision}: {candidatesLabel(preparation.items.total)} in{' '}
                {preparation.batches.total} batch{preparation.batches.total === 1 ? '' : 'es'}.
              </p>
              <dl className="mapping-plan-totals" aria-label="Plan progress">
                <div><dt>Batches finished</dt><dd>{preparation.batches.settled} of {preparation.batches.total}</dd></div>
                <div><dt>Candidates done</dt><dd>{preparation.items.completed} of {preparation.items.total}</dd></div>
                <div><dt>Promoted</dt><dd>{preparation.items.promoted}</dd></div>
                <div><dt>Refused</dt><dd>{preparation.items.refused}</dd></div>
                <div><dt>Unresolved</dt><dd>{preparation.items.unresolved}</dd></div>
                <div><dt>Remaining</dt><dd>{preparation.items.remaining}</dd></div>
              </dl>
            </>
          )}

          {live !== undefined && (
            <div className="mapping-plan-live" role="status" aria-label="Current batch">
              <p>
                <strong>Current:</strong> {safeText(batchLabel(live, totalBatches, unitName))} — {safeText(live.runStatus)}
                {live.attempt > 1 && ` (attempt ${live.attempt})`}
              </p>
              {live.revision !== progress.revision && (
                <p className="note">This batch belongs to revision {live.revision}; the plan has been revised since it started.</p>
              )}
              {unlaunched && (live.revision === progress.revision ? (
                <p className="note">
                  {live.launchState === 'launch_failed'
                    ? 'The worker for this batch could not be started.'
                    : 'The worker for this batch has not been started.'}
                  {' '}Launching it starts this same run; nothing is created twice.
                </p>
              ) : (
                <p className="note">
                  No worker was started for this batch, and a batch of an earlier revision is never launched. It holds
                  the plan until an operator resolves it: no other batch can start until then.
                </p>
              ))}
              <div className="button-row">
                <button type="button" className="button button--quiet" onClick={() => onOpenRun(live.runId)}>
                  Show this run
                </button>
                {progress.controls.cancel.available && (
                  <button type="button" className="button button--quiet" disabled={busy} onClick={onCancelBatch}>
                    Cancel this batch
                  </button>
                )}
              </div>
            </div>
          )}

          {start?.available && startBatch !== undefined ? (
            confirming ? (
              <div className="mapping-plan-confirm" role="group" aria-label="Confirm batch start">
                <p>
                  {start.relaunch ? 'Launch' : start.retry ? 'Retry' : 'Start'} {safeText(batchLabel(startBatch, totalBatches, unitName))}?
                  {' '}This starts ONE paid run for this batch only. Nothing else starts automatically.
                </p>
                <div className="button-row">
                  <button type="button" className="button button--primary" disabled={busy} onClick={onConfirmStart}>
                    {busy ? 'Starting…' : 'Yes, start this batch'}
                  </button>
                  <button type="button" className="button button--quiet" disabled={busy} onClick={onCancelStart}>
                    Not now
                  </button>
                </div>
              </div>
            ) : (
              <div className="button-row">
                <button type="button" className="button button--primary" disabled={busy} onClick={onRequestStart}>
                  {start.relaunch
                    ? `Launch batch ${startBatch.batchNumber}`
                    : start.retry
                      ? `Retry batch ${startBatch.batchNumber}`
                      : preparation !== undefined && preparation.batches.settled > 0
                        ? `Continue with batch ${startBatch.batchNumber}`
                        : `Start batch ${startBatch.batchNumber}`}
                </button>
              </div>
            )
          ) : (
            // "Running" and "not prepared" are already said above, in full.
            start?.blockedBy !== undefined && start.blockedBy !== 'batch_running'
              && start.blockedBy !== 'not_prepared'
              && <p className="muted">{START_BLOCKER_COPY[start.blockedBy]}</p>
          )}

          {(progress.controls.pause.available || progress.controls.resume.available) && (
            <div className="button-row">
              {progress.controls.pause.available && (
                <button type="button" className="button button--quiet" disabled={busy} onClick={onPause}>Pause plan</button>
              )}
              {progress.controls.resume.available && (
                <button type="button" className="button button--quiet" disabled={busy} onClick={onResume}>Resume plan</button>
              )}
            </div>
          )}

          {preparation !== undefined && preparation.units.length > 0 && (
            <ol className="mapping-plan-unit-progress" aria-label="Manufacturers in priority order">
              {preparation.units.map((unit) => (
                <li key={unit.unitKey}>
                  <span className="mapping-plan-unit-name">{unit.priority}. {safeText(unitName(unit.unitKey))}</span>
                  <span className="note"> — {UNIT_PROGRESS_COPY[unit.progress]}</span>
                  {unit.state === 'prepared' && unit.batchCount > 0 && (
                    <span className="note">
                      {' '}· {unit.settledBatches} of {unit.batchCount} batch{unit.batchCount === 1 ? '' : 'es'} finished
                      {' '}· {unit.promoted} promoted, {unit.refused} refused, {unit.unresolved} unresolved
                    </span>
                  )}
                  {unit.state !== 'prepared' && (
                    <span className="note">
                      {' '}· {unit.reasonCode !== undefined && UNIT_REASON_COPY[unit.reasonCode]
                        ? UNIT_REASON_COPY[unit.reasonCode] : 'Nothing was queued for it.'}
                    </span>
                  )}
                </li>
              ))}
            </ol>
          )}

          {preparation !== undefined && preparation.recent.length > 0 && (
            <>
              <h5 className="section-title">Recent batches</h5>
              <ol className="mapping-plan-recent" aria-label="Recently started batches, newest first">
                {preparation.recent.map((item) => (
                  <li key={item.batchId}>
                    {safeText(batchLabel(item, totalBatches, unitName))} — {BATCH_STATE_COPY[item.state]}
                    {item.state !== 'active' && item.promoted !== undefined && (
                      <> · {item.promoted} promoted, {item.refused} refused{item.state === 'completed' || item.state === 'partial' ? `, ${item.unresolved} unresolved` : ''}</>
                    )}
                    {item.runId !== undefined && (
                      <>
                        {' '}
                        <button type="button" className="button button--quiet" onClick={() => onOpenRun(item.runId as string)}>
                          Show run
                        </button>
                      </>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}
        </>
      )}
      <div className="button-row">
        <button type="button" className="button button--quiet" disabled={loading} onClick={onRefresh}>Refresh progress</button>
      </div>
    </section>
  );
}
