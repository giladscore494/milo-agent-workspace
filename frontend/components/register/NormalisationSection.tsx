'use client';
import { useCallback, useEffect, useState } from 'react';
import { safeErrorText } from '@/lib/errorText';
import { NormalisationGroup, NormalisationView, approvalGroups, parseNormalisation } from '@/lib/normalisation';
import { safeText } from '@/lib/sanitize';

export type NormalisationClient = {
  read: (projectId: string) => Promise<unknown>;
  request: (projectId: string, conversationId: string) => Promise<unknown>;
  approve: (projectId: string, expectedVersion: number, groups: unknown[]) => Promise<unknown>;
};

/**
 * PR-D3 — manufacturer normalisation (decisions 2, 14, 15): the approved
 * canonical manufacturers with their exact source names, the pending groups
 * (the code-owned rules, then the latest model proposal), the "Normalise
 * manufacturers" button (ONE guarded K3 call) and the owner's approvals:
 * every high-confidence, non-conflicting group together; any other alone.
 * Nothing is active until approved. Source names never change.
 */
export function NormalisationSection({ projectId, conversationId, client }: {
  projectId: string; conversationId?: string; client: NormalisationClient;
}) {
  const [view, setView] = useState<NormalisationView | undefined>();
  const [error, setError] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setView(parseNormalisation(await client.read(projectId)));
    } catch {
      // Unreadable (or off): the section is not shown; the Register page is unaffected.
      setView(undefined);
    }
  }, [client, projectId]);

  useEffect(() => { void load(); }, [load]);

  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError(undefined);
    try {
      await work();
    } catch (failure) {
      setError(safeErrorText(failure, 'The request was refused.'));
    } finally {
      setBusy(false);
      await load();
    }
  };

  if (!view) return null;
  const bulk = view.pending.filter((item) => item.bulkApprovable);
  const approve = (groups: NormalisationGroup[]) =>
    act(() => client.approve(projectId, view.version, approvalGroups(groups)));

  return (
    <section aria-label="Manufacturer normalisation" className="normalisation">
      <h4>Manufacturers</h4>
      {error && <p className="alert" role="alert">{safeText(error)}</p>}
      <p className="muted">
        Normalisation version {view.version}; {view.unmapped} source name{view.unmapped === 1 ? '' : 's'} unmapped.
        Filters and plans keep using the exact source names.
      </p>
      {view.canonical.length > 0 && (
        <ul aria-label="Canonical manufacturers">
          {view.canonical.map((item) => (
            <li key={item.canonical}>
              <strong>{safeText(item.canonical)}</strong> — {item.sources.map((s) => safeText(s)).join(', ')}
            </li>
          ))}
        </ul>
      )}
      {view.proposal?.status === 'requested' && (
        <p className="muted" role="status">A model proposal was requested. Reload when it ends; if it never
          does, press the button again.</p>
      )}
      {view.proposal?.status === 'refused' && (
        <p className="note">The last model proposal was refused: <span className="identifier">
          {safeText(view.proposal.reasonCode ?? 'UNKNOWN')}</span></p>
      )}
      <div className="register-actions">
        {view.canNormalise && (
          <button type="button" className="button" disabled={busy || !conversationId}
            onClick={() => conversationId && void act(() => client.request(projectId, conversationId))}>
            Normalise manufacturers
          </button>
        )}
        {view.canApprove && bulk.length > 0 && (
          <button type="button" className="button" disabled={busy} onClick={() => void approve(bulk)}>
            Approve {bulk.length} high-confidence group{bulk.length === 1 ? '' : 's'}
          </button>
        )}
      </div>
      {view.pending.length > 0 && (
        <table className="register-table">
          <caption>Pending groups — nothing is active until approved</caption>
          <thead>
            <tr><th scope="col">Canonical</th><th scope="col">Source names</th><th scope="col">Confidence</th>
              <th scope="col">Why</th><th scope="col"><span className="sr-only">Approve</span></th></tr>
          </thead>
          <tbody>
            {view.pending.map((item) => (
              <tr key={`${item.ruleId ?? item.proposalId}:${item.canonical}:${item.members.join('|')}`}>
                <td>{safeText(item.canonical)}</td>
                <td>{item.members.map((s) => safeText(s)).join(', ')}</td>
                <td>{item.confidence}{item.conflicting ? ' (conflicting)' : ''}</td>
                <td>{item.ruleId ? <span className="identifier">{item.ruleId}</span> : 'model'} {safeText(item.reason)}</td>
                <td>
                  {view.canApprove && !item.bulkApprovable && (
                    <button type="button" className="button button--quiet" disabled={busy}
                      onClick={() => void approve([item])}>Approve</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
