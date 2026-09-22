import { EventId, eventCursorParam } from './eventId';
import { parseJsonPreservingBigIntegers } from './losslessJson';
import { getCurrentAccessToken } from './supabaseClient';
import { Conversation, Project, Proposal, Run, RunEvent, RunSummary } from './types';

const API = '/api/gateway';

export const clientConfig = {
  apiBaseUrl: API,
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL,
  supabaseAnonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
};

/**
 * The execution UI flag only controls what the browser renders; it is never
 * a security boundary. The gateway allowlist and the backend execution
 * flags plus membership authorization remain authoritative.
 */
export function executionUiEnabled(): boolean {
  return (process.env.NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI ?? '')
    .trim()
    .toLowerCase() === 'true';
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function authHeaders(): Promise<HeadersInit> {
  // Read the token per request so Supabase session refreshes are picked up.
  const token = await getCurrentAccessToken();
  return token ? { authorization: `Bearer ${token}` } : {};
}

type JsonParser = (text: string) => unknown;

async function request<T>(
  path: string,
  init?: RequestInit,
  parse: JsonParser = JSON.parse,
): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      'content-type': 'application/json',
      ...(await authHeaders()),
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = `Request failed with status ${response.status}.`;
    try {
      const body = await response.json();
      code = body?.error?.code ?? code;
      message = body?.error?.message ?? body?.error ?? message;
    } catch {
      // Non-JSON error bodies are never surfaced raw to the UI.
    }
    throw new ApiError(response.status, code, String(message));
  }

  return parse(await response.text()) as T;
}

export function newIdempotencyKey(): string {
  return `ui-${crypto.randomUUID()}`;
}

export const api = {
  projects: () => request<Project[]>('/projects'),

  conversations: (projectId: string) =>
    request<Conversation[]>(`/projects/${projectId}/conversations`),

  createConversation: (projectId: string, title?: string) =>
    request<Conversation>(`/projects/${projectId}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),

  createProposal: (projectId: string, userRequest: string) =>
    request<Proposal>('/workflow-proposals', {
      method: 'POST',
      body: JSON.stringify({ project_id: projectId, user_request: userRequest }),
    }),

  proposal: (id: string) => request<Proposal>(`/workflow-proposals/${id}`),

  decideProposal: (id: string, decision: 'approve' | 'reject', reason?: string) =>
    request<Proposal>(`/workflow-proposals/${id}/${decision}`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  reviseProposal: (id: string, userRequest: string) =>
    request<Proposal>(`/workflow-proposals/${id}/revise`, {
      method: 'POST',
      body: JSON.stringify({ user_request: userRequest }),
    }),

  startRun: (
    conversationId: string,
    content: string,
    idempotencyKey: string,
    metadata: Record<string, unknown> = {},
  ) =>
    request<{ run_id: string; status: string }>(
      `/conversations/${conversationId}/runs`,
      {
        method: 'POST',
        body: JSON.stringify({
          content,
          metadata,
          idempotency_key: idempotencyKey,
        }),
      },
    ),

  run: (id: string) => request<Run>(`/runs/${id}`),

  /**
   * The conversation's durable run history, newest first and bounded by the
   * server. It is what lets a completed result outlive session storage: after
   * a browser restart the workspace reopens the latest run from here.
   */
  runs: (conversationId: string, limit = 20) =>
    request<RunSummary[]>(`/conversations/${conversationId}/runs?limit=${limit}`),

  /**
   * Incremental event polling.
   *
   * `run_events.id` is a PostgreSQL bigint. The backend serializes it as a JSON
   * number with full digits and the gateway route streams the body through
   * unchanged, so the exact digits reach here; only `JSON.parse` would round
   * them. The response body is therefore parsed with the bigint-preserving
   * parser and the cursor stays a decimal string all the way into the query
   * string, where the backend reads it as an arbitrary-precision Python int.
   */
  events: (id: string, after?: EventId) =>
    request<RunEvent[]>(
      after === undefined
        ? `/runs/${id}/events`
        : `/runs/${id}/events?after_event_id=${encodeURIComponent(eventCursorParam(after))}`,
      undefined,
      parseJsonPreservingBigIntegers,
    ),

  cancel: (id: string, reason?: string) =>
    request<{ run_id: string; status: string }>(`/runs/${id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),

  /**
   * CODE-3 — the read-only catalog review surface.
   *
   * Both methods are GET with no body, and there is no mutating counterpart in
   * this client: no approve, no reject, no promote, no capture. That is not
   * only a convention — `tests/catalogReviewApi.test.ts` asserts the request
   * `fetch` actually received, and the gateway allowlists only GET for these
   * paths.
   *
   * The returned value is `unknown` ON PURPOSE. A typed return here would be a
   * claim about a response nobody has checked, and the workspace must not
   * render a value merely because the server sent it. `lib/catalogReview.ts`
   * turns it into trusted state field by field.
   */
  catalogCanonical: (projectId: string, params: CatalogPageParams = {}) =>
    request<unknown>(
      `/projects/${projectId}/catalog/canonical${catalogQuery(params)}`,
    ),

  catalogReviewCandidates: (projectId: string, params: CatalogPageParams = {}) =>
    request<unknown>(
      `/projects/${projectId}/catalog/review-candidates${catalogQuery(params)}`,
    ),
};

/** Exactly the query parameters the CODE-3 routes declare. Nothing else. */
export type CatalogPageParams = {
  limit?: number;
  offset?: number;
  manufacturer?: string;
  commercialModel?: string;
  modelYear?: number;
  canonicalKey?: string;
};

/**
 * The query string for a catalog page request.
 *
 * Built from a CLOSED set of names: a caller cannot add a parameter through
 * this, so the client cannot ask the server for an ordering, a column, a table
 * or a status even by accident. Every value is encoded, and an absent one
 * contributes no parameter at all — `?manufacturer=` is a filter the server
 * refuses, and sending one for an unset field would turn "no filter" into a
 * request that matches nothing.
 */
function catalogQuery(params: CatalogPageParams): string {
  const query = new URLSearchParams();
  if (params.limit !== undefined) query.set('limit', String(params.limit));
  if (params.offset !== undefined) query.set('offset', String(params.offset));
  if (params.manufacturer) query.set('manufacturer', params.manufacturer);
  if (params.commercialModel) query.set('commercial_model', params.commercialModel);
  if (params.modelYear !== undefined) query.set('model_year', String(params.modelYear));
  if (params.canonicalKey) query.set('canonical_key', params.canonicalKey);
  const encoded = query.toString();
  return encoded === '' ? '' : `?${encoded}`;
}
