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

  it.each([
    ['conversations', CONVERSATION_ID, 'runs'],
    ['workflow-proposals', PROPOSAL_ID, 'runs'],
    ['work-scopes', CONVERSATION_ID, 'runs'],
  ])('refuses a run start while plan authoring is open (%s), before authentication', async (...path) => {
    // Stage P, and every moment while the backend is being armed: the
    // execution routes are open for plan writes, the run-start flag is not.
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    delete process.env.GATEWAY_ALLOW_RUN_START_ROUTES;
    try {
      const request = new NextRequest(
        `https://milo-agent-workspace.vercel.app/api/gateway/${path.join('/')}`,
        { method: 'POST', headers: { authorization: 'Bearer any-user-token' } },
      );
      const response = await POST(request, { params: Promise.resolve({ path }) });
      expect(response.status).toBe(403);
      expect(await response.json()).toEqual({
        error: 'Run creation is disabled by the gateway safety policy.',
      });
      expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
      expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
    } finally {
      delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    }
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

  /**
   * CODE-3 — the catalog review reads, at the gateway boundary.
   *
   * Every one of these must hold with no execution flag set, which is the
   * default posture in this suite.
   */
  describe('CODE-3 catalog review reads', () => {
    const PROJECT_ID = '677db6c2-b44c-41c1-b4e1-b51229d697df';
    const SEGMENTS = {
      canonical: ['projects', PROJECT_ID, 'catalog', 'canonical'],
      review: ['projects', PROJECT_ID, 'catalog', 'review-candidates'],
    };

    function proxied(path: string[], query = '') {
      return GET(
        new NextRequest(`https://x/api/gateway/${path.join('/')}${query}`, {
          method: 'GET',
          headers: { authorization: 'Bearer good' },
        }),
        { params: Promise.resolve({ path }) },
      );
    }

    it.each(Object.entries(SEGMENTS))(
      'rejects the %s read with no Supabase token, without contacting Cloud Run',
      async (_name, path) => {
        const response = await GET(
          new NextRequest(`https://x/api/gateway/${path.join('/')}`, { method: 'GET' }),
          { params: Promise.resolve({ path }) },
        );
        expect(response.status).toBe(401);
        expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
        expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
      });

    it.each(Object.entries(SEGMENTS))(
      'rejects the %s read with an invalid Supabase token', async (_name, path) => {
        global.fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 401 }));
        const response = await proxied(path);
        expect(response.status).toBe(401);
        expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
      });

    it('never lets a browser header decide which user the backend sees', async () => {
      mocks.getCloudRunServiceUrl.mockReturnValue('https://cloudrun.example');
      mocks.getCloudRunIdToken.mockResolvedValue('google-id-token');
      const fetchMock = vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa', email: 'user@example.com' }), { status: 200, headers: { 'content-type': 'application/json' } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ page: {}, items: [] }), { status: 200, headers: { 'content-type': 'application/json' } }));
      global.fetch = fetchMock;
      const response = await GET(
        new NextRequest(`https://x/api/gateway/${SEGMENTS.canonical.join('/')}`, {
          method: 'GET',
          headers: {
            authorization: 'Bearer good',
            // A browser trying to inspect the catalog AS SOMEONE ELSE.
            'x-milo-auth-user-id': 'bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb',
            'x-milo-auth-user-email': 'victim@example.com',
            'x-milo-gateway-token': 'forged',
          },
        }),
        { params: Promise.resolve({ path: SEGMENTS.canonical }) },
      );
      expect(response.status).toBe(200);
      const upstream = fetchMock.mock.calls[1][1].headers as Headers;
      // The identity upstream sees is the one the Supabase token proved.
      expect(upstream.get('x-milo-auth-user-id')).toBe('aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa');
      expect(upstream.get('x-milo-auth-user-email')).toBe('user@example.com');
      expect(upstream.get('x-milo-gateway-token')).toBe('google-id-token');
    });

    it('forwards the bounded pagination query to the backend unchanged', async () => {
      mocks.getCloudRunServiceUrl.mockReturnValue('https://cloudrun.example');
      mocks.getCloudRunIdToken.mockResolvedValue('google-id-token');
      const fetchMock = vi.fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa' }), { status: 200, headers: { 'content-type': 'application/json' } }))
        .mockResolvedValueOnce(new Response(JSON.stringify({ available: false }), { status: 200, headers: { 'content-type': 'application/json' } }));
      global.fetch = fetchMock;
      await proxied(SEGMENTS.review, '?limit=25&offset=50');
      const target = fetchMock.mock.calls[1][0] as URL;
      expect(target.pathname).toBe(`/projects/${PROJECT_ID}/catalog/review-candidates`);
      expect(target.searchParams.get('limit')).toBe('25');
      expect(target.searchParams.get('offset')).toBe('50');
      // The backend owns the bound; the gateway neither raises nor rewrites it.
      expect(fetchMock.mock.calls[1][1].method).toBe('GET');
      expect(fetchMock.mock.calls[1][1].body).toBeUndefined();
    });

    it.each(Object.entries(SEGMENTS))(
      'refuses a POST to the %s path even with a valid session', async (_name, path) => {
        const response = await POST(
          new NextRequest(`https://x/api/gateway/${path.join('/')}`, {
            method: 'POST',
            headers: { authorization: 'Bearer good' },
          }),
          { params: Promise.resolve({ path }) },
        );
        expect(response.status).toBe(403);
        expect(await response.json()).toEqual({
          error: 'This API route is not allowed by the gateway policy.',
        });
        expect(mocks.getCloudRunIdToken).not.toHaveBeenCalled();
        expect(mocks.getCloudRunServiceUrl).not.toHaveBeenCalled();
      });

    it('exports no handler for PUT, PATCH or DELETE at all', async () => {
      const route = await import('@/app/api/gateway/[...path]/route');
      // Next.js answers 405 for a method with no export, so the mutating verbs
      // are structurally unreachable rather than refused by a rule.
      for (const method of ['PUT', 'PATCH', 'DELETE']) {
        expect(method in route).toBe(false);
      }
      expect(Object.keys(route).sort()).toEqual(['GET', 'POST', 'dynamic', 'runtime']);
    });
  });
});
