/**
 * PR-D3: manufacturer normalisation on the Register page.
 *
 * What is asserted: the view is parsed STRICTLY (a group with two
 * provenances, or none, is unreadable); canonical names show with their exact
 * sources; high-confidence non-conflicting groups are approved together in
 * ONE call and any other group alone, each sent back exactly as proposed;
 * the button calls the one model request; and the gateway proxies the read as
 * a read and the two writes as execution routes that never start a run.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NormalisationClient, NormalisationSection } from '../components/register/NormalisationSection';
import { parseManufacturer } from '../lib/catalogBrowser';
import { approvalGroups, parseNormalisation } from '../lib/normalisation';
import { isGatewayRequestAllowed, isRunCreationRequest } from '../lib/server/gatewayPolicy';

const PROJECT = '00000000-0000-4000-8000-000000000001';
const CONVERSATION = '00000000-0000-4000-8000-000000000002';
const PROPOSAL = '00000000-0000-4000-8000-000000000003';
const MERCEDES = 'מרצדס בנץ';
const MERCEDES_DASH = 'מרצדס-בנץ';

function view(overrides: Record<string, unknown> = {}) {
  return {
    version: 2, unmapped: 5,
    canonical: [{ canonical: 'Toyota', sources: ['טויוטה', 'טויוטה יפן'] }],
    pending: [
      { canonical: MERCEDES, members: [MERCEDES_DASH, MERCEDES], confidence: 'high', rule_id: 'R1_SPELLING',
        reason: 'the same name up to case, whitespace and punctuation', conflicting: false, bulk_approvable: true },
      { canonical: 'Honda', members: ['הונדה'], confidence: 'high', proposal_id: PROPOSAL, reason: 'x',
        conflicting: false, bulk_approvable: true },
      { canonical: 'Lexus', members: ['לקסוס'], confidence: 'low', proposal_id: PROPOSAL, reason: 'y',
        conflicting: false, bulk_approvable: false },
    ],
    proposal: { id: PROPOSAL, status: 'proposed', reason_code: null },
    can_normalise: true, can_approve: true,
    ...overrides,
  };
}

function fakeClient(body: unknown = view()) {
  return {
    read: vi.fn(async () => body),
    request: vi.fn(async () => ({ started: true })),
    approve: vi.fn(async () => ({ version: 3, entry_count: 4 })),
  } satisfies NormalisationClient;
}

describe('the view is parsed strictly', () => {
  it('reads a valid view and refuses a group with no single provenance', () => {
    const parsed = parseNormalisation(view());
    expect(parsed?.pending.map((g) => g.ruleId ?? g.proposalId)).toEqual(['R1_SPELLING', PROPOSAL, PROPOSAL]);
    const both = view({ pending: [{ ...view().pending[0], proposal_id: PROPOSAL }] });
    expect(parseNormalisation(both)).toBeUndefined();
    const none = view({ pending: [{ ...view().pending[1], proposal_id: undefined }] });
    expect(parseNormalisation(none)).toBeUndefined();
    expect(parseNormalisation({ ...view(), version: -1 })).toBeUndefined();
  });

  it('keeps the exact source names in an approval', () => {
    const parsed = parseNormalisation(view())!;
    expect(approvalGroups(parsed.pending.slice(0, 2))).toEqual([
      { canonical: MERCEDES, members: [MERCEDES_DASH, MERCEDES], rule_id: 'R1_SPELLING' },
      { canonical: 'Honda', members: ['הונדה'], proposal_id: PROPOSAL },
    ]);
  });

  it('reads the canonical name beside the exact tozar in the browser', () => {
    expect(parseManufacturer({ tozar: 'טויוטה', variants: 3, canonical_manufacturer: 'Toyota' }))
      .toEqual({ tozar: 'טויוטה', variants: 3, canonical: 'Toyota' });
    expect(parseManufacturer({ tozar: 'טויוטה', variants: 3, canonical_manufacturer: null }))
      .toEqual({ tozar: 'טויוטה', variants: 3 });
  });
});

describe('the approval screen', () => {
  it('approves high-confidence groups together and a low one alone', async () => {
    const client = fakeClient();
    render(<NormalisationSection projectId={PROJECT} conversationId={CONVERSATION} client={client} />);
    expect(await screen.findByText('Toyota')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Approve 2 high-confidence groups' }));
    await waitFor(() => expect(client.approve).toHaveBeenCalledTimes(1));
    expect(client.approve).toHaveBeenCalledWith(PROJECT, 2, [
      { canonical: MERCEDES, members: [MERCEDES_DASH, MERCEDES], rule_id: 'R1_SPELLING' },
      { canonical: 'Honda', members: ['הונדה'], proposal_id: PROPOSAL },
    ]);
    const alone = await screen.findAllByRole('button', { name: 'Approve' });
    expect(alone).toHaveLength(1);
    fireEvent.click(alone[0]);
    await waitFor(() => expect(client.approve).toHaveBeenCalledTimes(2));
    expect(client.approve).toHaveBeenLastCalledWith(PROJECT, 2, [
      { canonical: 'Lexus', members: ['לקסוס'], proposal_id: PROPOSAL }]);
  });

  it('starts the one model call, and offers no approval to a non-owner', async () => {
    const client = fakeClient(view({ can_approve: false }));
    render(<NormalisationSection projectId={PROJECT} conversationId={CONVERSATION} client={client} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Normalise manufacturers' }));
    await waitFor(() => expect(client.request).toHaveBeenCalledWith(PROJECT, CONVERSATION));
    expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull();
  });
});

describe('the gateway', () => {
  const read = `/projects/${PROJECT}/register/normalisation`;
  const writes = [read, `${read}/approvals`];
  afterEach(() => {
    delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
  });

  it('proxies the read in every posture, and the writes only as execution routes', () => {
    expect(isGatewayRequestAllowed('GET', read)).toBe(true);
    for (const path of writes) {
      expect(isGatewayRequestAllowed('POST', path)).toBe(false);
      expect(isRunCreationRequest('POST', path)).toBe(false);
    }
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    for (const path of writes) {
      expect(isGatewayRequestAllowed('POST', path)).toBe(true);
      expect(isRunCreationRequest('POST', path)).toBe(false);
    }
    expect(isGatewayRequestAllowed('GET', `${read}/approvals`)).toBe(false);
  });
});
