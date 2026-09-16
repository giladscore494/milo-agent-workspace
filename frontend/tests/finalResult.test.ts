/**
 * The Swarm V2 final-result contract and parser.
 *
 * The four canonical payloads here are NOT hand-written: they are committed
 * output of the real `FinalBuilder` + `finalize_product_outcome`, regenerated
 * from `backend/engines/swarm_v2/`, and each one passed `validate_product_outcome`
 * before it was written to `fixtures/swarmV2FinalResult.json`. So these tests
 * assert the frontend contract against what the backend actually emits, not
 * against a second guess at it.
 *
 * Everything else in this file is adversarial: malformed shapes, contradictions,
 * hostile keys and secret-looking material that must never reach a surface.
 */

import { describe, expect, it } from 'vitest';
import {
  FinalResult,
  MAX_VALUE_DEPTH,
  MAX_VALUE_ITEMS,
  NO_USABLE_RESULT_CODE,
  describeOutcome,
  parseFinalResult,
} from '../lib/finalResult';
import fixtures from './fixtures/swarmV2FinalResult.json';
import { API_KEY_SENTINEL, BEARER_SENTINEL, JWT_SENTINEL } from './secretSentinels';

/** Narrow to a successful parse, failing loudly (not silently) otherwise. */
function ok(output: unknown, runStatus?: string): FinalResult {
  const parsed = parseFinalResult(output, { runStatus });
  if (parsed.state !== 'result') {
    throw new Error(`expected a result, got ${parsed.state}` +
      (parsed.state === 'invalid' ? ` (${parsed.code})` : ''));
  }
  return parsed.result;
}

function invalidCode(output: unknown, runStatus?: string): string {
  const parsed = parseFinalResult(output, { runStatus });
  expect(parsed.state).toBe('invalid');
  return parsed.state === 'invalid' ? parsed.code : '';
}

/** A minimal contract-valid payload, used as the base for mutation tests. */
function usablePayload() {
  return JSON.parse(JSON.stringify(fixtures.usable_result));
}

describe('1. the four result kinds, from real backend payloads', () => {
  it('1a. complete / usable_result parses and reports no outstanding items', () => {
    const result = ok(fixtures.usable_result, 'completed');
    expect(result.status).toBe('complete');
    expect(result.kind).toBe('usable_result');
    expect(result.review).toHaveLength(0);
    expect(result.fields.map((f) => f.key)).toEqual(['fuel_type', 'horsepower_hp']);
  });

  it('1b. partial_success / partial_result keeps BOTH halves: fields and outstanding items', () => {
    const result = ok(fixtures.partial_result, 'partial_success');
    expect(result.status).toBe('partial_success');
    expect(result.kind).toBe('partial_result');
    // A partial result is never a completed one: it must carry both.
    expect(result.fields.length).toBeGreaterThan(0);
    expect(result.review.length).toBeGreaterThan(0);
  });

  it('1c. partial_success / no_usable_result carries the static marker and no fields', () => {
    const result = ok(fixtures.no_usable_result, 'partial_success');
    expect(result.kind).toBe('no_usable_result');
    expect(result.fields).toHaveLength(0);
    expect(result.review.at(-1)).toEqual({ kind: 'empty_marker', code: NO_USABLE_RESULT_CODE });
  });

  it('1d. complete / not_found is a confirmed negative, distinct from an empty result', () => {
    const result = ok(fixtures.not_found, 'completed');
    expect(result.kind).toBe('not_found');
    expect(result.fields).toHaveLength(0);
    expect(result.review).toHaveLength(0);
    // The two empty-field kinds must never share a description.
    expect(describeOutcome('not_found').label).not.toBe(describeOutcome('no_usable_result').label);
    expect(describeOutcome('not_found').summary).not.toBe(describeOutcome('no_usable_result').summary);
  });

  it('1e. every kind has a distinct label, symbol and summary — never colour alone', () => {
    const kinds = ['usable_result', 'partial_result', 'no_usable_result', 'not_found'] as const;
    const descriptors = kinds.map(describeOutcome);
    expect(new Set(descriptors.map((d) => d.label)).size).toBe(4);
    expect(new Set(descriptors.map((d) => d.symbol)).size).toBe(4);
    expect(new Set(descriptors.map((d) => d.summary)).size).toBe(4);
    // `partial_result` must state plainly that it is not a completed success.
    expect(describeOutcome('partial_result').summary).toMatch(/not a completed result/i);
  });
});

