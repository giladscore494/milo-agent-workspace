import { NextRequest } from 'next/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  getCloudRunIdToken: vi.fn(),
  getCloudRunServiceUrl: vi.fn(),
}));

vi.mock('@/lib/server/cloudRunAuth', () => ({
  getCloudRunIdToken: mocks.getCloudRunIdToken,
  getCloudRunServiceUrl: mocks.getCloudRunServiceUrl,
}));

import { POST } from '@/app/api/gateway/[...path]/route';

const CONVERSATION_ID = '1f90f4ce-7844-4031-91d6-b74e40e1884e';
const PROPOSAL_ID = '11111111-2222-4333-8444-555555555555';

describe('private API gateway route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('blocks conversation run creation before authentication', async () => {
    const request = new NextRequest(
      `https://milo-agent-workspace.vercel.app/api/gateway/conversations/${CONVERSATION_ID}/runs`,
      { method: 'POST' },
    );

    const response = await POST(request, {
      params: Promise.resolve({
        path: ['conversations', CONVERSATION_ID, 'runs'],
      }),
    });

    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({
      error: 'Run creation is disabled by the gateway safety policy.',
    });
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
    expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
  });

  it('blocks workflow-proposal run creation before authentication', async () => {
    const request = new NextRequest(
      `https://milo-agent-workspace.vercel.app/api/gateway/workflow-proposals/${PROPOSAL_ID}/runs`,
      { method: 'POST' },
    );

    const response = await POST(request, {
      params: Promise.resolve({
        path: ['workflow-proposals', PROPOSAL_ID, 'runs'],
      }),
    });

    expect(response.status).toBe(403);
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
    expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
  });
});
