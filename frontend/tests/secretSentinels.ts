/**
 * Secret-SHAPED sentinels for leak assertions.
 *
 * These are assembled at runtime and never written as literals. A string with
 * a real credential's shape is exactly what `scripts/secret_scan.py` exists to
 * keep out of this repository, and a test fixture is not an exemption from
 * that rule — a committed key-shaped literal trains reviewers and scanners to
 * expect false positives, which is how a real one eventually gets through.
 *
 * Assembling them gives the assertions the shape they need to be meaningful
 * while leaving nothing in the source for a scanner to find.
 */

/** Provider-API-key shape: `sk-` plus a long opaque tail. */
export const API_KEY_SENTINEL = ['sk', 'live', 'A'.repeat(8) + 'B'.repeat(8) + '9876'].join('-');

/** Bearer-token shape, as an Authorization header value would carry it. */
export const BEARER_SENTINEL = `Bearer ${API_KEY_SENTINEL}`;

/** JWT shape, as a LEGACY Supabase service-role credential would look. */
export const JWT_SENTINEL = ['eyJhbGciOiJIUzI1NiJ9', 'eyJyb2xlIjoic2VydmljZSJ9', 'not-a-real-signature'].join('.');

/**
 * MODERN Supabase server-side secret key shape (`sb_secret_…`).
 *
 * This is the format `docs/production-readiness/DEPLOYMENT.md` (Supabase server-side key policy) mandates for
 * production and forbids ever reaching the browser. It shares no prefix with
 * the legacy JWT above, so `JWT_SENTINEL` proves nothing about it — the two
 * must be swept independently.
 */
export const SUPABASE_SECRET_SENTINEL =
  ['sb', 'secret', 'A'.repeat(10) + 'B'.repeat(10) + '1234'].join('_');

/**
 * PUBLIC Supabase configuration the browser is MEANT to hold.
 *
 * Asserted UNTOUCHED: redacting it would hide a legitimate public value while
 * protecting nothing, so the redactor targets `sb_secret_` and not `sb_`.
 */
export const SUPABASE_PUBLISHABLE_PUBLIC =
  ['sb', 'publishable', 'C'.repeat(10) + 'D'.repeat(10) + '5678'].join('_');

/** The prefix a leak assertion greps for; kept below the scanner's threshold. */
export const API_KEY_PREFIX = 'sk-live-';

/**
 * Every sentinel, for the sweeps that place a credential in EVERY durable
 * string position the product surface can render and then assert that none of
 * them survives into the display model or the DOM.
 */
export const ALL_SECRET_SENTINELS = [
  API_KEY_SENTINEL,
  BEARER_SENTINEL,
  JWT_SENTINEL,
  SUPABASE_SECRET_SENTINEL,
] as const;

/** Fragments that must not survive even partially redacted. */
export const SECRET_FRAGMENTS = [
  API_KEY_PREFIX, 'eyJhbGciOiJIUzI1NiJ9', 'Bearer ', 'sb_secret_',
] as const;