describe('2. verified fields and multiple values', () => {
  it('2a. a field verified twice keeps BOTH values and selects neither', () => {
    const result = ok(fixtures.usable_result, 'completed');
    const horsepower = result.fields.find((f) => f.key === 'horsepower_hp');
    expect(horsepower?.values).toHaveLength(2);
    expect(horsepower?.values.map((v) => v.value)).toEqual([
      { display: 'text', text: '302' },
      { display: 'text', text: '306' },
    ]);
    expect(result.multiValuedFieldCount).toBe(1);
  });

  it('2b. provenance is references and scope only — no fragment, locator, hash or confidence', () => {
    const result = ok(fixtures.usable_result, 'completed');
    const provenance = result.fields[0].values[0].provenance;
    expect(provenance.sourceId).toBe('src-gov-1');
    expect(provenance.taskId).toBe('government_record_2026');
    expect(provenance.claimId).toBe('claim-fuel');
    expect(provenance.entity).toBe('toyota_rav4_phev');
    expect(Object.keys(provenance).sort()).toEqual(
      ['claimId', 'entity', 'field', 'geography', 'market', 'sourceId', 'taskId'],
    );
  });

  it('2c. a field key is humanised deterministically and keeps its durable key', () => {
    const result = ok(fixtures.usable_result, 'completed');
    expect(result.fields.map((f) => f.label)).toEqual(['Fuel type', 'Horsepower hp']);
  });
});

describe('3. needs_review, conflicts and coverage gaps', () => {
  it('3a. each outstanding item is classified into the closed set', () => {
    const result = ok(fixtures.partial_result, 'partial_success');
    expect(result.review.map((item) => item.kind)).toEqual(
      ['conflict', 'task_failure', 'coverage_gap'],
    );
    expect(result.conflictCount).toBe(1);
    expect(result.taskFailureCount).toBe(1);
    expect(result.coverageGapCount).toBe(1);
  });

  it('3b. a conflict is told apart from an ordinary needs_review by its backend reason', () => {
    const conflict = ok(fixtures.partial_result, 'partial_success').review[0];
    expect(conflict.kind).toBe('conflict');
    expect(conflict.reason).toBe('unresolved conflict');
    expect(conflict.fieldKey).toBe('engine_displacement_cc');

    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    payload.needs_review[0].reason = 'source evidence is insufficient or ambiguous';
    expect(ok(payload, 'partial_success').review[0].kind).toBe('needs_review');
  });

  it('3c. a coverage gap is told apart from a task failure by its static code', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    expect(ok(payload, 'partial_success').review[2].kind).toBe('coverage_gap');
    payload.needs_review[2].code = 'REQUIRED_OUTPUT_MISSING';
    expect(ok(payload, 'partial_success').review[2].kind).toBe('coverage_gap');
    payload.needs_review[2].code = 'SOMETHING_ELSE';
    expect(ok(payload, 'partial_success').review[2].kind).toBe('task_failure');
  });
});

