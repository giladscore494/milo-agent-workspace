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

import { GET, POST } from '@/app/api/gateway/[...path]/route';

const CONVERSATION_ID = '1f90f4ce-7844-4031-91d6-b74e40e1884e';
const PROPOSAL_ID = '11111111-2222-4333-8444-555555555555';

describe('private API gateway route', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    process.env.NEXT_PUBLIC_SUPABASE_URL = 'https://example.supabase.co';
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = 'anon';
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

  it('allows health without authentication', async () => {
    mocks.getCloudRunServiceUrl.mockReturnValue('https://cloudrun.example');
    mocks.getCloudRunIdToken.mockResolvedValue('google-id-token');
    global.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200, headers: { 'content-type': 'application/json' } }));
    const response = await GET(new NextRequest('https://x/api/gateway/health', { method: 'GET' }), { params: Promise.resolve({ path: ['health'] }) });
    expect(response.status).toBe(200);
    expect(mocks.getCloudRunIdToken).toHaveBeenCalled();
  });

  it('rejects unauthenticated project access without contacting Cloud Run', async () => {
    const response = await GET(new NextRequest('https://x/api/gateway/projects', { method: 'GET' }), { params: Promise.resolve({ path: ['projects'] }) });
    expect(response.status).toBe(401);
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
  });

  it('rejects invalid or expired Supabase token before Cloud Run', async () => {
    global.fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }));
    const response = await GET(new NextRequest('https://x/api/gateway/projects', { method: 'GET', headers: { authorization: 'Bearer bad' } }), { params: Promise.resolve({ path: ['projects'] }) });
    expect(response.status).toBe(401);
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
  });

  it('blocks non-allowlisted routes without contacting Cloud Run', async () => {
    const RUN_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
    const response = await POST(new NextRequest(`https://x/api/gateway/runs/${RUN_ID}/tool-grants`, { method: 'POST' }), { params: Promise.resolve({ path: ['runs', RUN_ID, 'tool-grants'] }) });
    expect(response.status).toBe(403);
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
    expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
  });

  it('requires authentication for run polling reads before contacting Cloud Run', async () => {
    const RUN_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
    const response = await GET(new NextRequest(`https://x/api/gateway/runs/${RUN_ID}`, { method: 'GET' }), { params: Promise.resolve({ path: ['runs', RUN_ID] }) });
    expect(response.status).toBe(401);
    expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
  });

  it('returns 429 with a Retry-After header when the unauthenticated IP limit is hit', async () => {
    process.env.GATEWAY_RATE_LIMIT_UNAUTH_REQUESTS = '1';
    const { resetRateLimiterForTests } = await import('@/lib/server/rateLimit');
    resetRateLimiterForTests();
    try {
      const make = () => GET(
        new NextRequest('https://x/api/gateway/health', { method: 'GET', headers: { 'x-forwarded-for': '198.51.100.77' } }),
        { params: Promise.resolve({ path: ['health'] }) },
      );
      mocks.getCloudRunServiceUrl.mockReturnValue('https://cloudrun.example');
      mocks.getCloudRunIdToken.mockResolvedValue('google-id-token');
      global.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: 'ok' }), { status: 200, headers: { 'content-type': 'application/json' } }));
      expect((await make()).status).toBe(200);
      const limited = await make();
      expect(limited.status).toBe(429);
      expect(Number(limited.headers.get('retry-after'))).toBeGreaterThanOrEqual(1);
    } finally {
      delete process.env.GATEWAY_RATE_LIMIT_UNAUTH_REQUESTS;
      resetRateLimiterForTests();
    }
  });

  it('derives identity from Supabase token and ignores browser identity headers', async () => {
    mocks.getCloudRunServiceUrl.mockReturnValue('https://cloudrun.example');
    mocks.getCloudRunIdToken.mockResolvedValue('google-id-token');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', email: 'user@example.com' }), { status: 200, headers: { 'content-type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200, headers: { 'content-type': 'application/json' } }));
    global.fetch = fetchMock;
    const response = await GET(new NextRequest('https://x/api/gateway/projects', { method: 'GET', headers: { authorization: 'Bearer good', 'x-milo-auth-user-id': 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb' } }), { params: Promise.resolve({ path: ['projects'] }) });
    expect(response.status).toBe(200);
    const upstreamHeaders = fetchMock.mock.calls[1][1].headers as Headers;
    expect(upstreamHeaders.get('x-milo-auth-user-id')).toBe('aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa');
    expect(upstreamHeaders.get('authorization')).toBe('Bearer google-id-token');
  });
});
