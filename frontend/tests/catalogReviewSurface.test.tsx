/**
 * CODE-3 — the catalog review surface as an operator actually meets it.
 *
 * Driven through the SHIPPED page: the real `app/page.tsx`, the real parser and
 * the real panel, with only the API client and Supabase session mocked. So a
 * regression in the parser, in the ownership guards or in the panel fails these
 * tests too.
 *
 * What is asserted is BEHAVIOUR. That the panel renders is worth little on its
 * own; what matters is that no control can mutate anything, that a candidate can
 * never be mistaken for a canonical row, that every state has its own honest
 * message, and that nothing a response carries outside the contract reaches the
 * DOM.
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';
import { ALL_SECRET_SENTINELS } from './secretSentinels';

let mockSession: any = { access_token: 'fresh', user: { id: 'u1', email: 'u@example.com' } };

vi.mock('../lib/supabaseClient', () => ({
  getCurrentSession: vi.fn(() => Promise.resolve(mockSession)),
  onAuthStateChange: vi.fn(() => () => {}),
  signInWithSupabase: vi.fn(() => Promise.resolve(mockSession)),
  signOutFromSupabase: vi.fn(() => Promise.resolve()),
  getCurrentAccessToken: vi.fn(() => Promise.resolve('fresh')),
}));

const apiMocks = vi.hoisted(() => ({
  executionUi: false,
  api: {
    projects: vi.fn(),
    conversations: vi.fn(),
    createConversation: vi.fn(),
    createProposal: vi.fn(),
    proposal: vi.fn(),
    decideProposal: vi.fn(),
    reviseProposal: vi.fn(),
    startRun: vi.fn(),
    run: vi.fn(),
    runs: vi.fn(() => Promise.resolve([])),
    events: vi.fn(),
    cancel: vi.fn(),
    catalogCanonical: vi.fn(),
    catalogReviewCandidates: vi.fn(),
  },
}));

vi.mock('../lib/api', () => ({
  api: apiMocks.api,
  executionUiEnabled: () => apiMocks.executionUi,
  newIdempotencyKey: () => 'ui-test-idempotency-key',
  ApiError: class ApiError extends Error {
    constructor(public status: number, public code: string, message: string) {
      super(message);
    }
  },
}));

const PROJECT = {
  id: '677db6c2-b44c-41c1-b4e1-b51229d697df',
  slug: 'milo-vehicle-catalog',
  name: 'MILO Vehicle Catalog',
  workflow_key: 'vehicle_catalog_v1',
};
const OTHER_PROJECT = {
  id: '9a1d1b02-0b2f-4a5b-9a24-8f0d5a6f4c31',
  slug: 'beta',
  name: 'Beta Catalog',
  workflow_key: 'vehicle_catalog_v1',
};

const CANONICAL_ITEM = {
  canonical_key: 'cv1.acaa1a82665c383af059fcee98841783',
  model_canonical_key: 'cm1.2961c45222fe069fc7dcc36c96621725',
  manufacturer: 'Toyota',
  commercial_model: 'RAV4',
  model_year_start: 2022,
  model_year_end: 2022,
  official_model_code: 'AXAA54L-ANZVB',
  trim: 'ADVENTURE',
  identity_dimensions: { fuel_type: 'petrol' },
  promoted_at: '2026-09-17T01:00:00+00:00',
  revised_at: '2026-09-17T01:00:00+00:00',
};

const REVIEW_ITEM = {
  candidate_key: 'cc1.a2c54473fac31d1d2c05489f6a74fcbf',
  status: 'ready_for_review',
  manufacturer: 'Toyota',
  commercial_model: 'COROLLA',
  model_year_start: 2021,
  model_year_end: 2021,
  official_model_code: 'ZWE211L-DEXNBW',
  trim: 'HYBRID',
  identity_dimensions: { body_style: 'suv' },
};

const SNAPSHOT = {
  snapshot_key: 'cs1.60c62e4edf73492580680f1ddf5559dc',
  resource_id: '142afde2-6228-49f9-8a29-9b6c3a0cbe40',
  package_id: 'degem-rechev-wltp',
  publisher: 'ministry_of_transport',
  dataset_title: 'Private and commercial vehicle models',
  dataset_market_scope: 'IL',
  upstream_version: '2026-09-14T02:41:31',
  upstream_version_kind: 'dataset_version',
  activated_at: '2026-09-17T01:00:00+00:00',
  declared_record_count: 233,
  stored_record_count: 233,
  normalization_contract: 'gov.wltp.normalize.1',
  normalization_issue_count: 0,
};

function canonicalBody(extra: Record<string, unknown> = {}) {
  return {
    page: { limit: 25, offset: 0, total: 1, has_more: false },
    items: [CANONICAL_ITEM],
    ...extra,
  };
}

function reviewBody(extra: Record<string, unknown> = {}) {
  return {
    available: true,
    unavailable_reason: null,
    status: 'ready_for_review',
    snapshot: SNAPSHOT,
    page: { limit: 25, offset: 0, total: 1, has_more: false },
    items: [REVIEW_ITEM],
    ...extra,
  };
}

/** Sign in, select a project, and open the catalog review panel. */
async function openCatalog(project = PROJECT) {
  render(<Page />);
  await screen.findByText(project.name);
  fireEvent.click(screen.getByText(project.name));
  const toggle = await screen.findByRole('button', { name: 'Show' });
  fireEvent.click(toggle);
  return toggle;
}

