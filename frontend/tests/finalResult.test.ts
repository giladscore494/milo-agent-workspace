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
  describeReviewCode,
  parseFinalResult,
} from '../lib/finalResult';
import fixtures from './fixtures/swarmV2FinalResult.json';
import {
  ALL_SECRET_SENTINELS,
  API_KEY_SENTINEL,
  BEARER_SENTINEL,
  JWT_SENTINEL,
  SECRET_FRAGMENTS,
} from './secretSentinels';

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

/** Exactly the trace `FinalBuilder.build` writes. Every key, every scope key. */
function validProvenance(overrides: Record<string, unknown> = {}) {
  return {
    claim_id: 'claim-fuel',
    source_id: 'src-gov-1',
    run_id: 'cccccccc-1111-4111-8111-000000000f04',
    task_id: 'government_record_2026',
    scope: {
      entity: 'toyota_rav4_phev',
      field: 'fuel_type',
      geography: 'IL',
      market: 'IL',
      time_scope: { model_year: 2026 },
    },
    ...overrides,
  };
}

/** A whole contract-valid payload built around one field entry. */
function payloadWithEntry(entry: unknown): Record<string, unknown> & { fields: Record<string, unknown> } {
  return {
    status: 'complete',
    result_kind: 'usable_result',
    fields: { fuel_type: [entry] },
    needs_review: [],
  };
}

