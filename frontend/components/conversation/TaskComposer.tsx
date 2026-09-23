'use client';
import { safeText } from '@/lib/sanitize';
import type { DirectRunBlocker } from '@/lib/workScope';

/**
 * Where a task typed in this conversation may go, from the SERVER's answer.
 *
 * - `direct`: the ordinary composer creates a run (every non-catalog project,
 *   and a Swarm V2 project whose runs read no catalog).
 * - `unconfirmed`: a Swarm V2 project whose capability read has not answered
 *   (or failed). Whether a task here would be a catalog run is unknown, so no
 *   run is offered until it is known.
 * - `blocked`: the server said an ordinary run cannot be created here, and why.
 *   `catalog_batch_required` sends the person to the Mapping Plan, the one
 *   path by which catalog work runs.
 */
export type ComposerRoute =
  | { kind: 'direct' }
  | { kind: 'unconfirmed' }
  | { kind: 'blocked'; blockedBy: DirectRunBlocker; planAvailable: boolean };

export type TaskComposerProps = {
  executionUi: boolean;
  hasConversation: boolean;
  content: string;
  onContentChange: (value: string) => void;
  onSubmit: () => void;
  submitting: boolean;
  error: string;
  route?: ComposerRoute;
  /** Opens the Mapping Plan panel, where catalog batches start. */
  onOpenMappingPlan?: () => void;
  /** Re-reads the project's capabilities after an unconfirmed answer. */
  onRecheck?: () => void;
};

const BLOCKED_COPY: Readonly<Record<DirectRunBlocker, string>> = {
  catalog_batch_required:
    'Runs in this project read the Government catalog, so they start from the Mapping Plan: plan the manufacturers, '
    + 'an operator prepares that revision, and you start its next batch there. A task typed here would not run.',
  run_creation_disabled: 'Starting runs is turned off at the current activation stage.',
  unknown: 'The server did not confirm that a task can run in this project, so none is offered.',
};

/**
 * Task submission surface. Submission itself (idempotency key lifetime,
 * run-id storage, error handling) stays owned by the page.
 */
export function TaskComposer({
  executionUi,
  hasConversation,
  content,
  onContentChange,
  onSubmit,
  submitting,
  error,
  route = { kind: 'direct' },
  onOpenMappingPlan,
  onRecheck,
}: TaskComposerProps) {
  if (!executionUi || !hasConversation) {
    return (
      <div className="composer composer--inert">
        <p className="muted">
          {executionUi
            ? 'Select or create a conversation to send a task.'
            : 'Task submission is disabled until a separately approved execution stage.'}
        </p>
      </div>
    );
  }

  if (route.kind === 'unconfirmed') {
    return (
      <div className="composer composer--inert" role="status" aria-label="Task submission">
        <p className="muted">Confirming how runs start in this project. No task is offered until the server answers.</p>
        {onRecheck && (
          <div className="button-row">
            <button type="button" className="button button--quiet" onClick={onRecheck}>Check again</button>
          </div>
        )}
      </div>
    );
  }

  if (route.kind === 'blocked') {
    const toPlan = route.blockedBy === 'catalog_batch_required';
    return (
      <div className="composer composer--inert" role="status" aria-label="Task submission">
        <p className="muted">{BLOCKED_COPY[route.blockedBy]}</p>
        {toPlan && !route.planAvailable && (
          <p className="note">The Mapping Plan is not enabled at this activation stage, so no catalog run can start yet.</p>
        )}
        {toPlan && route.planAvailable && onOpenMappingPlan && (
          <div className="button-row">
            <button type="button" className="button button--primary" onClick={onOpenMappingPlan}>
              Open the Mapping Plan
            </button>
          </div>
        )}
        {error && <p className="alert" role="alert">{safeText(error)}</p>}
      </div>
    );
  }

  return (
    <div className="composer">
      <div className="field">
        <label className="field-label sr-only" htmlFor="task-content">Task content</label>
        <textarea
          id="task-content"
          value={content}
          onChange={(event) => onContentChange(event.target.value)}
          placeholder="Describe the task for this run…"
          rows={3}
        />
      </div>
      <div className="composer-actions">
        <p className="composer-hint">Backend execution flags and membership authorization stay authoritative.</p>
        <button type="button" className="button button--primary" onClick={onSubmit} disabled={submitting || !content.trim()}>
          {submitting ? 'Sending…' : 'Send task'}
        </button>
      </div>
      {error && <p className="alert" role="alert">{safeText(error)}</p>}
    </div>
  );
}
