/**
 * CODE-2 — the typed catalog vocabulary and the bounded catalog projection.
 *
 * The two catalog events were already RETAINED and rendered as raw developer
 * telemetry (that is CAT-12, and F5 tested it deliberately). What they had no
 * part in was recognition: they were in neither vocabulary set, so there was no
 * projection, no operator-facing status and nothing to act on. That is CAT-13.
 *
 * The rule this file exists to hold is the narrow one. Recognising a catalog
 * event must buy it exactly TWO things — its own catalog slice and the raw
 * event stream it already had — and nothing else. Not an agent, not a task, not
 * a lifecycle phase, not progress, not spend. A `catalog_promotion_refused`
 * event carrying `agent`, `phase`, `progress` and `payload.tokens` is a payload
 * asserting things the trusted server code that emits it never states, and the
 * event contract it really travels on (`PromotionAttempt.as_event()`) has no
 * such keys at all.
 */

import { describe, expect, it } from 'vitest';
import {
  CATALOG_EVENT_TYPES,
  SWARM_V2_EVENT_TYPES,
  V1_EVENT_TYPES,
  isKnownEventType,
  ownsAgentProjection,
  ownsCatalogProjection,
  ownsSpendTelemetry,
  ownsV1Projection,
} from '../lib/eventVocabulary';
import {
  CATALOG_REFUSAL_LABELS,
  MAX_CANONICAL_FIELDS,
  MAX_CATALOG_ACTIONS,
  MAX_CATALOG_KEY_CHARS,
  UNKNOWN_CATALOG_REFUSAL,
  UNKNOWN_CATALOG_REFUSAL_LABEL,
  catalogRefusalLabel,
  initialCatalogState,
  reduceCatalogEvent,
} from '../lib/catalogStatus';
import { reduceRunEvent, reconstructRun, initialWorkspaceState } from '../lib/runReducer';
import { reduceSwarmEvents } from '../lib/swarmReducer';
import { initialSwarmRunState } from '../lib/swarmTypes';
import { selectCatalogStatus } from '../lib/swarmViewModel';
import { API_KEY_SENTINEL, ALL_SECRET_SENTINELS, SECRET_FRAGMENTS } from './secretSentinels';
import { Run, RunEvent } from '../lib/types';

const PROMOTED = 'catalog_variant_promoted';
const REFUSED = 'catalog_promotion_refused';

let sequence = 0;
function event(eventType: string, payload: unknown = {}, overrides: Partial<RunEvent> = {}): RunEvent {
  sequence += 1;
  return {
    id: String(sequence),
    run_id: 'a1b2c3d4-1111-4111-8111-000000000001',
    event_type: eventType,
    message: eventType,
    payload,
    ...overrides,
  } as RunEvent;
}

/** The durable run row the reconstruction starts from. */
function run(status: string): Run {
  return {
    id: 'a1b2c3d4-1111-4111-8111-000000000001',
    conversation_id: 'ffffffff-1111-4111-8111-000000000001',
    status,
  } as Run;
}

function promoted(candidateKey: string, extra: Record<string, unknown> = {}): RunEvent {
  return event(PROMOTED, {
    candidate_key: candidateKey,
    promoted: true,
    canonical_key: `${candidateKey}::canonical`,
    promoted_fields: ['engine_capacity'],
    unsupported_fields: [],
    replayed: false,
    ...extra,
  });
}

function refused(candidateKey: string, reason: string): RunEvent {
  return event(REFUSED, { candidate_key: candidateKey, promoted: false, reason });
}

// ---------------------------------------------------------------------------
// vocabulary
// ---------------------------------------------------------------------------

