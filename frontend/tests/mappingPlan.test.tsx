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
  BATCH_ONE, BATCH_RUN, BATCH_TWO, CAPABILITIES, CONVERSATION, DIGEST, DIRECTORY, NEXT_DIGEST, PLAN,
  PROJECT, progressBody, stateBody,
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
    workScopeProgress: vi.fn(), startWorkScopeBatch: vi.fn(), pauseWorkScope: vi.fn(),
    resumeWorkScope: vi.fn(),
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

// ---------------------------------------------------------------------------
// Batches (scoped catalog PR3)
// ---------------------------------------------------------------------------

const BATCHES_ON = { ...CAPABILITIES, can_start_batches: true };
const LIVE = { batch_id: BATCH_TWO, batch_number: 2, revision: 1, unit_key: 'toyota', item_count: 10,
               attempt: 1, run_id: BATCH_RUN, run_status: 'running', launch_state: 'launched' };

function runningBody() {
  const body = progressBody();
  return progressBody({
    status: 'running', live: LIVE,
    controls: { ...body.controls,
                start: { available: false, blocked_by: 'batch_running', batch: null, retry: false, relaunch: false },
                cancel: { available: true, run_id: BATCH_RUN } },
  });
}

describe('the Mapping Plan batches', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.runs.mockResolvedValue([]);
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONV]);
    apiMocks.api.workScopeCapabilities.mockResolvedValue(BATCHES_ON);
    apiMocks.api.workScopeDirectory.mockResolvedValue(DIRECTORY);
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: stateBody() });
    apiMocks.api.workScopeProgress.mockResolvedValue(progressBody());
    apiMocks.api.run.mockReturnValue(new Promise(() => {}));
    apiMocks.api.events.mockReturnValue(new Promise(() => {}));
    window.sessionStorage.clear();
  });

  async function openBatches() {
    await openConversation();
    const panel = await openPanel();
    await waitFor(() => expect(apiMocks.api.workScopeProgress).toHaveBeenCalledWith(PLAN));
    return (await within(panel).findByRole('heading', { name: 'Batches' })).closest('section') as HTMLElement;
  }

  it('shows the server’s progress, per manufacturer and per batch', async () => {
    const batches = await openBatches();
    expect(batches.textContent).toContain('Prepared from revision 1: 25 candidates in 3 batches.');
    const totals = within(batches).getByLabelText('Plan progress');
    expect(totals.textContent).toContain('Batches finished1 of 3');
    expect(totals.textContent).toContain('Candidates done10 of 25');
    expect(totals.textContent).toContain('Promoted3');
    expect(totals.textContent).toContain('Unresolved6');
    expect(totals.textContent).toContain('Remaining15');
    const units = within(batches).getByRole('list', { name: 'Manufacturers in priority order' });
    const [toyota, lexus] = within(units).getAllByRole('listitem');
    expect(toyota.textContent).toContain('Partly done');
    expect(toyota.textContent).toContain('1 of 3 batches finished');
    expect(lexus.textContent).toContain('register spelling is not verified');
    const recent = within(batches).getByRole('list', { name: 'Recently started batches, newest first' });
    expect(recent.textContent).toContain('Batch 1 of 3 — Toyota, 10 candidates — Finished');
    expect(recent.textContent).toContain('3 promoted, 1 refused, 6 unresolved');
  });

  it('starts exactly the batch the server named, only after confirmation', async () => {
    apiMocks.api.startWorkScopeBatch.mockResolvedValue({
      run_id: BATCH_RUN, status: 'queued', work_scope_id: PLAN, batch_id: BATCH_TWO, attempt: 1, created: true });
    const batches = await openBatches();
    fireEvent.click(within(batches).getByRole('button', { name: 'Continue with batch 2' }));
    // Nothing is sent before the person confirms ONE paid run.
    expect(apiMocks.api.startWorkScopeBatch).not.toHaveBeenCalled();
    const confirm = within(batches).getByRole('group', { name: 'Confirm batch start' });
    expect(confirm.textContent).toContain('Start Batch 2 of 3 — Toyota, 10 candidates?');
    expect(confirm.textContent).toContain('ONE paid run');
    apiMocks.api.workScopeProgress.mockResolvedValue(runningBody());
    fireEvent.click(within(confirm).getByRole('button', { name: 'Yes, start this batch' }));

    await waitFor(() => expect(apiMocks.api.startWorkScopeBatch).toHaveBeenCalledWith(
      PLAN, { revision: 1, digest: DIGEST }, BATCH_TWO, 'ui-test-idempotency-key'));
    expect(apiMocks.api.startWorkScopeBatch).toHaveBeenCalledTimes(1);
    // The new run becomes the one the workspace shows, and the progress is
    // read back from the server rather than assumed.
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(BATCH_RUN));
    await waitFor(() => expect(within(batches).getByRole('status', { name: 'Current batch' }).textContent)
      .toContain('Batch 2 of 3 — Toyota, 10 candidates — running'));
    expect(apiMocks.api.startRun).not.toHaveBeenCalled();
    expect(within(batches).queryByRole('button', { name: /batch 3/ })).toBeNull();
  });

  it('reads the real progress back after a refusal, in authored copy', async () => {
    const { ApiError } = await import('../lib/api');
    apiMocks.api.startWorkScopeBatch.mockRejectedValue(
      new ApiError(409, 'WORK_SCOPE_BATCH_NOT_NEXT', 'upstream words that must not appear'));
    const batches = await openBatches();
    fireEvent.click(within(batches).getByRole('button', { name: 'Continue with batch 2' }));
    fireEvent.click(within(batches).getByRole('button', { name: 'Yes, start this batch' }));
    const alert = await within(batches).findByRole('alert');
    expect(alert.textContent).toContain('no longer the next one');
    expect(alert.textContent).not.toContain('upstream words');
    await waitFor(() => expect(apiMocks.api.workScopeProgress).toHaveBeenCalledTimes(2));
  });

  it('says why nothing can start, and offers no start', async () => {
    const body = progressBody();
    apiMocks.api.workScopeProgress.mockResolvedValue(progressBody({
      status: 'not_prepared', preparation: null,
      controls: { ...body.controls,
                  start: { available: false, blocked_by: 'not_prepared', batch: null, retry: false, relaunch: false } },
    }));
    const batches = await openBatches();
    expect(batches.textContent).toContain('has not been prepared yet');
    expect(within(batches).queryByRole('button', { name: /Start batch|Continue with batch/ })).toBeNull();
  });

  it('pauses and resumes the plan through the server', async () => {
    apiMocks.api.pauseWorkScope.mockResolvedValue({
      changed: true, paused: true,
      progress: progressBody({ paused: true, controls: { ...progressBody().controls,
        start: { available: false, blocked_by: 'paused', batch: null, retry: false, relaunch: false },
        pause: { available: false }, resume: { available: true } } }),
    });
    const batches = await openBatches();
    fireEvent.click(within(batches).getByRole('button', { name: 'Pause plan' }));
    await waitFor(() => expect(apiMocks.api.pauseWorkScope).toHaveBeenCalledWith(PLAN));
    await within(batches).findByRole('button', { name: 'Resume plan' });
    expect(batches.textContent).toContain('Paused — no batch will start');
    expect(batches.textContent).toContain('Resume it to start the next batch');
  });

  it('cancels the running batch through the existing run cancellation', async () => {
    apiMocks.api.workScopeProgress.mockResolvedValue(runningBody());
    apiMocks.api.cancel.mockResolvedValue({ run_id: BATCH_RUN, status: 'cancellation_requested' });
    const batches = await openBatches();
    const current = await within(batches).findByRole('status', { name: 'Current batch' });
    fireEvent.click(within(current).getByRole('button', { name: 'Cancel this batch' }));
    await waitFor(() => expect(apiMocks.api.cancel).toHaveBeenCalledWith(BATCH_RUN, 'Cancelled from the mapping plan'));
    expect(apiMocks.api.startWorkScopeBatch).not.toHaveBeenCalled();
  });

  it('launches a batch no worker was started for as the same run, and never offers to cancel it', async () => {
    const body = progressBody();
    const unlaunched = { ...LIVE, run_status: 'queued', launch_state: 'launch_failed' };
    const batch = { batch_id: BATCH_TWO, batch_number: 2, unit_key: 'toyota', item_count: 10,
                    state: 'active', attempts: 1 };
    apiMocks.api.workScopeProgress.mockResolvedValue(progressBody({
      status: 'running', live: unlaunched,
      controls: { ...body.controls,
                  start: { available: true, blocked_by: null, batch, retry: false, relaunch: true },
                  cancel: { available: false, run_id: BATCH_RUN } },
    }));
    const batches = await openBatches();
    const current = await within(batches).findByRole('status', { name: 'Current batch' });
    expect(current.textContent).toContain('The worker for this batch could not be started.');
    expect(current.textContent).toContain('Launching it starts this same run; nothing is created twice.');
    expect(within(current).queryByRole('button', { name: 'Cancel this batch' })).toBeNull();
    fireEvent.click(within(batches).getByRole('button', { name: 'Launch batch 2' }));
    const confirm = within(batches).getByRole('group', { name: 'Confirm batch start' });
    expect(confirm.textContent).toContain('Launch Batch 2 of 3 — Toyota, 10 candidates?');
  });

  it('says plainly when a revised plan is held by a batch that was never launched', async () => {
    const body = progressBody();
    apiMocks.api.workScopeProgress.mockResolvedValue(progressBody({
      revision: 2, status: 'running', preparation: null,
      live: { ...LIVE, run_status: 'queued', launch_state: 'pending' },
      controls: { ...body.controls,
                  start: { available: false, blocked_by: 'batch_running', batch: null, retry: false, relaunch: false },
                  cancel: { available: false, run_id: BATCH_RUN } },
    }));
    const batches = await openBatches();
    const current = await within(batches).findByRole('status', { name: 'Current batch' });
    expect(current.textContent).toContain('This batch belongs to revision 1');
    expect(current.textContent).toContain('It holds the plan until an operator resolves it');
    expect(within(batches).queryByRole('button', { name: /Launch batch|Start batch|Continue with batch|Cancel this batch/ }))
      .toBeNull();
  });

  it('is absent, and reads no progress, where the server does not let batches start', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(CAPABILITIES);
    await openConversation();
    const panel = await openPanel();
    await waitFor(() => expect(apiMocks.api.openWorkScope).toHaveBeenCalledWith(CONVERSATION));
    await act(async () => { await Promise.resolve(); });
    expect(within(panel).queryByRole('heading', { name: 'Batches' })).toBeNull();
    expect(apiMocks.api.workScopeProgress).not.toHaveBeenCalled();
    expect(BATCH_ONE).not.toBe(BATCH_TWO);
  });
});
