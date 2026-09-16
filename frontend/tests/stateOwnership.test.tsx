/**
 * Delayed-response ownership, driven through the real page.
 *
 * Every test here holds one authenticated request open, changes the selection
 * underneath it, and then lets the request finish. The page stays mounted the
 * whole time, so React unmounting is not what keeps the late answer out — the
 * ownership scope is, and these tests are how that is proven rather than
 * asserted. Each one fails against the pre-F5 page, which applied whatever
 * arrived whenever it arrived.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';

const ALICE = { access_token: 'alice-token', user: { id: 'aaaaaaaa-1111-4111-8111-00000000000a', email: 'alice@example.com' } };
const BOB = { access_token: 'bob-token', user: { id: 'aaaaaaaa-1111-4111-8111-00000000000b', email: 'bob@example.com' } };

let mockSession: any = ALICE;

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('token')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: true,
  keys: 0,
  api: {
    projects: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(),
    createProposal: vi.fn(), proposal: vi.fn(), decideProposal: vi.fn(), reviseProposal: vi.fn(),
    startRun: vi.fn(), run: vi.fn(), events: vi.fn(), cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => `ownership-key-${(apiMocks.keys += 1)}`,
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

const PROJECT_A = { id: '11111111-1111-4111-8111-00000000000a', slug: 'alpha', name: 'Alpha Project', workflow_key: 'vehicle_catalog_v1' };
const PROJECT_B = { id: '11111111-1111-4111-8111-00000000000b', slug: 'beta', name: 'Beta Project', workflow_key: 'vehicle_catalog_v1' };
const BOB_PROJECT = { id: '11111111-1111-4111-8111-00000000000c', slug: 'bobs', name: 'Bob Only Project', workflow_key: 'vehicle_catalog_v1' };

const CONVO_A = { id: '22222222-1111-4111-8111-00000000000a', project_id: PROJECT_A.id, title: 'Alpha conversation' };
const CONVO_B = { id: '22222222-1111-4111-8111-00000000000b', project_id: PROJECT_B.id, title: 'Beta conversation' };
const CONVO_A2 = { id: '22222222-1111-4111-8111-00000000000d', project_id: PROJECT_A.id, title: 'Alpha second conversation' };

const RUN_A = '33333333-1111-4111-8111-00000000000a';

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void; reject: (error: unknown) => void };

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** Let every already-scheduled microtask and state update flush. */
async function settle() {
  await waitFor(() => expect(true).toBe(true));
  await new Promise((resolve) => setTimeout(resolve, 20));
}

