import { ApiError } from './api';

/**
 * Safe user-facing error presentation — a CLOSED, application-owned policy.
 *
 * This is a fourth distinct boundary, and the other three do not substitute for
 * it:
 *
 *  - HTML/text escaping (`safeText`) stops markup from becoming markup. It
 *    happily prints a credential, a hostname or a stack frame;
 *  - structured validation (`parseFinalResult`) decides which SHAPES may be
 *    rendered. An error string has no shape to validate;
 *  - redaction (`redactSecretText`) removes credential-shaped substrings. It
 *    leaves `OpenRouter upstream quota exhausted for provider account`
 *    perfectly readable.
 *
 * ## Why "looks harmless" is not authorization
 *
 * The first version of this module asked whether a message LOOKED unsafe — did
 * it carry a credential, a URL, a stack frame, markup — and displayed anything
 * that did not. That inverts the burden. The workspace reads its message from
 * whatever the gateway, the API or the Supabase SDK handed back, and those are
 * operational surfaces owned by other systems. A provider quota message, a
 * PostgREST pool error and a Cloud Run diagnostic all pass a "looks harmless"
 * test while telling the user about infrastructure they do not operate, in
 * words nobody here wrote.
 *
 * So no upstream text is ever rendered. Not `ApiError.message`, not
 * `Error.message`. What the surface shows is:
 *
 *  1. copy authored HERE for a classification allowlisted BY VALUE, or
 *  2. the caller's own static fallback sentence for the action that failed,
 *
 * plus the classification code when it is one of ours, because a short stable
 * token is what makes an error actionable across a support boundary.
 *
 * A SCREAMING_SNAKE shape is not a classification. `REPOSITORY_ERROR` has that
 * shape and means "something inside the server went wrong"; it earns the
 * caller's fallback, not a sentence of its own.
 *
 * ## Consequence, stated plainly
 *
 * An upstream message that would have been genuinely useful and is not on the
 * list below is replaced by the caller's fallback. That is the intended
 * direction: adding a classification is a deliberate edit here, next to the
 * copy a user will read, rather than a decision made by whatever system
 * happened to produce the string.
 */

/**
 * Application error classifications the product surfaces, with the copy it
 * shows for each. Allowlisted BY VALUE.
 *
 * Every key is a real code from `backend/errors.py` / `backend/main.py` or a
 * `HTTP_<status>` classification `lib/api.ts` synthesises when the gateway
 * answers with its own body. Codes deliberately absent — `REPOSITORY_ERROR`,
 * `ENGINE_FAILED`, `RUN_TRANSITION_CONFLICT`, every `CATALOG_*`, every
 * `*_AUTH_*` — are internal conditions with nothing actionable to say to a
 * person using the workspace.
 */
