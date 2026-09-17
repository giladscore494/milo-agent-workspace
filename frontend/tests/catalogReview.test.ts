/**
 * CODE-3 — the closed parser between a catalog response and what is rendered.
 *
 * What is asserted here is that a response CANNOT put something on the screen.
 * The backend already projects and validates; this is the second boundary, and
 * the tests below drive it with the payloads a compromised, corrupted or simply
 * newer server could send.
 */

import { describe, expect, it } from 'vitest';
import {
  CATALOG_DIMENSION_LABELS,
  CATALOG_PAGE_SIZE,
  CATALOG_UNAVAILABLE_LABELS,
  MAX_CATALOG_PAGE_SIZE,
  MAX_CATALOG_TEXT_CHARS,
  REVIEW_STATUS,
  UNKNOWN_CATALOG_UNAVAILABLE_LABEL,
  catalogUnavailableLabel,
  parseCanonicalPage,
  parseReviewPage,
} from '../lib/catalogReview';
import { ALL_SECRET_SENTINELS } from './secretSentinels';

const REQUESTED = { limit: CATALOG_PAGE_SIZE, offset: 0 };

function canonicalItem(extra: Record<string, unknown> = {}) {
  return {
    canonical_key: 'cv1.acaa1a82665c383af059fcee98841783',
    model_canonical_key: 'cm1.2961c45222fe069fc7dcc36c96621725',
    manufacturer: 'טויוטה',
    commercial_model: 'RAV4',
    model_year_start: 2022,
    model_year_end: 2022,
    official_model_code: 'AXAA54L-ANZVB',
    trim: 'ADVENTURE',
    identity_dimensions: { fuel_type: 'petrol' },
    promoted_at: '2026-09-17T01:00:00+00:00',
    revised_at: '2026-09-17T01:00:00+00:00',
    ...extra,
  };
}

function reviewItem(extra: Record<string, unknown> = {}) {
  return {
    candidate_key: 'cc1.a2c54473fac31d1d2c05489f6a74fcbf',
    status: REVIEW_STATUS,
    manufacturer: 'טויוטה',
    commercial_model: 'RAV4',
    model_year_start: 2022,
    model_year_end: 2022,
    official_model_code: 'AXAA54L-ANZVB',
    trim: 'ADVENTURE',
    identity_dimensions: { body_style: 'suv' },
    ...extra,
  };
}

function reviewBody(extra: Record<string, unknown> = {}) {
  return {
    available: true,
    unavailable_reason: null,
    status: REVIEW_STATUS,
    snapshot: {
      snapshot_key: 'cs1.60c62e4edf73492580680f1ddf5559dc',
      resource_id: '142afde2-6228-49f9-8a29-9b6c3a0cbe40',
      package_id: 'degem-rechev-wltp',
      publisher: 'ministry_of_transport',
      dataset_title: 'WLTP',
      dataset_market_scope: 'IL',
      upstream_version: '2026-09-14T02:41:31',
      upstream_version_kind: 'dataset_version',
      activated_at: '2026-09-17T01:00:00+00:00',
      declared_record_count: 233,
      stored_record_count: 233,
      normalization_contract: 'gov.wltp.normalize.1',
      normalization_issue_count: 0,
    },
    page: { limit: 25, offset: 0, total: 1, has_more: false },
    items: [reviewItem()],
    ...extra,
  };
}