describe('delayed responses never cross a selection boundary', () => {
  beforeEach(() => {
    mockSession = ALICE;
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT_A, PROJECT_B]);
    apiMocks.api.conversations.mockResolvedValue([]);
    apiMocks.api.run.mockResolvedValue({ id: RUN_A, conversation_id: CONVO_A.id, status: 'running' });
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  it('project A conversations arriving after project B was selected are dropped', async () => {
    const slowA = deferred<any[]>();
    apiMocks.api.conversations.mockImplementation(async (projectId: string) =>
      projectId === PROJECT_A.id ? slowA.promise : [CONVO_B]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Beta Project'));
    await screen.findByText('Beta conversation');

    slowA.resolve([CONVO_A]); // project A answers late
    await settle();

    expect(screen.queryByText('Alpha conversation')).not.toBeInTheDocument();
    expect(screen.getByText('Beta conversation')).toBeInTheDocument();
  });

  it('a conversation-list failure for project A never becomes project B error text', async () => {
    const slowA = deferred<any[]>();
    apiMocks.api.conversations.mockImplementation(async (projectId: string) =>
      projectId === PROJECT_A.id ? slowA.promise : [CONVO_B]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Beta Project'));
    await screen.findByText('Beta conversation');

    slowA.reject(new Error('alpha listing exploded'));
    await settle();

    expect(screen.queryByText(/alpha listing exploded/)).not.toBeInTheDocument();
    expect(screen.getByText('Beta conversation')).toBeInTheDocument();
  });

  it('a conversation created for project A is not added to, or selected in, project B', async () => {
    const slowCreate = deferred<any>();
    apiMocks.api.createConversation.mockReturnValue(slowCreate.promise);
    apiMocks.api.conversations.mockImplementation(async (projectId: string) =>
      projectId === PROJECT_A.id ? [] : [CONVO_B]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.change(screen.getByLabelText('Conversation title'), { target: { value: 'Alpha second conversation' } });
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));

    fireEvent.click(await screen.findByText('Beta Project'));
    await screen.findByText('Beta conversation');

    slowCreate.resolve(CONVO_A2); // creation answers after the project changed
    await settle();

    expect(screen.queryByText('Alpha second conversation')).not.toBeInTheDocument();
    // …and it certainly was not opened as the active conversation of project B.
    expect(screen.queryByText(`ID ${CONVO_A2.id} • project ${CONVO_A2.project_id}`)).not.toBeInTheDocument();
  });

  it('a run created for conversation A never becomes the active run of conversation B', async () => {
    const slowRun = deferred<{ run_id: string; status: string }>();
    apiMocks.api.startRun.mockReturnValue(slowRun.promise);
    apiMocks.api.conversations.mockResolvedValue([CONVO_A, CONVO_A2]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));

    fireEvent.click(screen.getByText('Alpha second conversation'));
    slowRun.resolve({ run_id: RUN_A, status: 'queued' });
    await settle();

    // The run exists and is stored under the conversation it belongs to…
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBe(RUN_A);
    // …but it was never opened under, or stored against, the other one.
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A2.id}`)).toBeNull();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
    expect(screen.queryByText(RUN_A)).not.toBeInTheDocument();
  });

  it('a run-creation failure for conversation A is not shown under conversation B', async () => {
    const slowRun = deferred<{ run_id: string; status: string }>();
    apiMocks.api.startRun.mockReturnValue(slowRun.promise);
    apiMocks.api.conversations.mockResolvedValue([CONVO_A, CONVO_A2]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));

    fireEvent.click(screen.getByText('Alpha second conversation'));
    slowRun.reject(new Error('conversation A run refused'));
    await settle();

    expect(screen.queryByText(/conversation A run refused/)).not.toBeInTheDocument();
  });

  it('a proposal answering after its project changed is dropped', async () => {
    const slowProposal = deferred<any>();
    apiMocks.api.createProposal.mockReturnValue(slowProposal.promise);
    apiMocks.api.conversations.mockResolvedValue([]);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(screen.getByRole('button', { name: 'Workflow proposal' }));
    fireEvent.change(screen.getByLabelText('Proposal request'), { target: { value: 'alpha research' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate proposal' }));

    fireEvent.click(screen.getByText('Beta Project'));
    // The proposal panel stays open; only its project changed underneath it.
    slowProposal.resolve({
      id: '44444444-1111-4111-8111-00000000000a',
      status: 'draft',
      user_request: 'alpha research',
      draft: { agents: [{ key: 'a', role: 'alpha-only-agent', internet_policy: 'forbidden', internet_reason: 'alpha secret plan' }], workflow: ['plan'] },
      task_spec: {}, estimates: {}, critiques: [],
    });
    await settle();

    expect(screen.queryByText(/alpha-only-agent/)).not.toBeInTheDocument();
    expect(screen.queryByText(/alpha secret plan/)).not.toBeInTheDocument();
  });

  it('a proposal decision answering after its project changed is dropped', async () => {
    const proposal = {
      id: '44444444-1111-4111-8111-00000000000b', status: 'draft', user_request: 'alpha research',
      draft: { agents: [], workflow: ['plan'] }, task_spec: {}, estimates: {}, critiques: [],
    };
    apiMocks.api.createProposal.mockResolvedValue(proposal);
    const slowDecision = deferred<any>();
    apiMocks.api.decideProposal.mockReturnValue(slowDecision.promise);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(screen.getByRole('button', { name: 'Workflow proposal' }));
    fireEvent.change(screen.getByLabelText('Proposal request'), { target: { value: 'alpha research' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate proposal' }));
    await screen.findByRole('button', { name: 'Reject' });
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));

    fireEvent.click(screen.getByText('Beta Project'));
    slowDecision.resolve({ ...proposal, status: 'rejected-after-the-project-changed' });
    await settle();

    expect(screen.queryByText(/rejected-after-the-project-changed/)).not.toBeInTheDocument();
  });

  it('projects answering after sign-out never reach the signed-out page', async () => {
    const slowProjects = deferred<any[]>();
    apiMocks.api.projects.mockReturnValue(slowProjects.promise);
    window.sessionStorage.setItem(`milo.activeRun.${CONVO_A.id}`, RUN_A);

    render(<Page/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });

    slowProjects.resolve([PROJECT_A, PROJECT_B]);
    await settle();

    expect(screen.queryByText('Alpha Project')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument();
    // Sign-out also drops the browser's own record of which run each
    // conversation was on; it is not workspace data a signed-out page may keep.
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBeNull();
  });

  it("one user's projects never render for the user who replaced them", async () => {
    const aliceProjects = deferred<any[]>();
    apiMocks.api.projects.mockReturnValueOnce(aliceProjects.promise).mockResolvedValue([BOB_PROJECT]);

    render(<Page/>);
    fireEvent.click(await screen.findByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });

    mockSession = BOB;
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: BOB.user.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'bob-Password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByText('Bob Only Project')).toBeInTheDocument();

    aliceProjects.resolve([PROJECT_A, PROJECT_B]); // Alice's list, answering late
    await settle();

    expect(screen.queryByText('Alpha Project')).not.toBeInTheDocument();
    expect(screen.queryByText('Beta Project')).not.toBeInTheDocument();
    expect(screen.getByText('Bob Only Project')).toBeInTheDocument();
  });

  it('a stored run belonging to another conversation is refused, cleared and reported', async () => {
    apiMocks.api.conversations.mockResolvedValue([CONVO_A]);
    // The stored id says this conversation's run. The server says the run
    // belongs elsewhere — and the server is the only one that knows.
    apiMocks.api.run.mockResolvedValue({ id: RUN_A, conversation_id: CONVO_B.id, status: 'completed', output: { summary: 'other conversation output' } });
    window.sessionStorage.setItem(`milo.activeRun.${CONVO_A.id}`, RUN_A);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));

    expect(await screen.findByText(/could not be verified as belonging to it/)).toBeInTheDocument();
    expect(screen.queryByText(/other conversation output/)).not.toBeInTheDocument();
    // Quarantined: a refresh cannot re-open it.
    await waitFor(() => expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBeNull());
  });

  it('a run row that is not the run that was asked for is refused', async () => {
    apiMocks.api.conversations.mockResolvedValue([CONVO_A]);
    apiMocks.api.run.mockResolvedValue({ id: '99999999-1111-4111-8111-000000000099', conversation_id: CONVO_A.id, status: 'completed', output: { summary: 'a different run entirely' } });
    window.sessionStorage.setItem(`milo.activeRun.${CONVO_A.id}`, RUN_A);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));

    expect(await screen.findByText(/could not be verified as belonging to it/)).toBeInTheDocument();
    expect(screen.queryByText(/a different run entirely/)).not.toBeInTheDocument();
  });
});

/**
 * A durable browser-side write is a separate question from a rendered one.
 *
 * `sessionStorage` outlives this component and survives a sign-out, so "did
 * anything render" does not answer "did anything get written". Ownership is
 * therefore checked BEFORE the write, not after it — the original ordering
 * stored the run id first and checked second, which let an answer belonging to
 * a signed-out session put a key back that sign-out had just removed.
 */
describe('a durable write needs ownership first', () => {
  beforeEach(() => {
    mockSession = ALICE;
    apiMocks.executionUi = true;
    apiMocks.keys = 0;
    for (const fn of Object.values(apiMocks.api)) {
      if (typeof fn === 'function') fn.mockReset();
    }
    apiMocks.api.projects.mockResolvedValue([PROJECT_A]);
    apiMocks.api.conversations.mockResolvedValue([CONVO_A, CONVO_A2]);
    apiMocks.api.run.mockResolvedValue({ id: RUN_A, conversation_id: CONVO_A.id, status: 'queued' });
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  async function openConversationAndSubmit(pending: Promise<any>) {
    apiMocks.api.startRun.mockReturnValue(pending);
    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'go' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
  }

  it('a run created after sign-out is neither stored nor rendered', async () => {
    const slow = deferred<{ run_id: string; status: string }>();
    await openConversationAndSubmit(slow.promise);

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBeNull();

    slow.resolve({ run_id: RUN_A, status: 'queued' });
    await settle();

    // Nothing was written back into the browser…
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBeNull();
    // …nothing was polled for it…
    expect(apiMocks.api.run).not.toHaveBeenCalled();
    // …and the signed-out screen is still just the signed-out screen.
    expect(screen.getByRole('button', { name: 'Login' })).toBeInTheDocument();
    expect(screen.queryByText('Alpha Project')).not.toBeInTheDocument();
    expect(screen.queryByText(RUN_A)).not.toBeInTheDocument();
  });

  it("a run created for the previous user is neither stored nor rendered for the next one", async () => {
    const slow = deferred<{ run_id: string; status: string }>();
    await openConversationAndSubmit(slow.promise);

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });

    mockSession = BOB;
    apiMocks.api.projects.mockResolvedValue([BOB_PROJECT]);
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: BOB.user.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'bob-Password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    expect(await screen.findByText('Bob Only Project')).toBeInTheDocument();

    slow.resolve({ run_id: RUN_A, status: 'queued' }); // Alice's run, answering late
    await settle();

    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBeNull();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
    expect(screen.queryByText(RUN_A)).not.toBeInTheDocument();
    expect(screen.getByText('Bob Only Project')).toBeInTheDocument();
  });

  it('a run created for conversation A is stored under A and never activated under B', async () => {
    const slow = deferred<{ run_id: string; status: string }>();
    await openConversationAndSubmit(slow.promise);

    // Same session, different conversation.
    fireEvent.click(screen.getByText('Alpha second conversation'));
    slow.resolve({ run_id: RUN_A, status: 'queued' });
    await settle();

    // The run exists and belongs to A, so A's own key records it…
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A.id}`)).toBe(RUN_A);
    // …and B neither stores it nor opens it.
    expect(window.sessionStorage.getItem(`milo.activeRun.${CONVO_A2.id}`)).toBeNull();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
    expect(screen.queryByText(RUN_A)).not.toBeInTheDocument();

    // Going back to A finds it exactly where it belongs.
    fireEvent.click(screen.getByText('Alpha conversation'));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN_A));
  });
});

