/**
 * Where a typed task may go, driven through the SHIPPED page.
 *
 * With the Government read on, the worker refuses every Swarm V2 run that is
 * not a prepared Mapping Plan batch (GOVERNMENT_BATCH_REQUIRED), and the API
 * refuses to create one (CATALOG_RUN_REQUIRES_MAPPING_PLAN). The composer must
 * therefore never present an ordinary task as a way to run catalog work: it
 * follows the server's `direct_runs` answer, sends a catalog project to the
 * Mapping Plan, and offers nothing while the answer is unknown. Projects that
 * are not catalog projects keep their ordinary composer.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import { CAPABILITIES, CONVERSATION, DIRECTORY, PROJECT, stateBody } from './fixtures/workScope';

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
const V1_CONV = { id: '7b1c7a52-1f55-4c1b-8d0e-3c0f2f0b9a11', project_id: V1_PROJECT.id, title: 'V1 conversation' };

const CATALOG_PROJECT = { ...CAPABILITIES, direct_runs: { allowed: false, blocked_by: 'catalog_batch_required' } };
const PLAIN_SWARM = { ...CAPABILITIES, direct_runs: { allowed: true, blocked_by: null } };

async function openConversation(project = SWARM_PROJECT, conversation = CONV) {
  render(<Page/>);
  fireEvent.click(await screen.findByText(project.name));
  fireEvent.click(await screen.findByText(conversation.title));
}

function composer(): HTMLElement {
  return screen.getByRole('status', { name: 'Task submission' });
}

describe('the composer follows the server’s answer on ordinary runs', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.runs.mockResolvedValue([]);
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT, V1_PROJECT]);
    apiMocks.api.conversations.mockImplementation((projectId: string) =>
      Promise.resolve(projectId === PROJECT ? [CONV] : [V1_CONV]));
    apiMocks.api.workScopeDirectory.mockResolvedValue(DIRECTORY);
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: null });
    window.sessionStorage.clear();
  });

  it('sends a catalog project to the Mapping Plan and offers no ordinary task', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(CATALOG_PROJECT);
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('start from the Mapping Plan'));
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
    expect(screen.queryByLabelText('Task content')).toBeNull();
    // The one way on is the Mapping Plan itself.
    fireEvent.click(within(composer()).getByRole('button', { name: 'Open the Mapping Plan' }));
    const panel = (await screen.findByRole('heading', { name: 'Mapping plan' })).closest('section')!;
    expect(within(panel as HTMLElement).getByRole('button', { name: 'Hide' })).toBeTruthy();
    expect(apiMocks.api.startRun).not.toHaveBeenCalled();
  });

  it('says so when the Mapping Plan is not enabled at this stage', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(
      { ...CATALOG_PROJECT, available: false, reason: 'mutations_disabled' });
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('not enabled at this activation stage'));
    expect(within(composer()).queryByRole('button', { name: 'Open the Mapping Plan' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
  });

  it('offers nothing while the answer is unknown, and asks again on request', async () => {
    apiMocks.api.workScopeCapabilities.mockRejectedValueOnce(new Error('network'));
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('Confirming how runs start'));
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
    apiMocks.api.workScopeCapabilities.mockResolvedValue(PLAIN_SWARM);
    fireEvent.click(within(composer()).getByRole('button', { name: 'Check again' }));
    await screen.findByRole('button', { name: 'Send task' });
  });

  it('never unlocks an ordinary task from an answer that does not state it', async () => {
    // A capability read without `direct_runs` (an older or malformed answer).
    apiMocks.api.workScopeCapabilities.mockResolvedValue(CAPABILITIES);
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('did not confirm'));
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
  });

  it('keeps the stage copy when run creation is off', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(
      { ...CAPABILITIES, direct_runs: { allowed: false, blocked_by: 'run_creation_disabled' } });
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('turned off at the current activation stage'));
  });

  it('keeps the ordinary composer for a Swarm V2 project whose runs read no catalog', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(PLAIN_SWARM);
    apiMocks.api.startRun.mockResolvedValue({ run_id: '5d0f8a57-51f5-4a6c-9a8a-0d1f2e3c4b5a', status: 'queued' });
    await openConversation();
    fireEvent.change(await screen.findByLabelText('Task content'), { target: { value: 'summarize' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await waitFor(() => expect(apiMocks.api.startRun).toHaveBeenCalledWith(
      CONVERSATION, 'summarize', 'ui-test-idempotency-key'));
  });

  it('keeps the ordinary composer for a non-catalog project the server allows', async () => {
    apiMocks.api.workScopeCapabilities.mockResolvedValue(
      { ...PLAIN_SWARM, available: false, reason: 'workflow_not_supported' });
    apiMocks.api.startRun.mockResolvedValue({ run_id: '6e1f9b68-62a6-4b7d-8b9b-1e2f3a4b5c6d', status: 'queued' });
    await openConversation(V1_PROJECT, V1_CONV);
    fireEvent.change(await screen.findByLabelText('Task content'), { target: { value: 'map it' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await waitFor(() => expect(apiMocks.api.startRun).toHaveBeenCalled());
    expect(apiMocks.api.workScopeCapabilities).toHaveBeenCalledWith(V1_PROJECT.id);
  });
});

/**
 * Stage P (plan authoring): the execution UI is on, the gateway proxies the
 * Mapping Plan writes, and the API's run creation is OFF. No project may offer
 * an actionable task; the Mapping Plan stays usable for authoring.
 */