function panel() {
  return screen.getByRole('region', { name: 'Catalog review' });
}

beforeEach(() => {
  mockSession = { access_token: 'fresh', user: { id: 'u1', email: 'u@example.com' } };
  apiMocks.executionUi = false;
  for (const fn of Object.values(apiMocks.api)) fn.mockReset();
  apiMocks.api.projects.mockResolvedValue([PROJECT, OTHER_PROJECT]);
  apiMocks.api.conversations.mockResolvedValue([]);
  apiMocks.api.catalogCanonical.mockResolvedValue(canonicalBody());
  apiMocks.api.catalogReviewCandidates.mockResolvedValue(reviewBody());
  window.sessionStorage.clear();
});

describe('the surface only appears where it applies', () => {
  it('is absent until a project is selected', async () => {
    render(<Page />);
    await screen.findByText(PROJECT.name);
    expect(screen.queryByRole('region', { name: 'Catalog review' })).toBeNull();
  });

  it('appears once a project is selected, closed and having read nothing', async () => {
    render(<Page />);
    await screen.findByText(PROJECT.name);
    fireEvent.click(screen.getByText(PROJECT.name));
    await screen.findByRole('region', { name: 'Catalog review' });
    expect(screen.getByRole('button', { name: 'Show' })).toHaveAttribute('aria-expanded', 'false');
    // A closed panel issues no request at all.
    expect(apiMocks.api.catalogCanonical).not.toHaveBeenCalled();
    expect(apiMocks.api.catalogReviewCandidates).not.toHaveBeenCalled();
  });

  it('is shown even though the execution UI is off', async () => {
    // The execution flag hides EXECUTION controls. There are none here, and a
    // read-only inspection surface must survive the posture an operator
    // inspects the catalog from.
    apiMocks.executionUi = false;
    await openCatalog();
    await screen.findByRole('table');
    expect(apiMocks.api.catalogCanonical).toHaveBeenCalledWith(PROJECT.id, {
      limit: 25, offset: 0,
    });
  });
});

