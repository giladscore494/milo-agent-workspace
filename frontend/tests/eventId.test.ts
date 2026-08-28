import { describe, expect, it } from 'vitest';
import {
  compareEventIds,
  eventCursorParam,
  isEventId,
  maxEventId,
  normalizeEventId,
  sortByEventId,
} from '../lib/eventId';
import {
  parseJsonPreservingBigIntegers,
  quoteUnsafeIntegerLiterals,
} from '../lib/losslessJson';
import { initialWorkspaceState, reduceRunEvent } from '../lib/runReducer';
import { RunEvent } from '../lib/types';

// Number.MAX_SAFE_INTEGER is 9007199254740991. These two distinct bigints both
// become the double 9007199254740992, so JSON.parse cannot tell them apart.
const BIG_A = '9007199254740992';
const BIG_B = '9007199254740993';

describe('F. bigint event identity and ordering', () => {
  it('proves JSON.parse alone would lose the identity we must preserve', () => {
    const a = JSON.parse(`{"id": ${BIG_A}}`).id;
    const b = JSON.parse(`{"id": ${BIG_B}}`).id;
    expect(a).toBe(b); // two distinct durable ids collapse onto one double
    expect(Number.isSafeInteger(a)).toBe(false);
  });

  it('parses oversized integer literals losslessly as decimal strings', () => {
    const parsed = parseJsonPreservingBigIntegers(
      `[{"id": ${BIG_A}, "run_id": "r"}, {"id": ${BIG_B}, "run_id": "r"}]`,
    ) as Array<{ id: string }>;
    expect(parsed[0].id).toBe(BIG_A);
    expect(parsed[1].id).toBe(BIG_B);
    expect(parsed[0].id).not.toBe(parsed[1].id);
  });

  it('leaves safe integers, fractions, exponents and strings untouched', () => {
    const parsed = parseJsonPreservingBigIntegers(
      '{"id": 42, "cost": 0.019178, "exp": 1e3, "neg": -17, "text": "id 9007199254740993 here"}',
    ) as Record<string, unknown>;
    expect(parsed).toEqual({
      id: 42,
      cost: 0.019178,
      exp: 1000,
      neg: -17,
      text: 'id 9007199254740993 here',
    });
  });

  it('does not rewrite digits inside strings, including escaped quotes', () => {
    const text = `{"a": "${BIG_B}", "b": "he said \\"${BIG_B}\\"", "c": ${BIG_B}}`;
    const rewritten = quoteUnsafeIntegerLiterals(text);
    expect(rewritten).toBe(
      `{"a": "${BIG_B}", "b": "he said \\"${BIG_B}\\"", "c": "${BIG_B}"}`,
    );
    const parsed = parseJsonPreservingBigIntegers(text) as Record<string, string>;
    expect(parsed.a).toBe(BIG_B);
    expect(parsed.b).toBe(`he said "${BIG_B}"`);
    expect(parsed.c).toBe(BIG_B);
  });

  it('normalizes every accepted wire shape into one canonical form', () => {
    expect(normalizeEventId(7)).toBe('7');
    expect(normalizeEventId('7')).toBe('7');
    expect(normalizeEventId(' 007 ')).toBe('7');
    expect(normalizeEventId(BigInt(BIG_A))).toBe(BIG_A);
    expect(normalizeEventId(BIG_A)).toBe(BIG_A);
    expect(normalizeEventId('-0')).toBe('0');
    expect(isEventId(BIG_A)).toBe(true);
    expect(isEventId('abc')).toBe(false);
  });

  it('refuses a value that already lost precision instead of pretending', () => {
    expect(() => normalizeEventId(9007199254740993)).toThrow(/safe integer/);
    expect(() => normalizeEventId(9007199254740992)).toThrow(/safe integer/);
    expect(() => normalizeEventId(1.5)).toThrow(/integer/);
    expect(() => normalizeEventId(undefined)).toThrow();
    expect(() => normalizeEventId('12a')).toThrow();
  });

  it('orders bigint ids without a Number() cast', () => {
    expect(compareEventIds(BIG_A, BIG_B)).toBeLessThan(0);
    expect(compareEventIds(BIG_B, BIG_A)).toBeGreaterThan(0);
    expect(compareEventIds(BIG_A, BIG_A)).toBe(0);
    // Number() would report these as equal.
    expect(Number(BIG_A) === Number(BIG_B)).toBe(true);
    expect(compareEventIds(BIG_A, BIG_B)).not.toBe(0);

    expect(compareEventIds('9', '10')).toBeLessThan(0);
    expect(compareEventIds('-5', '3')).toBeLessThan(0);
    expect(compareEventIds('-5', '-9')).toBeGreaterThan(0);

    expect(maxEventId(undefined, BIG_A)).toBe(BIG_A);
    expect(maxEventId(BIG_B, BIG_A)).toBe(BIG_B);
    expect(sortByEventId([{ id: BIG_B }, { id: '2' }, { id: BIG_A }]).map((e) => e.id)).toEqual([
      '2',
      BIG_A,
      BIG_B,
    ]);
  });

  it('carries the cursor to the query string as exact digits', () => {
    expect(eventCursorParam(BIG_A)).toBe(BIG_A);
    expect(encodeURIComponent(eventCursorParam(BIG_A))).toBe(BIG_A);
  });
});

describe('F. reducer identity and cursor over bigint ids', () => {
  function event(id: string, taskId: string, type = 'task_started'): RunEvent {
    return { id, run_id: 'r', event_type: type, payload: { task_id: taskId } };
  }

  it('treats two adjacent bigint ids as two distinct events', () => {
    const state = [event(BIG_A, 'env_check'), event(BIG_B, 'list_catalog')].reduce(
      reduceRunEvent,
      initialWorkspaceState,
    );
    expect(state.events).toHaveLength(2);
    expect(state.lastEventId).toBe(BIG_B);
    expect(state.swarm.taskOrder).toEqual(['env_check', 'list_catalog']);
  });

  it('ignores a replayed bigint id and keeps the cursor at the maximum', () => {
    const first = reduceRunEvent(initialWorkspaceState, event(BIG_B, 'list_catalog'));
    const replay = reduceRunEvent(first, event(BIG_B, 'list_catalog'));
    expect(replay).toBe(first);
    // An out-of-order older event cannot drag the cursor backwards.
    const older = reduceRunEvent(first, event(BIG_A, 'env_check'));
    expect(older.lastEventId).toBe(BIG_B);
  });

  it('normalizes numeric ids from fixtures onto the same canonical identity', () => {
    const numeric = { id: 5 as unknown as string, run_id: 'r', event_type: 'run_started' };
    const stringy = { id: '5', run_id: 'r', event_type: 'run_started' };
    const state = [numeric, stringy].reduce(reduceRunEvent, initialWorkspaceState);
    expect(state.events).toHaveLength(1);
    expect(state.events[0].id).toBe('5');
  });
});
