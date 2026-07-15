import { createHash } from 'node:crypto';

/**
 * Rate limiter storage abstraction.
 *
 * Production uses Upstash Redis over REST so every serverless instance
 * shares one window state. Local development, tests and the documented
 * read-only fallback use the deterministic in-memory store. Raw
 * identifiers (IPs, user IDs, tokens) are never used as keys directly:
 * callers hash them via {@link hashRateLimitIdentifier}.
 */

export type WindowState = {
  count: number;
  resetAtMs: number;
};

export interface RateLimitStore {
  /** Atomically increment the key's counter within a fixed window. */
  increment(key: string, windowMs: number): Promise<WindowState>;
}

export class RateLimitStoreUnavailableError extends Error {
  constructor(message = 'Shared rate limit store unavailable.') {
    super(message);
  }
}

export function hashRateLimitIdentifier(raw: string): string {
  return createHash('sha256').update(raw).digest('hex').slice(0, 32);
}

const MAX_BUCKETS = 10_000;

export class MemoryRateLimitStore implements RateLimitStore {
  private buckets = new Map<string, WindowState>();

  constructor(private now: () => number = Date.now) {}

  async increment(key: string, windowMs: number): Promise<WindowState> {
    const now = this.now();
    this.sweep(now);
    const bucket = this.buckets.get(key);
    if (!bucket || bucket.resetAtMs <= now) {
      const fresh = { count: 1, resetAtMs: now + windowMs };
      // Re-insert so Map ordering tracks recency for the cardinality cap.
      this.buckets.delete(key);
      this.buckets.set(key, fresh);
      return { ...fresh };
    }
    bucket.count += 1;
    return { ...bucket };
  }

  private sweep(now: number): void {
    if (this.buckets.size < MAX_BUCKETS) return;
    for (const [key, bucket] of this.buckets) {
      if (bucket.resetAtMs <= now) this.buckets.delete(key);
    }
    while (this.buckets.size >= MAX_BUCKETS) {
      const oldest = this.buckets.keys().next().value;
      if (oldest === undefined) break;
      this.buckets.delete(oldest);
    }
  }

  bucketCount(): number {
    return this.buckets.size;
  }
}

type FetchLike = typeof fetch;

/**
 * Upstash Redis REST-backed fixed-window store. Uses a single pipeline of
 * INCR + PEXPIRE(NX) + PTTL, so concurrent instances share exact counts.
 */
export class UpstashRateLimitStore implements RateLimitStore {
  constructor(
    private url: string,
    private token: string,
    private fetchImpl: FetchLike = fetch,
    private now: () => number = Date.now,
  ) {}

  async increment(key: string, windowMs: number): Promise<WindowState> {
    let response: Response;
    try {
      response = await this.fetchImpl(`${this.url.replace(/\/$/, '')}/pipeline`, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${this.token}`,
          'content-type': 'application/json',
        },
        body: JSON.stringify([
          ['INCR', key],
          ['PEXPIRE', key, String(windowMs), 'NX'],
          ['PTTL', key],
        ]),
        cache: 'no-store',
      });
    } catch {
      throw new RateLimitStoreUnavailableError();
    }
    if (!response.ok) {
      throw new RateLimitStoreUnavailableError(`Upstash responded ${response.status}.`);
    }
    let payload: Array<{ result?: unknown; error?: string }>;
    try {
      payload = await response.json();
    } catch {
      throw new RateLimitStoreUnavailableError('Upstash response was not JSON.');
    }
    if (!Array.isArray(payload) || payload.some((entry) => entry?.error)) {
      throw new RateLimitStoreUnavailableError('Upstash pipeline returned an error.');
    }
    const count = Number(payload[0]?.result);
    const ttlMs = Number(payload[2]?.result);
    if (!Number.isFinite(count) || count < 1) {
      throw new RateLimitStoreUnavailableError('Upstash returned an invalid counter.');
    }
    const effectiveTtl = Number.isFinite(ttlMs) && ttlMs > 0 ? ttlMs : windowMs;
    return { count, resetAtMs: this.now() + effectiveTtl };
  }
}