const ERROR_COPY: ReadonlyMap<string, string> = new Map([
  // Staged activation. The single most common thing a user will hit, and the
  // one they most need explained rather than blamed for.
  ['EXECUTION_SURFACE_DISABLED', 'This action is turned off at the current activation stage.'],

  // Launch lifecycle. `JOB_LAUNCH_UNKNOWN` must say what it means without
  // trusting the upstream sentence: the run is parked and nothing retries it.
  ['JOB_LAUNCH_UNKNOWN', 'The worker launch outcome is unknown, so the run is parked for operator reconciliation. It will not be relaunched automatically.'],
  ['JOB_LAUNCH_FAILED', 'The worker could not be started. The run is still queued and the same request can be retried.'],

  // Idempotency and concurrency.
  ['IDEMPOTENCY_CONFLICT', 'That submission was already used with different content. Start a new one.'],
  ['USER_CONCURRENCY_LIMIT', 'You already have as many runs in flight as this stage allows. Wait for one to finish.'],
  ['PROJECT_CONCURRENCY_LIMIT', 'This project already has as many runs in flight as this stage allows.'],

  // Budgets.
  ['DAILY_USER_BUDGET_REACHED', 'Your daily budget for this stage is used up.'],
  ['DAILY_PROJECT_BUDGET_REACHED', "This project's daily budget for this stage is used up."],

  // Rate limiting, from the API and from the gateway.
  ['RATE_LIMITED', 'Too many requests. Wait a moment and try again.'],
  ['RATE_LIMITER_UNAVAILABLE', 'The request was refused because the shared rate limiter is unavailable. Try again shortly.'],
  ['HTTP_429', 'Too many requests. Wait a moment and try again.'],

  // Vehicle catalog scope. A vehicle catalog run maps exactly the manufacturer,
  // market and period its PROJECT configures; the typed task never replaces
  // it. A project that configures none is refused rather than quietly run
  // against a default, and the person is told where the fix belongs.
  ['VEHICLE_CATALOG_SCOPE_NOT_CONFIGURED', 'This vehicle catalog project has no configured manufacturer, market and period, so no run was started. The project configuration must state them first.'],
  ['VEHICLE_CATALOG_SCOPE_INVALID', "This vehicle catalog project's configured manufacturer, market or period is not valid, so no run was started. The project configuration must be corrected first."],

  // The Mapping Plan. A stale plan is refused rather than overwritten, and the
  // person is told the plan was reloaded so the change can be made again
  // against what is really there. Internal conditions (an unreadable stored
  // plan, a repository failure) are deliberately absent: they get the
  // caller's fallback.
  ['WORK_SCOPE_STALE', 'The mapping plan changed since you opened it. It has been reloaded; make your change again.'],
  ['WORK_SCOPE_OPEN_EXISTS', 'This conversation already has a mapping plan. It has been reloaded; change that plan instead.'],
  ['WORK_SCOPE_NOT_EDITABLE', 'This mapping plan can no longer be edited.'],
  ['WORK_SCOPE_WORKFLOW_UNSUPPORTED', "This project's engine does not use a mapping plan."],
  ['WORK_SCOPE_NOT_FOUND', 'That mapping plan is not available to your account.'],
  ['WORK_SCOPE_REQUEST_INVALID', 'Describe the plan in words or edit it in the form, not both at once.'],
  ['WORK_SCOPE_FIELDS_INVALID', 'The plan edit was incomplete. Reload the plan and try again.'],
  ['WORK_SCOPE_UNITS_INVALID', 'A plan names at least one manufacturer from the directory, each once.'],
  ['WORK_SCOPE_YEARS_INVALID', 'The model years must be whole years in range, the first not after the last.'],
  ['WORK_SCOPE_MAX_ITEMS_INVALID', 'The candidate limit must be a whole number within the server limit.'],
  ['WORK_SCOPE_BATCH_SIZE_INVALID', 'The batch size must be a whole number within the server limit.'],
  ['WORK_SCOPE_INSTRUCTION_INVALID', 'The instruction must be plain text within the length limit.'],
  ['WORK_SCOPE_INSTRUCTION_NOT_UNDERSTOOD', 'No manufacturer, model year, limit or batch size was recognized in that instruction. Check the spelling, or build the plan from the directory.'],
  ['WORK_SCOPE_COVERAGE_UNAVAILABLE', 'The catalog coverage that instruction depends on could not be read, so the plan was not changed. Try again shortly.'],

  // Mapping Plan batches. Each is a refusal a person can act on: the plan's
  // progress is reloaded after every one, so the screen shows what is really
  // next. None of them started a run.
  ['WORK_SCOPE_PAUSED', 'The mapping plan is paused, so no batch was started. Resume it first.'],
  ['WORK_SCOPE_BATCH_NOT_NEXT', 'That batch is no longer the next one, so nothing was started. The progress has been reloaded.'],
  ['WORK_SCOPE_BATCH_IN_PROGRESS', 'Another batch of this plan is still running, so nothing was started.'],
  ['WORK_SCOPE_BATCH_ALREADY_COMPLETED', 'That batch has already finished, so it was not started again.'],

  // Run and proposal lifecycle.
  ['RUN_ALREADY_FINISHED', 'That run has already finished.'],
  // A cancellation is finished by the run's worker; these two runs have none
  // that is known to exist, so nothing was changed.
  ['RUN_NOT_LAUNCHED', 'This run never started, so there is nothing to cancel yet. Start it again, or ask an operator to retire it.'],
  ['RUN_LAUNCH_UNRESOLVED', 'It is not yet known whether this run started, so it cannot be cancelled until an operator has checked it.'],
  ['PROPOSAL_NOT_APPROVABLE', 'This proposal cannot be approved in its current state.'],
  ['PROPOSAL_NOT_APPROVED', 'This proposal has not been approved.'],

  // Authorization and existence, which the backend deliberately conflates so
  // that a non-member cannot tell one from the other. The copy conflates them
  // too, on purpose.
  ['PROJECT_NOT_FOUND', 'That project is not available to your account.'],
  ['CONVERSATION_NOT_FOUND', 'That conversation is not available to your account.'],
  ['RUN_NOT_FOUND', 'That run is not available to your account.'],
  ['WORKFLOW_PROPOSAL_NOT_FOUND', 'That proposal is not available to your account.'],
  ['AUTHENTICATION_REQUIRED', 'Your session is no longer valid. Sign in again.'],
  ['HTTP_401', 'Your session is no longer valid. Sign in again.'],
  ['HTTP_403', 'That action is not permitted for your account at this stage.'],
  ['HTTP_404', 'That item is not available to your account.'],
]);

