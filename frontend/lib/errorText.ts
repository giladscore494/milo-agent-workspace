import { ApiError } from './api';
import { REDACTED, redactSecretText } from './sanitize';

/**
 * Safe user-facing error classification.
 *
 * This is a FOURTH distinct concept, and the other three do not substitute for
 * it:
 *
 *  - HTML/text escaping (`safeText`) stops markup from becoming markup. It
 *    happily prints a credential;
 *  - structured validation (`parseFinalResult`) decides which SHAPES may be
 *    rendered. An error string has no shape to validate;
 *  - redaction (`redactSecretText`) removes credential-shaped substrings. It
 *    leaves an internal hostname, a stack frame or a provider diagnostic
 *    perfectly readable.
 *
 * What is left is the question this module answers: is this text something a
 * product surface may show a user at all? The workspace reads its message from
 * whatever the gateway, the API or the Supabase client handed back, and those
 * are operational surfaces. An upstream that one day widens a message — a
 * provider error passed through, an exception string, a private URL — must not
 * silently become browser output.
 *
 * So a message is shown only when it survives, in order:
 *
 *  1. redaction. If redaction FIRED, the raw text contained credential-shaped
 *     material, and a partially-masked operational string is not worth showing:
 *     the whole message is replaced. Over-replacement is the safe direction;
 *  2. a marker scan for shapes a product message never legitimately has — a
 *     URL or scheme, a stack frame, markup;
 *  3. normalisation: control characters and newlines collapse to spaces and the
 *     result is bounded, so no error can reflow or flood the surface.
 *
 * Anything that fails becomes the caller's own fallback — a static sentence the
 * caller authored for that action, which keeps the error ACTIONABLE ("Run
 * creation failed.") rather than degrading to a single generic string.
 *
 * The CODE is treated separately and kept, because it is what makes an error
 * actionable across a support boundary. It is allowlisted by SHAPE, not by
 * value: a short SCREAMING_SNAKE token, which every `AppError` code and every
 * `HTTP_<status>` fallback already is, and which cannot carry a sentence.
 */

const SAFE_ERROR_CODE = /^[A-Z][A-Z0-9_]{1,63}$/;

/** The longest message the surface will print. Roughly two lines. */
export const MAX_ERROR_MESSAGE_LENGTH = 240;

const UNSAFE_MESSAGE_MARKERS: readonly RegExp[] = [
  // Any URL or scheme: internal hostnames, Cloud Run URLs, Supabase endpoints.
  /[a-z][a-z0-9+.-]*:\/\//i,
  // Stack traces, both runtimes.
  /traceback \(most recent call last\)/i,
  /\bat\s+[\w$.<>]+\s*\(/,
  /\bFile\s+"[^"]+",\s+line\s+\d+/,
  // Markup: never legitimate in a product error message.
  /<\s*[a-z!/]/i,
];

const CONTROL_CHARACTERS = new RegExp('[\\x00-\\x1f\\x7f]+', 'g');

function normalizeWhitespace(text: string): string {
  // Control characters (including newlines and tabs) collapse to one space, so
  // a multi-line operational dump cannot reflow the surface it lands in.
  return text.replace(CONTROL_CHARACTERS, ' ').replace(/\s{2,}/g, ' ').trim();
}

/**
 * Return `raw` when it is safe to show, otherwise `undefined`.
 * Exported for direct testing: the decision is the security boundary.
 */
export function classifyErrorMessage(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const redacted = redactSecretText(raw);
  // Redaction fired: the raw string held credential-shaped material, so the
  // whole message is suspect rather than merely partly masked.
  if (redacted.includes(REDACTED)) return undefined;
  const normalized = normalizeWhitespace(redacted);
  if (normalized === '') return undefined;
  for (const marker of UNSAFE_MESSAGE_MARKERS) {
    if (marker.test(normalized)) return undefined;
  }
  return normalized.length > MAX_ERROR_MESSAGE_LENGTH
    ? `${normalized.slice(0, MAX_ERROR_MESSAGE_LENGTH - 1)}…`
    : normalized;
}

/** Return `code` when it is an allowlisted shape, otherwise `undefined`. */
export function classifyErrorCode(code: unknown): string | undefined {
  return typeof code === 'string' && SAFE_ERROR_CODE.test(code) ? code : undefined;
}

/**
 * The one text an error surface may render.
 *
 * `fallback` is the caller's own static sentence for the action that failed and
 * is used whenever the upstream text does not survive classification.
 */
export function safeErrorText(error: unknown, fallback: string): string {
  const message = classifyErrorMessage(error instanceof Error ? error.message : undefined) ?? fallback;
  const code = error instanceof ApiError ? classifyErrorCode(error.code) : undefined;
  return code ? `${message} (${code})` : message;
}
