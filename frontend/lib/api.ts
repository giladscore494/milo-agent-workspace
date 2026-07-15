import { getCurrentAccessToken } from './supabaseClient';
import { Conversation, Project, Proposal, Run, RunEvent } from './types';

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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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

  return response.json() as Promise<T>;
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

  events: (id: string, after?: number) =>
    request<RunEvent[]>(
      after === undefined
        ? `/runs/${id}/events`
        : `/runs/${id}/events?after_event_id=${encodeURIComponent(after)}`,
    ),

  cancel: (id: string, reason?: string) =>
    request<{ run_id: string; status: string }>(`/runs/${id}/cancel`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
};
