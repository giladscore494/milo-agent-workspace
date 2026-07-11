import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  checkRateLimit,
  normalizeForwardedIp,
  resetRateLimiterForTests,
} from '../lib/server/rateLimit';
import {
  MemoryRateLimitStore,
  RateLimitStoreUnavailableError,
  UpstashRateLimitStore,
  hashRateLimitIdentifier,
} from '../lib/server/rateLimitStore';

const RATE_ENV_KEYS = [
  'GATEWAY_RATE_LIMIT_UNAUTH_REQUESTS',
  'GATEWAY_RATE_LIMIT_UNAUTH_WINDOW_MS',
  'GATEWAY_RATE_LIMIT_RUN_CREATION_REQUESTS',
  'GATEWAY_RATE_LIMIT_POLLING_REQUESTS',
  'UPSTASH_REDIS_REST_URL',
  'UPSTASH_REDIS_REST_TOKEN',
  'VERCEL_ENV',
];

describe('gateway rate limiting', () => {
  beforeEach(() => {
    resetRateLimiterForTests();
  });

  afterEach(() => {
    for (const key of RATE_ENV_KEYS) delete process.env[key];
    vi.useRealTimers();
    resetRateLimiterForTests();
  });

  it('normalizes forwarded IPs and collapses malformed headers', () => {
    expect(normalizeForwardedIp(' 203.0.113.1, 10.0.0.1 ')).toBe('203.0.113.1');
    expect(normalizeForwardedIp('<script>alert(1)</script>')).toBe('invalid-ip');
    expect(normalizeForwardedIp(null)).toBe('invalid-ip');
    expect(normalizeForwardedIp('a'.repeat(500))).toBe('invalid-ip');
  });

  it('hashes identifiers so raw tokens never become keys', () => {
    const hashed = hashRateLimitIdentifier('user:secret-token-value');
    expect(hashed).not.toContain('secret');
    expect(hashed).toHaveLength(32);
  });

  it('limits and reports retry-after once the window is full', async () => {
    process.env.GATEWAY_RATE_LIMIT_UNAUTH_REQUESTS = '2';
    await checkRateLimit('unauthenticated', '203.0.113.1');
    await checkRateLimit('unauthenticated', '203.0.113.1');
    const limited = await checkRateLimit('unauthenticated', '203.0.113.1');
    expect(limited.allowed).toBe(false);
    expect(limited.retryAfterSeconds).toBeGreaterThanOrEqual(1);
    expect(limited.retryAfterSeconds).toBeLessThanOrEqual(60);
  });

  it('isolates users and categories from each other', async () => {
    process.env.GATEWAY_RATE_LIMIT_RUN_CREATION_REQUESTS = '1';
    expect((await checkRateLimit('run_creation', 'user:a')).allowed).toBe(true);
    expect((await checkRateLimit('run_creation', 'user:a')).allowed).toBe(false);
    expect((await checkRateLimit('run_creation', 'user:b')).allowed).toBe(true);
    expect((await checkRateLimit('polling', 'user:a')).allowed).toBe(true);
  });

  it('expires memory-store windows', async () => {
    let now = 0;
    const store = new MemoryRateLimitStore(() => now);
    expect((await store.increment('k', 1000)).count).toBe(1);
    expect((await store.increment('k', 1000)).count).toBe(2);
    now = 1500;
    expect((await store.increment('k', 1000)).count).toBe(1);
  });

  it('bounds memory-store key cardinality under a flood', async () => {
    const store = new MemoryRateLimitStore(() => 0);
    for (let i = 0; i < 12_000; i += 1) {
      await store.increment(`key-${i}`, 60_000);
    }
    expect(store.bucketCount()).toBeLessThanOrEqual(10_000);
  });

  it('shares counts across limiter instances through the same store backend', async () => {
    const counters = new Map<string, number>();
    const fakeFetch: typeof fetch = async (_url, init) => {
      const commands = JSON.parse(String(init?.body));
      const key = commands[0][1] as string;
      counters.set(key, (counters.get(key) ?? 0) + 1);
      return new Response(
        JSON.stringify([
          { result: counters.get(key) },
          { result: 1 },
          { result: 30_000 },
        ]),
        { status: 200 },
      );
    };
    const instanceA = new UpstashRateLimitStore('https://redis.example', 't', fakeFetch);
    const instanceB = new UpstashRateLimitStore('https://redis.example', 't', fakeFetch);
    process.env.GATEWAY_RATE_LIMIT_RUN_CREATION_REQUESTS = '2';
    expect((await checkRateLimit('run_creation', 'user:a', instanceA)).allowed).toBe(true);
    expect((await checkRateLimit('run_creation', 'user:a', instanceB)).allowed).toBe(true);
    const third = await checkRateLimit('run_creation', 'user:a', instanceA);
    expect(third.allowed).toBe(false);
  });

  it('handles concurrent increments without losing counts', async () => {
    const store = new MemoryRateLimitStore(() => 0);
    const results = await Promise.all(
      Array.from({ length: 25 }, () => store.increment('concurrent', 60_000)),
    );
    const counts = results.map((r) => r.count).sort((a, b) => a - b);
    expect(counts[counts.length - 1]).toBe(25);
    expect(new Set(counts).size).toBe(25);
  });

  it('fails closed for mutations when Redis is unavailable', async () => {
    const brokenFetch: typeof fetch = async () => {
      throw new Error('connection refused');
    };
    const broken = new UpstashRateLimitStore('https://redis.example', 't', brokenFetch);
    const decision = await checkRateLimit('run_creation', 'user:a', broken);
    expect(decision.allowed).toBe(false);
    expect(decision.unavailable).toBe(true);
  });

  it('degrades reads to the per-instance store when Redis is unavailable', async () => {
    const brokenFetch: typeof fetch = async () => {
      throw new Error('connection refused');
    };
    const broken = new UpstashRateLimitStore('https://redis.example', 't', brokenFetch);
    const decision = await checkRateLimit('polling', 'user:a', broken);
    expect(decision.allowed).toBe(true);
    expect(decision.unavailable).toBe(false);
  });

  it('fails closed for mutation categories in production without a shared store', async () => {
    process.env.VERCEL_ENV = 'production';
    const decision = await checkRateLimit('run_creation', 'user:a');
    expect(decision.allowed).toBe(false);
    expect(decision.unavailable).toBe(true);
    // Reads keep working on the documented fail-safe path.
    const read = await checkRateLimit('polling', 'user:a');
    expect(read.allowed).toBe(true);
  });

  it('propagates store errors distinctly from limit rejections', async () => {
    const store = new UpstashRateLimitStore('https://redis.example', 't', async () =>
      new Response('oops', { status: 500 }),
    );
    await expect(store.increment('k', 1000)).rejects.toBeInstanceOf(
      RateLimitStoreUnavailableError,
    );
  });
});
