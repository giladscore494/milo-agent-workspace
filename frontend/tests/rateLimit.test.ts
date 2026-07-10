import { describe, expect, it } from 'vitest';
import { checkGatewayRateLimit, normalizeRateLimitKey } from '../lib/server/rateLimit';

describe('gateway rate limiting', () => {
  it('normalizes client IP keys conservatively', () => {
    expect(normalizeRateLimitKey(' 203.0.113.1, 10.0.0.1 ')).toBe('203.0.113.1');
    expect(normalizeRateLimitKey('bad key!')).toBe('badkey');
  });

  it('falls back safely for invalid configuration', () => {
    process.env.GATEWAY_RATE_LIMIT_REQUESTS = 'not-a-number';
    process.env.GATEWAY_RATE_LIMIT_WINDOW_MS = '-1';
    expect(checkGatewayRateLimit('rate-limit-invalid-config')).toBe(true);
  });
});
