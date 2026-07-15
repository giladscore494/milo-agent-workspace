import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Page from '../app/page';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let mockSession: any = { access_token: 'fresh', user: { email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: false,
  api: {
    projects: vi.fn(),
    conversations: vi.fn(),
    createConversation: vi.fn(),
    createProposal: vi.fn(),
    proposal: vi.fn(),
    decideProposal: vi.fn(),
    reviseProposal: vi.fn(),
    startRun: vi.fn(),
    run: vi.fn(),
    events: vi.fn(),
    cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => 'ui-test-idempotency-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) {
      super(message);
    }
  },
}));

const PROJECT = { id: '677db6c2-b44c-41c1-b4e1-b51229d697df', slug: 'milo-vehicle-catalog', name: 'MILO Vehicle Catalog', workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION = { id: '1f90f4ce-7844-4031-91d6-b74e40e1884e', project_id: PROJECT.id, title: 'Kickoff' };
const RUN = { id: '2c9e2c11-58c8-4b46-b7d5-3d8de9f4b7aa', conversation_id: CONVERSATION.id, status: 'queued' };

describe('authenticated workspace (execution UI disabled)', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { email: 'u@example.com' } };
    apiMocks.executionUi = false;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([]);
    apiMocks.api.createConversation.mockResolvedValue(CONVERSATION);
    window.sessionStorage.clear();
  });

  it('shows only login UI to unauthenticated visitors', async () => {
    mockSession = null;
    render(<Page/>);
    expect(await screen.findByText(/Sign in to access/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument();
    expect(apiMocks.api.projects).not.toHaveBeenCalled();
    expect(screen.queryByText('MILO Vehicle Catalog')).not.toBeInTheDocument();
  });

  it('loads the authenticated user projects through the gateway', async () => {
    render(<Page/>);
    expect(await screen.findByText('Loading your projects…')).toBeInTheDocument();
    expect(await screen.findByText('MILO Vehicle Catalog')).toBeInTheDocument();
    expect(apiMocks.api.projects).toHaveBeenCalledTimes(1);
  });

  it('shows an empty state when the user has no project memberships', async () => {
    apiMocks.api.projects.mockResolvedValue([]);
    render(<Page/>);
    expect(await screen.findByText(/No projects are assigned to your account yet/)).toBeInTheDocument();
  });

  it('shows a retryable error state when project loading fails', async () => {
    apiMocks.api.projects.mockRejectedValueOnce(new Error('403 gateway rejected'));
    render(<Page/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('403 gateway rejected');
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading projects' }));
    expect(await screen.findByText('MILO Vehicle Catalog')).toBeInTheDocument();
  });

  it('creates a conversation for a selected project through the approved endpoint', async () => {
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.change(screen.getByLabelText('Conversation title'), { target: { value: 'Kickoff' } });
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(apiMocks.api.createConversation).toHaveBeenCalledWith(PROJECT.id, 'Kickoff'));
    expect(await screen.findByText(`ID ${CONVERSATION.id} • project ${CONVERSATION.project_id}`)).toBeInTheDocument();
  });

  it('lists existing conversations for the selected project', async () => {
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    expect(await screen.findByText('Kickoff')).toBeInTheDocument();
    expect(apiMocks.api.conversations).toHaveBeenCalledWith(PROJECT.id);
  });

  it('surfaces conversation creation failures without crashing', async () => {
    apiMocks.api.createConversation.mockRejectedValue(new Error('403 blocked'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('403 blocked');
  });

  it('exposes no execution, proposal, run or cancel controls while the flag is off', async () => {
    render(<Page/>);
    await screen.findByText('MILO Vehicle Catalog');
    for (const name of [/send task/i, /approve/i, /reject/i, /revise/i, /cancel/i, /generate proposal/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText('Live run')).toBeInTheDocument();
    expect(screen.getByText(/No active run/)).toBeInTheDocument();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
    expect(apiMocks.api.events).not.toHaveBeenCalled();
  });
});

describe('authenticated workspace (execution UI enabled)', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { email: 'u@example.com' } };
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.createConversation.mockResolvedValue(CONVERSATION);
    apiMocks.api.run.mockResolvedValue(RUN);
    apiMocks.api.events.mockResolvedValue([]);
    apiMocks.api.startRun.mockResolvedValue({ run_id: RUN.id, status: 'queued' });
    window.sessionStorage.clear();
  });

  async function openConversation() {
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.click(await screen.findByText('Kickoff'));
  }

  it('sends a run with an idempotency key and prevents double submission', async () => {
    await openConversation();
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'Build the catalog' } });
    const send = screen.getByRole('button', { name: 'Send task' });
    fireEvent.click(send);
    fireEvent.click(send);
    await waitFor(() => expect(apiMocks.api.startRun).toHaveBeenCalledTimes(1));
    expect(apiMocks.api.startRun).toHaveBeenCalledWith(CONVERSATION.id, 'Build the catalog', 'ui-test-idempotency-key');
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVERSATION.id}`)).toBe(RUN.id);
  });

  it('displays queued state and polls the run', async () => {
    await openConversation();
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'Go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN.id));
    expect(await screen.findByText('queued')).toBeInTheDocument();
  });

  it('reopens an existing run after refresh via stored run id', async () => {
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN.id);
    await openConversation();
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN.id));
  });

  it('shows backend rejection reasons for run creation', async () => {
    apiMocks.api.startRun.mockRejectedValue(new Error('run creation is disabled (EXECUTION_SURFACE_DISABLED)'));
    await openConversation();
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'Go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('EXECUTION_SURFACE_DISABLED');
  });

  it('cancels an active run after confirmation with a reason', async () => {
    apiMocks.api.cancel.mockResolvedValue({ run_id: RUN.id, status: 'cancellation_requested' });
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN.id);
    await openConversation();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    fireEvent.change(screen.getByLabelText('Cancellation reason'), { target: { value: 'wrong task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Confirm cancellation' }));
    await waitFor(() => expect(apiMocks.api.cancel).toHaveBeenCalledWith(RUN.id, 'wrong task'));
  });

  it('renders terminal state and final output when the run completes', async () => {
    const leak = ['sk', 'leaked', 'value', 'must', 'be', 'redacted'].join('-');
    apiMocks.api.run.mockResolvedValue({ ...RUN, status: 'completed', output: { summary: 'done', api_key: leak } });
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN.id);
    await openConversation();
    expect(await screen.findByText(/Run finished with status/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel run' })).not.toBeInTheDocument();
    // Secrets are redacted from rendered output.
    expect(screen.queryByText(new RegExp(leak))).not.toBeInTheDocument();
    expect((document.body.textContent ?? '')).toContain('[REDACTED]');
  });

  it('generates, displays and decides workflow proposals with backend reasons', async () => {
    const proposal = {
      id: '3a9e2c11-58c8-4b46-b7d5-3d8de9f4b7bb',
      status: 'approved',
      user_request: 'Research the market',
      task_spec: {},
      draft: { agents: [{ key: 'researcher', role: 'researcher', internet_policy: 'required', internet_reason: 'live data' }], workflow: ['plan', 'research'] },
      estimates: { planned_agents: 5, cost_warning: 'normal' },
      critiques: [],
    };
    apiMocks.api.createProposal.mockResolvedValue(proposal);
    apiMocks.api.decideProposal.mockResolvedValue({ ...proposal, status: 'rejected' });
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.change(screen.getByLabelText('Proposal request'), { target: { value: 'Research the market' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate proposal' }));
    await waitFor(() => expect(apiMocks.api.createProposal).toHaveBeenCalledWith(PROJECT.id, 'Research the market'));
    expect(await screen.findByText(/live data/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(apiMocks.api.decideProposal).toHaveBeenCalledWith(proposal.id, 'reject'));
  });

  it('shows proposal errors from the backend', async () => {
    apiMocks.api.createProposal.mockRejectedValue(new Error('workflow proposal creation is disabled (EXECUTION_SURFACE_DISABLED)'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.change(screen.getByLabelText('Proposal request'), { target: { value: 'X' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate proposal' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('EXECUTION_SURFACE_DISABLED');
  });
});