/** Assert that no sentinel — whole or fragmentary — survives into `text`. */
function assertNoSecret(text: string, label: string) {
  for (const secret of ALL_SECRET_SENTINELS) expect(text, label).not.toContain(secret);
  for (const fragment of SECRET_FRAGMENTS) expect(text, label).not.toContain(fragment);
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
    expect(invalidCode(smuggled)).toBe('PROVENANCE_INVALID');

    const smuggledScope = usablePayload();
    smuggledScope.fields.fuel_type[0].provenance.scope.locator = 'sha256:deadbeef';
    expect(invalidCode(smuggledScope)).toBe('PROVENANCE_INVALID');
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
    // The provenance is complete and valid: this test is about the KEY, and a
    // trace that failed validation would refuse before the key ever mattered.
    const hostile = JSON.parse(JSON.stringify(
      payloadWithEntry({ value: 'polluted', provenance: validProvenance() }),
    ).replace('"fuel_type"', '"__proto__"'));
    const result = ok(hostile, 'completed');
    expect(result.fields.map((f) => f.key)).toEqual(['__proto__']);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
    expect(Object.prototype.hasOwnProperty.call(Object.prototype, 'value')).toBe(false);

    // The same key nested inside a structured VALUE is equally inert.
    const nested = JSON.parse(
      '{"status":"complete","result_kind":"usable_result","needs_review":[],' +
      '"fields":{"dimensions":[{"value":{"__proto__":{"polluted":true}},' +
      '"provenance":' + JSON.stringify(validProvenance()) + '}]}}',
    );
    ok(nested, 'completed');
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
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
  it('6a. a secret in a verified value is REDACTED, never displayed', () => {
    // The typed contract stops an unknown FIELD being rendered; it proves
    // nothing about what is inside a string the contract legitimately allows.
    // Redaction is the second barrier, and this asserts it actually fires.
    const payload = usablePayload();
    payload.fields.fuel_type[0].value = API_KEY_SENTINEL;
    const result = ok(payload, 'completed');
    expect(result.fields.find((f) => f.key === 'fuel_type')?.values[0].value)
      .toEqual({ display: 'text', text: '[REDACTED]' });
    assertNoSecret(JSON.stringify(result), 'verified value');
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

describe('10. redaction is a real boundary, in EVERY durable string position', () => {
  // The contract closes the SHAPE; it says nothing about what is inside a
  // string it allows. Each case below puts a credential in a position the
  // contract permits and asserts it does not survive into the display model.

  it('10a. a scalar verified value', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const payload = usablePayload();
      payload.fields.fuel_type[0].value = secret;
      assertNoSecret(JSON.stringify(ok(payload, 'completed')), `scalar ${secret}`);
    }
  });

  it('10b. a string nested inside a structured value', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const payload = usablePayload();
      payload.fields.fuel_type[0].value = { spec: { notes: [secret] } };
      assertNoSecret(JSON.stringify(ok(payload, 'completed')), `nested ${secret}`);
    }
  });

  it('10c. a structured-value KEY', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const payload = usablePayload();
      payload.fields.fuel_type[0].value = { [secret]: 'value' };
      assertNoSecret(JSON.stringify(ok(payload, 'completed')), `record key ${secret}`);
    }
  });

  it('10d. a field key — and the label derived from it', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const payload = payloadWithEntry({ value: 'petrol', provenance: validProvenance() });
      payload.fields = { [secret]: payload.fields.fuel_type };
      const result = ok(payload, 'completed');
      assertNoSecret(JSON.stringify(result), `field key ${secret}`);
      // The label is derived from the REDACTED key, so the two cannot diverge.
      assertNoSecret(result.fields[0].label, `field label ${secret}`);
    }
  });

  it('10e. a review reason and a review code', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const withReason = JSON.parse(JSON.stringify(fixtures.partial_result));
      withReason.needs_review[0].reason = secret;
      assertNoSecret(JSON.stringify(ok(withReason, 'partial_success')), `reason ${secret}`);

      const withCode = JSON.parse(JSON.stringify(fixtures.partial_result));
      withCode.needs_review[1].code = secret;
      assertNoSecret(JSON.stringify(ok(withCode, 'partial_success')), `code ${secret}`);
    }
  });

  it('10f. a task identifier', () => {
    for (const secret of ALL_SECRET_SENTINELS) {
      const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
      // `task_id` is bounded at 80 chars by the backend contract; keep inside it.
      payload.needs_review[1].task_id = secret.slice(0, 80);
      assertNoSecret(JSON.stringify(ok(payload, 'partial_success')), `task id ${secret}`);
    }
  });

  it('10g. provenance identifiers and displayed scope values', () => {
    const positions = [
      ['claim_id', (p: any, v: string) => { p.claim_id = v; }],
      ['source_id', (p: any, v: string) => { p.source_id = v; }],
      ['task_id', (p: any, v: string) => { p.task_id = v.slice(0, 80); }],
      ['scope.entity', (p: any, v: string) => { p.scope.entity = v; }],
      ['scope.field', (p: any, v: string) => { p.scope.field = v; }],
      ['scope.geography', (p: any, v: string) => { p.scope.geography = v; }],
      ['scope.market', (p: any, v: string) => { p.scope.market = v; }],
    ] as const;
    for (const secret of ALL_SECRET_SENTINELS) {
      for (const [name, place] of positions) {
        const provenance = validProvenance();
        place(provenance, secret);
        const payload = payloadWithEntry({ value: 'petrol', provenance });
        assertNoSecret(JSON.stringify(ok(payload, 'completed')), `${name} ${secret}`);
      }
    }
  });

  it('10h. redaction never changes how an item is CLASSIFIED', () => {
    // Classification reads the raw reason/code; only display text is redacted.
    // A conflict whose reason were redacted before classification would be
    // silently demoted to an ordinary review item.
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    expect(ok(payload, 'partial_success').conflictCount).toBe(1);
    expect(ok(payload, 'partial_success').coverageGapCount).toBe(1);
  });

  it('10i. an ordinary product value is left completely untouched', () => {
    // Over-redaction is the safe direction, but it must not be the common one.
    const result = ok(fixtures.partial_result, 'partial_success');
    expect(result.fields[0].values[0].value).toEqual({ display: 'text', text: 'plug-in hybrid' });
    expect(result.review[0].reason).toBe('unresolved conflict');
    expect(result.review[1].code).toBe('R5_GOV_RECORD_AMBIGUOUS');
    expect(result.fields[0].values[0].provenance.sourceId).toBe('src-gov-1');
  });
});

