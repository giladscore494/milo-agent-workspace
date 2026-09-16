/**
 * Explicit ownership for every asynchronous workspace response.
 *
 * The workspace issues authenticated requests whose answers arrive later than
 * the selection that asked for them. React unmounting is NOT the boundary here:
 * the page keeps one mounted tree and swaps which project, conversation and run
 * it is showing, so a response that resolves after a switch would otherwise be
 * written into whatever is selected when it lands. That is a cross-boundary
 * leak — project A's conversations rendered under project B, run A's row
 * rendered under conversation B, the previous user's projects rendered after a
 * sign-out — and hiding it visually would not make it safe.
 *
 * So ownership is stated, not inferred. Before a request is sent the caller
 * captures the scope it belongs to; when the response resolves it is applied
 * only if that scope still owns the surface it would write to. Everything else
 * is dropped — silently, because a dropped answer for a selection nobody is
 * looking at is not an error the user needs to see.
 *
 * The four levels are nested exactly as the data is:
 *
 *     session ⊃ project ⊃ conversation ⊃ run
 *
 * A change at any level invalidates every level below it, which is why the
 * `with*` helpers clear the narrower fields rather than merging them. The
 * monotonic `session` counter is what makes a sign-out final: a replacement
 * session can never compare equal to the one before it even if the same user
 * signs back in, so an answer in flight across the sign-out is always dropped.
 */

export type WorkspaceScope = {
  /** Monotonic. Every session replacement (sign-in, sign-out, user swap) bumps it. */
  session: number;
  /** The authenticated user this scope belongs to, when the session reports one. */
  userId?: string;
  projectId?: string;
  conversationId?: string;
  runId?: string;
};

export const INITIAL_WORKSPACE_SCOPE: WorkspaceScope = { session: 0 };

/**
 * Start a new session scope. The counter always advances, so this invalidates
 * every request in flight under the previous one — including when `userId` is
 * unchanged or unknown.
 */
export function nextSessionScope(previous: WorkspaceScope, userId?: string): WorkspaceScope {
  return { session: previous.session + 1, userId };
}

export function withProject(scope: WorkspaceScope, projectId?: string): WorkspaceScope {
  return { session: scope.session, userId: scope.userId, projectId };
}

export function withConversation(scope: WorkspaceScope, conversationId?: string): WorkspaceScope {
  return { session: scope.session, userId: scope.userId, projectId: scope.projectId, conversationId };
}

export function withRun(scope: WorkspaceScope, runId?: string): WorkspaceScope {
  return { ...scope, runId };
}

export function ownsSession(expected: WorkspaceScope, current: WorkspaceScope): boolean {
  return expected.session === current.session && expected.userId === current.userId;
}

export function ownsProject(expected: WorkspaceScope, current: WorkspaceScope): boolean {
  return ownsSession(expected, current) && expected.projectId === current.projectId;
}

export function ownsConversation(expected: WorkspaceScope, current: WorkspaceScope): boolean {
  return ownsProject(expected, current) && expected.conversationId === current.conversationId;
}

export function ownsRun(expected: WorkspaceScope, current: WorkspaceScope): boolean {
  return ownsConversation(expected, current) && expected.runId === current.runId;
}

/**
 * Does this run row belong where the browser is about to render it?
 *
 * Two independent facts, and neither implies the other:
 *
 *  1. it is the run that was ASKED for — a response carrying a different id is
 *     not the answer to this request, whatever produced it;
 *  2. it belongs to the conversation currently selected — a run id read back
 *     from session storage is a stored STRING, not a proof of ownership, and
 *     the server is the only thing that can say which conversation a run is in.
 *
 * `expectedConversationId` is optional so a caller that genuinely has no
 * conversation context (there is none today) does not get a false negative;
 * when it is supplied, a run whose `conversation_id` does not match it is
 * refused even though the user is authorized to read it.
 */
export function runBelongsToScope(
  run: { id?: unknown; conversation_id?: unknown } | null | undefined,
  requestedRunId: string,
  expectedConversationId?: string,
): boolean {
  if (!run || typeof run !== 'object') return false;
  if (typeof run.id !== 'string' || run.id !== requestedRunId) return false;
  if (expectedConversationId === undefined) return true;
  return typeof run.conversation_id === 'string' && run.conversation_id === expectedConversationId;
}

/**
 * Does this event belong to the run being rendered?
 *
 * `run_events.run_id` is `NOT NULL` in the schema and required by
 * `backend.schemas.RunEvent`, so a mismatch cannot happen on the authorized
 * path. It is checked anyway because the cost is one comparison and the
 * failure it prevents — one run's events folded into another run's state — is
 * exactly the confusion this module exists to make impossible.
 */
export function eventBelongsToRun(event: { run_id?: unknown }, runId: string): boolean {
  return typeof event?.run_id === 'string' && event.run_id === runId;
}
