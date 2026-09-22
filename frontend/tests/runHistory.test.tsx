import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import Page from '../app/page';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { identityFor } from './fixtures/runIdentity';

let mockSession: any = { access_token: 'fresh', user: { id: 'user-1', email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: true,
  api: {
    projects: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(),
    createProposal: vi.fn(), proposal: vi.fn(), decideProposal: vi.fn(), reviseProposal: vi.fn(),
    startRun: vi.fn(), run: vi.fn(), runs: vi.fn(), events: vi.fn(), cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => 'ui-test-idempotency-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

const PROJECT = { id: '677db6c2-b44c-41c1-b4e1-b51229d697df', slug: 'alpha', name: 'Alpha Research', workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION = { id: '1f90f4ce-7844-4031-91d6-b74e40e1884e', project_id: PROJECT.id, title: 'History conversation' };
const OTHER_CONVERSATION_ID = '9a1d1b02-0b2f-4a5b-9a24-8f0d5a6f4c31';
const OLD_RUN_ID = '2c9e2c11-58c8-4b46-b7d5-3d8de9f4b7aa';
const NEW_RUN_ID = '3d0f3d22-69d9-4c57-a8e6-4e9ef0a5c8bb';
const FOREIGN_RUN_ID = '4e1a4e33-7aea-4d68-b9f7-5fafa1b6d9cc';

const DOCUMENT = (name: string) => ({
  manufacturer: 'Alpha', market: 'IL', period: '2019-2024', status: 'complete',
  models: [{ canonical_model_name: name, verification_status: 'verified' }],
  needs_review: [], rejected: [], failed_agents: [],
  pipeline_quality: { discovery: 'success', normalizer: 'success', technical_enrichment: 'success', verifier: 'success', final_builder: 'success', data_depth: 'full_technical' },
});
const OUTCOME = { engine: 'vehicle_catalog_v1', semantic_status: 'complete', usability: 'usable', result_kind: 'usable_result', coverage: { produced: 1, outstanding: 0, ratio: 1 }, blocking: [], payload: { present: true } };

function terminalRun(id: string, name: string, created_at: string) {
  return {
    id, conversation_id: CONVERSATION.id, status: 'completed', created_at,
    run_identity: identityFor('vehicle_catalog_v1', id),
    output: { status: 'complete', result: DOCUMENT(name), summary: `${name} summary` },
    product_outcome: OUTCOME,
    limits: { max_cost_per_run: 1.0 },
  };
}

const NEW_RUN = terminalRun(NEW_RUN_ID, 'Newest Model', '2026-09-22T10:00:00Z');
const OLD_RUN = terminalRun(OLD_RUN_ID, 'Older Model', '2026-09-21T10:00:00Z');
const summary = (run: typeof NEW_RUN) => ({ id: run.id, conversation_id: run.conversation_id, status: run.status, created_at: run.created_at, run_identity: run.run_identity, product_outcome: run.product_outcome });

async function openConversation() {
  render(<Page/>);
  fireEvent.click(await screen.findByText('Alpha Research'));
  fireEvent.click(await screen.findByText('History conversation'));
}

describe('durable run history', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { id: 'user-1', email: 'u@example.com' } };
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.events.mockResolvedValue([]);
    apiMocks.api.run.mockImplementation((id: string) => Promise.resolve(id === NEW_RUN_ID ? NEW_RUN : OLD_RUN));
    apiMocks.api.runs.mockResolvedValue([summary(NEW_RUN), summary(OLD_RUN)]);
    window.sessionStorage.clear();
  });

  it('reopens the newest run from the durable history when nothing is stored (browser restart)', async () => {
    await openConversation();
    await waitFor(() => expect(apiMocks.api.runs).toHaveBeenCalledWith(CONVERSATION.id));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(NEW_RUN_ID));
    // The typed V1 result and the canonical verdict render from durable state.
    expect(await screen.findByText('Newest Model')).toBeInTheDocument();
    expect(screen.getByText('Canonical verdict: Complete')).toBeInTheDocument();
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVERSATION.id}`)).toBe(NEW_RUN_ID);
    // Both runs are listed, newest first, as the engine they were.
    const history = screen.getByRole('region', { name: 'Run history' });
    const rows = within(history).getAllByRole('button');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent(NEW_RUN_ID.slice(0, 8));
    expect(rows[0]).toHaveTextContent('V1');
    expect(rows[0]).toHaveTextContent('Complete');
  });

  it('a stored run id wins over the history default', async () => {
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, OLD_RUN_ID);
    await openConversation();
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(OLD_RUN_ID));
    expect(await screen.findByText('Older Model')).toBeInTheDocument();
    expect(apiMocks.api.run).not.toHaveBeenCalledWith(NEW_RUN_ID);
  });

  it('selecting an earlier run reopens it and remembers it for the conversation', async () => {
    await openConversation();
    expect(await screen.findByText('Newest Model')).toBeInTheDocument();
    const history = screen.getByRole('region', { name: 'Run history' });
    fireEvent.click(within(history).getAllByRole('button')[1]);
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(OLD_RUN_ID));
    expect(await screen.findByText('Older Model')).toBeInTheDocument();
    expect(screen.queryByText('Newest Model')).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVERSATION.id}`)).toBe(OLD_RUN_ID);
  });

  it('never lists or opens a run that names another conversation', async () => {
    apiMocks.api.runs.mockResolvedValue([
      { ...summary(NEW_RUN), id: FOREIGN_RUN_ID, conversation_id: OTHER_CONVERSATION_ID },
    ]);
    await openConversation();
    await waitFor(() => expect(apiMocks.api.runs).toHaveBeenCalledWith(CONVERSATION.id));
    expect(await screen.findByText('No runs have been recorded in this conversation.')).toBeInTheDocument();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
  });

  it('a failed history read is reported with a retry and takes nothing else down', async () => {
    apiMocks.api.runs.mockRejectedValue(new Error('boom'));
    await openConversation();
    expect(await screen.findByText(/Failed to load the run history/)).toBeInTheDocument();
    apiMocks.api.runs.mockResolvedValue([summary(NEW_RUN)]);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(apiMocks.api.runs).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('completed')).toBeInTheDocument();
  });

  it('the history is not read at all while the execution UI is off', async () => {
    apiMocks.executionUi = false;
    await openConversation();
    await waitFor(() => expect(apiMocks.api.conversations).toHaveBeenCalled());
    expect(apiMocks.api.runs).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Run history' })).not.toBeInTheDocument();
  });
});