describe('canonical and review rows are never confusable', () => {
  it('shows canonical rows under their own heading, with no candidate marker', async () => {
    await openCatalog();
    const table = await screen.findByRole('table');
    expect(within(table).getByText('RAV4')).toBeInTheDocument();
    expect(within(table).queryByText('Candidate')).toBeNull();
    expect(within(table).queryByText('not canonical')).toBeNull();
  });

  it('marks every review row as a candidate, in text rather than colour alone', async () => {
    await openCatalog();
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    const table = await screen.findByRole('table');
    const row = within(table).getByText('COROLLA').closest('tr')!;
    expect(within(row).getByText('Candidate')).toBeInTheDocument();
    expect(within(row).getByText('not canonical')).toBeInTheDocument();
    // And the page says so once for the whole page, too.
    expect(screen.getByText('Government candidates awaiting review')).toBeInTheDocument();
  });

  it('keeps the two views in separate tab panels', async () => {
    await openCatalog();
    const canonicalTab = screen.getByRole('tab', { name: /Canonical catalog/ });
    const reviewTab = screen.getByRole('tab', { name: /Ready for review/ });
    expect(canonicalTab).toHaveAttribute('aria-selected', 'true');
    expect(reviewTab).toHaveAttribute('aria-selected', 'false');
    fireEvent.click(reviewTab);
    await waitFor(() => expect(reviewTab).toHaveAttribute('aria-selected', 'true'));
    // Canonical rows are gone; they are not merged into the review list.
    await waitFor(() => expect(screen.queryByText('ADVENTURE')).toBeNull());
  });

  it('never shows both tables at once', async () => {
    await openCatalog();
    await screen.findByRole('table');
    expect(screen.getAllByRole('table')).toHaveLength(1);
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await waitFor(() => expect(screen.getAllByRole('table')).toHaveLength(1));
  });
});

describe('there is no way to mutate anything', () => {
  it('renders no control that could promote, approve, reject, edit or capture', async () => {
    await openCatalog();
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await screen.findByRole('table');

    const forbidden = [
      /approve/i, /reject/i, /promote/i, /\bedit\b/i, /capture/i, /refresh/i,
      /retry promotion/i, /enable/i, /start milo/i, /\bdelete\b/i, /deactivate/i,
      /activate/i, /\bsave\b/i, /\bapply\b/i, /reconcil/i, /migrat/i,
    ];
    const controls = [
      ...within(panel()).queryAllByRole('button'),
      ...within(panel()).queryAllByRole('link'),
      ...within(panel()).queryAllByRole('textbox'),
      ...within(panel()).queryAllByRole('checkbox'),
    ];
    for (const control of controls) {
      const label = `${control.textContent ?? ''} ${control.getAttribute('aria-label') ?? ''}`;
      for (const pattern of forbidden) {
        expect(label).not.toMatch(pattern);
      }
    }
  });

  it('exposes only navigation controls: the views, the pages and the disclosure', async () => {
    await openCatalog();
    await screen.findByRole('table');
    // EVERY `<button>` in the panel, whatever role it carries. Querying by role
    // alone would miss the tabs, which is exactly the gap a mutation control
    // could one day hide in.
    const labels = Array.from(panel().querySelectorAll('button'))
      .map((button) => (button.textContent ?? '').trim())
      .sort();
    expect(labels).toEqual([
      'Canonical catalogPromoted, with verified provenance',
      'Hide',
      'Next page',
      'Previous page',
      'Ready for reviewCandidates awaiting a human',
    ]);
  });

  it('accepts no text input anywhere on the surface', async () => {
    await openCatalog();
    await screen.findByRole('table');
    expect(within(panel()).queryAllByRole('textbox')).toHaveLength(0);
    expect(panel().querySelectorAll('form')).toHaveLength(0);
    expect(panel().querySelectorAll('input, textarea, select')).toHaveLength(0);
  });

  it('issues only the two read calls, however much the operator clicks', async () => {
    await openCatalog();
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('tab', { name: /Canonical catalog/ }));
    await screen.findByRole('table');
    // Every other client method stayed untouched, including every mutation.
    for (const [name, fn] of Object.entries(apiMocks.api)) {
      if (name === 'catalogCanonical' || name === 'catalogReviewCandidates') continue;
      if (name === 'projects' || name === 'conversations') continue;
      expect(fn, name).not.toHaveBeenCalled();
    }
  });
});

