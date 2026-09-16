export function safeText(value: unknown): string { return String(value ?? '').replace(/[<>]/g, c => ({'<':'‹','>':'›'}[c]!)); }
export function safeUrl(value?: string): string | undefined { if (!value) return undefined; try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.toString() : undefined; } catch { return undefined; } }
export function redactSecrets(value: unknown): unknown { const text = JSON.stringify(value, null, 2); return JSON.parse(text.replace(/(service[_-]?role|kimi|moonshot|api[_-]?key|authorization|secret)("?\s*[:=]\s*"?)[^",\n}]+/gi, '$1$2[REDACTED]').replace(/sk-[A-Za-z0-9_-]{8,}/g, '[REDACTED]')); }

/**
 * Credential-shaped material, in ONE durable string.
 *
 * `redactSecrets` above redacts a whole JSON structure and is what the V1
 * sanitized-output path and the Inspector use; its behaviour is deliberately
 * untouched. This function is its per-string counterpart, for surfaces that
 * render typed fields rather than a serialized blob — every durable string the
 * Swarm V2 final-result surface can show goes through it.
 *
 * It is DEFENSE IN DEPTH and nothing more. The typed contract in
 * lib/finalResult.ts is what keeps unknown fields off the surface; this is what
 * keeps a credential out of a field the contract legitimately allows. A string
 * value, a review reason, a record key, an identifier or a scope value is all
 * ordinary product data as far as the contract is concerned, and none of them
 * is proof that a credential cannot be inside.
 *
 * Deterministic and order-dependent: `Bearer <token>` is collapsed whole before
 * the token shapes run, so a redacted token can never leave a bare `Bearer`
 * behind. Over-redaction is the intended failure direction — a legitimate value
 * that happens to look like a key is shown as `[REDACTED]`, which is lossy and
 * safe, rather than printed, which is not.
 */
const SECRET_PATTERNS: readonly RegExp[] = [
  // PEM blocks: the header alone is enough to know what follows.
  /-----BEGIN[^-]*-----[\s\S]*?(?:-----END[^-]*-----|$)/gi,
  // `Bearer <token>` as a whole, before the token shapes below.
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}={0,2}/gi,
  // LEGACY Supabase service-role shape, and JWTs generally: two or three
  // base64url segments. This does NOT cover the modern key format below.
  /\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?/g,
  /**
   * MODERN Supabase server-side secret key.
   *
   * `docs/deployment/cloud-run-production.md` mandates this format for the
   * production server-side credential and forbids it ever reaching the
   * browser; `backend/production_config.py` and `scripts/check_unsafe_defaults.py`
   * both blocklist the `sb_secret` substring. A legacy JWT sentinel proves
   * nothing about it — the two formats share no prefix — so it is matched
   * explicitly.
   *
   * The credential's ENV VAR NAME is deliberately not written here:
   * `scripts/release/check-vercel-config.sh` blocks any reference to it from
   * `frontend/lib` or `frontend/app`, and that guard is worth more than the
   * convenience of naming it in a comment.
   *
   * Deliberately anchored on `sb_secret_` and NOT on `sb_`: a publishable or
   * anon key (`sb_publishable_…`) is public configuration the browser is
   * MEANT to hold, and redacting it would hide legitimate values while
   * protecting nothing.
   */
  /\bsb_secret_[A-Za-z0-9_-]{8,}/gi,
  // Provider API key shape.
  /\bsk-[A-Za-z0-9_-]{8,}/gi,
];

/**
 * Labelled secrets: the LABEL is kept, only the value is replaced.
 *
 * The lookahead skips a value the shape patterns above already collapsed, so
 * `authorization=Bearer <token>` ends as `authorization=[REDACTED]` rather
 * than re-wrapping the marker and leaving a stray bracket behind.
 */
const LABELLED_SECRET =
  /((?:service[_-]?role|api[_-]?key|apikey|authorization|secret|password|credential|access[_-]?token|refresh[_-]?token|private[_-]?key|lease[_-]?token)["']?\s*[:=]\s*["']?)(?!\[REDACTED\])([^"'\s,;}\]]+)/gi;

export const REDACTED = '[REDACTED]';

export function redactSecretText(value: string): string {
  let text = String(value ?? '');
  for (const pattern of SECRET_PATTERNS) {
    // Each pattern carries its own `g` flag, so reset lastIndex: these are
    // module-level regexes and a stale index would skip the next string.
    pattern.lastIndex = 0;
    text = text.replace(pattern, REDACTED);
  }
  LABELLED_SECRET.lastIndex = 0;
  return text.replace(LABELLED_SECRET, `$1${REDACTED}`);
}
