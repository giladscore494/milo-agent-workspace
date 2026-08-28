/**
 * JSON parsing that preserves integer literals too large for a JS number.
 *
 * `JSON.parse` maps every number onto an IEEE-754 double, so a PostgreSQL
 * bigint above 2^53-1 silently loses its low digits. That would break event
 * identity, deduplication and the `after_event_id` cursor.
 *
 * The pre-pass below is a string-aware scanner over the raw response text: it
 * quotes every integer literal that does not round-trip through `Number`, then
 * hands the result to `JSON.parse`. Safe integers, fractions and exponents are
 * left untouched, so nothing else in a payload changes shape.
 *
 * Only characters OUTSIDE string literals are rewritten. In valid JSON a digit
 * or `-` outside a string can only begin a number token, so the scan needs no
 * grammar beyond skipping strings correctly.
 */

const NUMBER_TOKEN = /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y;

/**
 * An integer literal is unsafe when a JS number cannot carry it exactly.
 *
 * Two checks are needed. `Number.isSafeInteger` catches values above 2^53-1
 * that happen to be exactly representable (9007199254740992 round-trips
 * through `String(Number(...))` yet collides with 9007199254740993), and the
 * round-trip catches the ones that visibly change digits.
 */
function isUnsafeIntegerLiteral(token: string): boolean {
  const value = Number(token);
  return !Number.isSafeInteger(value) || String(value) !== token;
}

export function quoteUnsafeIntegerLiterals(text: string): string {
  let out = '';
  let index = 0;
  const length = text.length;

  while (index < length) {
    const char = text[index];

    if (char === '"') {
      // Copy the whole string literal verbatim, honouring backslash escapes.
      const start = index;
      index += 1;
      while (index < length) {
        const inner = text[index];
        if (inner === '\\') {
          index += 2;
          continue;
        }
        index += 1;
        if (inner === '"') break;
      }
      out += text.slice(start, index);
      continue;
    }

    if (char === '-' || (char >= '0' && char <= '9')) {
      NUMBER_TOKEN.lastIndex = index;
      const match = NUMBER_TOKEN.exec(text);
      if (match) {
        const token = match[0];
        const isInteger = !token.includes('.') && !token.includes('e') && !token.includes('E');
        out += isInteger && isUnsafeIntegerLiteral(token) ? `"${token}"` : token;
        index += token.length;
        continue;
      }
    }

    out += char;
    index += 1;
  }

  return out;
}

/**
 * Parse JSON text, returning oversized integer literals as decimal strings.
 * Everything else parses exactly as `JSON.parse` would.
 */
export function parseJsonPreservingBigIntegers(text: string): unknown {
  return JSON.parse(quoteUnsafeIntegerLiterals(text));
}