/**
 * Classifications whose CODE may be shown beside the copy.
 *
 * The authored set above, minus the `HTTP_*` classifications — those are an
 * artifact of how `lib/api.ts` labels a gateway body, not a code anyone can
 * look up, so showing one would suggest a support handle that does not exist.
 */
function codeIsDisplayable(code: string): boolean {
  return ERROR_COPY.has(code) && !code.startsWith('HTTP_');
}

/**
 * Why a sign-in attempt failed, as a classification this application owns.
 *
 * `lib/supabaseClient.ts` raises this instead of re-throwing the SDK's own
 * `Error`, so an authentication failure stays understandable without any
 * Supabase prose reaching the screen.
 */
export type AuthFailureReason = 'invalid_credentials' | 'rate_limited' | 'unavailable' | 'not_configured' | 'expired';

export class AuthFailure extends Error {
  constructor(public readonly reason: AuthFailureReason) {
    super(reason);
    this.name = 'AuthFailure';
  }
}

const AUTH_COPY: Readonly<Record<AuthFailureReason, string>> = {
  invalid_credentials: 'That email and password combination was not accepted.',
  rate_limited: 'Too many sign-in attempts. Wait a moment and try again.',
  unavailable: 'Sign-in is temporarily unavailable. Try again shortly.',
  not_configured: 'Sign-in is not configured for this deployment.',
  expired: 'Your session has expired. Sign in again.',
};

/** The classification an error carries, or `undefined` when it has none. */
export function classifyError(error: unknown): string | undefined {
  if (error instanceof AuthFailure) return undefined; // handled by its own copy
  if (error instanceof ApiError && typeof error.code === 'string' && ERROR_COPY.has(error.code)) {
    return error.code;
  }
  return undefined;
}

/**
 * The one text an error surface may render.
 *
 * `fallback` is the caller's own static sentence for the action that failed. It
 * is used whenever the error carries no classification this application
 * authored copy for — which includes every plain `Error`, every unknown code,
 * and every provider, repository or internal failure.
 */
export function safeErrorText(error: unknown, fallback: string): string {
  if (error instanceof AuthFailure) return AUTH_COPY[error.reason] ?? fallback;
  const code = classifyError(error);
  if (code === undefined) return fallback;
  const copy = ERROR_COPY.get(code) ?? fallback;
  return codeIsDisplayable(code) ? `${copy} (${code})` : copy;
}

/** The classifications this application authors copy for. Exported for tests. */
export const CLASSIFIED_ERROR_CODES: readonly string[] = [...ERROR_COPY.keys()];
