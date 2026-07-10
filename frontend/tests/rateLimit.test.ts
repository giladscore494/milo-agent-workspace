import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  checkGatewayRateLimit,
  normalizeRateLimitKey,
  rateLimitBucketCountForTests,
} from '../lib/server/rateLimit';

describe('gateway rate limiting', () => {
  afterEach(() => {
    delete process.env.GATEWAY_RATE_LIMIT_REQUESTS;
    delete process.env.GATEWAY_RATE_LIMIT_WINDOW_MS;
    vi.useRealTimers();
  });

  it('normalizes client IP keys conservatively', () => {
    expect(normalizeRateLimitKey(' 203.0.113.1, 10.0.0.1 ')).toBe('203.0.113.1');
    expect(normalizeRateLimitKey('bad key!')).toBe('badkey');
  });

  it('falls back safely for invalid configuration', () => {
    process.env.GATEWAY_RATE_LIMIT_REQUESTS = 'not-a-number';
    process.env.GATEWAY_RATE_LIMIT_WINDOW_MS = '-1';
    expect(checkGatewayRateLimit('rate-limit-invalid-config').allowed).toBe(true);
  });

  it('returns a Retry-After hint once the per-instance limit is hit', () => {
    process.env.GATEWAY_RATE_LIMIT_REQUESTS = '2';
    process.env.GATEWAY_RATE_LIMIT_WINDOW_MS = '60000';
    expect(checkGatewayRateLimit('rate-limit-hint').allowed).toBe(true);
    expect(checkGatewayRateLimit('rate-limit-hint').allowed).toBe(true);
    const limited = checkGatewayRateLimit('rate-limit-hint');
    expect(limited.allowed).toBe(false);
    expect(limited.retryAfterSeconds).toBeGreaterThanOrEqual(1);
    expect(limited.retryAfterSeconds).toBeLessThanOrEqual(60);
  });

  it('expires stale buckets after the window passes', () => {
    vi.useFakeTimers();
    process.env.GATEWAY_RATE_LIMIT_REQUESTS = '1';
    process.env.GATEWAY_RATE_LIMIT_WINDOW_MS = '1000';
    expect(checkGatewayRateLimit('rate-limit-expiry').allowed).toBe(true);
    expect(checkGatewayRateLimit('rate-limit-expiry').allowed).toBe(false);
    vi.advanceTimersByTime(1500);
    expect(checkGatewayRateLimit('rate-limit-expiry').allowed).toBe(true);
  });

  it('keeps the bucket map bounded under a flood of unique keys', () => {
    vi.useFakeTimers();
    process.env.GATEWAY_RATE_LIMIT_WINDOW_MS = '60000';
    for (let i = 0; i < 12_000; i += 1) {
      checkGatewayRateLimit(`10.0.${Math.floor(i / 250)}.${i % 250}-${i}`);
    }
    expect(rateLimitBucketCountForTests()).toBeLessThanOrEqual(10_000);
  });
});
