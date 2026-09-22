/**
 * CODE-3 — which catalog response is allowed to become the visible state.
 *
 * Independent review found the original surface applied an answer whenever the
 * PROJECT it was issued under was still selected. That cannot tell two requests
 * inside one project apart, so every settle path was last-to-resolve-wins
 * rather than newest-wins: an older page could overwrite a newer one, an older
 * `.finally()` could clear a loading state its replacement had just set, and an
 * older failure could replace a newer success with an error.
 *
 * The workspace already had the right primitive. `lib/ownership.ts` says why a
 * busy flag cannot be a boolean, and `beginPending`/`PendingRequest` carry a
 * monotonic identity precisely so a superseded request can be recognised as
 * superseded. CODE-3 now uses it.
 *
 * Every test here drives the SHIPPED page with deferred promises, so the
 * interleaving is exact rather than timing-dependent: nothing resolves until
 * the test resolves it, and the order is written down.
 */

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Page from '../app/page';

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

const TOTAL = 233;

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
};

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  // An unhandled rejection here would fail the run before the assertion does.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

/** One in-flight call, with the arguments it was issued under. */
type Issued = { projectId: string; offset: number; deferred: Deferred<unknown> };

const canonicalCalls: Issued[] = [];
const reviewCalls: Issued[] = [];

/** A canonical page whose rows name their own offset, so they are tellable apart. */
function canonicalPage(offset: number, total = TOTAL) {
  return {
    page: { limit: 25, offset, total, has_more: offset + 25 < total },
    items: [
      {
        canonical_key: `cv1.${String(offset).padStart(32, '0')}`,
        model_canonical_key: 'cm1.' + '0'.repeat(32),
        manufacturer: 'Toyota',
        commercial_model: `MODEL-AT-OFFSET-${offset}`,
        model_year_start: 2022,
        model_year_end: 2022,
        official_model_code: 'AXAA54L-ANZVB',
        trim: 'ADVENTURE',
        identity_dimensions: { fuel_type: 'petrol' },
        promoted_at: '2026-09-17T01:00:00+00:00',
        revised_at: '2026-09-17T01:00:00+00:00',
      },
    ],
  };
}

function reviewPage(offset: number) {
  return {
    available: true,
    unavailable_reason: null,
    status: 'ready_for_review',
    snapshot: {
      snapshot_key: 'cs1.' + '6'.repeat(32),
      resource_id: '142afde2-6228-49f9-8a29-9b6c3a0cbe40',
      package_id: 'degem-rechev-wltp',
      publisher: 'ministry_of_transport',
      dataset_title: 'WLTP',
      dataset_market_scope: 'IL',
      upstream_version: '2026-09-14T02:41:31',
      upstream_version_kind: 'dataset_version',
      activated_at: '2026-09-17T01:00:00+00:00',
      declared_record_count: TOTAL,
      stored_record_count: TOTAL,
      normalization_contract: 'gov.wltp.normalize.1',
      normalization_issue_count: 0,
    },
    page: { limit: 25, offset, total: TOTAL, has_more: true },
    items: [
      {
        candidate_key: `cc1.${String(offset).padStart(32, '0')}`,
        status: 'ready_for_review',
        manufacturer: 'Toyota',
        commercial_model: `CANDIDATE-AT-OFFSET-${offset}`,
        model_year_start: 2021,
        model_year_end: 2021,
        official_model_code: 'ZWE211L-DEXNBW',
        trim: 'HYBRID',
        identity_dimensions: { body_style: 'suv' },
      },
    ],
  };
}

beforeEach(() => {
  mockSession = { access_token: 'fresh', user: { id: 'u1', email: 'u@example.com' } };
  apiMocks.executionUi = false;
  canonicalCalls.length = 0;
  reviewCalls.length = 0;
  for (const fn of Object.values(apiMocks.api)) fn.mockReset();
  apiMocks.api.projects.mockResolvedValue([PROJECT, OTHER_PROJECT]);
  apiMocks.api.conversations.mockResolvedValue([]);
  apiMocks.api.catalogCanonical.mockImplementation((projectId: string, params: any) => {
    const issued: Issued = { projectId, offset: params.offset, deferred: deferred<unknown>() };
    canonicalCalls.push(issued);
    return issued.deferred.promise;
  });
  apiMocks.api.catalogReviewCandidates.mockImplementation((projectId: string, params: any) => {
    const issued: Issued = { projectId, offset: params.offset, deferred: deferred<unknown>() };
    reviewCalls.push(issued);
    return issued.deferred.promise;
  });
  window.sessionStorage.clear();
});

