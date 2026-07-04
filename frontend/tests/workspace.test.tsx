import { render, screen } from '@testing-library/react';
import Page from '../app/page';
import { describe, expect, it, vi } from 'vitest';
vi.mock('../lib/useRunRealtime',()=>({useRunRealtime:()=>({mode:'polling',state:{events:[],agents:{},sources:[],claims:[],conflicts:[],currentPhase:'idle',progress:0,tokens:0,cost:0,supervisor:[],validationErrors:[],checkpoints:[],rawErrors:[]}})}));
it('renders mobile-capable workspace beyond a chat box',()=>{ render(<Page/>); expect(screen.getByText('MILO')).toBeInTheDocument(); expect(screen.getByText('Live run')).toBeInTheDocument(); expect(screen.getByText('Developer')).toBeInTheDocument(); expect(screen.getByLabelText('User instruction during run')).toBeInTheDocument(); });
it('shows internet badge variants',()=>{ render(<Page/>); expect(screen.getByText(/forbidden internet/)).toBeInTheDocument(); expect(screen.getByText(/active internet/)).toBeInTheDocument(); });
