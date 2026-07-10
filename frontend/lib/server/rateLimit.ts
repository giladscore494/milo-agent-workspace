type Bucket = { count: number; resetAt: number };

// Vercel serverless instances do not share memory. This conservative
// in-memory limiter is a fail-safe fallback per warm instance, not a global
// production quota: a shared store (Upstash/Redis or equivalent) is still
// required before broad public access.
const buckets = new Map<string, Bucket>();

// Hard bound so a flood of unique keys cannot grow the map without limit.
const MAX_BUCKETS = 10_000;
let nextSweepAt = 0;

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

function sweepStaleBuckets(now: number, windowMs: number): void {
  if (now < nextSweepAt && buckets.size < MAX_BUCKETS) return;
  nextSweepAt = now + windowMs;
  for (const [key, bucket] of buckets) {
    if (bucket.resetAt <= now) buckets.delete(key);
  }
  // Map preserves insertion order, so the first keys are the oldest buckets.
  while (buckets.size >= MAX_BUCKETS) {
    const oldest = buckets.keys().next().value;
    if (oldest === undefined) break;
    buckets.delete(oldest);
  }
}

export type RateLimitResult = { allowed: boolean; retryAfterSeconds: number };

export function checkGatewayRateLimit(rawKey: string | null): RateLimitResult {
  const limit = readPositiveNumber('GATEWAY_RATE_LIMIT_REQUESTS', 60);
  const windowMs = readPositiveNumber('GATEWAY_RATE_LIMIT_WINDOW_MS', 60_000);
  const key = normalizeRateLimitKey(rawKey);
  const now = Date.now();

  sweepStaleBuckets(now, windowMs);

  const bucket = buckets.get(key);
  if (!bucket || bucket.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + windowMs });
    return { allowed: true, retryAfterSeconds: 0 };
  }
  if (bucket.count >= limit) {
    return { allowed: false, retryAfterSeconds: Math.max(1, Math.ceil((bucket.resetAt - now) / 1000)) };
  }
  bucket.count += 1;
  return { allowed: true, retryAfterSeconds: 0 };
}

export function rateLimitBucketCountForTests(): number {
  return buckets.size;
}
