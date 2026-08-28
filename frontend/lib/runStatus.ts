/**
 * The single authoritative durable run-status contract for the frontend.
 *
 * backend/runtime.py TERMINAL_STATES is the source of truth; this module is
 * its only frontend mirror. Nothing else may hard-code a terminal set.
 */

export const TERMINAL_RUN_STATUSES = [
  'completed',
  'partial_success',
  'failed',
  'cancelled',
  'timed_out',
  'budget_exhausted',
] as const;

export type TerminalRunStatus = (typeof TERMINAL_RUN_STATUSES)[number];

const TERMINAL_SET: ReadonlySet<string> = new Set<string>(TERMINAL_RUN_STATUSES);

/** Non-terminal durable states, kept explicit so an unknown status is visible. */
export const ACTIVE_RUN_STATUSES = [
  'queued',
  'launching',
  'starting',
  'running',
  'waiting',
  'cancellation_requested',
] as const;

export type ActiveRunStatus = (typeof ACTIVE_RUN_STATUSES)[number];

const ACTIVE_SET: ReadonlySet<string> = new Set<string>(ACTIVE_RUN_STATUSES);

export function isTerminalRunStatus(status?: string | null): status is TerminalRunStatus {
  return typeof status === 'string' && TERMINAL_SET.has(status);
}

export function isActiveRunStatus(status?: string | null): status is ActiveRunStatus {
  return typeof status === 'string' && ACTIVE_SET.has(status);
}

/**
 * `completed` alone means "finished with nothing outstanding".
 *
 * `partial_success` is terminal but is NEVER equivalent to completed: the
 * backend reaches it when tasks failed, coverage gaps remain, conflicts were
 * found or a verdict was not `verified`.
 */
export function isFullySuccessfulRunStatus(status?: string | null): boolean {
  return status === 'completed';
}

export function isPartialSuccessRunStatus(status?: string | null): boolean {
  return status === 'partial_success';
}

/** Terminal and not fully successful: partial_success included. */
export function isUnsuccessfulTerminalRunStatus(status?: string | null): boolean {
  return isTerminalRunStatus(status) && !isFullySuccessfulRunStatus(status);
}

const TERMINAL_LABELS: Record<TerminalRunStatus, string> = {
  completed: 'Completed',
  partial_success: 'Partial success',
  failed: 'Failed',
  cancelled: 'Cancelled',
  timed_out: 'Timed out',
  budget_exhausted: 'Budget exhausted',
};

export function terminalRunStatusLabel(status: TerminalRunStatus): string {
  return TERMINAL_LABELS[status];
}
