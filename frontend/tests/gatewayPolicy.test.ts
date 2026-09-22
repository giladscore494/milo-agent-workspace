import { describe, expect, it } from 'vitest';

import {
  executionRoutesEnabled,
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
    expect(isGatewayRequestAllowed('GET', `/runs/${RUN_ID}/export`)).toBe(true);
    // The export is a READ of one run: GET only, and nothing under it.
    expect(isGatewayRequestAllowed('POST', `/runs/${RUN_ID}/export`)).toBe(false);
    expect(isGatewayRequestAllowed('GET', `/runs/${RUN_ID}/export/anything`)).toBe(false);
    expect(isGatewayRequestAllowed('GET', `/runs/not-a-uuid/export`)).toBe(false);
    expect(
      isGatewayRequestAllowed('GET', `/projects/${PROJECT_ID}/conversations`),
    ).toBe(true);
  });

  /**
   * CODE-3 — the read-only catalog review routes.
   *
   * SAFE, not EXECUTION: they must answer while the execution stage is off,
   * because that is exactly the posture an operator inspects the catalog from
   * during a rollback.
   */
  describe('CODE-3 catalog review routes', () => {
    const CANONICAL = `/projects/${PROJECT_ID}/catalog/canonical`;
    const REVIEW = `/projects/${PROJECT_ID}/catalog/review-candidates`;

    it('allowlists exactly the two GET routes', () => {
      expect(isGatewayRequestAllowed('GET', CANONICAL)).toBe(true);
      expect(isGatewayRequestAllowed('GET', REVIEW)).toBe(true);
    });

    it('allows them while execution routes stay disabled', () => {
      // No GATEWAY_ALLOW_EXECUTION_ROUTES is set in this suite, so this is the
      // default deployed posture — and the reads still go through.
      expect(executionRoutesEnabled()).toBe(false);
      expect(isGatewayRequestAllowed('GET', CANONICAL)).toBe(true);
      expect(isGatewayRequestAllowed('GET', REVIEW)).toBe(true);
      // ...while an execution route is still refused in the same breath.
      expect(isGatewayRequestAllowed('POST', `/runs/${RUN_ID}/cancel`)).toBe(false);
    });

    it.each(['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])(
      'blocks the %s counterpart of both paths', (method) => {
        expect(isGatewayRequestAllowed(method, CANONICAL)).toBe(false);
        expect(isGatewayRequestAllowed(method, REVIEW)).toBe(false);
      });

    it('is not a generic catalog proxy', () => {
      // A path the two rules do not name is refused, including a plausible
      // mutating one a later release might add.
      for (const path of [
        `/projects/${PROJECT_ID}/catalog`,
        `/projects/${PROJECT_ID}/catalog/`,
        `/projects/${PROJECT_ID}/catalog/promote`,
        `/projects/${PROJECT_ID}/catalog/canonical/promote`,
        `/projects/${PROJECT_ID}/catalog/review-candidates/approve`,
        `/projects/${PROJECT_ID}/catalog/candidates`,
        '/catalog/canonical',
      ]) {
        expect(isGatewayRequestAllowed('GET', path)).toBe(false);
        expect(isGatewayRequestAllowed('POST', path)).toBe(false);
      }
    });

    it('requires a well-formed project id', () => {
      expect(isGatewayRequestAllowed('GET', '/projects/not-a-uuid/catalog/canonical'))
        .toBe(false);
      expect(isGatewayRequestAllowed('GET', '/projects/../catalog/canonical')).toBe(false);
    });

    it('does not treat a catalog read as run creation', () => {
      expect(isRunCreationRequest('GET', CANONICAL)).toBe(false);
      expect(isRunCreationRequest('POST', REVIEW)).toBe(false);
    });
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