describe('catalog event vocabulary', () => {
  it('recognises exactly the two types the worker emits', () => {
    expect([...CATALOG_EVENT_TYPES].sort()).toEqual([REFUSED, PROMOTED].sort());
    expect(isKnownEventType(PROMOTED)).toBe(true);
    expect(isKnownEventType(REFUSED)).toBe(true);
  });

  it('never grants either type the V1 projection', () => {
    for (const type of [PROMOTED, REFUSED]) {
      expect(V1_EVENT_TYPES.has(type)).toBe(false);
      expect(ownsV1Projection(type)).toBe(false);
      expect(ownsAgentProjection(type)).toBe(false);
      expect(ownsSpendTelemetry(type)).toBe(false);
    }
  });

  it('keeps the catalog set disjoint from both existing sets', () => {
    for (const type of CATALOG_EVENT_TYPES) {
      expect(V1_EVENT_TYPES.has(type)).toBe(false);
      expect(SWARM_V2_EVENT_TYPES.has(type)).toBe(false);
    }
  });

  it('matches exactly, never by substring or prefix', () => {
    for (const invented of [
      'catalog_variant_promoted_v2', 'x_catalog_variant_promoted', 'catalog_',
      'catalog_promotion', 'catalog_promotion_refused ', 'CATALOG_VARIANT_PROMOTED',
    ]) {
      expect(ownsCatalogProjection(invented), invented).toBe(false);
      expect(isKnownEventType(invented), invented).toBe(false);
    }
    expect(ownsCatalogProjection(PROMOTED)).toBe(true);
    expect(ownsCatalogProjection(REFUSED)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// the projection reads only the safe event contract
// ---------------------------------------------------------------------------

describe('catalog status projection', () => {
  it('records a promotion with its counts and canonical key', () => {
    const state = reduceCatalogEvent(initialCatalogState, promoted('mazda_3_2021'), '1');
    expect(state.observed).toBe(true);
    expect(state.promotedCount).toBe(1);
    expect(state.refusedCount).toBe(0);
    expect(state.lastRefusalReason).toBeUndefined();
    expect(state.actions).toHaveLength(1);
    expect(state.actions[0]).toMatchObject({
      outcome: 'promoted',
      candidateKey: 'mazda_3_2021',
      canonicalKey: 'mazda_3_2021::canonical',
      promotedFieldCount: 1,
      unsupportedFieldCount: 0,
      replayed: false,
    });
  });

  it('records a refusal without turning the run into a failure', () => {
    const state = reduceCatalogEvent(
      initialCatalogState, refused('kia_niro_2020', 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED'), '1');
    expect(state.refusedCount).toBe(1);
    expect(state.promotedCount).toBe(0);
    expect(state.lastRefusalReason)
      .toEqual({ known: true, code: 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED' });
    expect(state.actions[0].outcome).toBe('refused');
    // A refusal is an operational catalog outcome. The projection carries no
    // run-failure concept at all, which is the structural version of that rule.
    expect(Object.keys(state)).not.toContain('failed');
  });

  it('marks a replayed promotion as a replay', () => {
    const state = reduceCatalogEvent(
      initialCatalogState, promoted('mazda_3_2021', { replayed: true }), '1');
    expect(state.actions[0].replayed).toBe(true);
    expect(state.replayedCount).toBe(1);
    // A replay is still a recorded promotion, not a second canonical write.
    expect(state.promotedCount).toBe(1);
  });

  it('ignores unknown payload keys entirely', () => {
    const hostile = event(PROMOTED, {
      candidate_key: 'ok', promoted: true, canonical_key: 'ok::c',
      promoted_fields: [], unsupported_fields: [], replayed: false,
      sql_error: 'ERROR: relation "catalog_canonical_variants" does not exist',
      raw_row: { id: 1, payload: 'preserved register row' },
      model_text: 'the Commander thinks this is a Mazda',
      nested: { deep: { deeper: ['x'] } },
    });
    const state = reduceCatalogEvent(initialCatalogState, hostile, '1');
    const serialized = JSON.stringify(state);
    for (const leak of ['sql_error', 'relation', 'raw_row', 'preserved register row',
                        'model_text', 'Commander', 'nested', 'deeper']) {
      expect(serialized, leak).not.toContain(leak);
    }
  });

  it('fails safely on malformed field types instead of rendering them', () => {
    const malformed = event(PROMOTED, {
      candidate_key: { not: 'a string' },
      promoted: 'yes',
      canonical_key: 42,
      promoted_fields: 'engine_capacity',
      unsupported_fields: { 0: 'x' },
      replayed: 'true',
    });
    const state = reduceCatalogEvent(initialCatalogState, malformed, '1');
    expect(state.actions).toHaveLength(1);
    const action = state.actions[0];
    expect(action.candidateKey).toBeUndefined();
    expect(action.canonicalKey).toBeUndefined();
    // A malformed list is not "zero fields" — that would be a false count.
    // It is DROPPED, and the surface says the count is unavailable.
    expect(action.promotedFieldCount).toBeUndefined();
    expect(action.unsupportedFieldCount).toBeUndefined();
    expect(action.replayed).toBe(false);
    // The event TYPE decides the outcome, not the payload: the type is what
    // the trusted emitter chose, `promoted: 'yes'` is just data travelling
    // beside it. So this is still a promotion, with every malformed field
    // dropped rather than coerced.
    expect(state.observed).toBe(true);
    expect(action.outcome).toBe('promoted');
    expect(state.promotedCount).toBe(1);
  });

  it('treats a non-object payload as carrying nothing', () => {
    for (const payload of [null, undefined, 'a string', 42, ['a', 'list']]) {
      const state = reduceCatalogEvent(initialCatalogState, event(PROMOTED, payload), '1');
      expect(state.observed).toBe(true);
      expect(state.actions[0].candidateKey).toBeUndefined();
    }
  });

  it('bounds every retained string', () => {
    const long = 'k'.repeat(MAX_CATALOG_KEY_CHARS * 5);
    const state = reduceCatalogEvent(
      initialCatalogState,
      event(PROMOTED, { candidate_key: long, promoted: true, canonical_key: long,
                        promoted_fields: [], unsupported_fields: [], replayed: false }),
      '1');
    expect(state.actions[0].candidateKey!.length).toBeLessThanOrEqual(MAX_CATALOG_KEY_CHARS);
    expect(state.actions[0].canonicalKey!.length).toBeLessThanOrEqual(MAX_CATALOG_KEY_CHARS);
  });

  it('refuses to report an over-contract list length as telemetry', () => {
    // The backend bounds a canonical promotion to MAX_CANONICAL_FIELDS. A
    // longer list cannot have come from `PromotionAttempt.as_event()`, so its
    // length is not a number this surface may present as a field count.
    const many = Array.from({ length: 5_000 }, (_, i) => `field_${i}`);
    const state = reduceCatalogEvent(
      initialCatalogState,
      event(PROMOTED, { candidate_key: 'k', promoted: true, canonical_key: 'c',
                        promoted_fields: many, unsupported_fields: many, replayed: false }),
      '1');
    expect(state.actions[0].promotedFieldCount).toBeUndefined();
    expect(state.actions[0].unsupportedFieldCount).toBeUndefined();
    // Counts, not contents: the serialized slice cannot grow with the list.
    expect(JSON.stringify(state).length).toBeLessThan(1_000);
  });

  it('keeps the recent-action list to a fixed-size ring', () => {
    let state = initialCatalogState;
    for (let i = 0; i < MAX_CATALOG_ACTIONS * 3; i += 1) {
      state = reduceCatalogEvent(state, promoted(`candidate_${i}`), String(i + 1));
    }
    expect(state.actions).toHaveLength(MAX_CATALOG_ACTIONS);
    expect(state.promotedCount).toBe(MAX_CATALOG_ACTIONS * 3);
    // The ring keeps the MOST RECENT actions.
    expect(state.actions[state.actions.length - 1].candidateKey)
      .toBe(`candidate_${MAX_CATALOG_ACTIONS * 3 - 1}`);
  });
});

// ---------------------------------------------------------------------------
// refusal codes: a closed allowlist, or a static fallback
// ---------------------------------------------------------------------------

describe('refusal codes', () => {
  it('labels every code the backend can actually emit', () => {
    for (const code of [
      'CATALOG_PROMOTION_CANDIDATE_NOT_READY', 'CATALOG_PROMOTION_SOURCE_NOT_EVIDENCE',
      'CATALOG_PROMOTION_FIELD_UNSUPPORTED', 'CATALOG_PROMOTION_FIELD_UNEXPECTED',
      'CATALOG_PROMOTION_VALUE_MISMATCH', 'CATALOG_PROMOTION_LINK_CANDIDATE_MISMATCH',
      'CATALOG_PROMOTION_LINK_UNVERIFIED', 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED',
      'CATALOG_PROMOTION_REFUSED', 'CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE',
      'CATALOG_PROMOTION_SNAPSHOT_UNUSABLE',
    ]) {
      expect(CATALOG_REFUSAL_LABELS[code], code).toBeTruthy();
      expect(catalogRefusalLabel(code)).toBe(CATALOG_REFUSAL_LABELS[code]);
    }
  });

  it('renders one static fallback for anything outside the allowlist', () => {
    for (const code of ['CATALOG_PROMOTION_INVENTED', '<img src=x onerror=1>',
                        'DROP TABLE catalog_canonical_variants', '', API_KEY_SENTINEL]) {
      expect(catalogRefusalLabel(code)).toBe(UNKNOWN_CATALOG_REFUSAL_LABEL);
    }
  });

  it('never stores an unrecognised reason code, but still records the refusal', () => {
    const state = reduceCatalogEvent(
      initialCatalogState, refused('k', 'ERROR: duplicate key value violates unique constraint'), '1');
    // The untrusted text is nowhere in the slice — but the slice still knows
    // that the LATEST refusal had a reason it cannot name.
    expect(state.lastRefusalReason).toEqual(UNKNOWN_CATALOG_REFUSAL);
    expect(state.lastRefusalReason).not.toHaveProperty('code');
    expect(JSON.stringify(state)).not.toContain('duplicate key');
    expect(state.refusedCount).toBe(1);
  });

  it('carries no secret or evidence sentinel through any field', () => {
    let state = initialCatalogState;
    let id = 0;
    for (const sentinel of ALL_SECRET_SENTINELS) {
      id += 1;
      state = reduceCatalogEvent(state, event(PROMOTED, {
        candidate_key: sentinel, promoted: true, canonical_key: sentinel,
        promoted_fields: [sentinel], unsupported_fields: [sentinel],
        replayed: false, reason: sentinel,
      }), String(id));
    }
    const serialized = JSON.stringify(state);
    for (const fragment of SECRET_FRAGMENTS) {
      expect(serialized, fragment).not.toContain(fragment);
    }
  });
});

// ---------------------------------------------------------------------------
// CORRECTION 1: "Latest refusal" must describe the ACTUAL latest refusal
// ---------------------------------------------------------------------------

/**
 * The defect this suite exists to hold closed.
 *
 * The first implementation kept the previous reason when a later refusal had no
 * allowlisted one (`reasonCode ?? state.lastRefusalCode`). So a run that was
 * refused for `VALUE_MISMATCH` and then refused again for something this
 * release cannot name went on displaying "value mismatch" as *the latest
 * refusal* — an operator reading the summary was told the newest refusal had a
 * reason it did not have.
 *
 * The state model now distinguishes THREE conditions, and the middle one is
 * the one that was missing:
 *
 *   1. no refusal has ever been observed      -> `lastRefusalReason` absent
 *   2. the latest refusal is allowlisted      -> `{ known: true, code }`
 *   3. the latest refusal is unknown/missing/
 *      malformed                              -> `{ known: false }`
 *
 * Condition 3 is a TRUSTED SENTINEL, not the payload's value: the variant has
 * no `code` property at all, so there is nowhere for untrusted text to sit.
 */
describe('the latest refusal is the latest refusal', () => {
  const KNOWN = 'CATALOG_PROMOTION_VALUE_MISMATCH';
  const OTHER_KNOWN = 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED';

  /** A refusal whose `reason` key is whatever the caller passes — or absent. */
  function refusedWith(payload: Record<string, unknown>): RunEvent {
    return event(REFUSED, { candidate_key: 'k', promoted: false, ...payload });
  }

  function fold(events: readonly RunEvent[]) {
    return events.reduce(
      (state, e, i) => reduceCatalogEvent(state, e, String(i + 1)),
      initialCatalogState);
  }

  it('known -> unknown reports the unknown fallback, not the stale known reason', () => {
    const state = fold([refusedWith({ reason: KNOWN }),
                        refusedWith({ reason: 'CATALOG_PROMOTION_INVENTED' })]);
    expect(state.lastRefusalReason).toEqual(UNKNOWN_CATALOG_REFUSAL);
    // The exact regression: the older known code must NOT survive as "latest".
    expect(state.lastRefusalReason).not.toEqual({ known: true, code: KNOWN });
    expect(JSON.stringify(state.lastRefusalReason)).not.toContain(KNOWN);
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: state })
      .lastRefusalLabel).toBe(UNKNOWN_CATALOG_REFUSAL_LABEL);
  });

  it('known -> MISSING reason reports the unknown fallback', () => {
    const state = fold([refusedWith({ reason: KNOWN }), refusedWith({})]);
    expect(state.lastRefusalReason).toEqual(UNKNOWN_CATALOG_REFUSAL);
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: state })
      .lastRefusalLabel).toBe(UNKNOWN_CATALOG_REFUSAL_LABEL);
  });

  it.each([
    ['a number', 42],
    ['an object', { code: 'CATALOG_PROMOTION_VALUE_MISMATCH' }],
    ['an array', ['CATALOG_PROMOTION_VALUE_MISMATCH']],
    ['null', null],
    ['a boolean', true],
    ['an empty string', ''],
    ['whitespace', '   '],
  ])('known -> malformed reason (%s) reports the unknown fallback', (_label, reason) => {
    const state = fold([refusedWith({ reason: KNOWN }), refusedWith({ reason })]);
    expect(state.lastRefusalReason).toEqual(UNKNOWN_CATALOG_REFUSAL);
    expect(JSON.stringify(state.lastRefusalReason)).not.toContain(KNOWN);
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: state })
      .lastRefusalLabel).toBe(UNKNOWN_CATALOG_REFUSAL_LABEL);
  });

  it('unknown -> known reports the NEW static label', () => {
    const state = fold([refusedWith({ reason: 'CATALOG_PROMOTION_INVENTED' }),
                        refusedWith({ reason: OTHER_KNOWN })]);
    expect(state.lastRefusalReason).toEqual({ known: true, code: OTHER_KNOWN });
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: state })
      .lastRefusalLabel).toBe(CATALOG_REFUSAL_LABELS[OTHER_KNOWN]);
  });

  it('known -> other known reports the newer of the two', () => {
    const state = fold([refusedWith({ reason: KNOWN }),
                        refusedWith({ reason: OTHER_KNOWN })]);
    expect(state.lastRefusalReason).toEqual({ known: true, code: OTHER_KNOWN });
  });

  it('a later PROMOTION preserves the refusal summary — it is not a newer refusal', () => {
    const state = fold([refusedWith({ reason: KNOWN }), promoted('mazda_3_2021')]);
    expect(state.lastRefusalReason).toEqual({ known: true, code: KNOWN });
    expect(state.promotedCount).toBe(1);
    expect(state.refusedCount).toBe(1);
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: state })
      .lastRefusalLabel).toBe(CATALOG_REFUSAL_LABELS[KNOWN]);
  });

  it('a promotion after an UNKNOWN refusal preserves the unknown fallback', () => {
    const state = fold([refusedWith({ reason: 'CATALOG_PROMOTION_INVENTED' }),
                        promoted('mazda_3_2021')]);
    expect(state.lastRefusalReason).toEqual(UNKNOWN_CATALOG_REFUSAL);
  });

  it('no refusal at all omits the summary entirely', () => {
    expect(initialCatalogState.lastRefusalReason).toBeUndefined();
    const promotionsOnly = fold([promoted('a'), promoted('b')]);
    expect(promotionsOnly.lastRefusalReason).toBeUndefined();
    expect(selectCatalogStatus({ ...initialSwarmRunState, catalog: promotionsOnly })
      .lastRefusalLabel).toBeUndefined();
  });

  it('never stores an untrusted reason string in any of the three conditions', () => {
    const hostile = 'ERROR: relation "catalog_canonical_variants" does not exist';
    const state = fold([refusedWith({ reason: KNOWN }), refusedWith({ reason: hostile })]);
    const serialized = JSON.stringify(state);
    expect(serialized).not.toContain('relation');
    expect(serialized).not.toContain('does not exist');
    // And the per-action record is equally sentinel-only.
    expect(state.actions[1].reason).toEqual(UNKNOWN_CATALOG_REFUSAL);
    expect(state.actions[1].reason).not.toHaveProperty('code');
  });

  it('keeps the action ring bounded and deterministic across mixed outcomes', () => {
    const many: RunEvent[] = [];
    for (let i = 0; i < MAX_CATALOG_ACTIONS * 2; i += 1) {
      many.push(i % 2 === 0 ? refusedWith({ reason: KNOWN }) : promoted(`c_${i}`));
    }
    const state = fold(many);
    expect(state.actions).toHaveLength(MAX_CATALOG_ACTIONS);
    expect(state.refusedCount).toBe(MAX_CATALOG_ACTIONS);
    expect(state.promotedCount).toBe(MAX_CATALOG_ACTIONS);
    // The last event was a promotion, so the newest refusal reason survives.
    expect(state.lastRefusalReason).toEqual({ known: true, code: KNOWN });
    // Folding the same stream twice is identical.
    expect(fold(many)).toEqual(state);
  });
});