describe('every state has its own honest message', () => {
  it('shows loading before the first page arrives, and calls it that', async () => {
    let settle: (value: unknown) => void = () => {};
    apiMocks.api.catalogCanonical.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    await openCatalog();
    expect(await screen.findByText('Loading')).toBeInTheDocument();
    // "Loading" is never the empty message.
    expect(screen.queryByText('No canonical variants')).toBeNull();
    settle(canonicalBody());
    await screen.findByRole('table');
  });

  it('distinguishes an empty canonical page from an unavailable one', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({ items: [], page: { limit: 25, offset: 0, total: 0, has_more: false } }),
    );
    await openCatalog();
    expect(await screen.findByText('No canonical variants')).toBeInTheDocument();
  });

  it('states that there is no snapshot without claiming an empty catalog', async () => {
    apiMocks.api.catalogReviewCandidates.mockResolvedValue({
      available: false,
      unavailable_reason: 'no_active_snapshot',
      status: 'ready_for_review',
      snapshot: null,
      page: { limit: 25, offset: 0, total: null, has_more: null },
      items: [],
    });
    await openCatalog();
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    expect(await screen.findByText('Nothing to review')).toBeInTheDocument();
    expect(
      screen.getByText(/No Government catalog snapshot is active/),
    ).toBeInTheDocument();
    // No table, no snapshot banner, no fabricated count.
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByText(/Showing from row/)).toBeNull();
  });

  it.each([
    ['snapshot_incomplete', /rows its reviewed vocabulary could not read/],
    ['snapshot_not_normalized', /captured raw-only/],
    ['snapshot_not_read', /records no reading of its rows/],
    ['snapshot_state_invalid', /malformed or disagrees/],
  ])('renders static authored copy for %s', async (reason, pattern) => {
    apiMocks.api.catalogReviewCandidates.mockResolvedValue({
      available: false, unavailable_reason: reason, status: 'ready_for_review',
      snapshot: null, page: { limit: 25, offset: 0, total: null, has_more: null }, items: [],
    });
    await openCatalog();
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    expect(await screen.findByText(pattern)).toBeInTheDocument();
    // The code itself is never shown.
    expect(panel().textContent).not.toContain(reason);
  });

  it('reports a backend failure with the caller-authored sentence, never upstream text', async () => {
    const { ApiError } = await import('../lib/api');
    apiMocks.api.catalogCanonical.mockRejectedValue(
      new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE',
                   'ERROR: relation catalog_models does not exist at line 3 of SELECT'),
    );
    await openCatalog();
    expect(await screen.findByText('Not loaded')).toBeInTheDocument();
    expect(screen.getByText('Failed to load the catalog page.')).toBeInTheDocument();
    // No database text, no SQL, on any path.
    expect(panel().textContent).not.toContain('catalog_models');
    expect(panel().textContent).not.toContain('SELECT');
    // And no stale rows sit beside the error.
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('offers a retry that re-reads and nothing else', async () => {
    const { ApiError } = await import('../lib/api');
    apiMocks.api.catalogCanonical.mockRejectedValueOnce(
      new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE', 'internal'),
    );
    apiMocks.api.catalogCanonical.mockResolvedValue(canonicalBody());
    await openCatalog();
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));
    await screen.findByRole('table');
    expect(apiMocks.api.catalogCanonical).toHaveBeenCalledTimes(2);
  });

  it('shows an authorization failure as the account-scoped sentence', async () => {
    const { ApiError } = await import('../lib/api');
    apiMocks.api.catalogCanonical.mockRejectedValue(
      new ApiError(404, 'PROJECT_NOT_FOUND', 'project not found: 677db6c2'),
    );
    await openCatalog();
    expect(await screen.findByText(/not available to your account/)).toBeInTheDocument();
  });
});

