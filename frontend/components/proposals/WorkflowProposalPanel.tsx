'use client';
import { InternetBadge } from '@/components/common/InternetBadge';
import { redactSecrets, safeText } from '@/lib/sanitize';
import { InternetPolicy, Proposal } from '@/lib/types';

const PANEL_ID = 'workflow-proposal-panel';

export type WorkflowProposalPanelProps = {
  executionUi: boolean;
  hasProject: boolean;
  hardeningNote: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  request: string;
  onRequestChange: (value: string) => void;
  proposal?: Proposal;
  error: string;
  busy: boolean;
  onGenerate: () => void;
  onRevise: () => void;
  onDecide: (decision: 'approve' | 'reject') => void;
};

/**
 * Secondary "advanced" surface. The workflow-proposal API behaviour is
 * unchanged: this panel only moves it behind a keyboard-accessible disclosure
 * so the conversation stays the primary surface.
 */
export function WorkflowProposalPanel({
  executionUi,
  hasProject,
  hardeningNote,
  open,
  onOpenChange,
  request,
  onRequestChange,
  proposal,
  error,
  busy,
  onGenerate,
  onRevise,
  onDecide,
}: WorkflowProposalPanelProps) {
  if (!executionUi || !hasProject) {
    return (
      <section className="panel panel--quiet">
        <h3 className="panel-title">Workflow proposal</h3>
        <p className="muted">{executionUi ? 'Select a project to draft a workflow proposal.' : hardeningNote}</p>
      </section>
    );
  }

  return (
    <section className="panel">
      <h3 className="panel-title">
        <button type="button" className="disclosure" aria-expanded={open} aria-controls={PANEL_ID} onClick={() => onOpenChange(!open)}>
          <span className="disclosure-marker" aria-hidden="true">{open ? '▾' : '▸'}</span>
          Workflow proposal
        </button>
      </h3>
      {open && (
        <div id={PANEL_ID} className="panel-body">
          <div className="field">
            <label className="field-label sr-only" htmlFor="proposal-request">Proposal request</label>
            <textarea
              id="proposal-request"
              value={request}
              onChange={(event) => onRequestChange(event.target.value)}
              placeholder="Describe the workflow you need…"
              rows={3}
            />
          </div>
          <div className="button-row">
            <button type="button" className="button button--primary" onClick={onGenerate} disabled={busy || !request.trim()}>
              {busy ? 'Working…' : 'Generate proposal'}
            </button>
            {proposal && (
              <button type="button" className="button button--quiet" onClick={onRevise} disabled={busy || !request.trim()}>
                Revise with new request
              </button>
            )}
          </div>
          {error && <p className="alert" role="alert">{safeText(error)}</p>}
          {proposal && (
            <div className="proposal-detail">
              <p><span className={`badge is-status-${safeText(proposal.status)}`}>{safeText(proposal.status)}</span></p>
              <p>{safeText(proposal.user_request)}</p>
              {(proposal.draft?.agents ?? []).map((agent: any) => (
                <p key={agent.key ?? agent.role}>
                  <b>{safeText(agent.role ?? agent.key)}</b>{' '}
                  <InternetBadge policy={(agent.internet_policy ?? 'conditional') as InternetPolicy} reason={agent.internet_reason} />
                </p>
              ))}
              <pre className="code-block">{JSON.stringify(redactSecrets({ steps: proposal.draft?.workflow ?? proposal.draft?.steps ?? [], budget: proposal.estimates, critiques: proposal.critiques }), null, 2)}</pre>
              <div className="button-row">
                <button type="button" className="button button--primary" onClick={() => onDecide('approve')} disabled={busy || proposal.status !== 'approved'}>Approve</button>
                <button type="button" className="button button--quiet" onClick={() => onDecide('reject')} disabled={busy}>Reject</button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