/**
 * A busy flag is not a boolean.
 *
 * Two things must hold at once, and the unconditional `finally` setters could
 * satisfy neither: a replacement owner must not inherit a busy state it never
 * set, and a superseded request settling must not clear the state its
 * successor is still waiting on. Clearing flags on sign-out fixes only the
 * first, which is why the pending state holds WHICH request is in flight.
 */
describe('pending state belongs to the request that set it', () => {
  beforeEach(() => {
    mockSession = ALICE;
    apiMocks.executionUi = true;
    apiMocks.keys = 0;
    for (const fn of Object.values(apiMocks.api)) {
      if (typeof fn === 'function') fn.mockReset();
    }
    apiMocks.api.projects.mockResolvedValue([PROJECT_A]);
    apiMocks.api.conversations.mockResolvedValue([]);
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  it('a replacement session starts unblocked rather than inheriting the busy state', async () => {
    const slowA = deferred<any>();
    apiMocks.api.createConversation.mockReturnValue(slowA.promise);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Creating conversation…' })).toBeDisabled());

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });
    mockSession = BOB;
    apiMocks.api.projects.mockResolvedValue([BOB_PROJECT]);
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: BOB.user.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'bob-Password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    fireEvent.click(await screen.findByText('Bob Only Project'));

    const control = screen.getByRole('button', { name: /New conversation|Creating conversation/ });
    expect(control).toHaveTextContent('New conversation');
    expect(control).not.toBeDisabled();
  });

  it("an old request settling cannot clear the newer owner's busy state", async () => {
    const slowA = deferred<any>();
    const slowB = deferred<any>();
    apiMocks.api.createConversation.mockReturnValueOnce(slowA.promise).mockReturnValueOnce(slowB.promise);

    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Creating conversation…' })).toBeDisabled());

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });
    mockSession = BOB;
    apiMocks.api.projects.mockResolvedValue([BOB_PROJECT]);
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: BOB.user.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'bob-Password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    fireEvent.click(await screen.findByText('Bob Only Project'));

    // Bob starts his own creation and is now waiting on it.
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Creating conversation…' })).toBeDisabled());

    // Alice's superseded request settles. Its `finally` must be a no-op here.
    slowA.resolve(CONVO_A);
    await settle();

    expect(screen.getByRole('button', { name: /New conversation|Creating conversation/ }))
      .toHaveTextContent('Creating conversation…');

    // Bob's own request still frees his control when it settles.
    slowB.resolve({ id: '55555555-1111-4111-8111-00000000000b', project_id: BOB_PROJECT.id, title: "Bob's conversation" });
    await settle();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /New conversation|Creating conversation/ }))
        .toHaveTextContent('New conversation'));
  });
});

