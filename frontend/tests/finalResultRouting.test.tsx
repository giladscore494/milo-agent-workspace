/**
 * Which result surface the workspace mounts, and why.
 *
 * The decision comes from the RUN'S OWN IMMUTABLE IDENTITY — never from the
 * run's payload, and never from what the project's `workflow_key` says today.
 * These tests drive the real page so the routing is proven where it actually
 * lives, not in a re-implementation of it.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import fixtures from './fixtures/swarmV2FinalResult.json';
import { identityFor } from './fixtures/runIdentity';

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve({ access_token: 'fresh', user: { email: 'u@example.com' } })),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: true,
  api: {
    projects: vi.fn(), conversations: vi.fn(), createConversation: vi.fn(),
    createProposal: vi.fn(), proposal: vi.fn(), decideProposal: vi.fn(), reviseProposal: vi.fn(),
    startRun: vi.fn(), run: vi.fn(), runs: vi.fn(() => Promise.resolve([])), events: vi.fn(), cancel: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => 'routing-test-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) { super(message); }
  },
}));

const SWARM_PROJECT = { id: '11111111-1111-4111-8111-000000000001', slug: 'swarm', name: 'Swarm V2 Project', workflow_key: 'swarm_v2' };
const V1_PROJECT = { id: '22222222-1111-4111-8111-000000000002', slug: 'catalog', name: 'Vehicle Catalog V1', workflow_key: 'vehicle_catalog_v1' };
const CONVERSATION_ID = '33333333-1111-4111-8111-000000000003';
const RUN_ID = 'cccccccc-1111-4111-8111-000000000f04';

function conversationFor(project: { id: string }) {
  return { id: CONVERSATION_ID, project_id: project.id, title: 'Result' };
}

/** Select a project and open a conversation whose run is already terminal. */
async function openTerminalRun(project: typeof SWARM_PROJECT, output: unknown, status = 'completed') {
  apiMocks.api.projects.mockResolvedValue([project]);
  apiMocks.api.conversations.mockResolvedValue([conversationFor(project)]);
  apiMocks.api.run.mockResolvedValue({
    id: RUN_ID, conversation_id: CONVERSATION_ID, status, output, usage: null,
    // The run states the engine it WAS created as. The workspace reads the
    // surface from this, never from the project's workflow key.
    run_identity: identityFor(project.workflow_key, RUN_ID),
  });
  apiMocks.api.events.mockResolvedValue([]);
  // A stored active run is exactly what a browser refresh restores.
  window.sessionStorage.setItem(`milo.activeRun.${CONVERSATION_ID}`, RUN_ID);

  render(<Page/>);
  fireEvent.click(await screen.findByText(project.name));
  fireEvent.click(await screen.findByText('Result'));
  await waitFor(() => expect(apiMocks.api.run).toHaveBeenCalledWith(RUN_ID));
}

