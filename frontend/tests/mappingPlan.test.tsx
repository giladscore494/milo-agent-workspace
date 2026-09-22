/**
 * The Mapping Plan, driven through the SHIPPED page.
 *
 * What is asserted is behaviour at the boundary: which requests the page
 * issues, with exactly which bodies, and what it renders from the answers. The
 * page never interprets an instruction itself -- the words go to the server as
 * words -- and it never renders a plan the server did not return.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import {
  CAPABILITIES, CONVERSATION, DIGEST, DIRECTORY, NEXT_DIGEST, PLAN, PROJECT, stateBody,
} from './fixtures/workScope';

const SESSION = { access_token: 'fresh', user: { id: 'u1', email: 'u@example.com' } };

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
    startRun: vi.fn(), run: vi.fn(), runs: vi.fn(() => Promise.resolve([])), events: vi.fn(),
    cancel: vi.fn(), catalogCanonical: vi.fn(), catalogReviewCandidates: vi.fn(),
    workScopeCapabilities: vi.fn(), workScopeDirectory: vi.fn(), openWorkScope: vi.fn(),
    createWorkScope: vi.fn(), reviseWorkScope: vi.fn(),
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

const SWARM_PROJECT = { id: PROJECT, slug: 'swarm', name: 'Swarm Project', workflow_key: 'swarm_v2' };
const V1_PROJECT = { id: '9a1d1b02-0b2f-4a5b-9a24-8f0d5a6f4c31', slug: 'v1', name: 'V1 Project', workflow_key: 'vehicle_catalog_v1' };
const CONV = { id: CONVERSATION, project_id: PROJECT, title: 'Plan conversation' };
const OTHER_CONV = { id: '2f90f4ce-7844-4031-91d6-b74e40e1884e', project_id: PROJECT, title: 'Other conversation' };

async function openConversation(project = SWARM_PROJECT, conversation = CONV) {
  render(<Page/>);
  fireEvent.click(await screen.findByText(project.name));
  fireEvent.click(await screen.findByText(conversation.title));
}

async function openPanel() {
  const section = (await screen.findByRole('heading', { name: 'Mapping plan' })).closest('section')!;
  fireEvent.click(within(section).getByRole('button', { name: 'Show' }));
  return section as HTMLElement;
}

describe('the Mapping Plan surface', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.runs.mockResolvedValue([]);
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT, V1_PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONV, OTHER_CONV]);
    apiMocks.api.workScopeCapabilities.mockResolvedValue(CAPABILITIES);
    apiMocks.api.workScopeDirectory.mockResolvedValue(DIRECTORY);
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: null });
    window.sessionStorage.clear();
  });

  it('turns typed words into a server plan, sent as words', async () => {
    apiMocks.api.createWorkScope.mockResolvedValue({ applied: true, notes: [], work_scope: stateBody() });
    await openConversation();
    const panel = await openPanel();
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalledWith(CONVERSATION));

    fireEvent.change(within(panel).getByLabelText('Tell MILO what to map'), {
      target: { value: '  Map Toyota and Lexus, starting with 2018+, up to 800 variants.  ' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Create plan' }));

    await waitFor(() => expect(apiMocks.api.createWorkScope).toHaveBeenCalledWith(
      CONVERSATION, { instruction: 'Map Toyota and Lexus, starting with 2018+, up to 800 variants.' }));
    const units = await within(panel).findByRole('list', { name: 'Manufacturers in priority order' });
    expect(within(units).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      expect.stringContaining('1. Toyota'), expect.stringContaining('2. Lexus')]);
    expect(panel.textContent).toContain('Revision 1');
    expect(panel.textContent).toContain(DIGEST.slice(0, 12));
    // Nothing executes from a plan, and the page says so.
    expect(panel.textContent).toContain('Planning only');
    expect(apiMocks.api.startRun).not.toHaveBeenCalled();
  });

  it('turns clicks into ONE complete edit against the exact head it read', async () => {
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: stateBody() });
    apiMocks.api.reviseWorkScope.mockResolvedValue({
      applied: true, notes: [],
      work_scope: stateBody({
        revision: 2, digest: NEXT_DIGEST,
        plan: { ...stateBody().plan, units: ['mazda', 'toyota', 'lexus'], batch_size: 15 },
        head: { revision: 2, digest: NEXT_DIGEST, input_kind: 'edit', notes: [] },
      }),
    });
    await openConversation();
    const panel = await openPanel();
    const directory = await within(panel).findByRole('list', { name: 'Manufacturer directory' });

    fireEvent.click(within(directory).getByRole('button', { name: 'Add Mazda' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Move Mazda up' }));
    fireEvent.click(within(panel).getByRole('button', { name: 'Move Mazda up' }));
    fireEvent.change(within(panel).getByLabelText(/Candidates per batch/), { target: { value: '15' } });
    // An unsaved draft blocks an instruction: the two inputs never mix.
    expect(within(panel).getByRole('button', { name: 'Update plan' })).toBeDisabled();
    fireEvent.click(within(panel).getByRole('button', { name: 'Save plan' }));

    await waitFor(() => expect(apiMocks.api.reviseWorkScope).toHaveBeenCalledWith(
      PLAN, { revision: 1, digest: DIGEST },
      { edit: { units: ['mazda', 'toyota', 'lexus'], model_year_from: 2018, model_year_to: null,
                max_items: 800, batch_size: 15 } }));
    await waitFor(() => expect(panel.textContent).toContain('Revision 2'));
  });

  it('refuses a stale plan rather than overwriting it, and reads the real one back', async () => {
    const { ApiError } = await import('../lib/api');
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: stateBody() });
    apiMocks.api.reviseWorkScope.mockRejectedValue(
      new ApiError(409, 'WORK_SCOPE_STALE', 'upstream words that must not appear'));
    await openConversation();
    const panel = await openPanel();
    await within(panel).findByRole('list', { name: 'Manufacturers in priority order' });

    fireEvent.change(within(panel).getByLabelText('Tell MILO what to map'), { target: { value: 'add Kia' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Update plan' }));

    const alert = await within(panel).findByRole('alert');
    expect(alert.textContent).toContain('changed since you opened it');
    expect(alert.textContent).not.toContain('upstream words');
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalledTimes(2));
  });

  it('shows how the words were read, in authored copy', async () => {
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: stateBody() });
    apiMocks.api.reviseWorkScope.mockResolvedValue({
      applied: false,
      notes: [{ code: 'WORK_SCOPE_NOTE_NO_CHANGE' }, { code: 'WORK_SCOPE_NOTE_UNRECOGNIZED', terms: ['<b>polestar</b>'] }],
      work_scope: stateBody(),
    });
    await openConversation();
    const panel = await openPanel();
    await within(panel).findByRole('list', { name: 'Manufacturers in priority order' });
    fireEvent.change(within(panel).getByLabelText('Tell MILO what to map'), { target: { value: 'Map Toyota and Polestar' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Update plan' }));

    const notes = await within(panel).findByRole('list', { name: 'How the plan was read' });
    expect(notes.textContent).toContain('nothing changed');
    expect(notes.textContent).toContain('polestar');
    expect(notes.innerHTML).not.toContain('<b>');
  });

  it('states coverage as the server stated it, never a fabricated zero', async () => {
    await openConversation();
    const panel = await openPanel();
    const directory = await within(panel).findByRole('list', { name: 'Manufacturer directory' });
    const toyota = within(directory).getByText('Toyota').closest('li')!;
    const mazda = within(directory).getByText('Mazda').closest('li')!;
    expect(toyota.textContent).toContain('Not mapped yet');
    expect(mazda.textContent).toContain('Coverage unknown — register spelling not verified');
  });

  it('stays hidden, and reads no plan, when the server says it does not apply', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue({ ...CAPABILITIES, available: false, reason: 'mutations_disabled' });
    await openConversation();
    await waitFor(() => expect(apiMocks.api.workScopeCapabilities).toHaveBeenCalledWith(PROJECT));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole('heading', { name: 'Mapping plan' })).toBeNull();
    expect(apiMocks.api.openWorkScope).not.toHaveBeenCalled();
  });

  it('never asks about a project whose engine reads no plan', async () => {
    apiMocks.api.conversations.mockResolvedValue([{ ...CONV, project_id: V1_PROJECT.id }]);
    await openConversation(V1_PROJECT);
    await act(async () => { await Promise.resolve(); });
    expect(apiMocks.api.workScopeCapabilities).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Mapping plan' })).toBeNull();
  });

  it('is absent when the execution UI flag is off', async () => {
    apiMocks.executionUi = false;
    await openConversation();
    await act(async () => { await Promise.resolve(); });
    expect(apiMocks.api.workScopeCapabilities).not.toHaveBeenCalled();
    expect(screen.queryByRole('heading', { name: 'Mapping plan' })).toBeNull();
  });

  it('never shows one conversation\'s plan under another', async () => {
    let resolveFirst!: (value: unknown) => void;
    apiMocks.api.openWorkScope
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValue({ work_scope: null });
    await openConversation();
    const panel = await openPanel();
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalledWith(CONVERSATION));
    fireEvent.click(screen.getByText('Other conversation'));
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalledWith(OTHER_CONV.id));
    // The FIRST conversation's plan arrives late.
    await act(async () => { resolveFirst({ work_scope: stateBody() }); });
    expect(panel.textContent).toContain('This conversation has no mapping plan yet.');
    expect(panel.textContent).not.toContain('Revision 1');
  });

  it('announces nothing while collapsed', async () => {
    apiMocks.api.openWorkScope.mockRejectedValue(new Error('boom'));
    await openConversation();
    const section = (await screen.findByRole('heading', { name: 'Mapping plan' })).closest('section')!;
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalled());
    expect(within(section as HTMLElement).queryByRole('alert')).toBeNull();
    expect(within(section as HTMLElement).queryByRole('status')).toBeNull();
  });
});