describe('4. the backend invariants, mirrored', () => {
  it('4a. a status that contradicts the result kind is refused', () => {
    const payload = usablePayload();
    payload.status = 'partial_success';
    expect(invalidCode(payload)).toBe('STATUS_CONTRADICTS_KIND');
  });

  it('4b. a kind claiming usable content with no fields is refused', () => {
    const payload = usablePayload();
    payload.fields = {};
    expect(invalidCode(payload)).toBe('KIND_CONTRADICTS_FIELDS');
  });

  it('4c. a kind claiming no usable content while carrying fields is refused', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.no_usable_result));
    payload.fields = { fuel_type: [{ value: 'petrol', provenance: {} }] };
    expect(invalidCode(payload)).toBe('KIND_CONTRADICTS_FIELDS');
  });

  it('4d. a complete outcome carrying review items is refused', () => {
    const payload = usablePayload();
    payload.needs_review = [{ task_id: 'compile_report', code: 'TASK_FAILED' }];
    expect(invalidCode(payload)).toBe('COMPLETE_WITH_REVIEW_ITEMS');
  });

  it('4e. the empty-result marker must be exactly one, last, and carry only its code', () => {
    const base = JSON.parse(JSON.stringify(fixtures.no_usable_result));

    const misplaced = JSON.parse(JSON.stringify(base));
    misplaced.needs_review = [{ code: NO_USABLE_RESULT_CODE },
                              { task_id: 'compile_report', code: 'TASK_FAILED' }];
    expect(invalidCode(misplaced)).toBe('EMPTY_MARKER_INVALID');

    const duplicated = JSON.parse(JSON.stringify(base));
    duplicated.needs_review = [{ code: NO_USABLE_RESULT_CODE }, { code: NO_USABLE_RESULT_CODE }];
    expect(invalidCode(duplicated)).toBe('EMPTY_MARKER_INVALID');

    // An extra key beside the code is exactly how prose would be smuggled in.
    const padded = JSON.parse(JSON.stringify(base));
    padded.needs_review.at(-1).note = 'the model thinks the source was unclear';
    expect(invalidCode(padded)).toBe('EMPTY_MARKER_INVALID');

    const missing = JSON.parse(JSON.stringify(base));
    missing.needs_review = missing.needs_review.slice(0, -1);
    expect(invalidCode(missing)).toBe('EMPTY_MARKER_INVALID');
  });

  it('4f. the marker may not appear under any other result kind', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    payload.needs_review.push({ code: NO_USABLE_RESULT_CODE });
    expect(invalidCode(payload)).toBe('EMPTY_MARKER_INVALID');
  });

  it('4g. vocabulary outside the allowlist is refused, and unhashable lookalikes fail closed', () => {
    const unknownKind = usablePayload();
    unknownKind.result_kind = 'probably_fine';
    expect(invalidCode(unknownKind)).toBe('VOCABULARY_NOT_ALLOWLISTED');

    const arrayStatus = usablePayload();
    arrayStatus.status = [];
    expect(invalidCode(arrayStatus)).toBe('VOCABULARY_NOT_TEXT');

    const nullKind = usablePayload();
    nullKind.result_kind = null;
    expect(invalidCode(nullKind)).toBe('VOCABULARY_NOT_TEXT');
  });
});

