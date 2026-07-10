import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Page from '../app/page';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let mockSession: any = { access_token: 'fresh', user: { email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
}));

const apiMocks = vi.hoisted(() => ({
  projects: vi.fn(),
  createConversation: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: apiMocks }));

const PROJECT = { id: '677db6c2-b44c-41c1-b4e1-b51229d697df', slug: 'milo-vehicle-catalog', name: 'MILO Vehicle Catalog', workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION = { id: '1f90f4ce-7844-4031-91d6-b74e40e1884e', project_id: PROJECT.id, title: 'Kickoff' };

describe('authenticated workspace', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { email: 'u@example.com' } };
    apiMocks.projects.mockReset().mockResolvedValue([PROJECT]);
    apiMocks.createConversation.mockReset().mockResolvedValue(CONVERSATION);
  });

  it('shows only login UI to unauthenticated visitors', async () => {
    mockSession = null;
    render(<Page/>);
    expect(await screen.findByText(/Sign in to access/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument();
    expect(apiMocks.projects).not.toHaveBeenCalled();
    expect(screen.queryByText('MILO Vehicle Catalog')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Conversation UUID')).not.toBeInTheDocument();
  });

  it('loads the authenticated user projects through the gateway', async () => {
    render(<Page/>);
    expect(await screen.findByText('Loading your projects…')).toBeInTheDocument();
    expect(await screen.findByText('MILO Vehicle Catalog')).toBeInTheDocument();
    expect(apiMocks.projects).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('Market research')).not.toBeInTheDocument();
    expect(screen.queryByText('run active')).not.toBeInTheDocument();
  });

  it('shows an empty state when the user has no project memberships', async () => {
    apiMocks.projects.mockResolvedValue([]);
    render(<Page/>);
    expect(await screen.findByText(/No projects are assigned to your account yet/)).toBeInTheDocument();
  });

  it('shows a retryable error state when project loading fails', async () => {
    apiMocks.projects.mockRejectedValueOnce(new Error('403 gateway rejected'));
    render(<Page/>);
    expect(await screen.findByRole('alert')).toHaveTextContent('403 gateway rejected');
    apiMocks.projects.mockResolvedValue([PROJECT]);
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading projects' }));
    expect(await screen.findByText('MILO Vehicle Catalog')).toBeInTheDocument();
  });

  it('creates a conversation for a selected project through the approved endpoint', async () => {
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.change(screen.getByLabelText('Conversation title'), { target: { value: 'Kickoff' } });
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(apiMocks.createConversation).toHaveBeenCalledWith(PROJECT.id, 'Kickoff'));
    expect(await screen.findByText(`ID ${CONVERSATION.id} • project ${CONVERSATION.project_id}`)).toBeInTheDocument();
    expect(screen.getAllByText('Kickoff').length).toBeGreaterThan(0);
  });

  it('surfaces conversation creation failures without crashing', async () => {
    apiMocks.createConversation.mockRejectedValue(new Error('403 blocked'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('MILO Vehicle Catalog'));
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('403 blocked');
  });

  it('exposes no execution, proposal, run or manual UUID controls', async () => {
    render(<Page/>);
    await screen.findByText('MILO Vehicle Catalog');
    expect(screen.queryByLabelText('Conversation UUID')).not.toBeInTheDocument();
    for (const name of [/start run/i, /approve/i, /reject/i, /revise/i, /retry/i, /resume/i, /cancel/i, /proposal/i, /kimi/i, /worker/i, /grant/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText('Live run')).toBeInTheDocument();
    expect(screen.getByText(/No active run/)).toBeInTheDocument();
  });
});
