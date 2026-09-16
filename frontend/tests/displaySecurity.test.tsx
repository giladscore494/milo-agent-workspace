/**
 * Four separate display defences, kept separate on purpose.
 *
 * - escaping (`safeText`) keeps markup from becoming markup;
 * - structured validation keeps unknown shapes off a product surface;
 * - redaction (`redactSecrets` / `redactSecretText`) keeps credentials out of
 *   what is printed;
 * - error classification (`safeErrorText`) keeps operational text off the
 *   screen entirely.
 *
 * Each covers something the others do not, and the most dangerous mistake is
 * to believe one of them is doing another's job. React escapes everything it
 * renders; escaping a credential prints the credential.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import { REDACTED, redactSecretText, safeText } from '../lib/sanitize';
import { API_KEY_PREFIX, API_KEY_SENTINEL, SUPABASE_SECRET_SENTINEL } from './secretSentinels';

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
  newIdempotencyKey: () => 'display-security-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

const HOSTILE_NAME = '<img src=x onerror="alert(1)">Project';
const HOSTILE_TITLE = '</button><script>alert(2)</script>Conversation';
const PROJECT = { id: '11111111-1111-4111-8111-00000000000a', slug: '<b>slug</b>', name: HOSTILE_NAME, workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION = { id: '22222222-1111-4111-8111-00000000000a', project_id: PROJECT.id, title: HOSTILE_TITLE };
const RUN_ID = '33333333-1111-4111-8111-00000000000a';

describe('hostile workspace content stays inert text', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([CONVERSATION]);
    apiMocks.api.events.mockResolvedValue([]);
    apiMocks.api.run.mockResolvedValue({ id: RUN_ID, conversation_id: CONVERSATION.id, status: 'running' });
    window.sessionStorage.clear();
  });

  it('a hostile project name and conversation title create no elements', async () => {
    render(<Page/>);
    fireEvent.click(await screen.findByText(/Project$/));
    await screen.findByText(/Conversation$/);
    // No element and no attribute was created from the payload.
    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('script')).toBeNull();
    expect(document.querySelector('[onerror]')).toBeNull();
    // It is present, as TEXT, with the angle brackets substituted outright so
    // nothing downstream can re-interpret them either.
    expect(document.body.textContent).toContain('\u2039img src=x onerror="alert(1)"\u203aProject');
    expect(document.body.textContent).toContain('\u2039/button\u203a\u2039script\u203aalert(2)');
  });

  it('a hostile run status and launch state render as text', async () => {
    apiMocks.api.run.mockResolvedValue({
      id: RUN_ID, conversation_id: CONVERSATION.id,
      status: '<script>alert(3)</script>running',
      launch_state: '<b>launched</b>' as never,
    });
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN_ID);
    render(<Page/>);
    fireEvent.click(await screen.findByText(/Project$/));
    fireEvent.click(await screen.findByText(/Conversation$/));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN_ID));
    expect(document.querySelector('script')).toBeNull();
    // The status and the launch state are text, with their angle brackets
    // substituted: no element was created from either.
    expect(document.body.textContent).toContain('\u2039script\u203aalert(3)\u2039/script\u203arunning');
    expect(document.body.textContent).toContain('\u2039b\u203alaunched\u2039/b\u203a');
    expect([...document.querySelectorAll('b')].map((node) => node.textContent))
      .not.toContain('launched');
  });
});

describe('escaping is not redaction', () => {
  it('safeText prints a credential unchanged — which is exactly why redaction exists', () => {
    // If this ever stops being true, it means someone made `safeText` redact,
    // and the two boundaries have been silently merged.
    expect(safeText(API_KEY_SENTINEL)).toContain(API_KEY_PREFIX);
    expect(safeText(SUPABASE_SECRET_SENTINEL)).toBe(SUPABASE_SECRET_SENTINEL);
  });

  it('redaction removes the credential but does nothing about markup', () => {
    expect(redactSecretText(API_KEY_SENTINEL)).toBe(REDACTED);
    expect(redactSecretText('<img src=x onerror=alert(1)>')).toBe('<img src=x onerror=alert(1)>');
  });

  it('the two together are what a durable string needs', () => {
    const hostile = `<b>${API_KEY_SENTINEL}</b>`;
    const shown = safeText(redactSecretText(hostile));
    expect(shown).not.toContain(API_KEY_PREFIX);
    expect(shown).not.toContain('<b>');
  });
});

describe('the inspector and the V1 output path stay redacted', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    apiMocks.api.projects.mockResolvedValue([{ ...PROJECT, name: 'Plain Project' }]);
    apiMocks.api.conversations.mockResolvedValue([{ ...CONVERSATION, title: 'Plain conversation' }]);
    apiMocks.api.events.mockResolvedValue([]);
    window.sessionStorage.clear();
  });

  async function openRunWith(run: Record<string, unknown>) {
    apiMocks.api.run.mockResolvedValue({ id: RUN_ID, conversation_id: CONVERSATION.id, ...run });
    window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION.id}`, RUN_ID);
    render(<Page/>);
    fireEvent.click(await screen.findByText('Plain Project'));
    fireEvent.click(await screen.findByText('Plain conversation'));
    await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN_ID));
  }

  it('the V1 sanitized-output panel redacts a credential in the durable payload', async () => {
    await openRunWith({
      status: 'completed',
      output: { summary: 'done', api_key: API_KEY_SENTINEL, nested: { authorization: `Bearer ${API_KEY_SENTINEL}` } },
    });
    const body = document.body.textContent ?? '';
    expect(body).toContain(REDACTED);
    expect(body).not.toContain(API_KEY_PREFIX);
  });

  it('the inspector Developer and Claims tabs redact what they serialize', async () => {
    apiMocks.api.events.mockResolvedValue([
      {
        id: 1, run_id: RUN_ID, event_type: 'claim_recorded', message: 'claim',
        payload: { id: 'claim-1', value: API_KEY_SENTINEL },
      },
      {
        // A REAL backend event type (backend/runtime.py EVENT_TYPES). The
        // reducer only projects types on that allowlist, so a made-up type
        // would prove nothing about the production path.
        id: 2, run_id: RUN_ID, event_type: 'agent_failed', message: 'bad', agent: 'researcher',
        payload: { detail: `authorization: Bearer ${API_KEY_SENTINEL}` },
      },
    ]);
    await openRunWith({ status: 'running' });
    await waitFor(() => expect(screen.getAllByText('claim_recorded').length).toBeGreaterThan(0));

    for (const tab of ['Claims', 'Developer']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }));
      const panel = document.getElementById('inspector-panel');
      expect(panel?.textContent, tab).not.toContain(API_KEY_PREFIX);
      expect(panel?.textContent, tab).toContain(REDACTED);
    }
  });
});
