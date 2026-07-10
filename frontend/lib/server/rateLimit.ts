type Bucket = { count: number; resetAt: number };

// Vercel serverless instances do not share memory. This conservative in-memory
// limiter is a fail-safe fallback per warm instance, not a global quota system.
const buckets = new Map<string, Bucket>();

function readPositiveNumber(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return value;
}

export function normalizeRateLimitKey(value: string | null): string {
  const first = value?.split(',')[0]?.trim().toLowerCase() || 'anonymous';
  return first.replace(/[^a-z0-9:.\-[\]]/g, '').slice(0, 128) || 'anonymous';
}

export function checkGatewayRateLimit(rawKey: string | null): boolean {
  const limit = readPositiveNumber('GATEWAY_RATE_LIMIT_REQUESTS', 60);
  const windowMs = readPositiveNumber('GATEWAY_RATE_LIMIT_WINDOW_MS', 60_000);
  const key = normalizeRateLimitKey(rawKey);
  const now = Date.now();
  const bucket = buckets.get(key);

  if (!bucket || bucket.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + windowMs });
    return true;
  }
  if (bucket.count >= limit) return false;
  bucket.count += 1;
  return true;
}