describe('5. absent, malformed and hostile payloads', () => {
  it('5a. absent output is its own state, never a result and never invalid', () => {
    expect(parseFinalResult(undefined)).toEqual({ state: 'absent' });
    expect(parseFinalResult(null)).toEqual({ state: 'absent' });
    expect(parseFinalResult({})).toEqual({ state: 'absent' });
  });

  it('5b. a non-object payload is refused, arrays included', () => {
    expect(invalidCode('complete')).toBe('NOT_AN_OBJECT');
    expect(invalidCode(42)).toBe('NOT_AN_OBJECT');
    expect(invalidCode(true)).toBe('NOT_AN_OBJECT');
    // An array is `typeof 'object'`: it must not be read as an empty mapping.
    expect(invalidCode(['complete', 'usable_result'])).toBe('NOT_AN_OBJECT');
  });

  it('5c. structurally wrong fields / needs_review containers are refused', () => {
    const arrayFields = usablePayload();
    arrayFields.fields = [];
    expect(invalidCode(arrayFields)).toBe('STRUCTURALLY_INVALID');

    const objectReview = usablePayload();
    objectReview.needs_review = {};
    expect(invalidCode(objectReview)).toBe('STRUCTURALLY_INVALID');

    const scalarReviewItem = JSON.parse(JSON.stringify(fixtures.partial_result));
    scalarReviewItem.needs_review = ['everything is fine'];
    expect(invalidCode(scalarReviewItem)).toBe('STRUCTURALLY_INVALID');
  });

  it('5d. an UNKNOWN or malformed review item invalidates the whole outcome', () => {
    // This is the fail-closed rule: an outstanding item nobody can classify
    // must never be rendered beside verified fields as a sound result.
    const unknownShape = JSON.parse(JSON.stringify(fixtures.partial_result));
    unknownShape.needs_review.push({ severity: 'high', note: 'trust me' });
    expect(invalidCode(unknownShape)).toBe('REVIEW_ITEM_INVALID');

    const extraKey = JSON.parse(JSON.stringify(fixtures.partial_result));
    extraKey.needs_review[1].rationale = 'the model explains itself here';
    expect(invalidCode(extraKey)).toBe('REVIEW_ITEM_INVALID');

    const missingKey = JSON.parse(JSON.stringify(fixtures.partial_result));
    delete missingKey.needs_review[0].provenance;
    expect(invalidCode(missingKey)).toBe('REVIEW_ITEM_INVALID');

    const wrongType = JSON.parse(JSON.stringify(fixtures.partial_result));
    wrongType.needs_review[1].code = 17;
    expect(invalidCode(wrongType)).toBe('REVIEW_ITEM_INVALID');

    const emptyItem = JSON.parse(JSON.stringify(fixtures.partial_result));
    emptyItem.needs_review.push({});
    expect(invalidCode(emptyItem)).toBe('REVIEW_ITEM_INVALID');
  });

  it('5e. a field entry that is not exactly {value, provenance} is refused', () => {
    const bareValue = usablePayload();
    bareValue.fields.fuel_type = ['plug-in hybrid'];
    expect(invalidCode(bareValue)).toBe('FIELD_ENTRY_INVALID');

    const extraKey = usablePayload();
    extraKey.fields.fuel_type[0].confidence = 0.91;
    expect(invalidCode(extraKey)).toBe('FIELD_ENTRY_INVALID');

    // `FinalBuilder` appends into the list, so a key with an EMPTY list is a
    // shape it cannot write — even while another field still carries entries.
    const emptyList = usablePayload();
    emptyList.fields.fuel_type = [];
    expect(invalidCode(emptyList)).toBe('FIELD_ENTRY_INVALID');

    const notAList = usablePayload();
    notAList.fields.extra_field = { value: 'x', provenance: {} };
    expect(invalidCode(notAList)).toBe('FIELD_ENTRY_INVALID');
  });

  it('5f. a provenance trace carrying a key the builder never writes is refused', () => {
    const smuggled = usablePayload();
    smuggled.fields.fuel_type[0].provenance.fragment = 'the source text, verbatim';
    expect(invalidCode(smuggled)).toBe('FIELD_ENTRY_INVALID');

    const smuggledScope = usablePayload();
    smuggledScope.fields.fuel_type[0].provenance.scope.locator = 'sha256:deadbeef';
    expect(invalidCode(smuggledScope)).toBe('FIELD_ENTRY_INVALID');
  });

  it('5g. a value JSON could never have carried is refused, not bounded', () => {
    const nonFinite = usablePayload();
    nonFinite.fields.fuel_type[0].value = Number.POSITIVE_INFINITY;
    expect(invalidCode(nonFinite)).toBe('VALUE_NOT_JSON');

    const fn = usablePayload();
    fn.fields.fuel_type[0].value = () => 'surprise';
    expect(invalidCode(fn)).toBe('VALUE_NOT_JSON');
  });

  it('5h. a hostile __proto__ key is inert data, never a prototype mutation', () => {
    const hostile = JSON.parse(
      '{"status":"complete","result_kind":"usable_result","needs_review":[],' +
      '"fields":{"__proto__":[{"value":"polluted","provenance":{}}]}}',
    );
    const result = ok(hostile, 'completed');
    expect(result.fields.map((f) => f.key)).toEqual(['__proto__']);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect(Object.prototype.hasOwnProperty.call(Object.prototype, 'value')).toBe(false);
  });

  it('5i. unknown TOP-LEVEL keys are never rendered and never invent a field', () => {
    const extra = usablePayload();
    extra.commander_rationale = 'I chose this because…';
    extra.prompt = 'You are a helpful assistant';
    const result = ok(extra, 'completed');
    expect(result.fields.map((f) => f.key)).toEqual(['fuel_type', 'horsepower_hp']);
    expect(JSON.stringify(result)).not.toMatch(/helpful assistant|I chose this/);
  });
});