describe('canonical page parsing', () => {
  it('reads a well-formed page', () => {
    const page = parseCanonicalPage(
      { page: { limit: 25, offset: 0, total: 3, has_more: true }, items: [canonicalItem()] },
      REQUESTED,
    );
    expect(page.page).toEqual({ limit: 25, offset: 0, total: 3, hasMore: true });
    expect(page.items).toHaveLength(1);
    expect(page.items[0].manufacturer).toBe('טויוטה');
    expect(page.items[0].identityDimensions).toEqual({ fuel_type: 'petrol' });
  });

  it('never copies a key the contract does not name', () => {
    const page = parseCanonicalPage(
      {
        page: { limit: 25, offset: 0, total: 1, has_more: false },
        items: [
          canonicalItem({
            variant_id: '11111111-1111-4111-8111-111111111111',
            field_revisions: { trim: 3 },
            lease_token: 'lease-token-value',
            payload: { tozar: 'raw register row' },
            an_unreviewed_column: 'value',
          }),
        ],
      },
      REQUESTED,
    );
    // The rendered item is exactly the contract's keys, and nothing else.
    expect(Object.keys(page.items[0]).sort()).toEqual([
      'canonicalKey', 'commercialModel', 'identityDimensions', 'manufacturer',
      'modelCanonicalKey', 'modelYearEnd', 'modelYearStart', 'officialModelCode',
      'promotedAt', 'revisedAt', 'trim',
    ]);
    const serialized = JSON.stringify(page);
    for (const leaked of ['variant_id', 'field_revisions', 'lease_token', 'tozar',
                          'an_unreviewed_column', 'raw register row']) {
      expect(serialized).not.toContain(leaked);
    }
  });

  it('drops an item with no canonical key rather than rendering an anonymous row', () => {
    const page = parseCanonicalPage(
      { page: {}, items: [canonicalItem({ canonical_key: null }), canonicalItem()] },
      REQUESTED,
    );
    expect(page.items).toHaveLength(1);
  });

  it.each([
    ['2022', 'a string year'],
    [true, 'a boolean'],
    [20.22, 'a fraction'],
    [null, 'a null'],
  ])('reports a malformed model year as absent, not zero (%s)', (value, _label) => {
    const page = parseCanonicalPage(
      { page: {}, items: [canonicalItem({ model_year_start: value })] },
      REQUESTED,
    );
    expect(page.items[0].modelYearStart).toBeUndefined();
  });

  it.each([
    ['', 'an empty string'],
    ['   ', 'whitespace'],
    [42, 'a number'],
    [{ a: 1 }, 'an object'],
  ])('reports unusable text as absent, not an empty string (%s)', (value, _label) => {
    const page = parseCanonicalPage(
      { page: {}, items: [canonicalItem({ trim: value })] },
      REQUESTED,
    );
    expect(page.items[0].trim).toBeUndefined();
  });

  it('bounds every stored string', () => {
    const page = parseCanonicalPage(
      { page: {}, items: [canonicalItem({ manufacturer: 'x'.repeat(10_000) })] },
      REQUESTED,
    );
    expect(page.items[0].manufacturer).toHaveLength(MAX_CATALOG_TEXT_CHARS);
  });

  it('bounds the number of items it will lay out', () => {
    const page = parseCanonicalPage(
      {
        page: { limit: 25, offset: 0, total: 5000, has_more: true },
        items: Array.from({ length: 5_000 }, (_unused, index) =>
          canonicalItem({ canonical_key: `cv1.${String(index).padStart(32, '0')}` })),
      },
      REQUESTED,
    );
    expect(page.items.length).toBeLessThanOrEqual(MAX_CATALOG_PAGE_SIZE);
  });

  it.each([
    [{ not_a_dimension: 'x' }, 'an unknown dimension name'],
    [{ body_style: { nested: true } }, 'a nested object'],
    ['a string', 'a string'],
    [['a', 'list'], 'a list'],
    [null, 'a null'],
  ])('drops an unreadable identity dimension (%s)', (dimensions, _label) => {
    const page = parseCanonicalPage(
      { page: {}, items: [canonicalItem({ identity_dimensions: dimensions })] },
      REQUESTED,
    );
    expect(page.items[0].identityDimensions).toEqual({});
  });

  it('keeps an absent total as unknown rather than turning it into zero', () => {
    const page = parseCanonicalPage(
      { page: { limit: 25, offset: 0 }, items: [] },
      REQUESTED,
    );
    // Unknown, NOT "the catalog is empty".
    expect(page.page.total).toBeNull();
    expect(page.page.hasMore).toBeNull();
  });

  it('distinguishes a real zero total from an unknown one', () => {
    const page = parseCanonicalPage(
      { page: { limit: 25, offset: 0, total: 0, has_more: false }, items: [] },
      REQUESTED,
    );
    expect(page.page.total).toBe(0);
    expect(page.page.hasMore).toBe(false);
  });

  it('never infers hasMore from the item count', () => {
    const page = parseCanonicalPage(
      {
        page: { limit: 2, offset: 0, total: 9 },
        items: [canonicalItem(), canonicalItem({ canonical_key: 'cv1.' + '1'.repeat(32) })],
      },
      REQUESTED,
    );
    // A full page and a total of 9 would both suggest "yes". The server did not
    // say, so neither does the page.
    expect(page.page.hasMore).toBeNull();
  });

  it('survives a response that is not an object at all', () => {
    for (const body of [null, undefined, 'text', 42, []]) {
      const page = parseCanonicalPage(body, REQUESTED);
      expect(page.items).toEqual([]);
      expect(page.page.limit).toBe(CATALOG_PAGE_SIZE);
    }
  });

  it('redacts a credential that somehow reached a durable string', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const page = parseCanonicalPage(
        { page: {}, items: [canonicalItem({ trim: `trim ${secret}` })] },
        REQUESTED,
      );
      expect(page.items[0].trim ?? '').not.toContain(secret);
    }
  });
});