describe('pagination is bounded and truthful', () => {
  it('disables Previous on the first page and Next when there is no more', async () => {
    await openCatalog();
    await screen.findByRole('table');
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  });

  it('advances by exactly the server-stated page size', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({ page: { limit: 25, offset: 0, total: 233, has_more: true } }),
    );
    await openCatalog();
    await screen.findByRole('table');
    expect(screen.getByText('Showing from row 1 of 233.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() =>
      expect(apiMocks.api.catalogCanonical).toHaveBeenLastCalledWith(PROJECT.id, {
        limit: 25, offset: 25,
      }));
  });

  it('never asks for more than the client bound, whatever the server states', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({ page: { limit: 100000, offset: 0, total: 5, has_more: true } }),
    );
    await openCatalog();
    await screen.findByRole('table');
    for (const call of apiMocks.api.catalogCanonical.mock.calls) {
      expect(call[1].limit).toBeLessThanOrEqual(100);
    }
  });

  it('says the total is unknown rather than showing a number it does not have', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({ page: { limit: 25, offset: 0, total: null, has_more: null } }),
    );
    await openCatalog();
    await screen.findByRole('table');
    expect(screen.getByText('Showing from row 1. The server did not state a total.'))
      .toBeInTheDocument();
    // Unknown is never rendered as "of 0".
    expect(panel().textContent).not.toContain('of 0');
  });

  it('lays out at most the bounded number of rows however many are sent', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({
        page: { limit: 25, offset: 0, total: 5000, has_more: true },
        items: Array.from({ length: 5_000 }, (_unused, index) => ({
          ...CANONICAL_ITEM,
          canonical_key: `cv1.${String(index).padStart(32, '0')}`,
        })),
      }),
    );
    await openCatalog();
    const table = await screen.findByRole('table');
    expect(within(table).getAllByRole('row').length).toBeLessThanOrEqual(101); // header + 100
  });

  it('starts a switched view at the first page', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({ page: { limit: 25, offset: 0, total: 233, has_more: true } }),
    );
    await openCatalog();
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(apiMocks.api.catalogCanonical).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await waitFor(() =>
      expect(apiMocks.api.catalogReviewCandidates).toHaveBeenLastCalledWith(PROJECT.id, {
        limit: 25, offset: 0,
      }));
  });
});

describe('nothing outside the contract reaches the DOM', () => {
  it('renders no unexpected field, as text or as JSON', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({
        items: [{
          ...CANONICAL_ITEM,
          variant_id: '11111111-1111-4111-8111-111111111111',
          field_revisions: { trim: 3 },
          payload: { tozar: 'raw register row', koah_sus: 197 },
          an_unreviewed_column: 'should never render',
        }],
        an_unexpected_top_level_key: { nested: 'value' },
      }),
    );
    await openCatalog();
    await screen.findByRole('table');
    const text = panel().textContent ?? '';
    for (const leaked of ['11111111-1111', 'field_revisions', 'tozar', 'koah_sus',
                          'raw register row', 'should never render',
                          'an_unexpected_top_level_key', 'nested']) {
      expect(text).not.toContain(leaked);
    }
    // No JSON dump anywhere: braces would be the tell.
    expect(text).not.toContain('{"');
  });

  it('surfaces no credential even when one reaches a durable string', async () => {
    apiMocks.api.catalogReviewCandidates.mockResolvedValue(
      reviewBody({
        items: [{ ...REVIEW_ITEM, trim: `HYBRID ${ALL_SECRET_SENTINELS[0]}` }],
        snapshot: { ...SNAPSHOT, publisher: `ministry ${ALL_SECRET_SENTINELS[0]}` },
      }),
    );
    await openCatalog();
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await screen.findByRole('table');
    const html = panel().innerHTML;
    for (const secret of ALL_SECRET_SENTINELS) {
      expect(html).not.toContain(secret);
    }
  });

  it('renders no markup from a durable string', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({
        items: [{ ...CANONICAL_ITEM, trim: '<img src=x onerror="alert(1)">' }],
      }),
    );
    await openCatalog();
    const table = await screen.findByRole('table');
    // No element was created and no handler attribute exists: the string is
    // TEXT. `safeText` also substitutes the angle brackets, so what a reader
    // sees cannot be mistaken for markup either.
    expect(panel().querySelector('img')).toBeNull();
    expect(panel().querySelector('[onerror]')).toBeNull();
    expect(panel().innerHTML).not.toContain('<img');
    expect(within(table).getByText(/onerror/).textContent)
      .toBe('\u2039img src=x onerror="alert(1)"\u203a');
  });

  it('says a field is not stated rather than rendering an empty cell', async () => {
    apiMocks.api.catalogCanonical.mockResolvedValue(
      canonicalBody({
        items: [{ ...CANONICAL_ITEM, trim: null, official_model_code: null,
                  identity_dimensions: {} }],
      }),
    );
    await openCatalog();
    const table = await screen.findByRole('table');
    expect(within(table).getAllByText('Not stated').length).toBeGreaterThanOrEqual(2);
    expect(within(table).getByText('None stated')).toBeInTheDocument();
  });
});

