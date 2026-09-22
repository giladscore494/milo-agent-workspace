import { describe, expect, it } from 'vitest';
import { BLOCKING_CODES, describeProductOutcome, parseProductOutcome } from '../lib/productOutcome';

const RECORD = {
  engine: 'vehicle_catalog_v1',
  semantic_status: 'partial',
  usability: 'partial',
  result_kind: 'partial_result',
  coverage: { produced: 3, outstanding: 1, ratio: 0.75 },
  blocking: [{ code: 'OUTSTANDING_REVIEW_ITEMS', count: 1 }],
  payload: { present: true, digest: 'a'.repeat(64), byte_size: 512, shape: ['result', 'status'] },
};

describe('the canonical ProductOutcome projection', () => {
  it('parses exactly the finalizer record shape', () => {
    const outcome = parseProductOutcome(RECORD)!;
    expect(outcome.engine).toBe('vehicle_catalog_v1');
    expect(outcome.semanticStatus).toBe('partial');
    expect(outcome.usability).toBe('partial');
    expect(outcome.coverage).toEqual({ produced: 3, outstanding: 1, ratio: 0.75 });
    expect(outcome.blocking).toEqual([{ code: 'OUTSTANDING_REVIEW_ITEMS', count: 1, label: 'items awaiting review' }]);
    expect(outcome.payload).toEqual({ present: true, digest: 'a'.repeat(64), byteSize: 512 });
    expect(describeProductOutcome(outcome).label).toBe('Partial');
  });

  it.each([
    ['null', null],
    ['a string', 'complete'],
    ['an array', [RECORD]],
    ['an unknown semantic status', { ...RECORD, semantic_status: 'excellent' }],
    ['an unknown usability', { ...RECORD, usability: 'great' }],
    ['a missing engine', { ...RECORD, engine: '' }],
    ['an uncountable coverage', { ...RECORD, coverage: { produced: 'many', outstanding: 0 } }],
    ['a ratio above one', { ...RECORD, coverage: { produced: 1, outstanding: 0, ratio: 2 } }],
    ['a blocking code outside the allowlist', { ...RECORD, blocking: [{ code: 'MADE_UP', count: 1 }] }],
    ['a zero blocking count', { ...RECORD, blocking: [{ code: 'REJECTED_ITEMS', count: 0 }] }],
    ['a payload without presence', { ...RECORD, payload: { digest: 'x' } }],
  ])('refuses %s as a whole rather than partially believing it', (_label, record) => {
    expect(parseProductOutcome(record)).toBeUndefined();
  });

  it('never carries a payload digest that is not a sha256 hex', () => {
    const outcome = parseProductOutcome({ ...RECORD, payload: { present: true, digest: 'not-hex' } })!;
    expect(outcome.payload.digest).toBeUndefined();
  });

  it('mirrors every blocking code the backend can record', () => {
    // Kept in lockstep with backend/product_outcome.py BLOCKING_CODES.
    expect([...BLOCKING_CODES].sort()).toEqual([
      'CONFLICTING_CLAIMS', 'COVERAGE_GAPS', 'DEGRADED_ENRICHMENT', 'DEGRADED_VERIFICATION',
      'ENGINE_REPORTED_FAILURE', 'FAILED_AGENTS', 'NO_PRODUCT_PAYLOAD', 'NO_USABLE_RESULT',
      'OUTCOME_CONTRACT_VIOLATION', 'OUTSTANDING_REVIEW_ITEMS', 'REFUSED_BEFORE_EXECUTION',
      'REJECTED_ITEMS', 'RUN_NOT_PRODUCED', 'TASK_FAILURES', 'UNVERIFIED_CLAIMS',
    ]);
  });

  it('describes every semantic status with static text', () => {
    for (const status of ['complete', 'partial', 'unusable', 'refused', 'not_produced'] as const) {
      const outcome = parseProductOutcome({ ...RECORD, semantic_status: status, blocking: [] })!;
      const described = describeProductOutcome(outcome);
      expect(described.label.length).toBeGreaterThan(0);
      expect(described.summary.length).toBeGreaterThan(0);
    }
  });
});