function panel() {
  return screen.getByRole('region', { name: 'Catalog review' });
}

/** Sign in, select a project, open the panel. Leaves request #0 in flight. */
async function openCatalog(project = PROJECT) {
  render(<Page />);
  await screen.findByText(project.name);
  fireEvent.click(screen.getByText(project.name));
  fireEvent.click(await screen.findByRole('button', { name: 'Show' }));
  await waitFor(() => expect(canonicalCalls.length).toBeGreaterThan(0));
}

/** Settle one deferred and let React apply whatever it produced. */
async function settle(fn: () => void) {
  await act(async () => {
    fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('an older catalog response can never overwrite a newer one', () => {
  it('pagination: the superseded page does not replace the page on screen', async () => {
    await openCatalog();

    // Page 0 arrives and renders; the pagination control appears with it.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();

    // Next → request for offset 25. It is NOT resolved yet.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    expect(canonicalCalls[1].offset).toBe(25);

    // Previous → request for offset 0 again. Now TWO are in flight, and this
    // one is the newer of the two.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Previous page' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(3));
    expect(canonicalCalls[2].offset).toBe(0);

    // The newer one resolves first and becomes the visible state.
    await settle(() => canonicalCalls[2].deferred.resolve(canonicalPage(0)));
    expect(screen.getByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();
    expect(within(panel()).getByText(`Showing from row 1 of ${TOTAL}.`)).toBeInTheDocument();

    // ...and the SUPERSEDED offset-25 answer lands afterwards. It must be a
    // complete no-op. Without request identity it passes `ownsProject` — the
    // project never changed — and the surface would then show offset-25 rows
    // under an offset-0 heading, which is a page claiming to be a page it is
    // not.
    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(25)));
    expect(screen.queryByText('MODEL-AT-OFFSET-25')).toBeNull();
    expect(screen.getByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();
    expect(within(panel()).getByText(`Showing from row 1 of ${TOTAL}.`)).toBeInTheDocument();
  });

  it('loading: a superseded request does not clear the busy state of its replacement', async () => {
    await openCatalog();
    expect(await screen.findByText('Loading')).toBeInTheDocument();

    // Supersede the first request without resolving it: closing and reopening
    // the panel re-reads, which is a reachable path with no page in between.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));

    // The OLD request settles. Its `.finally()` must not clear loading while
    // its replacement is still in flight — that is how a spinner disappears
    // with nothing behind it.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(within(panel()).getByText('Loading')).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();

    // The replacement settles and is the one that clears it.
    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByRole('table')).toBeInTheDocument();
  });

  it('error: a superseded failure does not replace a newer successful page', async () => {
    const { ApiError } = await import('../lib/api');
    await openCatalog();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));

    // The newer request succeeds first.
    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByRole('table')).toBeInTheDocument();

    // The older one fails afterwards. An error note here would replace a page
    // the user is reading with a failure that is not theirs.
    await settle(() =>
      canonicalCalls[0].deferred.reject(new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE', 'x')));
    expect(screen.queryByText('Not loaded')).toBeNull();
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it('error: a superseded success does not hide a newer error', async () => {
    const { ApiError } = await import('../lib/api');
    await openCatalog();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));

    // The newer request fails first, and its error is the current truth.
    await settle(() =>
      canonicalCalls[1].deferred.reject(new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE', 'x')));
    expect(await screen.findByText('Not loaded')).toBeInTheDocument();

    // The older one succeeds afterwards. Rendering its page would tell the user
    // the read worked when the current read did not.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(screen.getByText('Not loaded')).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('view: a superseded canonical answer does not disturb the review view', async () => {
    await openCatalog();

    // Switch views while the canonical request is still in flight.
    fireEvent.click(within(panel()).getByRole('tab', { name: /Ready for review/ }));
    await waitFor(() => expect(reviewCalls).toHaveLength(1));

    await settle(() => reviewCalls[0].deferred.resolve(reviewPage(0)));
    expect(await screen.findByText('CANDIDATE-AT-OFFSET-0')).toBeInTheDocument();

    // The canonical request settles last. It belongs to a view nobody is
    // looking at, so it must change nothing at all — not the rows, not the
    // loading state.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(screen.getByText('CANDIDATE-AT-OFFSET-0')).toBeInTheDocument();
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
    expect(within(panel()).getByRole('tab', { name: /Ready for review/ }))
      .toHaveAttribute('aria-selected', 'true');
  });

  it('retry: only the latest attempt for the same view and offset may settle', async () => {
    const { ApiError } = await import('../lib/api');
    await openCatalog();

    await settle(() =>
      canonicalCalls[0].deferred.reject(new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE', 'x')));
    expect(await screen.findByText('Not loaded')).toBeInTheDocument();

    // Two retries for the same view and offset. They differ only by identity,
    // which is exactly what a project-scope check cannot see.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(3));

    await settle(() => canonicalCalls[2].deferred.resolve(canonicalPage(50)));
    expect(await screen.findByText('MODEL-AT-OFFSET-50')).toBeInTheDocument();

    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(0)));
    expect(screen.getByText('MODEL-AT-OFFSET-50')).toBeInTheDocument();
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
  });
});

describe('the existing cross-boundary ownership still holds', () => {
  it('a response for the previous project is dropped, not rendered under the new one', async () => {
    await openCatalog();
    expect(canonicalCalls[0].projectId).toBe(PROJECT.id);

    fireEvent.click(screen.getByText(OTHER_PROJECT.name));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    expect(canonicalCalls[1].projectId).toBe(OTHER_PROJECT.id);

    // The first project's answer lands after the switch.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();

    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(75)));
    expect(await screen.findByText('MODEL-AT-OFFSET-75')).toBeInTheDocument();
  });

  it('a response in flight across a sign-out is dropped', async () => {
    await openCatalog();
    fireEvent.click(screen.getByRole('button', { name: 'Logout' }));
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Catalog review' })).toBeNull());

    // Nothing to render it into, and nothing may be stored for the next user.
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
  });

  it('a project switch while the panel is closed still invalidates the answer', async () => {
    // Both rules cover this now: closing the panel and selecting another
    // project each drop the token, and `ownsProject` would catch it even if
    // neither did. The assertion is on the OUTCOME, so it holds whichever rule
    // fires first.
    await openCatalog();
    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(screen.getByText(OTHER_PROJECT.name));
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));

    fireEvent.click(await screen.findByRole('button', { name: 'Show' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    expect(canonicalCalls[1].projectId).toBe(OTHER_PROJECT.id);
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
  });
});

describe('a new intent invalidates the old answer BEFORE the effect runs', () => {
  /**
   * The window the monotonic token alone does not close.
   *
   * The token protects "an old request settling after its replacement exists".
   * It says nothing about "an old request settling after the user expressed a
   * new intent but before React's effect created that replacement" — and that
   * window is real, because `setCatalogOffset` only schedules a render while a
   * settled promise runs on the microtask queue.
   *
   * Reaching it deterministically takes care. `fireEvent` is wrapped in `act`,
   * which flushes passive effects synchronously, so a click followed by a
   * settle steps straight over the gap — the replacement already exists. Doing
   * both inside ONE outer `act` scope is what holds the gap open: the click
   * records the intent, the effect flush is deferred to the scope's exit, and
   * the microtask in between is exactly where the old answer lands. Each test
   * asserts the call count inside the scope so the window is proved open
   * rather than assumed.
   */

  it('pagination: the old page cannot land in the gap between the click and the effect', async () => {
    await openCatalog();
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    expect(canonicalCalls[1].offset).toBe(25);

    await act(async () => {
      // New intent: back to offset 0.
      fireEvent.click(within(panel()).getByRole('button', { name: 'Previous page' }));
      // The window is open — the replacement has NOT been created yet.
      expect(canonicalCalls).toHaveLength(2);
      // ...and the offset-25 answer arrives right here, inside it.
      canonicalCalls[1].deferred.resolve(canonicalPage(25));
      await Promise.resolve();
      await Promise.resolve();
    });

    // It belongs to a page the user has already navigated away from, so it may
    // not become visible state. Without invalidation at the intent it would:
    // `catalogRequest.current` is still its own token and `ownsProject` passes.
    expect(screen.queryByText('MODEL-AT-OFFSET-25')).toBeNull();
    expect(screen.getByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();

    // Only the replacement's answer may.
    await waitFor(() => expect(canonicalCalls).toHaveLength(3));
    expect(canonicalCalls[2].offset).toBe(0);
    await settle(() => canonicalCalls[2].deferred.resolve(canonicalPage(0)));
    expect(screen.getByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();
    expect(within(panel()).getByText(`Showing from row 1 of ${TOTAL}.`)).toBeInTheDocument();
  });

  it('pagination: an old FAILURE cannot land in the gap either', async () => {
    const { ApiError } = await import('../lib/api');
    await openCatalog();
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    await screen.findByText('MODEL-AT-OFFSET-0');

    fireEvent.click(within(panel()).getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));

    await act(async () => {
      fireEvent.click(within(panel()).getByRole('button', { name: 'Previous page' }));
      expect(canonicalCalls).toHaveLength(2);
      canonicalCalls[1].deferred.reject(new ApiError(502, 'CATALOG_REVIEW_UNAVAILABLE', 'x'));
      await Promise.resolve();
      await Promise.resolve();
    });

    // An error note here would replace a page the user is still reading with a
    // failure belonging to a page they already left.
    //
    // Stated honestly: this one is a GUARD rather than a discriminating proof.
    // The replacement's own `setCatalogError('')` runs when the effect flushes,
    // so a failure written in the gap is masked in every reachable
    // interleaving. It is kept because the invariant is real and a future
    // change that stopped clearing the error on a new read would make it
    // discriminating; the page assertion above is the one that fails without
    // intent invalidation.
    expect(screen.queryByText('Not loaded')).toBeNull();
    expect(screen.getByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();
  });

  it('closing the panel ends the intent, and its `.finally` cannot clear the next one', async () => {
    await openCatalog();
    expect(within(panel()).getByText('Loading')).toBeInTheDocument();

    await act(async () => {
      // New intent: closed. No replacement request is issued for it, which is
      // what makes BOTH halves of the old settle observable here — the page it
      // would have written, and the busy state its `.finally` would have
      // cleared. In the pagination gap above only the page is observable,
      // because the replacement immediately re-sets loading.
      fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
      expect(canonicalCalls).toHaveLength(1);
      canonicalCalls[0].deferred.resolve(canonicalPage(0));
      await Promise.resolve();
      await Promise.resolve();
    });

    // Reopening re-reads. Nothing from the closed visit is waiting to appear,
    // and the surface is busy for the NEW read rather than resting on a
    // loading state the old request cleared.
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
    expect(within(panel()).getByText('Loading')).toBeInTheDocument();
    expect(panel().querySelector('[role="tabpanel"]')).toHaveAttribute('aria-busy', 'true');

    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();
  });

  it('reopening shows no stale page from the previous visit', async () => {
    await openCatalog();
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    expect(await screen.findByText('MODEL-AT-OFFSET-0')).toBeInTheDocument();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));

    // The panel's own contract says opening re-reads. A page retained from the
    // last visit would make that claim false while looking like current state —
    // and it became retainable when the surface started keeping a page visible
    // during a refresh, so the claim and the behaviour are reconciled here.
    expect(screen.queryByText('MODEL-AT-OFFSET-0')).toBeNull();
    expect(within(panel()).getByText('Loading')).toBeInTheDocument();
  });

  it('reopening returns to the same page rather than silently jumping to the first', async () => {
    await openCatalog();
    await settle(() => canonicalCalls[0].deferred.resolve(canonicalPage(0)));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(canonicalCalls).toHaveLength(2));
    await settle(() => canonicalCalls[1].deferred.resolve(canonicalPage(25)));
    expect(await screen.findByText('MODEL-AT-OFFSET-25')).toBeInTheDocument();

    fireEvent.click(within(panel()).getByRole('button', { name: 'Hide' }));
    fireEvent.click(within(panel()).getByRole('button', { name: 'Show' }));

    // Closing drops the rendered rows but keeps WHERE the operator was; the
    // re-read is for that same page, freshly.
    await waitFor(() => expect(canonicalCalls).toHaveLength(3));
    expect(canonicalCalls[2].offset).toBe(25);
  });
});