describe('6. secret and redaction sentinels', () => {
  it('6a. a secret smuggled into a verified value is never promoted to a field of its own', () => {
    // Redaction is defense in depth; the CONTRACT is what keeps secrets out of
    // the shape. A secret-looking string is still just one field's value, and
    // the surface renders it through safeText — it is never a new key, never a
    // provenance reference and never an unrendered passthrough.
    const payload = usablePayload();
    payload.fields.fuel_type[0].value = API_KEY_SENTINEL;
    const result = ok(payload, 'completed');
    expect(result.fields.find((f) => f.key === 'fuel_type')?.values[0].value)
      .toEqual({ display: 'text', text: API_KEY_SENTINEL });
  });

  it('6b. secret-shaped top-level keys are dropped by the closed contract', () => {
    const payload = usablePayload();
    payload.service_role_key = JWT_SENTINEL;
    payload.authorization = BEARER_SENTINEL;
    const result = ok(payload, 'completed');
    const serialised = JSON.stringify(result);
    for (const secret of [JWT_SENTINEL, BEARER_SENTINEL, API_KEY_SENTINEL]) {
      expect(serialised).not.toContain(secret);
    }
  });

  it('6c. the parsed contract has no field for reasoning, prompts or provider errors', () => {
    const result = ok(fixtures.partial_result, 'partial_success');
    const serialised = JSON.stringify(result);
    for (const forbidden of ['chain_of_thought', 'reasoning', 'prompt', 'provider_error',
                             'stack', 'traceback', 'fragment', 'locator', 'hash', 'token']) {
      expect(serialised).not.toContain(forbidden);
    }
  });
});

describe('7. terminal run status vs. the recorded outcome', () => {
  it('7a. a completed run carrying a partial payload is a contradiction', () => {
    expect(invalidCode(fixtures.partial_result, 'completed'))
      .toBe('RUN_STATUS_CONTRADICTS_OUTCOME');
  });

  it('7b. a partial_success run carrying a complete payload is a contradiction', () => {
    expect(invalidCode(fixtures.usable_result, 'partial_success'))
      .toBe('RUN_STATUS_CONTRADICTS_OUTCOME');
  });

  it('7c. a run that failed, was cancelled, timed out or exhausted its budget reaches no product outcome', () => {
    for (const status of ['failed', 'cancelled', 'timed_out', 'budget_exhausted']) {
      expect(invalidCode(fixtures.usable_result, status))
        .toBe('RUN_STATUS_CONTRADICTS_OUTCOME');
    }
  });

  it('7d. a non-terminal status never vetoes the payload — the run simply has not finished', () => {
    // The panel decides what to SHOW while a run is live; the parser must not
    // pretend a mid-flight status contradicts an outcome it cannot have yet.
    expect(ok(fixtures.usable_result, 'running').kind).toBe('usable_result');
    expect(ok(fixtures.usable_result).kind).toBe('usable_result');
  });
});