// ---------------------------------------------------------------------------
// CORRECTION 2: the declared list contract is enforced, not assumed
// ---------------------------------------------------------------------------

/**
 * `listLength` previously accepted ANY array and exposed its full length. That
 * contradicted this module's own claim that every field is checked for its
 * exact declared type and that collections are bounded: an array of objects
 * counted, and a 5 000-entry array reported 5 000 promoted fields as if it
 * were telemetry.
 *
 * The declared type is `list[str]`, and the backend bounds a canonical
 * promotion to `MAX_CANONICAL_FIELDS` (`backend/catalog/contracts.py`). A list
 * that breaks either rule cannot have come from
 * `PromotionAttempt.as_event()`, so its length is DROPPED rather than
 * presented — an absent count, never a false zero.
 */
describe('field-list counts', () => {
  function countsFor(payload: Record<string, unknown>) {
    const state = reduceCatalogEvent(
      initialCatalogState,
      event(PROMOTED, { candidate_key: 'k', promoted: true, canonical_key: 'c',
                        replayed: false, ...payload }),
      '1');
    return state.actions[0];
  }

  it('accepts a valid empty string array as a real zero', () => {
    const action = countsFor({ promoted_fields: [], unsupported_fields: [] });
    expect(action.promotedFieldCount).toBe(0);
    expect(action.unsupportedFieldCount).toBe(0);
  });

  it('accepts a valid non-empty string array', () => {
    const action = countsFor({
      promoted_fields: ['model_year_start', 'model_year_end', 'trim'],
      unsupported_fields: ['identity_dimensions.market'],
    });
    expect(action.promotedFieldCount).toBe(3);
    expect(action.unsupportedFieldCount).toBe(1);
  });

  it('accepts exactly MAX_CANONICAL_FIELDS entries', () => {
    const exact = Array.from({ length: MAX_CANONICAL_FIELDS }, (_, i) => `field_${i}`);
    expect(countsFor({ promoted_fields: exact }).promotedFieldCount)
      .toBe(MAX_CANONICAL_FIELDS);
  });

  it('drops one entry OVER the contract bound', () => {
    const over = Array.from({ length: MAX_CANONICAL_FIELDS + 1 }, (_, i) => `field_${i}`);
    expect(countsFor({ promoted_fields: over }).promotedFieldCount).toBeUndefined();
  });

  it.each([
    ['a string', 'model_year_start'],
    ['a number', 3],
    ['null', null],
    ['a boolean', true],
    ['an object', { 0: 'model_year_start', length: 1 }],
    ['an absent key', undefined],
  ])('drops a non-array value (%s)', (_label, value) => {
    expect(countsFor({ promoted_fields: value }).promotedFieldCount).toBeUndefined();
  });

  it.each([
    ['an object member', ['model_year_start', { field: 'trim' }]],
    ['a number member', ['model_year_start', 7]],
    ['a null member', ['model_year_start', null]],
    ['an undefined member', ['model_year_start', undefined]],
    ['a nested array member', ['model_year_start', ['trim']]],
    ['only non-string members', [1, 2, 3]],
  ])('drops an array containing %s', (_label, value) => {
    expect(countsFor({ promoted_fields: value }).promotedFieldCount).toBeUndefined();
  });

  it('judges each list independently', () => {
    const action = countsFor({
      promoted_fields: ['model_year_start'],
      unsupported_fields: [{ not: 'a string' }],
    });
    expect(action.promotedFieldCount).toBe(1);
    expect(action.unsupportedFieldCount).toBeUndefined();
  });

  it('still stores only counts — never a field name', () => {
    const action = countsFor({
      promoted_fields: ['model_year_start', 'identity_dimensions.market'],
      unsupported_fields: ['identity_dimensions.generation'],
    });
    const serialized = JSON.stringify(action);
    for (const name of ['model_year_start', 'identity_dimensions.market',
                        'identity_dimensions.generation']) {
      expect(serialized, name).not.toContain(name);
    }
    expect(action.promotedFieldCount).toBe(2);
  });
});

