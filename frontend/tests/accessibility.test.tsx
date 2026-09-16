/**
 * Focus, keyboard and announcement semantics — asserted as BEHAVIOUR.
 *
 * None of these tests look for an ARIA attribute and stop there. An attribute
 * satisfies a selector; what a keyboard or screen-reader user experiences is
 * where focus actually goes, what actually closes, and what is actually
 * announced. Each test below fails against the pre-F5 UI, which had the
 * markup and none of the behaviour.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import { CurrentRunPanel } from '../components/run/CurrentRunPanel';

const SESSION = { access_token: 'fresh', user: { id: 'aaaaaaaa-1111-4111-8111-00000000000a', email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(SESSION)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(SESSION)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: true,
  api: {
    projects: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(),
    createProposal: vi.fn(), proposal: vi.fn(), decideProposal: vi.fn(), reviseProposal: vi.fn(),
    startRun: vi.fn(), run: vi.fn(), events: vi.fn(), cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => 'a11y-test-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

const PROJECT = { id: '11111111-1111-4111-8111-00000000000a', slug: 'alpha', name: 'Alpha Project', workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION = { id: '22222222-1111-4111-8111-00000000000a', project_id: PROJECT.id, title: 'Alpha conversation' };
const RUN_ID = '33333333-1111-4111-8111-00000000000a';

function runRow(extra: Record<string, unknown> = {}) {
  return { id: RUN_ID, conversation_id: CONVERSATION.id, status: 'running', ...extra };
}

async function openRun(extra: Record<string, unknown> = {}) {
  apiMocks.api.run.mockResolvedValue(runRow(extra));
  window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN_ID);
  render(<Page/>);
  fireEvent.click(await screen.findByText('Alpha Project'));
  fireEvent.click(await screen.findByText('Alpha conversation'));
  await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN_ID));
}

describe('cancellation confirmation focus contract', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  it('moves focus into the confirmation when it opens', async () => {
    await openRun();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    await waitFor(() => expect(screen.getByLabelText('Cancellation reason')).toHaveFocus());
  });

  it('Escape keeps the run and returns focus to the control that opened it', async () => {
    await openRun();
    const trigger = await screen.findByRole('button', { name: 'Cancel run' });
    fireEvent.click(trigger);
    const reason = await screen.findByLabelText('Cancellation reason');

    fireEvent.keyDown(reason, { key: 'Escape' });

    // Escape is the non-destructive choice: nothing was cancelled.
    expect(apiMocks.api.cancel).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('Cancellation reason')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel run' })).toHaveFocus());
  });

  it('"Keep running" returns focus to the control that opened it', async () => {
    await openRun();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Keep running' }));

    expect(apiMocks.api.cancel).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel run' })).toHaveFocus());
  });

  it('Escape inside the confirmation does not also close an open drawer', async () => {
    await openRun();
    const inspectorToggle = screen.getByRole('button', { name: 'Run inspector' });
    fireEvent.click(inspectorToggle);
    expect(inspectorToggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    fireEvent.keyDown(await screen.findByLabelText('Cancellation reason'), { key: 'Escape' });

    // One key press dismisses one thing.
    expect(screen.queryByLabelText('Cancellation reason')).not.toBeInTheDocument();
    expect(inspectorToggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('a terminal state that withdraws the control does not strand focus', () => {
    // Rendered directly so the transition is exactly "the control was there,
    // focus was inside it, and the run went terminal".
    const props = {
      executionUi: true, hasConversation: true, runId: RUN_ID, runStatus: 'running',
      phase: 'research', connection: 'polling' as const, isPartialSuccess: false,
      confirmingCancel: true, cancelReason: '', cancelError: '',
      onCancelReasonChange: () => {}, onRequestCancel: () => {},
      onConfirmCancel: () => {}, onKeepRunning: () => {},
    };
    const { rerender } = render(<CurrentRunPanel {...props} isTerminal={false}/>);
    const reason = screen.getByLabelText('Cancellation reason');
    reason.focus();
    expect(reason).toHaveFocus();

    rerender(<CurrentRunPanel {...props} isTerminal runStatus="cancelled"/>);

    expect(screen.queryByLabelText('Cancellation reason')).not.toBeInTheDocument();
    // Focus landed on the surface that replaced the control, not on <body>.
    expect(document.activeElement).not.toBe(document.body);
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Live run' }));
  });

  it('the confirmation is a labelled group, not a dialog it does not behave like', async () => {
    await openRun();
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel run' }));
    const group = await screen.findByRole('group', { name: 'Confirm run cancellation' });
    expect(within(group).getByRole('button', { name: 'Confirm cancellation' })).toBeInTheDocument();
    expect(within(group).getByRole('button', { name: 'Keep running' })).toBeInTheDocument();
    // It claims no dialog semantics, because it traps no focus and marks
    // nothing inert — the run keeps updating around it.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('workspace drawer focus', () => {
  beforeEach(() => {
    apiMocks.executionUi = false;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    window.sessionStorage.clear();
  });

  async function renderWorkspace() {
    render(<Page/>);
    await screen.findByText('Alpha Project');
    return {
      sidebarToggle: screen.getByRole('button', { name: 'Workspace navigation' }),
      inspectorToggle: screen.getByRole('button', { name: 'Run inspector' }),
    };
  }

  it('opening a drawer moves focus into it', async () => {
    const { sidebarToggle } = await renderWorkspace();
    fireEvent.click(sidebarToggle);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Close projects and conversations' })).toHaveFocus());
  });

  it('closing a drawer returns focus to its own toggle', async () => {
    const { sidebarToggle } = await renderWorkspace();
    fireEvent.click(sidebarToggle);
    fireEvent.click(screen.getByRole('button', { name: 'Close projects and conversations' }));
    await waitFor(() => expect(sidebarToggle).toHaveFocus());
  });

  it('Escape closes the drawer on top and returns focus to that drawer toggle', async () => {
    const { sidebarToggle, inspectorToggle } = await renderWorkspace();
    fireEvent.click(sidebarToggle);
    fireEvent.click(inspectorToggle);
    // Focus followed the drawer that opened last.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Close inspector panel' })).toHaveFocus());

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(inspectorToggle).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(inspectorToggle).toHaveFocus());
    // The sidebar keeps its own state; one drawer never corrupts the other.
    expect(sidebarToggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('closing a drawer that does not hold focus leaves focus where the user left it', async () => {
    const { sidebarToggle, inspectorToggle } = await renderWorkspace();
    fireEvent.click(sidebarToggle);
    fireEvent.click(inspectorToggle);
    fireEvent.keyDown(window, { key: 'Escape' }); // inspector closes, focus -> its toggle
    await waitFor(() => expect(inspectorToggle).toHaveFocus());

    fireEvent.keyDown(window, { key: 'Escape' }); // now the sidebar closes

    expect(sidebarToggle).toHaveAttribute('aria-expanded', 'false');
    // Focus was NOT inside the sidebar, so it is not yanked out of where the
    // user actually is. Reclaiming focus is for the drawer that held it.
    expect(inspectorToggle).toHaveFocus();
  });

  it('selecting a project closes the drawer and hands focus back to the toggle', async () => {
    const { sidebarToggle, inspectorToggle } = await renderWorkspace();
    fireEvent.click(sidebarToggle);
    fireEvent.click(screen.getByText('Alpha Project'));
    expect(sidebarToggle).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(sidebarToggle).toHaveFocus());
    expect(inspectorToggle).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('announcements stay rare and meaningful', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  it('an ordinary run announces nothing at all on the V1 surface', async () => {
    await openRun();
    // No polling tick, task row, counter or status field is a live region.
    expect(document.querySelectorAll('[aria-live]')).toHaveLength(0);
    expect(document.querySelectorAll('[role="status"]')).toHaveLength(0);
  });

  it('launch_unknown announces, and says plainly that nothing will retry it', async () => {
    await openRun({ launch_state: 'launch_unknown', launch_reconciliation_required: true });

    const note = await screen.findByRole('status');
    expect(note).toHaveTextContent('Launch outcome unknown');
    expect(note).toHaveTextContent('launch_unknown');
    expect(note).toHaveTextContent('operator reconciliation required');
    expect(note).toHaveTextContent('will not be relaunched automatically');
    expect(note).toHaveTextContent('nothing on this screen retries it');
  });

  it('an ordinary launch state is reported without announcing anything', async () => {
    await openRun({ launch_state: 'launched', launch_reconciliation_required: false });
    expect(await screen.findByText(/Launch state/)).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('status meaning never depends on colour alone', async () => {
    await openRun({ status: 'partial_success' });
    // The verdict is carried by words. `data-tone` may colour it; it may not
    // be the only thing that says what happened.
    const verdict = await screen.findByText(/Run finished with status/);
    expect(verdict).toHaveTextContent('partial_success');
    expect(verdict).toHaveTextContent('Partial success is not a completed run');
  });
});