describe('the surface belongs to the selected project', () => {
  it('drops one project\'s rows when another is selected', async () => {
    await openCatalog();
    await screen.findByRole('table');
    expect(screen.getByText('RAV4')).toBeInTheDocument();
    fireEvent.click(screen.getByText(OTHER_PROJECT.name));
    // The panel closes with the switch and shows nothing from the old project.
    await waitFor(() => expect(screen.queryByText('RAV4')).toBeNull());
  });

  it('reads with the selected project\'s id, never a remembered one', async () => {
    await openCatalog();
    await screen.findByRole('table');
    expect(apiMocks.api.catalogCanonical).toHaveBeenLastCalledWith(PROJECT.id, expect.anything());
    // The panel stays open across the switch and re-reads for the NEW project,
    // from the first page. What must never happen is a read still carrying the
    // old project id, or the old project's rows surviving the switch.
    fireEvent.click(screen.getByText(OTHER_PROJECT.name));
    await waitFor(() =>
      expect(apiMocks.api.catalogCanonical)
        .toHaveBeenLastCalledWith(OTHER_PROJECT.id, { limit: 25, offset: 0 }));
    expect(screen.getByRole('button', { name: 'Hide' })).toBeInTheDocument();
  });
});

describe('the surface is usable on a small screen and without colour', () => {
  it('puts the wide table in its own scroll region rather than widening the page', async () => {
    await openCatalog();
    await screen.findByRole('table');
    const scroller = panel().querySelector('.catalog-table-scroll');
    expect(scroller).not.toBeNull();
    expect(scroller!.querySelector('table')).not.toBeNull();
  });

  it('carries every distinction in text, so it survives with no stylesheet', async () => {
    await openCatalog();
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    await screen.findByRole('table');
    // Strip every class and style, then check the meaning is still present.
    for (const node of Array.from(panel().querySelectorAll('*'))) {
      node.removeAttribute('class');
      node.removeAttribute('style');
      node.removeAttribute('data-surface');
      node.removeAttribute('data-kind');
    }
    const text = panel().textContent ?? '';
    expect(text).toContain('Candidate');
    expect(text).toContain('not canonical');
    expect(text).toContain('Ready for review');
  });

  it('gives each table a caption that names what it holds', async () => {
    await openCatalog();
    const canonical = await screen.findByRole('table');
    expect(canonical.querySelector('caption')?.textContent)
      .toContain('Canonical catalog variants');
    fireEvent.click(screen.getByRole('tab', { name: /Ready for review/ }));
    const review = await screen.findByRole('table');
    expect(review.querySelector('caption')?.textContent).toContain('not');
    expect(review.querySelector('caption')?.textContent).toContain('canonical');
  });
});