/**
 * The idempotency key belongs to one logical submission.
 *
 * Retrying the SAME submission must reuse it, so the backend returns the
 * original run instead of creating a second one. Anything else — a different
 * session, a different conversation, different content — is a different
 * submission and must not inherit it.
 */
describe('the idempotency key is scoped to its submission', () => {
  beforeEach(() => {
    mockSession = ALICE;
    apiMocks.executionUi = true;
    apiMocks.keys = 0;
    for (const fn of Object.values(apiMocks.api)) {
      if (typeof fn === 'function') fn.mockReset();
    }
    apiMocks.api.projects.mockResolvedValue([PROJECT_A]);
    apiMocks.api.conversations.mockResolvedValue([CONVO_A, CONVO_A2]);
    apiMocks.api.run.mockResolvedValue({ id: RUN_A, conversation_id: CONVO_A.id, status: 'queued' });
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  function keysUsed(): string[] {
    return apiMocks.api.startRun.mock.calls.map((call: unknown[]) => call[2] as string);
  }

  it('retrying the same submission reuses the key', async () => {
    apiMocks.api.startRun.mockRejectedValue(new Error('upstream refused'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'the same task' } });

    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    const [first, second] = keysUsed();
    expect(second).toBe(first);
  });

  it('a different session cannot reuse a failed attempt key', async () => {
    apiMocks.api.startRun.mockRejectedValue(new Error('upstream refused'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'the same task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await screen.findByRole('button', { name: 'Login' });
    mockSession = BOB;
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: BOB.user.email } });
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'bob-Password-1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Login' }));
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'the same task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    const [aliceKey, bobKey] = keysUsed();
    expect(bobKey).not.toBe(aliceKey);
  });

  it('an unrelated submission in the same session cannot reuse it', async () => {
    apiMocks.api.startRun.mockRejectedValue(new Error('upstream refused'));
    render(<Page/>);
    fireEvent.click(await screen.findByText('Alpha Project'));
    fireEvent.click(await screen.findByText('Alpha conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'first task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    // Different content is a different logical submission…
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'a completely different task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    // …and so is the same content in a different conversation.
    fireEvent.click(screen.getByText('Alpha second conversation'));
    fireEvent.change(screen.getByLabelText('Task content'), { target: { value: 'a completely different task' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send task' }));
    await settle();

    const [first, second, third] = keysUsed();
    expect(second).not.toBe(first);
    expect(third).not.toBe(second);
  });
});