describe('11. provenance is fail-closed, mirroring FinalBuilder exactly', () => {
  const REQUIRED = ['claim_id', 'source_id', 'run_id', 'task_id', 'scope'] as const;
  const SCOPE_REQUIRED = ['entity', 'field', 'geography', 'market', 'time_scope'] as const;

  it('11a. a complete builder trace parses, and surfaces only the safe references', () => {
    const result = ok(payloadWithEntry({ value: 'petrol', provenance: validProvenance() }), 'completed');
    expect(result.fields[0].values[0].provenance).toEqual({
      claimId: 'claim-fuel',
      sourceId: 'src-gov-1',
      taskId: 'government_record_2026',
      entity: 'toyota_rav4_phev',
      field: 'fuel_type',
      geography: 'IL',
      market: 'IL',
    });
  });

  it('11b. a MISSING provenance key refuses the whole outcome', () => {
    for (const key of REQUIRED) {
      const provenance: Record<string, unknown> = validProvenance();
      delete provenance[key];
      expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance })), key)
        .toBe('PROVENANCE_INVALID');
    }
  });

  it('11c. an EMPTY or whole-object-missing provenance refuses', () => {
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: {} })))
      .toBe('PROVENANCE_INVALID');
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: null })))
      .toBe('PROVENANCE_INVALID');
  });

  it('11d. an EMPTY required identifier refuses', () => {
    for (const key of ['claim_id', 'source_id', 'run_id', 'task_id'] as const) {
      const provenance: Record<string, unknown> = validProvenance();
      provenance[key] = '';
      expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance })), key)
        .toBe('PROVENANCE_INVALID');
    }
  });

  it('11e. a MISTYPED identifier refuses', () => {
    for (const bad of [42, null, [], {}, true] as const) {
      const provenance: Record<string, unknown> = validProvenance();
      provenance.claim_id = bad;
      expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance })), String(bad))
        .toBe('PROVENANCE_INVALID');
    }
  });

  it('11f. an identifier past the backend bound refuses rather than truncating', () => {
    const tooLongClaim: Record<string, unknown> = validProvenance();
    tooLongClaim.claim_id = 'c'.repeat(201); // EvidenceReference: max_length=200
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: tooLongClaim })))
      .toBe('PROVENANCE_INVALID');

    const tooLongTask: Record<string, unknown> = validProvenance();
    tooLongTask.task_id = 't'.repeat(81); // task_id: max_length=80
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: tooLongTask })))
      .toBe('PROVENANCE_INVALID');

    // Exactly at the bound is fine.
    const atBound: Record<string, unknown> = validProvenance();
    atBound.task_id = 't'.repeat(80);
    expect(ok(payloadWithEntry({ value: 'petrol', provenance: atBound }), 'completed')
      .fields[0].values[0].provenance.taskId).toHaveLength(80);
  });

  it('11g. an EXTRA provenance or scope key refuses', () => {
    const extra: Record<string, unknown> = validProvenance();
    extra.content_hash = 'sha256:deadbeef';
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: extra })))
      .toBe('PROVENANCE_INVALID');

    const extraScope = validProvenance();
    (extraScope.scope as Record<string, unknown>).unit = 'hp';
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: extraScope })))
      .toBe('PROVENANCE_INVALID');
  });

  it('11h. a MISSING or malformed scope key refuses', () => {
    for (const key of SCOPE_REQUIRED) {
      const provenance = validProvenance();
      delete (provenance.scope as Record<string, unknown>)[key];
      expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance })), key)
        .toBe('PROVENANCE_INVALID');
    }
    const notAnObject = validProvenance({ scope: 'IL' });
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: notAnObject })))
      .toBe('PROVENANCE_INVALID');
  });

  it('11i. entity and field are REQUIRED; geography and market may be null', () => {
    for (const key of ['entity', 'field'] as const) {
      const provenance = validProvenance();
      (provenance.scope as Record<string, unknown>)[key] = null;
      expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance })), key)
        .toBe('PROVENANCE_INVALID');
    }
    // `EvidenceReference` types these as `str | None`.
    const nullable = validProvenance();
    (nullable.scope as Record<string, unknown>).geography = null;
    (nullable.scope as Record<string, unknown>).market = null;
    const provenance = ok(payloadWithEntry({ value: 'petrol', provenance: nullable }), 'completed')
      .fields[0].values[0].provenance;
    expect(provenance.geography).toBeUndefined();
    expect(provenance.market).toBeUndefined();
    expect(provenance.entity).toBe('toyota_rav4_phev');
  });

  it('11j. time_scope is validated even though it is never displayed', () => {
    const notAnObject = validProvenance();
    (notAnObject.scope as Record<string, unknown>).time_scope = 2026;
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: notAnObject })))
      .toBe('PROVENANCE_INVALID');

    const tooManyKeys = validProvenance();
    (tooManyKeys.scope as Record<string, unknown>).time_scope =
      Object.fromEntries(Array.from({ length: 9 }, (_, i) => [`k${i}`, i])); // bound is 8
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: tooManyKeys })))
      .toBe('PROVENANCE_INVALID');

    const tooLarge = validProvenance();
    (tooLarge.scope as Record<string, unknown>).time_scope = { note: 'x'.repeat(600) };
    expect(invalidCode(payloadWithEntry({ value: 'petrol', provenance: tooLarge })))
      .toBe('PROVENANCE_INVALID');

    // An empty time_scope is what `EvidenceReference` defaults to.
    const empty = validProvenance();
    (empty.scope as Record<string, unknown>).time_scope = {};
    expect(ok(payloadWithEntry({ value: 'petrol', provenance: empty }), 'completed')
      .fields[0].values).toHaveLength(1);
  });

  it('11k. a review item with malformed provenance refuses too', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    delete payload.needs_review[0].provenance.scope.market;
    expect(invalidCode(payload, 'partial_success')).toBe('PROVENANCE_INVALID');
  });
});

