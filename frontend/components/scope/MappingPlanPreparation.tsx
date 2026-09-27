'use client';
import { safeText } from '@/lib/sanitize';
import {
  PREPARATION_BLOCKER_COPY,
  PREPARATION_STATE_COPY,
  WorkScopePreparation,
  candidatesLabel,
  preparationReasonText,
} from '@/lib/workScope';

export type MappingPlanPreparationProps = {
  /** Whether THIS server can prepare from the website at all (capabilities). */
  serverCanPrepare: boolean;
  /** The revision on screen. */
  revision: number;
  /** `undefined` is "not read yet"; the error says why when a read failed. */
  preparation?: WorkScopePreparation;
  loading: boolean;
  busy: boolean;
  /** The caller's own authored sentence; never upstream error text. */
  error: string;
  unitName: (key: string) => string;
  onPrepare: () => void;
  onRefresh: () => void;
};

/**
 * Preparing ONE plan revision (E'): where it stands, and the one thing a
 * person may do about it.
 *
 * Everything shown is the server's status read (`lib/workScope.ts`), which the
 * server derives from durable database state only -- never from a job's exit
 * or a log. The Prepare button follows the server's `canPrepare` alone: a
 * missing or unreadable answer never unlocks it. Preparing captures the
 * Government register for this revision and builds its queue; it starts no
 * batch and spends nothing on models.
 */
export function MappingPlanPreparation({
  serverCanPrepare,
  revision,
  preparation,
  loading,
  busy,
  error,
  unitName,
  onPrepare,
  onRefresh,
}: MappingPlanPreparationProps) {
  const matches = preparation !== undefined && preparation.revision === revision;
  const enabled = serverCanPrepare && matches && preparation.canPrepare === true && !busy;

  return (
    <section className="mapping-plan-preparation" aria-labelledby="mapping-plan-preparation-title">
      <h4 className="section-title" id="mapping-plan-preparation-title">Preparation</h4>
      {error && <p className="alert" role="alert">{safeText(error)}</p>}
      {loading && preparation === undefined && <p className="muted">Loading the preparation…</p>}
      {matches && (
        <>
          <p role="status" aria-label="Preparation status">
            Revision {preparation.revision}: <strong>{PREPARATION_STATE_COPY[preparation.state]}</strong>
            {preparation.attempt > 1 && ` (attempt ${preparation.attempt})`}
          </p>
          {preparation.state === 'failed' && (
            <p className="note">
              {safeText(preparationReasonText(preparation.reasonCode))}{' '}
              <span className="identifier">{safeText(preparation.reasonCode ?? '')}</span>
            </p>
          )}
          {preparation.blockedBy !== undefined && preparation.state !== 'prepared' && (
            <p className="muted">{PREPARATION_BLOCKER_COPY[preparation.blockedBy]}</p>
          )}
          {preparation.state === 'prepared' && preparation.figures !== undefined && (
            <p className="muted">
              Queued {candidatesLabel(preparation.figures.queuedItemCount)} in{' '}
              {preparation.figures.batchCount} batch{preparation.figures.batchCount === 1 ? '' : 'es'}.
            </p>
          )}
          {preparation.state === 'prepared' && preparation.units.length > 0 && (
            <ol className="mapping-plan-preparation-units" aria-label="Prepared manufacturers">
              {preparation.units.map((unit) => (
                <li key={unit.unitKey}>
                  <span className="mapping-plan-unit-name">{safeText(unitName(unit.unitKey))}</span>
                  {unit.coverage !== undefined ? (
                    <span className="note mapping-plan-unit-coverage" aria-label="Variant coverage">
                      {' '}— {unit.coverage.enriched} enriched, {unit.coverage.ambiguous} ambiguous,
                      {' '}{unit.coverage.pending} pending, {unit.coverage.queued} queued
                    </span>
                  ) : (
                    <span className="note"> — {candidatesLabel(unit.queuedCount)} queued</span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </>
      )}
      <div className="button-row">
        {serverCanPrepare && (
          <button type="button" className="button button--primary" disabled={!enabled} onClick={onPrepare}>
            {busy ? 'Preparing…' : preparation?.state === 'failed' ? 'Prepare again' : 'Prepare this revision'}
          </button>
        )}
        <button type="button" className="button button--quiet" disabled={loading} onClick={onRefresh}>
          Refresh
        </button>
      </div>
      {serverCanPrepare && (
        <p className="note">
          Preparing reads the Government register for this revision and builds its batch queue. It starts no batch
          and makes no paid model call.
        </p>
      )}
    </section>
  );
}