describe('plan-authoring posture: plans can be written, nothing can start', () => {
  const STAGE_P_SWARM = { ...CAPABILITIES, can_start_batches: false,
    direct_runs: { allowed: false, blocked_by: 'run_creation_disabled' } };
  const STAGE_P_V1 = { ...STAGE_P_SWARM, available: false, reason: 'workflow_not_supported' };

  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.runs.mockResolvedValue([]);
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT, V1_PROJECT]);
    apiMocks.api.conversations.mockImplementation((projectId: string) =>
      Promise.resolve(projectId === PROJECT ? [CONV] : [V1_CONV]));
    apiMocks.api.workScopeCapabilities.mockImplementation((projectId: string) =>
      Promise.resolve(projectId === PROJECT ? STAGE_P_SWARM : STAGE_P_V1));
    apiMocks.api.workScopeDirectory.mockResolvedValue(DIRECTORY);
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: null });
    window.sessionStorage.clear();
  });

  it('offers no task in a Vehicle Catalog V1 project', async () => {
    await openConversation(V1_PROJECT, V1_CONV);
    await waitFor(() => expect(composer().textContent).toContain('turned off at the current activation stage'));
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
    expect(screen.queryByLabelText('Task content')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Mapping plan' })).toBeNull();
    expect(apiMocks.api.startRun).not.toHaveBeenCalled();
  });

  it('offers no task in a Swarm V2 project, and its Mapping Plan can be authored but not run', async () => {
    apiMocks.api.createWorkScope.mockResolvedValue({ applied: true, notes: [], work_scope: stateBody() });
    await openConversation();
    await waitFor(() => expect(composer().textContent).toContain('turned off at the current activation stage'));
    expect(screen.queryByRole('button', { name: 'Send task' })).toBeNull();
    const panel = (await screen.findByRole('heading', { name: 'Mapping plan' })).closest('section')! as HTMLElement;
    fireEvent.click(within(panel).getByRole('button', { name: 'Show' }));
    fireEvent.change(within(panel).getByLabelText('Tell MILO what to map'),
      { target: { value: 'Map Toyota, starting with 2018+, up to 10 variants.' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Create plan' }));
    await waitFor(() => expect(apiMocks.api.createWorkScope).toHaveBeenCalled());
    // Planning only: no batch control is offered while starts are off.
    expect(panel.textContent).toContain('Planning only');
    expect(within(panel).queryByRole('button', { name: /Start batch/ })).toBeNull();
    expect(apiMocks.api.startWorkScopeBatch).not.toHaveBeenCalled();
    expect(apiMocks.api.startRun).not.toHaveBeenCalled();
  });
});

describe('the Mapping Plan states what can really be prepared', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.runs.mockResolvedValue([]);
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONV]);
    apiMocks.api.workScopeCapabilities.mockResolvedValue(CATALOG_PROJECT);
    apiMocks.api.workScopeDirectory.mockResolvedValue(DIRECTORY);
    window.sessionStorage.clear();
  });

  async function openPanel() {
    await openConversation();
    const section = (await screen.findByRole('heading', { name: 'Mapping plan' })).closest('section')! as HTMLElement;
    fireEvent.click(within(section).getByRole('button', { name: 'Show' }));
    return section;
  }

  it('names the verified manufacturers and the ones preparation will not capture', async () => {
    // The plan names Toyota (verified register spelling) and Lexus (not).
    apiMocks.api.openWorkScope.mockResolvedValue({ work_scope: stateBody() });
    const panel = await openPanel();
    const note = await within(panel).findByRole('note', { name: 'What can be prepared' });
    expect(note.textContent).toContain('Can be prepared from the Government register: Toyota.');
    expect(note.textContent).toContain('Not preparable until their register spelling is verified');
    expect(note.textContent).toContain('Lexus');
    expect(note.textContent).not.toMatch(/prepared from the Government register: [^.]*Lexus/);
    // The directory states its own verified coverage, never "all ready".
    expect(panel.textContent).toContain('Verified Government-register spelling: 1 of 4 manufacturers (Toyota)');
  });

  it('says a plan of only unverified manufacturers can run nothing', async () => {
    apiMocks.api.openWorkScope.mockResolvedValue({
      work_scope: stateBody({ plan: { ...stateBody().plan, units: ['lexus', 'mazda'] } }) });
    const panel = await openPanel();
    const note = await within(panel).findByRole('note', { name: 'What can be prepared' });
    expect(note.textContent).toContain('would queue nothing and no batch could run');
  });
});
