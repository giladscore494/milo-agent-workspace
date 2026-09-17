/**
 * CODE-3 — what the API client actually puts on the wire.
 *
 * Asserted against the `fetch` call itself rather than against the client's
 * shape: "the method is GET" is a property of the request, and a test that only
 * read the source would keep passing if a later edit added a body.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/supabaseClient', () => ({
  getCurrentAccessToken: vi.fn(() => Promise.resolve('access-token')),
}));

import { api } from '../lib/api';

const PROJECT = '677db6c2-b44c-41c1-b4e1-b51229d697df';

function ok(body: unknown = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

describe('the CODE-3 API client', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    // A fresh `Response` per call: a body can only be read once, so a single
    // shared resolved value would fail the second request in a test.
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(ok()));
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it('sends a GET with no body for the canonical page', async () => {
    await api.catalogCanonical(PROJECT);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/gateway/projects/${PROJECT}/catalog/canonical`);
    // `undefined` method means GET for `fetch`, and there is no body either way.
    expect(init?.method ?? 'GET').toBe('GET');
    expect(init?.body).toBeUndefined();
  });

  it('sends a GET with no body for the review page', async () => {
    await api.catalogReviewCandidates(PROJECT);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/gateway/projects/${PROJECT}/catalog/review-candidates`);
    expect(init?.method ?? 'GET').toBe('GET');
    expect(init?.body).toBeUndefined();
  });

  it('never issues a mutating request for either surface', async () => {
    await api.catalogCanonical(PROJECT, { limit: 25, offset: 50 });
    await api.catalogReviewCandidates(PROJECT, { limit: 25, offset: 50 });
    for (const [, init] of fetchMock.mock.calls) {
      expect(['GET', undefined]).toContain(init?.method);
    }
  });

  it('exposes no mutating catalog method at all', () => {
    // The whole client surface, against every mutation CODE-3 forbids.
    const names = Object.keys(api);
    for (const forbidden of ['promote', 'approve', 'reject', 'activate', 'capture',
                             'deactivate', 'editCatalog', 'deleteCatalog',
                             'catalogPromote', 'catalogApprove']) {
      expect(names).not.toContain(forbidden);
    }
    const catalogMethods = names.filter((name) => name.toLowerCase().includes('catalog'));
    expect(catalogMethods.sort()).toEqual(['catalogCanonical', 'catalogReviewCandidates']);
  });

  it('builds the query string from a closed set of parameter names', async () => {
    await api.catalogCanonical(PROJECT, {
      limit: 10,
      offset: 20,
      manufacturer: 'Toyota',
      commercialModel: 'RAV4',
      modelYear: 2022,
      canonicalKey: 'cv1.' + 'a'.repeat(32),
    });
    const url = new URL(fetchMock.mock.calls[0][0] as string, 'https://milo.test');
    expect([...url.searchParams.keys()].sort()).toEqual([
      'canonical_key', 'commercial_model', 'limit', 'manufacturer', 'model_year', 'offset',
    ]);
    expect(url.searchParams.get('model_year')).toBe('2022');
  });

  it('cannot be made to send an ordering, a column, a table or a status', async () => {
    // A caller reaching for query control has nowhere to put it: the params
    // type names six fields and `catalogQuery` reads only those.
    await api.catalogCanonical(
      PROJECT,
      // @ts-expect-error — the params type rejects every one of these, and the
      // runtime drops them: `catalogQuery` reads six names and nothing else.
      { order: 'manufacturer.desc', select: '*', status: 'promoted', table: 'runs' },
    );
    const url = new URL(fetchMock.mock.calls[0][0] as string, 'https://milo.test');
    expect([...url.searchParams.keys()]).toEqual([]);
  });

  it('sends no parameter at all for an unset filter', async () => {
    // An empty filter value is refused by the server, so the client must not
    // turn "no filter" into `?manufacturer=`.
    await api.catalogCanonical(PROJECT, { manufacturer: '', commercialModel: undefined });
    expect(fetchMock.mock.calls[0][0])
      .toBe(`/api/gateway/projects/${PROJECT}/catalog/canonical`);
  });

  it('encodes a filter value rather than letting it change the query shape', async () => {
    await api.catalogCanonical(PROJECT, { manufacturer: 'a&limit=9999&b=' });
    const url = new URL(fetchMock.mock.calls[0][0] as string, 'https://milo.test');
    expect(url.searchParams.get('manufacturer')).toBe('a&limit=9999&b=');
    expect(url.searchParams.get('limit')).toBeNull();
  });

  it('carries the Supabase bearer token and targets the gateway, never the backend', async () => {
    await api.catalogReviewCandidates(PROJECT);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url).startsWith('/api/gateway/')).toBe(true);
    expect((init?.headers as Record<string, string>).authorization).toBe('Bearer access-token');
  });
});
