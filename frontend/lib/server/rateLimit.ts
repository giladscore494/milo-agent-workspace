type Bucket = { count: number; resetAt: number };

// Vercel serverless instances do not share memory. This conservative in-memory
// limiter is a fail-safe fallback per warm instance, not a global quota system.
const buckets = new Map<string, Bucket>();

export function checkGatewayRateLimit(key: string): boolean {
  const limit = Number(process.env.GATEWAY_RATE_LIMIT_REQUESTS ?? 60);
  const windowMs = Number(process.env.GATEWAY_RATE_LIMIT_WINDOW_MS ?? 60_000);
  const now = Date.now();
  const bucket = buckets.get(key);

  if (!Number.isFinite(limit) || limit <= 0) return false;
  if (!bucket || bucket.resetAt <= now) {
    buckets.set(key, { count: 1, resetAt: now + windowMs });
    return true;
  }
  if (bucket.count >= limit) return false;
  bucket.count += 1;
  return true;
}
