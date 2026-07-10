import { render, screen } from '@testing-library/react';
import Page from '../app/page';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let mockSession: any = { access_token: 'fresh', user: { email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
}));
vi.mock('../lib/useRunRealtime',()=>({useRunRealtime:()=>({mode:'polling',state:{events:[],agents:{},sources:[],claims:[],conflicts:[],currentPhase:'idle',progress:0,tokens:0,cost:0,supervisor:[],validationErrors:[],checkpoints:[],rawErrors:[]}})}));

describe('workspace auth gating', () => {
  beforeEach(() => {
    mockSession = { access_token: 'fresh', user: { email: 'u@example.com' } };
  });

  it('renders mobile-capable workspace beyond a chat box for authenticated users', async () => {
    render(<Page/>);
    expect(await screen.findByText('MILO')).toBeInTheDocument();
    expect(screen.getByText('Live run')).toBeInTheDocument();
    expect(screen.getByText('Developer')).toBeInTheDocument();
    expect(screen.getByLabelText('User instruction during run')).toBeInTheDocument();
  });

  it('shows internet badge variants for authenticated users', async () => {
    render(<Page/>);
    expect(await screen.findByText(/forbidden internet/)).toBeInTheDocument();
    expect(screen.getByText(/active internet/)).toBeInTheDocument();
  });

  it('does not expose active run or proposal controls', async () => {
    render(<Page/>);
    expect(await screen.findByRole('button',{name:/Create workflow proposal \(disabled\)/})).toBeDisabled();
    expect(screen.getByRole('button',{name:/Retry disabled/})).toBeDisabled();
    expect(screen.getByRole('button',{name:/Cancel disabled/})).toBeDisabled();
  });

  it('shows only login UI to unauthenticated visitors', async () => {
    mockSession = null;
    render(<Page/>);
    expect(await screen.findByText(/Sign in to access/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument();
    expect(screen.queryByText('Professional realtime run console')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Conversation UUID')).not.toBeInTheDocument();
  });
});