describe('8. structured values are bounded, not discarded', () => {
  it('8a. a nested verified value is rendered, not dropped', () => {
    const result = ok(fixtures.structured_value, 'completed');
    const value = result.fields[0].values[0].value;
    expect(value.display).toBe('record');
    if (value.display !== 'record') throw new Error('unreachable');
    expect(value.entries.map((e) => e.key)).toEqual(['length_mm', 'width_mm', 'axles']);
    expect(value.entries[0].value).toEqual({ display: 'text', text: '4600' });
    expect(value.hidden).toBe(0);
  });

  it('8b. the depth bound is declared, never a silent truncation', () => {
    const payload = usablePayload();
    // depth 0 record -> 1 record -> 2 record -> 3 is past the bound.
    payload.fields.fuel_type[0].value = { a: { b: { c: { d: 'too deep' } } } };
    const value = ok(payload, 'completed').fields
      .find((f) => f.key === 'fuel_type')!.values[0].value;
    let node: any = value;
    for (let depth = 0; depth < MAX_VALUE_DEPTH; depth += 1) {
      expect(node.display).toBe('record');
      node = node.entries[0].value;
    }
    expect(node).toEqual({ display: 'depth_bounded' });
  });

  it('8c. the breadth bound counts what it is not showing', () => {
    const payload = usablePayload();
    payload.fields.fuel_type[0].value = Array.from({ length: MAX_VALUE_ITEMS + 5 }, (_, i) => i);
    const value = ok(payload, 'completed').fields
      .find((f) => f.key === 'fuel_type')!.values[0].value;
    expect(value.display).toBe('list');
    if (value.display !== 'list') throw new Error('unreachable');
    expect(value.items).toHaveLength(MAX_VALUE_ITEMS);
    expect(value.hidden).toBe(5);
  });

  it('8d. null and empty string are a recorded absence, not the text "null"', () => {
    const payload = usablePayload();
    payload.fields.fuel_type[0].value = null;
    expect(ok(payload, 'completed').fields.find((f) => f.key === 'fuel_type')!.values[0].value)
      .toEqual({ display: 'empty' });
    payload.fields.fuel_type[0].value = '';
    expect(ok(payload, 'completed').fields.find((f) => f.key === 'fuel_type')!.values[0].value)
      .toEqual({ display: 'empty' });
  });

  it('8e. booleans read as words, and long text is bounded', () => {
    const payload = usablePayload();
    payload.fields.fuel_type[0].value = false;
    expect(ok(payload, 'completed').fields.find((f) => f.key === 'fuel_type')!.values[0].value)
      .toEqual({ display: 'text', text: 'No' });

    payload.fields.fuel_type[0].value = 'x'.repeat(5000);
    const bounded = ok(payload, 'completed').fields
      .find((f) => f.key === 'fuel_type')!.values[0].value;
    if (bounded.display !== 'text') throw new Error('unreachable');
    expect(bounded.text.length).toBeLessThan(600);
    expect(bounded.text.endsWith('…')).toBe(true);
  });
});

describe('9. refresh and resume determinism', () => {
  it('9a. parsing the same durable output twice produces an identical result', () => {
    // This is the frontend half of the roadmap §1.13 criterion: the result is
    // reconstructed from durable output alone, with no memory of the session.
    for (const payload of Object.values(fixtures)) {
      const first = parseFinalResult(payload);
      const second = parseFinalResult(JSON.parse(JSON.stringify(payload)));
      expect(second).toEqual(first);
    }
  });

  it('9b. a round trip through JSON — exactly what a refresh does — changes nothing', () => {
    const overTheWire = JSON.parse(JSON.stringify(fixtures.partial_result));
    expect(parseFinalResult(overTheWire, { runStatus: 'partial_success' }))
      .toEqual(parseFinalResult(fixtures.partial_result, { runStatus: 'partial_success' }));
  });

  it('9c. field and review order follow the payload, so the surface cannot reorder itself', () => {
    const result = ok(fixtures.partial_result, 'partial_success');
    expect(result.fields.map((f) => f.key)).toEqual(Object.keys(fixtures.partial_result.fields));
    expect(result.review.map((item) => item.taskId ?? item.fieldKey)).toEqual(
      fixtures.partial_result.needs_review.map((item: any) => item.task_id ?? item.field),
    );
  });

  it('9d. the parser is pure: it never mutates the payload it was given', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    const before = JSON.stringify(payload);
    parseFinalResult(payload, { runStatus: 'partial_success' });
    expect(JSON.stringify(payload)).toBe(before);
  });
});
