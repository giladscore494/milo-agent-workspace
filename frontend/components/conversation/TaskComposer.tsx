'use client';
import { safeText } from '@/lib/sanitize';

export type TaskComposerProps = {
  executionUi: boolean;
  hasConversation: boolean;
  content: string;
  onContentChange: (value: string) => void;
  onSubmit: () => void;
  submitting: boolean;
  error: string;
};

/**
 * Task submission surface. Submission itself (idempotency key lifetime,
 * run-id storage, error handling) stays owned by the page.
 */
export function TaskComposer({ executionUi, hasConversation, content, onContentChange, onSubmit, submitting, error }: TaskComposerProps) {
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