describe('12. a valid partial result with NO itemized review rows', () => {
  // Generated by the real FinalBuilder from one verified and one REJECTED
  // verdict: a rejection makes the run partial without writing a review row.
  const PAYLOAD = fixtures.partial_result_no_review_items;

  it('12a. the fixture really is a backend-valid partial_result with empty needs_review', () => {
    expect(PAYLOAD.status).toBe('partial_success');
    expect(PAYLOAD.result_kind).toBe('partial_result');
    expect(PAYLOAD.needs_review).toEqual([]);
    expect(Object.keys(PAYLOAD.fields)).toEqual(['fuel_type']);
  });

  it('12b. it parses as a result — not as invalid, and not as a completed success', () => {
    const result = ok(PAYLOAD, 'partial_success');
    expect(result.kind).toBe('partial_result');
    expect(result.fields).toHaveLength(1);
    expect(result.review).toHaveLength(0);
    expect(result.conflictCount + result.coverageGapCount + result.taskFailureCount).toBe(0);
  });

  it('12c. the outcome summary promises no list it cannot show', () => {
    const summary = describeOutcome('partial_result').summary;
    expect(summary).not.toMatch(/items below|listed below|below are/i);
    expect(summary).toMatch(/not a completed result/i);
    expect(summary).toMatch(/not every claim/i);
  });
});

describe('13. undefined is refused inside a durable value', () => {
  it('13a. as a field entry value', () => {
    expect(invalidCode(payloadWithEntry({ value: undefined, provenance: validProvenance() })))
      .toBe('VALUE_NOT_JSON');
  });

  it('13b. inside a list', () => {
    expect(invalidCode(payloadWithEntry({ value: [1, undefined, 3], provenance: validProvenance() })))
      .toBe('VALUE_NOT_JSON');
    // A sparse array reads its hole as undefined and is refused the same way.
    // eslint-disable-next-line no-sparse-arrays
    expect(invalidCode(payloadWithEntry({ value: [1, , 3], provenance: validProvenance() })))
      .toBe('VALUE_NOT_JSON');
  });

  it('13c. inside a structured object', () => {
    expect(invalidCode(
      payloadWithEntry({ value: { length_mm: 4600, width_mm: undefined }, provenance: validProvenance() }),
    )).toBe('VALUE_NOT_JSON');
  });

  it('13d. and inside a review item value', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    // Assigning after the clone leaves an OWN `value` key holding `undefined`,
    // so the item still matches the verdict-review shape and reaches the value.
    payload.needs_review[0].value = undefined;
    expect(invalidCode(payload, 'partial_success')).toBe('VALUE_NOT_JSON');
  });

  it('13e. but an explicit JSON null is still a recorded absence, not a refusal', () => {
    const result = ok(payloadWithEntry({ value: null, provenance: validProvenance() }), 'completed');
    expect(result.fields[0].values[0].value).toEqual({ display: 'empty' });
  });

  it('13f. and a top-level undefined output is still the ABSENT state', () => {
    expect(parseFinalResult(undefined)).toEqual({ state: 'absent' });
  });
});

describe('14. payload-controlled keys never reach a prototype', () => {
  it('14a. a review code naming an Object.prototype member resolves to nothing', () => {
    // A plain-object lookup would return a FUNCTION here, and React throws when
    // handed one as a child — so the surface built to fail closed would have
    // crashed instead. The label table is a Map for exactly this reason.
    for (const key of ['constructor', 'toString', 'hasOwnProperty', 'valueOf', '__proto__']) {
      expect(describeReviewCode(key), key).toBeUndefined();
    }
    // The real codes still resolve.
    expect(describeReviewCode('TASK_FAILED')).toBe('The task did not complete');
    expect(describeReviewCode('EVIDENCE_REQUIREMENTS_UNMET')).toBe('Evidence requirements were not met');
    expect(describeReviewCode('NOT_A_KNOWN_CODE')).toBeUndefined();
  });

  it('14b. such a code parses as an ordinary task failure and carries no function', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    payload.needs_review[1].code = 'constructor';
    const result = ok(payload, 'partial_success');
    const item = result.review.find((entry) => entry.kind === 'task_failure');
    expect(item?.code).toBe('constructor');
    for (const entry of result.review) {
      expect(typeof entry.code === 'string' || entry.code === undefined).toBe(true);
    }
  });
});
