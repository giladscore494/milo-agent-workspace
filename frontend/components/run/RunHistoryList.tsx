'use client';
import { parseProductOutcome, describeProductOutcome } from '@/lib/productOutcome';
import { runIdentityWorkflowKey } from '@/lib/runIdentity';
import { safeText } from '@/lib/sanitize';
import { RunSummary } from '@/lib/types';

export type RunHistoryListProps = {
  visible: boolean;
  runs?: RunSummary[];
  loading: boolean;
  error: string;
  activeRunId?: string;
  onSelect: (runId: string) => void;
  onRetry: () => void;
};

const ENGINE_LABELS: Record<string, string> = { vehicle_catalog_v1: 'V1', swarm_v2: 'V2' };

/**
 * The conversation's durable run history.
 *
 * This is what lets a completed result outlive the browser: the ids the
 * workspace remembers live in session storage and a restart forgets them, but
 * the history is read from the server under the same membership
 * authorization as the run itself. Each row shows the run as the engine it
 * WAS (from its immutable identity) and the verdict the finalizer recorded.
 */
export function RunHistoryList({ visible, runs, loading, error, activeRunId, onSelect, onRetry }: RunHistoryListProps) {
  if (!visible) return null;
  return (
    <section className="panel panel--quiet run-history" aria-labelledby="run-history-title">
      <h3 className="panel-title" id="run-history-title">Run history</h3>
      {loading && <p className="muted">Loading runs…</p>}
      {error && (
        <p className="alert" role="alert">
          {error} <button type="button" className="button button--quiet" onClick={onRetry}>Retry</button>
        </p>
      )}
      {!loading && !error && runs !== undefined && runs.length === 0 && (
        <p className="muted">No runs have been recorded in this conversation.</p>
      )}
      {runs !== undefined && runs.length > 0 && (
        <ul className="run-history-list">
          {runs.map((run) => {
            const workflow = runIdentityWorkflowKey(run as unknown as Parameters<typeof runIdentityWorkflowKey>[0]);
            const outcome = parseProductOutcome((run as unknown as Record<string, unknown>).product_outcome);
            const verdict = outcome ? describeProductOutcome(outcome).label : undefined;
            const active = run.id === activeRunId;
            return (
              <li key={run.id} className="run-history-item" data-active={active}>
                <button type="button" className="run-history-button" aria-current={active ? 'true' : undefined} onClick={() => onSelect(run.id)}>
                  <span className="identifier run-history-id">{safeText(run.id.slice(0, 8))}</span>
                  <span className="badge">{workflow ? ENGINE_LABELS[workflow] ?? safeText(workflow) : 'identity unavailable'}</span>
                  <span className="run-history-status">{safeText(run.status)}</span>
                  {verdict && <span className="run-history-verdict">{verdict}</span>}
                  {run.created_at && <time className="run-history-time" dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