// ---------------------------------------------------------------------------
// determinism under the existing event-id rules
// ---------------------------------------------------------------------------

describe('determinism', () => {
  const stream = [
    promoted('a'),
    refused('b', 'CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE'),
    promoted('c', { replayed: true }),
  ];

  it('does not double count a duplicated or replayed event id', () => {
    const once = reduceSwarmEvents(stream, initialSwarmRunState);
    const twice = reduceSwarmEvents([...stream, ...stream], initialSwarmRunState);
    expect(twice.catalog).toEqual(once.catalog);
    expect(twice.catalog.promotedCount).toBe(2);
    expect(twice.catalog.refusedCount).toBe(1);
  });

  it('is deterministic when a caller hands events out of order', () => {
    const forward = reconstructRun(run('completed'), stream);
    const shuffled = reconstructRun(run('completed'), [stream[2], stream[0], stream[1]]);
    expect(shuffled.swarm.catalog).toEqual(forward.swarm.catalog);
  });

  it('ignores an event at or below the id the swarm slice already folded', () => {
    const state = reduceSwarmEvents(stream, initialSwarmRunState);
    const stale = reduceSwarmEvents([{ ...stream[0], id: '1' }], state);
    expect(stale.catalog).toEqual(state.catalog);
  });
});

// ---------------------------------------------------------------------------
// what a catalog event may NOT touch
// ---------------------------------------------------------------------------