describe('result-surface routing', () => {
  beforeEach(() => {
    apiMocks.executionUi = true;
    for (const fn of Object.values(apiMocks.api)) fn.mockReset();
    window.sessionStorage.clear();
  });

  it('1. a Swarm V2 project gets the typed Final Result surface and NOT the raw output panel', async () => {
    await openTerminalRun(SWARM_PROJECT, fixtures.usable_result);
    expect(await screen.findByRole('heading', { name: 'Final result', level: 3 })).toBeInTheDocument();
    expect(await screen.findByText('Usable result')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Final artifacts' })).not.toBeInTheDocument();
    // The raw JSON dump F4 replaces must be gone for this workflow.
    expect(document.querySelector('.final-result pre')).toBeNull();
  });

  it('2. a V1 project gets the typed Vehicle Catalog surface and never the Swarm V2 one', async () => {
    await openTerminalRun(V1_PROJECT, {
      status: 'complete',
      summary: 'V1 mocked output',
      result: {
        manufacturer: 'Alpha', market: 'IL', period: '2020-2024', status: 'complete',
        models: [{ canonical_model_name: 'Alpha One', verification_status: 'verified', fuel_type: 'petrol' }],
        needs_review: [], rejected: [], failed_agents: [],
        pipeline_quality: { discovery: 'success', normalizer: 'success', technical_enrichment: 'success', verifier: 'success', final_builder: 'success', data_depth: 'full_technical' },
      },
    });
    expect(await screen.findByText('Vehicle Catalog V1 product result')).toBeInTheDocument();
    expect(screen.getByText(/V1 mocked output/)).toBeInTheDocument();
    expect(screen.getByText('Alpha One')).toBeInTheDocument();
    expect(screen.getByText('Models (1)')).toBeInTheDocument();
    expect(screen.queryByText('Swarm V2 product result')).not.toBeInTheDocument();
    // The raw-payload dump is gone from the product surface.
    expect(screen.queryByRole('heading', { name: 'Final artifacts' })).not.toBeInTheDocument();
  });

  it('3. workflow identity comes from the RUN — a V1 run carrying a V2 payload still uses V1', async () => {
    // The payload is a perfectly valid Swarm V2 product outcome. It must NOT
    // be able to promote a V1 project onto the V2 product surface, and the V1
    // surface refuses it because it is not a catalog document.
    await openTerminalRun(V1_PROJECT, fixtures.usable_result);
    expect(await screen.findByText('Vehicle Catalog V1 product result')).toBeInTheDocument();
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('V1_NOT_A_CATALOG_DOCUMENT')).toBeInTheDocument();
    expect(screen.queryByText('Swarm V2 product result')).not.toBeInTheDocument();
    expect(screen.queryByText('Usable result')).not.toBeInTheDocument();
  });

  it('4. the execution surface and the product surface are both present and stay separate', async () => {
    await openTerminalRun(SWARM_PROJECT, fixtures.partial_result, 'partial_success');
    const execution = await screen.findByRole('region', { name: 'Swarm run' });
    const product = await screen.findByRole('region', { name: 'Final result' });
    expect(execution).not.toBe(product);
    expect(execution.contains(product)).toBe(false);

    // Execution reports the durable terminal status; it states no product answer.
    expect(within(execution).getByText(/Run finished with status/)).toBeInTheDocument();
    expect(execution.textContent).not.toContain('Verified fields');
    // The product surface reports the answer; it states no telemetry.
    expect(within(product).getByText('Partial result')).toBeInTheDocument();
    expect(product.textContent).not.toContain('Usage and scale');
  });

  it('5. neither result surface renders while the execution UI flag is off', async () => {
    apiMocks.executionUi = false;
    apiMocks.api.projects.mockResolvedValue([SWARM_PROJECT]);
    apiMocks.api.conversations.mockResolvedValue([conversationFor(SWARM_PROJECT)]);
    render(<Page/>);
    fireEvent.click(await screen.findByText(SWARM_PROJECT.name));
    fireEvent.click(await screen.findByText('Result'));
    expect(screen.queryByRole('heading', { name: 'Final result' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Final artifacts' })).not.toBeInTheDocument();
    expect(apiMocks.api.run).not.toHaveBeenCalled();
  });

  it('6. refresh reconstructs the same result from the durable run output alone', async () => {
    await openTerminalRun(SWARM_PROJECT, fixtures.partial_result, 'partial_success');
    const before = (await screen.findByRole('region', { name: 'Final result' })).innerHTML;

    // Tear the page down completely — a browser refresh keeps only
    // sessionStorage — and mount it again against the same durable run.
    cleanup();
    render(<Page/>);
    fireEvent.click(await screen.findByText(SWARM_PROJECT.name));
    fireEvent.click(await screen.findByText('Result'));
    await waitFor(() => expect(screen.getByText('Partial result')).toBeInTheDocument());
    expect((await screen.findByRole('region', { name: 'Final result' })).innerHTML).toBe(before);
  });

  it('6b. a partial result with NO itemized rows also survives a refresh unchanged', async () => {
    await openTerminalRun(SWARM_PROJECT, fixtures.partial_result_no_review_items, 'partial_success');
    const before = (await screen.findByRole('region', { name: 'Final result' })).innerHTML;
    expect(before).toContain('contains no itemized entries');

    cleanup();
    render(<Page/>);
    fireEvent.click(await screen.findByText(SWARM_PROJECT.name));
    fireEvent.click(await screen.findByText('Result'));
    await waitFor(() => expect(screen.getByText('Partial result')).toBeInTheDocument());
    expect((await screen.findByRole('region', { name: 'Final result' })).innerHTML).toBe(before);
  });

  it('7. an invalid durable payload reaches the browser as unavailable, never as a success', async () => {
    await openTerminalRun(SWARM_PROJECT, { status: 'complete', result_kind: 'partial_result', fields: {}, needs_review: [] });
    expect(await screen.findByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('STATUS_CONTRADICTS_KIND')).toBeInTheDocument();
    expect(screen.queryByText('Usable result')).not.toBeInTheDocument();
    expect(screen.queryByText('Partial result')).not.toBeInTheDocument();
  });
});
