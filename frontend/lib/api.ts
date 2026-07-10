import { getCurrentAccessToken } from './supabaseClient';
import { Conversation, Project, Proposal, Run, RunEvent } from './types';

const API = '/api/gateway';
const DISABLED_MESSAGE =
  'This operation is disabled until the private gateway safety review is complete.';

export const clientConfig = {
  apiBaseUrl: API,
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL,
  supabaseAnonKey: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY,
};

async function authHeaders(): Promise<HeadersInit> {
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
    throw new Error(`${response.status} ${await response.text()}`);
  }

  return response.json() as Promise<T>;
}

function disabledRequest<T>(): Promise<T> {
  return Promise.reject(new Error(DISABLED_MESSAGE));
}

export const api = {
  projects: () => request<Project[]>('/projects'),

  createConversation: (projectId: string, title?: string) =>
    request<Conversation>(`/projects/${projectId}/conversations`, {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),

  createProposal: (_userRequest: string) =>
    disabledRequest<Proposal>(),

  decideProposal: (
    _id: string,
    _decision: 'approve' | 'reject',
    _reason?: string,
  ) => disabledRequest<Proposal>(),

  reviseProposal: (_id: string, _userRequest: string) =>
    disabledRequest<Proposal>(),

  startRun: (
    _conversationId: string,
    _content: string,
    _metadata = {},
  ) => disabledRequest<{ run_id: string; status: string }>(),

  run: (_id: string) => disabledRequest<Run>(),

  events: (_id: string, _after?: number) =>
    disabledRequest<RunEvent[]>(),

  cancel: (_id: string, _reason?: string) =>
    disabledRequest<unknown>(),
};