describe('a recognised catalog event owns nothing but its own slice', () => {
  const hostile = event(PROMOTED, {
    candidate_key: 'k', promoted: true, canonical_key: 'c',
    promoted_fields: [], unsupported_fields: [], replayed: false,
    // Every field a payload could use to reach a projection it does not own.
    agent: 'catalog-agent', phase: 'promotion', progress: { percent: 99 },
    tokens: 9_999, cost_usd: 42.5, token_usage: { total: 9_999 },
    task_id: 'invented_task', code: 'INVENTED', claim_id: 'invented-claim',
    graph_revision: 77, batch_index: 5, decision: 'ADD_TASKS',
    status: 'completed', result_kind: 'usable_result',
  }, { agent: 'catalog-agent', phase: 'promotion', progress: { percent: 99 } });

  it('creates no V1 agent, phase, progress or spend', () => {
    const state = reduceRunEvent(initialWorkspaceState, hostile);
    expect(state.agents).toEqual({});
    expect(state.currentPhase).toBe('idle');
    expect(state.progress).toBe(0);
    expect(state.tokens).toBe(0);
    expect(state.cost).toBe(0);
    expect(state.sources).toEqual([]);
    expect(state.claims).toEqual([]);
    expect(state.conflicts).toEqual([]);
    expect(state.rawErrors).toEqual([]);
    expect(state.checkpoints).toEqual([]);
  });

  it('creates no Swarm task and moves no lifecycle phase', () => {
    const state = reduceSwarmEvents([hostile], initialSwarmRunState);
    expect(state.tasks).toEqual({});
    expect(state.taskOrder).toEqual([]);
    expect(state.lifecycle).toBe('idle');
    expect(state.plan.planCreated).toBe(false);
    expect(state.plan.graphRevision).toBe(0);
    expect(state.verification.started).toBe(false);
    expect(state.evidenceClaimIds).toEqual([]);
    expect(state.conflictClaimIds).toEqual([]);
  });

  it('still reaches its own catalog slice and the raw event stream', () => {
    const state = reduceRunEvent(initialWorkspaceState, hostile);
    expect(state.events).toHaveLength(1);
    expect(state.swarm.catalog.observed).toBe(true);
    expect(state.swarm.catalog.promotedCount).toBe(1);
  });

  it('is no longer tracked as an unknown event type', () => {
    const state = reduceSwarmEvents([hostile], initialSwarmRunState);
    expect(state.unknownEventTypes).toEqual([]);
  });
});