describe('review page parsing', () => {
  it('reads a well-formed available page', () => {
    const page = parseReviewPage(reviewBody(), REQUESTED);
    expect(page.available).toBe(true);
    expect(page.unavailableReason).toBeUndefined();
    expect(page.snapshot?.snapshotKey).toBe('cs1.60c62e4edf73492580680f1ddf5559dc');
    expect(page.items).toHaveLength(1);
    expect(page.items[0].status).toBe(REVIEW_STATUS);
  });

  it('refuses to render a candidate under any other status', () => {
    for (const status of ['candidate', 'ambiguous', 'rejected', 'promoted', '']) {
      const page = parseReviewPage(
        reviewBody({ items: [reviewItem({ status })] }),
        REQUESTED,
      );
      expect(page.items).toEqual([]);
    }
  });

  it('states an unavailable page without claiming an empty catalog', () => {
    const page = parseReviewPage(
      {
        available: false,
        unavailable_reason: 'no_active_snapshot',
        status: REVIEW_STATUS,
        snapshot: null,
        page: { limit: 25, offset: 0, total: null, has_more: null },
        items: [],
      },
      REQUESTED,
    );
    expect(page.available).toBe(false);
    expect(page.unavailableReason).toBe('no_active_snapshot');
    expect(page.snapshot).toBeUndefined();
    expect(page.items).toEqual([]);
    // NOT zero. Zero would be the claim that a snapshot was read and held none.
    expect(page.page.total).toBeNull();
  });

  it('shows no rows and no snapshot for an unavailable page, whatever it carries', () => {
    // A server that sent items beside `available: false` is contradicting
    // itself; the safe reading is the one that claims less.
    const page = parseReviewPage(
      reviewBody({ available: false, unavailable_reason: 'snapshot_incomplete' }),
      REQUESTED,
    );
    expect(page.items).toEqual([]);
    expect(page.snapshot).toBeUndefined();
  });

  it('drops an unavailable reason outside the closed vocabulary', () => {
    const page = parseReviewPage(
      { available: false, unavailable_reason: 'GOV_PROJECTION_SOMETHING_NEW', page: {}, items: [] },
      REQUESTED,
    );
    expect(page.unavailableReason).toBeUndefined();
    // And it renders as static authored text, never as the code.
    expect(catalogUnavailableLabel(page.unavailableReason))
      .toBe(UNKNOWN_CATALOG_UNAVAILABLE_LABEL);
  });

  it('never lets an available page carry a leftover unavailable reason', () => {
    const page = parseReviewPage(
      reviewBody({ unavailable_reason: 'no_active_snapshot' }),
      REQUESTED,
    );
    expect(page.unavailableReason).toBeUndefined();
  });

  it('treats a missing `available` flag as unavailable', () => {
    // Fail closed: a response that does not say it is available is not
    // presented as a reviewed page.
    const page = parseReviewPage({ page: {}, items: [reviewItem()] }, REQUESTED);
    expect(page.available).toBe(false);
    expect(page.items).toEqual([]);
  });

  it('never copies a key the candidate contract does not name', () => {
    const page = parseReviewPage(
      reviewBody({
        items: [reviewItem({
          upstream_record_id: '36327',
          source_locator: { page_index: 3 },
          payload_sha256: 'a'.repeat(64),
          payload: { tozar: 'raw row' },
          candidate_id: '11111111-1111-4111-8111-111111111111',
        })],
      }),
      REQUESTED,
    );
    expect(Object.keys(page.items[0]).sort()).toEqual([
      'candidateKey', 'commercialModel', 'identityDimensions', 'manufacturer',
      'modelYearEnd', 'modelYearStart', 'officialModelCode', 'status', 'trim',
    ]);
    const serialized = JSON.stringify(page);
    for (const leaked of ['upstream_record_id', 'source_locator', 'payload_sha256',
                          'tozar', 'raw row', 'candidate_id', '36327']) {
      expect(serialized).not.toContain(leaked);
    }
  });

  it('never copies a key the snapshot contract does not name', () => {
    const body = reviewBody();
    const page = parseReviewPage(
      {
        ...body,
        snapshot: {
          ...(body.snapshot as Record<string, unknown>),
          content_sha256: 'b'.repeat(64),
          page_chain_sha256: 'c'.repeat(64),
          query: { q: 'RAV4' },
          id: '22222222-2222-4222-8222-222222222222',
        },
      },
      REQUESTED,
    );
    const serialized = JSON.stringify(page.snapshot);
    for (const leaked of ['content_sha256', 'page_chain_sha256', 'query', 'b'.repeat(64)]) {
      expect(serialized).not.toContain(leaked);
    }
  });
});

describe('the closed vocabularies', () => {
  it('renders every unavailable reason as authored static text', () => {
    for (const [reason, label] of Object.entries(CATALOG_UNAVAILABLE_LABELS)) {
      expect(label.length).toBeGreaterThan(10);
      // The code itself never appears in the sentence shown for it.
      expect(label).not.toContain(reason);
      expect(catalogUnavailableLabel(reason as never)).toBe(label);
    }
  });

  it('bounds its own page requests inside the server maximum', () => {
    expect(CATALOG_PAGE_SIZE).toBeLessThanOrEqual(MAX_CATALOG_PAGE_SIZE);
    expect(MAX_CATALOG_PAGE_SIZE).toBe(100);
  });

  it('labels exactly the closed identity-dimension vocabulary', () => {
    expect(Object.keys(CATALOG_DIMENSION_LABELS).sort()).toEqual([
      'body_style', 'drivetrain', 'engine_code', 'fuel_type', 'generation',
      'market', 'propulsion_technology', 'transmission',
    ]);
  });
});
