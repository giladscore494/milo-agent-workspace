/**
 * Safe identity and ordering for `run_events.id`.
 *
 * Production `run_events.id` is a PostgreSQL bigint. The backend serializes it
 * with FastAPI as a JSON *number* (`schemas.RunEvent.id: int`), and Python
 * emits the full digits, so the wire format is lossless. The gateway route
 * (`app/api/gateway/[...path]/route.ts`) streams the upstream body through
 * unchanged, so the exact digits also reach the browser.
 *
 * The ONLY place precision could be lost is `JSON.parse` in the browser, which
 * would coerce a value above `Number.MAX_SAFE_INTEGER` to the nearest double.
 * `parseJsonPreservingBigIntegers` (lib/losslessJson.ts) avoids that, and this
 * module gives the parsed value a single canonical representation.
 *
 * Canonical representation: a normalized decimal STRING. String identity is
 * then exact for every bigint, `===` is a correct duplicate check, and
 * ordering goes through `compareEventIds` rather than `Number(id)`.
 */

export type EventId = string;

const DECIMAL_INTEGER = /^-?\d+$/;

function stripLeadingZeros(digits: string): string {
  const trimmed = digits.replace(/^0+/, '');
  return trimmed === '' ? '0' : trimmed;
}

/**
 * Normalize any accepted wire shape into the canonical decimal string.
 *
 * Accepts a decimal string (the lossless path), a safe integer number (small
 * ids, and every id in existing fixtures/tests) and a bigint. A non-integer
 * number is rejected rather than rounded: silently rounding an id would
 * fabricate identity.
 */
export function normalizeEventId(raw: unknown): EventId {
  if (typeof raw === 'bigint') return raw.toString(10);
  if (typeof raw === 'number') {
    if (!Number.isInteger(raw)) throw new TypeError('event id must be an integer');
    if (!Number.isSafeInteger(raw)) {
      // Reaching here means the value already went through a lossy JSON.parse.
      throw new TypeError('event id exceeded safe integer precision before normalization');
    }
    return String(raw);
  }
  if (typeof raw === 'string') {
    const value = raw.trim();
    if (!DECIMAL_INTEGER.test(value)) throw new TypeError('event id must be a decimal integer');
    const negative = value.startsWith('-');
    const normalized = stripLeadingZeros(negative ? value.slice(1) : value);
    return negative && normalized !== '0' ? `-${normalized}` : normalized;
  }
  throw new TypeError('event id is missing or not a supported type');
}

export function isEventId(value: unknown): value is EventId {
  return typeof value === 'string' && DECIMAL_INTEGER.test(value);
}

/**
 * Numeric comparison over canonical decimal strings, with no `Number()` cast.
 * Returns <0, 0 or >0 like any comparator.
 */
export function compareEventIds(left: EventId, right: EventId): number {
  const leftNegative = left.startsWith('-');
  const rightNegative = right.startsWith('-');
  if (leftNegative !== rightNegative) return leftNegative ? -1 : 1;
  const leftDigits = leftNegative ? left.slice(1) : left;
  const rightDigits = rightNegative ? right.slice(1) : right;
  let magnitude: number;
  if (leftDigits.length !== rightDigits.length) {
    magnitude = leftDigits.length < rightDigits.length ? -1 : 1;
  } else if (leftDigits === rightDigits) {
    magnitude = 0;
  } else {
    magnitude = leftDigits < rightDigits ? -1 : 1;
  }
  return leftNegative ? -magnitude : magnitude;
}

export function maxEventId(left: EventId | undefined, right: EventId): EventId {
  if (left === undefined) return right;
  return compareEventIds(left, right) >= 0 ? left : right;
}

/** Ascending order, stable for equal ids. */
export function sortByEventId<T extends { id: EventId }>(items: readonly T[]): T[] {
  return [...items].sort((a, b) => compareEventIds(a.id, b.id));
}

/**
 * The `after_event_id` polling cursor value. It stays a decimal string all the
 * way into the query string; the backend parses it as a Python int, which is
 * arbitrary precision.
 */
export function eventCursorParam(cursor: EventId): string {
  return cursor;
}