describe('an unrecognised catalog-LOOKING event stays inert', () => {
  const invented = event('catalog_variant_promoted_v2', {
    candidate_key: 'k', promoted: true, agent: 'x', phase: 'p',
    progress: { percent: 100 }, tokens: 100, cost_usd: 1,
  }, { agent: 'x', phase: 'p', progress: { percent: 100 } });

  it('touches no projection at all, including the catalog one', () => {
    const state = reduceRunEvent(initialWorkspaceState, invented);
    expect(state.swarm.catalog).toEqual(initialCatalogState);
    expect(state.agents).toEqual({});
    expect(state.currentPhase).toBe('idle');
    expect(state.tokens).toBe(0);
  });

  it('remains visible exactly where F5 put it: raw stream and unknown list', () => {
    const state = reduceRunEvent(initialWorkspaceState, invented);
    expect(state.events).toHaveLength(1);
    expect(state.swarm.unknownEventTypes).toEqual(['catalog_variant_promoted_v2']);
  });
});

// ---------------------------------------------------------------------------
// the raw event stream is unchanged
// ---------------------------------------------------------------------------

describe('the raw event stream', () => {
  it('still contains recognised and unrecognised events alike', () => {
    const events = [
      event('run_started'),
      promoted('a'),
      refused('b', 'CATALOG_PROMOTION_REFUSED'),
      event('some_future_type'),
    ];
    const state = reconstructRun(run('running'), events);
    expect(state.events.map((e) => e.event_type)).toEqual(
      ['run_started', PROMOTED, REFUSED, 'some_future_type']);
    expect(state.swarm.unknownEventTypes).toEqual(['some_future_type']);
  });
});

// ---------------------------------------------------------------------------
// the selector
// ---------------------------------------------------------------------------

describe('selectCatalogStatus', () => {
  it('summarises the slice into counts, a label and a bounded action list', () => {
    const swarm = reduceSwarmEvents(
      [promoted('a'), refused('b', 'CATALOG_PROMOTION_VALUE_MISMATCH')],
      initialSwarmRunState);
    const status = selectCatalogStatus(swarm);
    expect(status.observed).toBe(true);
    expect(status.promotedCount).toBe(1);
    expect(status.refusedCount).toBe(1);
    expect(status.lastRefusalLabel).toBe(
      CATALOG_REFUSAL_LABELS.CATALOG_PROMOTION_VALUE_MISMATCH);
    expect(status.actions).toHaveLength(2);
    expect(status.actions.length).toBeLessThanOrEqual(MAX_CATALOG_ACTIONS);
  });

  it('reports nothing observed for a run with no catalog event', () => {
    const status = selectCatalogStatus(initialSwarmRunState);
    expect(status.observed).toBe(false);
    expect(status.promotedCount).toBe(0);
    expect(status.refusedCount).toBe(0);
    expect(status.lastRefusalLabel).toBeUndefined();
    expect(status.actions).toEqual([]);
  });
});
