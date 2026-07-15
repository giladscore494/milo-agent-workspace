import { describe, expect, it } from 'vitest';

import {
  isGatewayRequestAllowed,
  isRunCreationRequest,
} from '@/lib/server/gatewayPolicy';

const PROJECT_ID = '677db6c2-b44c-41c1-b4e1-b51229d697df';
const CONVERSATION_ID = '1f90f4ce-7844-4031-91d6-b74e40e1884e';
const PROPOSAL_ID = '11111111-2222-4333-8444-555555555555';
const RUN_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';

describe('gateway policy', () => {
  it('allows only the initial safe API routes', () => {
    expect(isGatewayRequestAllowed('GET', '/health')).toBe(true);
    expect(isGatewayRequestAllowed('GET', '/projects')).toBe(true);
    expect(
      isGatewayRequestAllowed('GET', `/projects/${PROJECT_ID}`),
    ).toBe(true);
    expect(
      isGatewayRequestAllowed(
        'POST',
        `/projects/${PROJECT_ID}/conversations`,
      ),
    ).toBe(true);
    expect(
      isGatewayRequestAllowed(
        'GET',
        `/conversations/${CONVERSATION_ID}`,
      ),
    ).toBe(true);
  });

  it('rejects malformed identifiers and unsupported methods', () => {
    expect(isGatewayRequestAllowed('POST', '/health')).toBe(false);
    expect(isGatewayRequestAllowed('GET', '/projects/not-a-uuid')).toBe(false);
    expect(
      isGatewayRequestAllowed(
        'DELETE',
        `/conversations/${CONVERSATION_ID}`,
      ),
    ).toBe(false);
  });

  it('blocks both run creation endpoints', () => {
    const conversationRunPath = `/conversations/${CONVERSATION_ID}/runs`;
    const proposalRunPath = `/workflow-proposals/${PROPOSAL_ID}/runs`;

    expect(isRunCreationRequest('POST', conversationRunPath)).toBe(true);
    expect(isRunCreationRequest('POST', proposalRunPath)).toBe(true);

    expect(isGatewayRequestAllowed('POST', conversationRunPath)).toBe(false);
    expect(isGatewayRequestAllowed('POST', proposalRunPath)).toBe(false);
  });

  it('allows the authorized run read endpoints needed for polling', () => {
    expect(isGatewayRequestAllowed('GET', `/runs/${RUN_ID}`)).toBe(true);
    expect(isGatewayRequestAllowed('GET', `/runs/${RUN_ID}/events`)).toBe(true);
    expect(
      isGatewayRequestAllowed('GET', `/projects/${PROJECT_ID}/conversations`),
    ).toBe(true);
  });

  it('blocks execution and internal worker routes by default', () => {
    expect(
      isGatewayRequestAllowed('POST', `/runs/${RUN_ID}/cancel`),
    ).toBe(false);
    expect(
      isGatewayRequestAllowed(
        'POST',
        `/runs/${RUN_ID}/tool-access-requests`,
      ),
    ).toBe(false);
    expect(isGatewayRequestAllowed('POST', '/workflow-proposals')).toBe(false);
    expect(
      isGatewayRequestAllowed('POST', `/internal/runs/${RUN_ID}/events`),
    ).toBe(false);
  });

  it('opens execution routes only under the explicit server flag', () => {
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    try {
      expect(
        isGatewayRequestAllowed('POST', `/conversations/${CONVERSATION_ID}/runs`),
      ).toBe(true);
      expect(isGatewayRequestAllowed('POST', '/workflow-proposals')).toBe(true);
      expect(
        isGatewayRequestAllowed('POST', `/workflow-proposals/${PROPOSAL_ID}/approve`),
      ).toBe(true);
      expect(
        isGatewayRequestAllowed('POST', `/runs/${RUN_ID}/cancel`),
      ).toBe(true);
      expect(isRunCreationRequest('POST', `/conversations/${CONVERSATION_ID}/runs`)).toBe(false);
      // Worker routes stay blocked even with the flag on.
      expect(
        isGatewayRequestAllowed('POST', `/runs/${RUN_ID}/tool-grants`),
      ).toBe(false);
      expect(
        isGatewayRequestAllowed('POST', `/internal/runs/${RUN_ID}/complete`),
      ).toBe(false);
    } finally {
      delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    }
  });
});
