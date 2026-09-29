/**
 * PR-CAT: the catalog browser (D5) and "Add to plan" (D6).
 *
 * What is asserted: the server's pages are parsed strictly (an unreadable
 * item makes the page unreadable, never partly rendered); every state renders
 * (loading, empty, error, and nothing at all while the server answers 404);
 * the tree pages through the server (offset), and filters travel as exactly
 * the route's query parameters; "Add to plan" sends exactly the edit the
 * existing plan routes take -- a new plan, or the open plan's units plus the
 * selection under the plan's own year range (a different stated range is
 * refused, never silently widened or narrowed) -- and never anything else;
 * and the gateway proxies the five reads as reads and nothing else.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CatalogBrowserClient, CatalogBrowserPanel } from '../components/catalog/CatalogBrowserPanel';
import { ApiError } from '../lib/api';
import {
  browserQuery, parseFacets, parseManufacturer, parsePage, parseVariant, planAddition,
} from '../lib/catalogBrowser';
import { isGatewayRequestAllowed, isRunCreationRequest } from '../lib/server/gatewayPolicy';
import { parseCapabilities, parseDirectory, parseWorkScopeState } from '../lib/workScope';
import { CAPABILITIES, CONVERSATION, DIGEST, DIRECTORY, PLAN, PROJECT, stateBody } from './fixtures/workScope';

const TOYOTA = 'טויוטה';
const MAZDA = 'מאזדה';
const UNMAPPED = 'יצרן לא ידוע';
const DIRECTORY_WITH_MAZDA = {
  ...DIRECTORY,
  entries: DIRECTORY.entries.map((entry) => (entry.key === 'mazda'
    ? { ...entry, register_marque: MAZDA, register_marque_verified: true } : entry)),
};

function page(items: unknown[], total = items.length, offset = 0) {
  return { total, limit: 25, offset, items };
}

function variantBody(id: string, extra: Record<string, unknown> = {}) {
  return {
    upstream_record_id: id, snapshot_key: 'cs1.toyota', vehicle_segment: 'private', tozar: TOYOTA,
    kinuy_mishari: 'RAV4', shnat_yitzur: 2024, degem_nm: 'AXAH54L', ramat_gimur: 'XLE', delek_nm: 'בנזין',
    automatic_ind: 1, mishkal_kolel: 2150, co2_wltp: null, abs_ind: 1, parse_issues: [],
    coverage: { identity: { status: 'enriched', reason_code: null, current: true },
                register: { status: 'pending', reason_code: null, current: false } },
    ...extra,
  };
}

const FACETS = { segments: [{ value: 'private', variants: 30 }, { value: 'commercial', variants: 5 }],
                 fuels: [{ delek_cd: 1, delek_nm: 'בנזין', variants: 30 }],
                 bodies: [{ merkav: 'פנאי-שטח', variants: 35 }], year_min: 2018, year_max: 2026 };

function client(answers: Partial<Record<string, unknown>> = {}, overrides: Partial<CatalogBrowserClient> = {}):
  CatalogBrowserClient {
  const levels: Record<string, unknown> = {
    manufacturers: page([{ tozar: TOYOTA, variants: 30 }, { tozar: MAZDA, variants: 4 },
                         { tozar: UNMAPPED, variants: 1 }]),
    models: page([{ kinuy_mishari: 'RAV4', variants: 11, year_min: 2022, year_max: 2026 }]),
    years: page([{ shnat_yitzur: 2024, variants: 2 }]),
    variants: page([variantBody('42732'), variantBody('42733')]),
    facets: FACETS,
    ...answers,
  };
  return {
    browse: vi.fn(async (_project: string, level: string) => {
      const answer = levels[level];
      if (answer instanceof Error) throw answer;
      return answer;
    }),
    capabilities: vi.fn(async () => CAPABILITIES),
    directory: vi.fn(async () => DIRECTORY_WITH_MAZDA),
    openPlan: vi.fn(async () => ({ work_scope: null })),
    createPlan: vi.fn(async () => ({ applied: true, notes: [], work_scope: stateBody() })),
    revisePlan: vi.fn(async () => ({ applied: true, notes: [], work_scope: stateBody({ revision: 2 }) })),
    ...overrides,
  };
}

async function openPanel(api: CatalogBrowserClient, conversationId: string | null = CONVERSATION,
  props: { planWrites?: boolean; onPlanChanged?: () => void } = {}) {
  const view = render(<CatalogBrowserPanel projectId={PROJECT} conversationId={conversationId ?? undefined}
    planWrites={props.planWrites ?? true} onPlanChanged={props.onPlanChanged} client={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Show' }));
  return view;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('parsing', () => {
  it('reads a page, and refuses it whole when any item is unreadable', () => {
    expect(parsePage(page([{ tozar: TOYOTA, variants: 3 }]), parseManufacturer)?.items).toEqual(
      [{ tozar: TOYOTA, variants: 3 }]);
    expect(parsePage(page([{ tozar: TOYOTA, variants: -1 }]), parseManufacturer)).toBeUndefined();
    expect(parsePage({ items: [] }, parseManufacturer)).toBeUndefined();
  });

  it('reads a variant: its source, its level-1.5 fields and a badge per coverage level', () => {
    const variant = parseVariant(variantBody('42732', { parse_issues: [{ field: 'x', reason: 'not_a_number' }] }))!;
    expect(variant.upstreamRecordId).toBe('42732');
    expect(variant.fields).toContainEqual(['Automatic', 'Yes']);
    expect(variant.fields).toContainEqual(['Total mass', '2150']);
    expect(variant.fields.map(([label]) => label)).not.toContain('CO2 WLTP');
    expect(variant.coverage).toEqual({ register: { status: 'pending', current: false },
                                       identity: { status: 'enriched', current: true }, government_fields: null });
    expect(variant.parseIssues).toBe(1);
    expect(parseVariant(variantBody('1', { vehicle_segment: 'truck' }))).toBeUndefined();
  });

  it('reads the facets', () => {
    expect(parseFacets(FACETS)?.fuels).toEqual([{ delekCd: 1, delekNm: 'בנזין', variants: 30 }]);
    expect(parseFacets({ segments: [] })).toBeUndefined();
  });

  it('states exactly the query parameters the route reads', () => {
    expect(browserQuery({})).toBe('');
    expect(browserQuery({ segment: 'private', yearFrom: 2020, yearTo: 2024, delekCd: 1, merkav: 'סדאן' },
                        { tozar: TOYOTA, limit: 25, offset: 50, kinuy_mishari: undefined })).toBe(
      `?segment=private&year_from=2020&year_to=2024&delek_cd=1&merkav=${encodeURIComponent('סדאן')}`
      + `&tozar=${encodeURIComponent(TOYOTA)}&limit=25&offset=50`);
  });
});

describe('planAddition', () => {
  const limits = parseCapabilities(CAPABILITIES)!.limits;
  const entries = parseDirectory(DIRECTORY_WITH_MAZDA)!.entries;

  it('makes a new plan of the verified manufacturers and the year range', () => {
    expect(planAddition({ tozars: [TOYOTA, UNMAPPED], yearFrom: 2020, yearTo: null }, null, entries, limits))
      .toEqual({ kind: 'create', unmapped: [UNMAPPED],
                 edit: { units: ['toyota'], model_year_from: 2020, model_year_to: null, max_items: 100,
                         batch_size: 10 } });
  });

  it('adds to the open plan under the plan\'s own year range', () => {
    const plan = parseWorkScopeState(stateBody())!;          // toyota + lexus, 2018 onwards
    const revised = {
      kind: 'revise', unmapped: [], head: { revision: 1, digest: DIGEST },
      edit: { units: ['toyota', 'lexus', 'mazda'], model_year_from: 2018, model_year_to: null,
              max_items: 800, batch_size: 10 } };
    expect(planAddition({ tozars: [MAZDA], yearFrom: null, yearTo: null }, plan, entries, limits)).toEqual(revised);
    expect(planAddition({ tozars: [MAZDA], yearFrom: 2018, yearTo: null }, plan, entries, limits)).toEqual(revised);
    expect(planAddition({ tozars: [TOYOTA], yearFrom: null, yearTo: null }, plan, entries, limits))
      .toEqual({ kind: 'refused', reason: 'no_change', unmapped: [] });
  });

  it('refuses a stated year range other than the plan\'s: one range covers every manufacturer', () => {
    const plan = parseWorkScopeState(stateBody())!;
    for (const [yearFrom, yearTo] of [[2015, 2020], [2020, null], [null, 2024]] as const) {
      expect(planAddition({ tozars: [MAZDA], yearFrom, yearTo }, plan, entries, limits))
        .toEqual({ kind: 'refused', reason: 'years_differ', unmapped: [] });
    }
  });

  it('keeps include_unresolved when the open plan has it', () => {
    const plan = parseWorkScopeState(stateBody())!;
    const withUnresolved = { ...plan, plan: { ...plan.plan, includeUnresolved: true } };
    const addition = planAddition({ tozars: [MAZDA], yearFrom: null, yearTo: null }, withUnresolved, entries, limits);
    expect(addition.kind === 'revise' && addition.edit.include_unresolved).toBe(true);
  });

  it('refuses what the plan contract cannot express', () => {
    expect(planAddition({ tozars: [UNMAPPED], yearFrom: null, yearTo: null }, null, entries, limits))
      .toEqual({ kind: 'refused', reason: 'nothing_mappable', unmapped: [UNMAPPED] });
    expect(planAddition({ tozars: [TOYOTA], yearFrom: null, yearTo: null }, null, entries,
                        { ...limits, maxUnits: 0 }).kind).toBe('refused');
  });
});

describe('CatalogBrowserPanel', () => {
  it('does not exist while the server answers 404 (flag off)', async () => {
    const api = client({ manufacturers: new ApiError(404, 'CATALOG_BROWSER_DISABLED', 'not enabled') });
    const { container } = render(<CatalogBrowserPanel projectId={PROJECT} conversationId={CONVERSATION} planWrites
      client={api} />);
    await waitFor(() => expect(api.browse).toHaveBeenCalled());
    expect(container.innerHTML).toBe('');
    expect(api.openPlan).not.toHaveBeenCalled();
  });

  it('renders the manufacturers', async () => {
    await openPanel(client());
    expect(await screen.findByRole('button', { name: TOYOTA })).toBeTruthy();
    expect(screen.getByText('30 variants')).toBeTruthy();
  });

  it('says so when nothing matches', async () => {
    await openPanel(client({ manufacturers: page([]) }));
    expect(await screen.findByText(/No catalog variants match/)).toBeTruthy();
  });

  it('walks manufacturer -> model -> year -> variants, showing loading and the badges', async () => {
    let release: (value: unknown) => void = () => undefined;
    const api = client({}, {});
    const browse = api.browse as ReturnType<typeof vi.fn>;
    await openPanel(api);
    browse.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    fireEvent.click(await screen.findByRole('button', { name: TOYOTA }));
    expect(screen.getByRole('status').textContent).toBe('Loading…');
    await act(async () => release(page([{ kinuy_mishari: 'RAV4', variants: 11, year_min: 2022, year_max: 2026 }])));
    expect(screen.getByText(/cannot be added to a plan on its own/)).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'RAV4' }));
    fireEvent.click(await screen.findByRole('button', { name: '2024' }));
    const variant = await screen.findByLabelText('Variant 42732');
    expect(variant.textContent).toContain('42732');
    expect(variant.textContent).toContain('cs1.toyota');
    expect(variant.textContent).toContain('Identity: enriched');
    expect(variant.textContent).toContain('Register run: pending (older content)');
    expect(variant.textContent).toContain('Government fields: not covered');
    expect(browse).toHaveBeenLastCalledWith(PROJECT, 'variants',
      `?tozar=${encodeURIComponent(TOYOTA)}&kinuy_mishari=RAV4&shnat_yitzur=2024&limit=25&offset=0`);
  });

  it('shows an error on a later read that fails, and keeps the page', async () => {
    const api = client({ models: new ApiError(500, 'REPOSITORY_ERROR', 'boom') });
    await openPanel(api);
    fireEvent.click(await screen.findByRole('button', { name: TOYOTA }));
    expect((await screen.findByRole('alert')).textContent).toBeTruthy();
    expect(screen.getByRole('button', { name: 'All manufacturers' })).toBeTruthy();
  });

  it('pages through the server', async () => {
    const api = client({ manufacturers: page([{ tozar: TOYOTA, variants: 3 }], 60) });
    await openPanel(api);
    expect(await screen.findByText('1–1 of 60')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.browse).toHaveBeenLastCalledWith(PROJECT, 'manufacturers', '?limit=25&offset=25'));
  });

  it('sends the filters as the route parameters and refuses a bad year range', async () => {
    const api = client();
    await openPanel(api);
    await screen.findByRole('button', { name: TOYOTA });
    fireEvent.change(screen.getByLabelText('Segment'), { target: { value: 'commercial' } });
    fireEvent.change(screen.getByLabelText('From year'), { target: { value: '2020' } });
    fireEvent.change(screen.getByLabelText('Fuel'), { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(api.browse).toHaveBeenLastCalledWith(
      PROJECT, 'manufacturers', '?segment=commercial&year_from=2020&delek_cd=1&limit=25&offset=0'));
    fireEvent.change(screen.getByLabelText('To year'), { target: { value: '2019' } });
    expect((screen.getByRole('button', { name: 'Apply filters' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('adds the selection to a new plan with exactly the plan route\'s edit', async () => {
    const api = client();
    await openPanel(api);
    const box = await screen.findByRole('checkbox', { name: `Select ${TOYOTA}` });
    expect((screen.getByRole('checkbox', { name: `Select ${UNMAPPED}` }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Add to plan (1)' }));
    await waitFor(() => expect(api.createPlan).toHaveBeenCalledWith(CONVERSATION, {
      units: ['toyota'], model_year_from: null, model_year_to: null, max_items: 100, batch_size: 10 }));
    expect(api.revisePlan).not.toHaveBeenCalled();
    expect((await screen.findByRole('status')).textContent).toContain('nothing was started here');
  });

  it('revises the open plan, naming its head', async () => {
    const api = client({}, { openPlan: vi.fn(async () => ({ work_scope: stateBody() })) });
    await openPanel(api);
    fireEvent.click(await screen.findByRole('checkbox', { name: `Select ${MAZDA}` }));
    fireEvent.click(screen.getByRole('button', { name: 'Add to plan (1)' }));
    await waitFor(() => expect(api.revisePlan).toHaveBeenCalledWith(PLAN, { revision: 1, digest: DIGEST }, {
      // No year range selected: the plan keeps its own range.
      units: ['toyota', 'lexus', 'mazda'], model_year_from: 2018, model_year_to: null, max_items: 800,
      batch_size: 10 }));
  });

  it('reads the plan again after a write and tells the page', async () => {
    const onPlanChanged = vi.fn();
    const api = client();
    await openPanel(api, CONVERSATION, { onPlanChanged });
    fireEvent.click(await screen.findByRole('checkbox', { name: `Select ${TOYOTA}` }));
    fireEvent.click(screen.getByRole('button', { name: 'Add to plan (1)' }));
    await waitFor(() => expect(onPlanChanged).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.openPlan).toHaveBeenCalledTimes(2));
    expect((screen.getByRole('checkbox', { name: `Select ${TOYOTA}` }) as HTMLInputElement).checked).toBe(false);
  });

  it('writes nothing when the stated year range differs from the open plan\'s', async () => {
    const api = client({}, { openPlan: vi.fn(async () => ({ work_scope: stateBody() })) });
    await openPanel(api);
    await screen.findByRole('button', { name: TOYOTA });
    fireEvent.change(screen.getByLabelText('From year'), { target: { value: '2015' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: `Select ${MAZDA}` }));
    fireEvent.click(screen.getByRole('button', { name: 'Add to plan (1)' }));
    expect((await screen.findByRole('status')).textContent).toContain('one model-year range');
    expect(api.revisePlan).not.toHaveBeenCalled();
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  it('offers no add while segment, fuel or body narrows the view', async () => {
    const api = client();
    await openPanel(api);
    await screen.findByRole('button', { name: TOYOTA });
    for (const [label, value] of [['Segment', 'private'], ['Fuel', '1'], ['Body', 'פנאי-שטח']]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
      fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
      fireEvent.click(await screen.findByRole('checkbox', { name: `Select ${TOYOTA}` }));
      expect((screen.getByRole('button', { name: /Add to plan/ }) as HTMLButtonElement).disabled).toBe(true);
      expect(screen.getByText(/clear the segment, fuel and body filters/)).toBeTruthy();
      fireEvent.click(screen.getByRole('checkbox', { name: `Select ${TOYOTA}` }));
      fireEvent.change(screen.getByLabelText(label), { target: { value: '' } });
    }
    expect(api.createPlan).not.toHaveBeenCalled();
  });

  it('drops the selection and the plan when the conversation changes', async () => {
    const other = '2f90f4ce-7844-4031-91d6-b74e40e1884e';
    const api = client({}, { openPlan: vi.fn(async (id: string) => ({ work_scope: id === CONVERSATION ? stateBody() : null })) });
    const { rerender } = await openPanel(api);
    fireEvent.click(await screen.findByRole('checkbox', { name: `Select ${MAZDA}` }));
    rerender(<CatalogBrowserPanel projectId={PROJECT} conversationId={other} planWrites client={api} />);
    await waitFor(() => expect(api.openPlan).toHaveBeenLastCalledWith(other));
    const box = await screen.findByRole('checkbox', { name: `Select ${MAZDA}` });
    expect((box as HTMLInputElement).checked).toBe(false);
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Add to plan (1)' }));
    // The other conversation has no plan: a new plan there, never a revision of the first one's.
    await waitFor(() => expect(api.createPlan).toHaveBeenCalledWith(other, expect.objectContaining({ units: ['mazda'] })));
    expect(api.revisePlan).not.toHaveBeenCalled();
  });

  it('shows a model or year the register does not state, without opening it', async () => {
    const api = client({ models: page([{ kinuy_mishari: null, variants: 2, year_min: null, year_max: null }]),
                         years: page([{ shnat_yitzur: null, variants: 1 }]) });
    await openPanel(api);
    fireEvent.click(await screen.findByRole('button', { name: TOYOTA }));
    expect(await screen.findByText('(not stated)')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '(not stated)' })).toBeNull();
    expect(screen.getByText('2 variants, —–—')).toBeTruthy();
  });

  it('offers no plan action without a conversation or while plan writes are off', async () => {
    await openPanel(client(), null);
    await screen.findByRole('button', { name: TOYOTA });
    expect(screen.queryByRole('checkbox')).toBeNull();
    cleanup();
    const pageOff = client();
    await openPanel(pageOff, CONVERSATION, { planWrites: false });
    await screen.findByRole('button', { name: TOYOTA });
    expect(screen.queryByRole('checkbox')).toBeNull();
    expect(pageOff.openPlan).not.toHaveBeenCalled();
    cleanup();
    const off = client({}, { capabilities: vi.fn(async () => ({ ...CAPABILITIES, available: false })) });
    await openPanel(off);
    await waitFor(() => expect(off.capabilities).toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: /Add to plan/ })).toBeNull();
  });
});

describe('api client', () => {
  it('reads the browser with a GET and nothing else', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(page([])), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await import('../lib/api');
    await api.catalogBrowser(PROJECT, 'models', '?tozar=x');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/gateway/projects/${PROJECT}/catalog/browser/models?tozar=x`);
    expect(init.method).toBeUndefined();
    expect(init.body).toBeUndefined();
    vi.unstubAllGlobals();
  });
});

describe('gateway', () => {
  it('proxies the five reads as reads, and nothing else', () => {
    for (const level of ['manufacturers', 'models', 'years', 'variants', 'facets']) {
      const path = `/projects/${PROJECT}/catalog/browser/${level}`;
      expect(isGatewayRequestAllowed('GET', path)).toBe(true);
      expect(isGatewayRequestAllowed('POST', path)).toBe(false);
      expect(isRunCreationRequest('GET', path)).toBe(false);
    }
    expect(isGatewayRequestAllowed('GET', `/projects/${PROJECT}/catalog/browser/everything`)).toBe(false);
    expect(isGatewayRequestAllowed('GET', '/projects/not-a-uuid/catalog/browser/models')).toBe(false);
  });
});
