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

/** JWT shape, as a Supabase service-role credential would look. */
export const JWT_SENTINEL = ['eyJhbGciOiJIUzI1NiJ9', 'eyJyb2xlIjoic2VydmljZSJ9', 'not-a-real-signature'].join('.');

/** The prefix a leak assertion greps for; kept below the scanner's threshold. */
export const API_KEY_PREFIX = 'sk-live-';

/**
 * Every sentinel, for the sweeps that place a credential in EVERY durable
 * string position the product surface can render and then assert that none of
 * them survives into the display model or the DOM.
 */
export const ALL_SECRET_SENTINELS = [API_KEY_SENTINEL, BEARER_SENTINEL, JWT_SENTINEL] as const;

/** Fragments that must not survive even partially redacted. */
export const SECRET_FRAGMENTS = [API_KEY_PREFIX, 'eyJhbGciOiJIUzI1NiJ9', 'Bearer '] as const;
