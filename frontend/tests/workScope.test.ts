/**
 * The Mapping Plan's client contract: parse what the server says, never trust
 * it; turn a draft into exactly one edit; put exactly the right request on the
 * wire; and let the gateway proxy exactly the Mapping Plan's five routes.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/supabaseClient', () => ({
  getCurrentAccessToken: vi.fn(() => Promise.resolve('access-token')),
}));

import { api } from '../lib/api';
import { isGatewayRequestAllowed, isRunCreationRequest } from '../lib/server/gatewayPolicy';
import {
  WORK_SCOPE_NOTE_COPY,
  addUnit,
  coverageLabel,
  draftEdit,
  draftFromPlan,
  draftMatchesPlan,
  emptyDraft,
  moveUnit,
  parseCapabilities,
  parseDirectory,
  parseBatchStart,
  parseOpenWorkScope,
  parsePauseResult,
  parseProgress,
  parseWorkScopeMutation,
  parseWorkScopeState,
  removeUnit,
} from '../lib/workScope';

import {
  BATCH_ONE, BATCH_RUN, BATCH_TWO, CAPABILITIES, CONVERSATION, DIGEST, PLAN, PROJECT, progressBody,
  stateBody,
} from './fixtures/workScope';

const LIMITS = parseCapabilities(CAPABILITIES)!.limits;

describe('parsing what the server said', () => {
  it('reads the capability answer and never unlocks a control by omission', () => {
    const parsed = parseCapabilities(CAPABILITIES)!;
    expect(parsed.available).toBe(true);
    expect(parsed.limits.maxBatchSize).toBe(20);
    expect(parsed.limits.defaultBatchSize).toBe(10);
    expect(parsed.canPrepare).toBe(false);
    expect(parseCapabilities({ ...CAPABILITIES, can_prepare: 'yes' })!.canPrepare).toBe(false);
    expect(parseCapabilities({ ...CAPABILITIES, limits: {} })).toBeUndefined();
    expect(parseCapabilities({ ...CAPABILITIES, available: 'true' })).toBeUndefined();
    expect(parseCapabilities(null)).toBeUndefined();
  });

  it('reads a plan state and refuses one that disagrees with itself', () => {
    const state = parseWorkScopeState(stateBody())!;
    expect(state.plan.units).toEqual(['toyota', 'lexus']);
    expect(state.plan.modelYearFrom).toBe(2018);
    expect(state.plan.modelYearTo).toBeNull();
    expect(state.head.instruction).toBe('Map Toyota and Lexus');
    // The head the state names must be the head it carries.
    expect(parseWorkScopeState(stateBody({ revision: 2 }))).toBeUndefined();
    expect(parseWorkScopeState(stateBody({ digest: 'b'.repeat(64) }))).toBeUndefined();
    // A required field of the wrong type makes the whole state unreadable.
    for (const plan of [{ ...stateBody().plan, units: 'toyota' }, { ...stateBody().plan, units: [] },
                        { ...stateBody().plan, max_items: '800' }, { ...stateBody().plan, contract: 'x' },
                        { ...stateBody().plan, units: ['Toyota'] }]) {
      expect(parseWorkScopeState(stateBody({ plan }))).toBeUndefined();
    }
  });

  it('keeps notes only from the closed vocabulary, redacted and bounded', () => {
    const state = parseWorkScopeState(stateBody({
      head: {
        ...stateBody().head,
        notes: [
          { code: 'WORK_SCOPE_NOTE_UNRECOGNIZED', terms: ['polestar', 'sk-' + 'a'.repeat(40)] },
          { code: 'SOMETHING_ELSE', terms: ['x'] },
          { code: 'WORK_SCOPE_NOTE_COVERAGE_UNKNOWN', units: ['mazda', 'Not A Key'] },
        ],
      },
    }))!;
    expect(state.head.notes.map((note) => note.code)).toEqual([
      'WORK_SCOPE_NOTE_UNRECOGNIZED', 'WORK_SCOPE_NOTE_COVERAGE_UNKNOWN']);
    expect(state.head.notes[0].terms[0]).toBe('polestar');
    expect(state.head.notes[0].terms.join(' ')).not.toContain('sk-aaaaaaaa');
    // A malformed unit key list is dropped whole rather than half-read.
    expect(state.head.notes[1].units).toEqual([]);
    for (const code of Object.keys(WORK_SCOPE_NOTE_COPY)) {
      expect(code).toMatch(/^WORK_SCOPE_NOTE_[A-Z_]+$/);
    }
  });

  it('tells "no plan yet" apart from "unreadable"', () => {
    expect(parseOpenWorkScope({ work_scope: null })).toBeNull();
    expect(parseOpenWorkScope({})).toBeUndefined();
    expect(parseOpenWorkScope({ work_scope: stateBody() })!.id).toBe(PLAN);
    expect(parseWorkScopeMutation({ applied: false, notes: [{ code: 'WORK_SCOPE_NOTE_NO_CHANGE' }],
                                    work_scope: stateBody() })!.applied).toBe(false);
    expect(parseWorkScopeMutation({ applied: 'no', work_scope: stateBody() })).toBeUndefined();
  });

  it('shows coverage exactly as stated, and never a fabricated zero', () => {
    const directory = parseDirectory({
      directory_version: 'milo-manufacturer-directory/1',
      origins: [{ key: 'japan', label: 'Japan' }],
      entries: [
        { key: 'toyota', name: 'Toyota', name_he: 'טויוטה', origin: 'japan', register_marque: 'טויוטה',
          register_marque_verified: true, coverage: { state: 'known', canonical_variants: 0 } },
        { key: 'mazda', name: 'Mazda', name_he: 'מאזדה', origin: 'japan', register_marque: null,
          register_marque_verified: false, coverage: { state: 'unverifiable', canonical_variants: 7 } },
        { key: 'honda', name: 'Honda', origin: 'japan', register_marque_verified: false,
          coverage: { state: 'something', canonical_variants: 3 } },
        { key: 'Bad Key', name: 'x', origin: 'japan' },
      ],
      coverage: { available: true, catalog_variants: 0, attributed_variants: 0 },
    })!;
    expect(directory.entries.map((entry) => entry.key)).toEqual(['toyota', 'mazda', 'honda']);
    const [toyota, mazda, honda] = directory.entries;
    expect(coverageLabel(toyota)).toBe('Not mapped yet');
    // A count beside a state that is not `known` is not a fact.
    expect(mazda.canonicalVariants).toBeNull();
    expect(coverageLabel(mazda)).toBe('Coverage unknown — register spelling not verified');
    expect(coverageLabel(honda)).toBe('Coverage unavailable');
    expect(coverageLabel({ coverageState: 'known', canonicalVariants: 3 })).toBe('3 variants mapped');
    expect(directory.origins.get('japan')).toBe('Japan');
  });
});

describe('the local draft', () => {
  it('reduces to exactly one edit, or names the field that cannot be one', () => {
    const plan = parseWorkScopeState(stateBody())!.plan;
    const draft = draftFromPlan(plan);
    expect(draftMatchesPlan(draft, plan)).toBe(true);
    expect(draftEdit(draft, LIMITS)).toEqual({ edit: {
      units: ['toyota', 'lexus'], model_year_from: 2018, model_year_to: null, max_items: 800, batch_size: 10 } });
    expect(draftEdit({ ...draft, units: [] }, LIMITS)).toEqual({ problem: 'units' });
    expect(draftEdit({ ...draft, modelYearFrom: '2024', modelYearTo: '2018' }, LIMITS)).toEqual({ problem: 'years' });
    expect(draftEdit({ ...draft, modelYearFrom: '20x8' }, LIMITS)).toEqual({ problem: 'years' });
    expect(draftEdit({ ...draft, maxItems: '' }, LIMITS)).toEqual({ problem: 'maxItems' });
    expect(draftEdit({ ...draft, maxItems: '2001' }, LIMITS)).toEqual({ problem: 'maxItems' });
    expect(draftEdit({ ...draft, batchSize: 21 }, LIMITS)).toEqual({ problem: 'batchSize' });
  });

  it('orders units by position, which is their priority', () => {
    let draft = emptyDraft(LIMITS);
    expect(draft.batchSize).toBe(10);
    draft = addUnit(addUnit(addUnit(draft, 'toyota'), 'mazda'), 'toyota');
    expect(draft.units).toEqual(['toyota', 'mazda']);
    draft = moveUnit(draft, 'mazda', -1);
    expect(draft.units).toEqual(['mazda', 'toyota']);
    expect(moveUnit(draft, 'mazda', -1).units).toEqual(['mazda', 'toyota']);
    expect(removeUnit(draft, 'mazda').units).toEqual(['toyota']);
  });
});

describe('what the API client puts on the wire', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}', {
      status: 200, headers: { 'content-type': 'application/json' } })));
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it('reads with bodiless GETs', async () => {
    await api.workScopeCapabilities(PROJECT);
    await api.workScopeDirectory(PROJECT);
    await api.openWorkScope(CONVERSATION);
    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET', init?.body])).toEqual([
      [`/api/gateway/projects/${PROJECT}/work-scope/capabilities`, 'GET', undefined],
      [`/api/gateway/projects/${PROJECT}/work-scope/directory`, 'GET', undefined],
      [`/api/gateway/conversations/${CONVERSATION}/work-scopes/open`, 'GET', undefined],
    ]);
  });

  it('writes EITHER the words OR the edit, and a revision names its head', async () => {
    await api.createWorkScope(CONVERSATION, { instruction: 'Map Toyota' });
    const edit = { units: ['toyota'], model_year_from: null, model_year_to: null, max_items: 10, batch_size: 10 };
    await api.reviseWorkScope(PLAN, { revision: 3, digest: DIGEST }, { edit });
    const [[createUrl, createInit], [reviseUrl, reviseInit]] = fetchMock.mock.calls;
    expect(createUrl).toBe(`/api/gateway/conversations/${CONVERSATION}/work-scopes`);
    expect(createInit.method).toBe('POST');
    expect(JSON.parse(createInit.body)).toEqual({ instruction: 'Map Toyota' });
    expect(reviseUrl).toBe(`/api/gateway/work-scopes/${PLAN}/revisions`);
    expect(JSON.parse(reviseInit.body)).toEqual({ expected_revision: 3, expected_digest: DIGEST, edit });
  });

  it('adds no catalog-named method', () => {
    const names = Object.keys(api).filter((name) => name.toLowerCase().includes('catalog'));
    expect(names.sort()).toEqual(['catalogCanonical', 'catalogReviewCandidates']);
  });
});

describe('the gateway proxies exactly the Mapping Plan routes', () => {
  afterEach(() => { delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES; });

  it('always proxies the three reads, GET only', () => {
    for (const path of [`/projects/${PROJECT}/work-scope/capabilities`,
                        `/projects/${PROJECT}/work-scope/directory`,
                        `/conversations/${CONVERSATION}/work-scopes/open`]) {
      expect(isGatewayRequestAllowed('GET', path)).toBe(true);
      expect(isGatewayRequestAllowed('POST', path)).toBe(false);
    }
  });

  it('holds the two writes behind the execution stage', () => {
    const create = `/conversations/${CONVERSATION}/work-scopes`;
    const revise = `/work-scopes/${PLAN}/revisions`;
    expect(isGatewayRequestAllowed('POST', create)).toBe(false);
    expect(isGatewayRequestAllowed('POST', revise)).toBe(false);
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    expect(isGatewayRequestAllowed('POST', create)).toBe(true);
    expect(isGatewayRequestAllowed('POST', revise)).toBe(true);
    // Neither is run creation, whatever the stage.
    expect(isRunCreationRequest('POST', create)).toBe(false);
  });

  it('is not a work-scope prefix proxy', () => {
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    for (const [method, path] of [
      ['GET', `/work-scopes/${PLAN}`],
      ['POST', `/work-scopes/${PLAN}/prepare`],
      ['POST', `/work-scopes/${PLAN}/batches`],
      ['GET', `/projects/${PROJECT}/work-scope`],
      ['POST', `/projects/${PROJECT}/work-scope/directory`],
      ['GET', `/conversations/not-a-uuid/work-scopes/open`],
      ['DELETE', `/work-scopes/${PLAN}/revisions`],
    ] as const) {
      expect(isGatewayRequestAllowed(method, path), `${method} ${path}`).toBe(false);
    }
  });
});

// ---------------------------------------------------------------------------
// Batches (scoped catalog PR3)
// ---------------------------------------------------------------------------

describe('reading a plan’s batch progress', () => {
  it('reads the server’s progress field by field', () => {
    const progress = parseProgress(progressBody());
    expect(progress).toBeDefined();
    expect(progress?.status).toBe('ready');
    expect(progress?.preparation?.items).toEqual({
      total: 25, promoted: 3, refused: 1, unresolved: 6, completed: 10, remaining: 15 });
    expect(progress?.preparation?.batches).toEqual({
      total: 3, settled: 1, active: 0, interrupted: 0, remaining: 2 });
    expect(progress?.preparation?.units.map((unit) => [unit.unitKey, unit.progress])).toEqual([
      ['toyota', 'in_progress'], ['lexus', 'not_queued']]);
    expect(progress?.preparation?.next?.batchId).toBe(BATCH_TWO);
    expect(progress?.preparation?.recent[0]).toMatchObject({
      batchId: BATCH_ONE, state: 'completed', runId: BATCH_RUN, promoted: 3, refused: 1, unresolved: 6 });
    expect(progress?.controls.start).toMatchObject({ available: true, retry: false, relaunch: false });
    expect(progress?.controls.start.batch?.batchNumber).toBe(2);
  });

  it('refuses the whole answer rather than render a count it cannot read', () => {
    const body = progressBody();
    for (const broken of [
      { ...body, digest: 'not-a-digest' },
      { ...body, status: 'mystery' },
      { ...body, paused: 'no' },
      { ...body, live: { batch_id: BATCH_ONE } },
      { ...body, preparation: { ...body.preparation, items: { ...body.preparation.items, promoted: -1 } } },
      { ...body, preparation: { ...body.preparation, units: [{ unit_key: 'toyota' }] } },
      { ...body, preparation: { ...body.preparation, recent: [{ batch_id: BATCH_ONE, state: 'done' }] } },
      { ...body, controls: { ...body.controls, start: { ...body.controls.start, batch: { batch_id: 'x' } } } },
    ]) {
      expect(parseProgress(broken)).toBeUndefined();
    }
  });

  it('never unlocks a control by omission', () => {
    const base = progressBody();
    // A start that answers anything but a boolean is not an answer at all.
    expect(parseProgress(progressBody({ controls: { ...base.controls,
      start: { ...base.controls.start, available: 'yes' } } }))).toBeUndefined();
    const body = progressBody({ controls: { start: { available: false, batch: null }, pause: {}, resume: {},
                                            cancel: { available: true, run_id: 'not-a-uuid' } } });
    const progress = parseProgress(body);
    expect(progress).toBeDefined();
    expect(progress?.controls.start.available).toBe(false);
    expect(progress?.controls.pause.available).toBe(false);
    expect(progress?.controls.resume.available).toBe(false);
    expect(progress?.controls.cancel.available).toBe(false);
    // A start that is "available" with no batch to start is not a start.
    const empty = parseProgress(progressBody({ controls: { ...progressBody().controls,
      start: { available: true, batch: null, retry: false, relaunch: false } } }));
    expect(empty?.controls.start.available).toBe(false);
  });

  it('keeps "not prepared" apart from "unreadable"', () => {
    const progress = parseProgress(progressBody({ preparation: null, status: 'not_prepared',
      controls: { ...progressBody().controls, start: { available: false, blocked_by: 'not_prepared',
                                                        batch: null, retry: false, relaunch: false } } }));
    expect(progress?.preparation).toBeUndefined();
    expect(progress?.controls.start.blockedBy).toBe('not_prepared');
  });

  it('reads a start and a pause answer', () => {
    expect(parseBatchStart({ run_id: BATCH_RUN, status: 'queued', work_scope_id: PLAN,
                             batch_id: BATCH_TWO, attempt: 1, created: true })).toEqual({
      runId: BATCH_RUN, status: 'queued', batchId: BATCH_TWO, attempt: 1, created: true });
    expect(parseBatchStart({ run_id: BATCH_RUN, status: 'queued', batch_id: BATCH_TWO, attempt: 1 }))
      .toBeUndefined();
    const paused = parsePauseResult({ changed: true, paused: true,
                                      progress: progressBody({ paused: true }) });
    expect(paused?.paused).toBe(true);
    expect(paused?.progress.paused).toBe(true);
    expect(parsePauseResult({ changed: true, paused: true, progress: {} })).toBeUndefined();
  });
});

describe('what the batch client puts on the wire', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response('{}', {
      status: 200, headers: { 'content-type': 'application/json' } })));
    global.fetch = fetchMock as unknown as typeof fetch;
  });

  it('reads progress with a bodiless GET, and a start names its head and its batch', async () => {
    await api.workScopeProgress(PLAN);
    await api.startWorkScopeBatch(PLAN, { revision: 3, digest: DIGEST }, BATCH_TWO, 'ui-key-00000001');
    await api.pauseWorkScope(PLAN);
    await api.resumeWorkScope(PLAN);
    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init?.method ?? 'GET']);
    expect(calls).toEqual([
      [`/api/gateway/work-scopes/${PLAN}/progress`, 'GET'],
      [`/api/gateway/work-scopes/${PLAN}/runs`, 'POST'],
      [`/api/gateway/work-scopes/${PLAN}/pause`, 'POST'],
      [`/api/gateway/work-scopes/${PLAN}/resume`, 'POST'],
    ]);
    expect(fetchMock.mock.calls[0][1]?.body).toBeUndefined();
    // The start is a set of PRECONDITIONS: which head, which batch, which key.
    // No count, no candidate, no snapshot and no scope travels from the browser.
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      expected_revision: 3, expected_digest: DIGEST, batch_id: BATCH_TWO,
      idempotency_key: 'ui-key-00000001' });
  });
});

describe('the gateway proxies exactly the batch routes', () => {
  afterEach(() => {
    delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    delete process.env.GATEWAY_ALLOW_RUN_START_ROUTES;
  });

  it('always proxies the progress read, GET only', () => {
    expect(isGatewayRequestAllowed('GET', `/work-scopes/${PLAN}/progress`)).toBe(true);
    expect(isGatewayRequestAllowed('POST', `/work-scopes/${PLAN}/progress`)).toBe(false);
  });

  it('holds start, pause and resume behind the execution stage, and a start IS run creation', () => {
    const start = `/work-scopes/${PLAN}/runs`;
    for (const path of [start, `/work-scopes/${PLAN}/pause`, `/work-scopes/${PLAN}/resume`]) {
      expect(isGatewayRequestAllowed('POST', path), path).toBe(false);
    }
    expect(isRunCreationRequest('POST', start)).toBe(true);
    expect(isRunCreationRequest('POST', `/work-scopes/${PLAN}/pause`)).toBe(false);
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    // Plan authoring (Stage P): pause and resume open, a START still refused --
    // starting is the separate, last-opened run-start permission.
    for (const path of [`/work-scopes/${PLAN}/pause`, `/work-scopes/${PLAN}/resume`]) {
      expect(isGatewayRequestAllowed('POST', path), path).toBe(true);
    }
    expect(isGatewayRequestAllowed('POST', start)).toBe(false);
    expect(isRunCreationRequest('POST', start)).toBe(true);
    process.env.GATEWAY_ALLOW_RUN_START_ROUTES = 'true';
    expect(isGatewayRequestAllowed('POST', start)).toBe(true);
    expect(isRunCreationRequest('POST', start)).toBe(false);
    for (const [method, path] of [
      ['GET', start], ['POST', `/work-scopes/${PLAN}/runs/next`], ['POST', `/work-scopes/${PLAN}/stop`],
      ['POST', `/work-scopes/not-a-uuid/runs`], ['DELETE', `/work-scopes/${PLAN}/pause`],
    ] as const) {
      expect(isGatewayRequestAllowed(method, path), `${method} ${path}`).toBe(false);
    }
  });
});
