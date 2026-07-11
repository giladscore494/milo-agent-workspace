import {
  MemoryRateLimitStore,
  RateLimitStore,
  RateLimitStoreUnavailableError,
  UpstashRateLimitStore,
  hashRateLimitIdentifier,
} from '@/lib/server/rateLimitStore';

/**
 * Gateway rate limiting by category.
 *
 * Production requires the shared Upstash store. When it is missing or
 * unreachable, mutation categories fail closed (503) while read categories
 * fall back to the per-instance memory store — this keeps the documented
 * read-only workspace alive while execution stays disabled, matching the
 * "fail closed or remain execution-disabled" production rule.
 */

export type RateLimitCategory =
  | 'unauthenticated'
  | 'auth_pressure'
  | 'authenticated'
  | 'polling'
  | 'run_creation'
  | 'cancellation';

const MUTATION_CATEGORIES: ReadonlySet<RateLimitCategory> = new Set([
  'run_creation',
  'cancellation',
]);

const DEFAULT_LIMITS: Record<RateLimitCategory, { limit: number; windowMs: number }> = {
  unauthenticated: { limit: 30, windowMs: 60_000 },
  auth_pressure: { limit: 30, windowMs: 60_000 },
  authenticated: { limit: 120, windowMs: 60_000 },
  polling: { limit: 120, windowMs: 60_000 },
  run_creation: { limit: 5, windowMs: 60_000 },
  cancellation: { limit: 10, windowMs: 60_000 },
};

const ENV_PREFIX: Record<RateLimitCategory, string> = {
  unauthenticated: 'GATEWAY_RATE_LIMIT_UNAUTH',
  auth_pressure: 'GATEWAY_RATE_LIMIT_AUTH_PRESSURE',
  authenticated: 'GATEWAY_RATE_LIMIT_AUTHENTICATED',
  polling: 'GATEWAY_RATE_LIMIT_POLLING',
  run_creation: 'GATEWAY_RATE_LIMIT_RUN_CREATION',
  cancellation: 'GATEWAY_RATE_LIMIT_CANCELLATION',
};

function readPositiveNumber(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === '') return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return value;
}

export function normalizeForwardedIp(value: string | null): string {
  const first = value?.split(',')[0]?.trim().toLowerCase() ?? '';
  // Loose IPv4/IPv6 shape check; anything else collapses to one bucket so
  // header garbage cannot create unbounded key cardinality.
  if (!/^[0-9a-f:.\[\]]{3,45}$/.test(first)) return 'invalid-ip';
  return first;
}

export type RateLimitDecision = {
  allowed: boolean;
  retryAfterSeconds: number;
  /** True when the shared store was required but unavailable. */
  unavailable: boolean;
};

let memoryStore = new MemoryRateLimitStore();
let sharedStore: RateLimitStore | null | undefined;

function resolveSharedStore(): RateLimitStore | null {
  if (sharedStore !== undefined) return sharedStore;
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  sharedStore = url && token ? new UpstashRateLimitStore(url, token) : null;
  return sharedStore;
}

export function isProductionGateway(): boolean {
  return (process.env.VERCEL_ENV ?? process.env.NODE_ENV) === 'production';
}

export function resetRateLimiterForTests(): void {
  memoryStore = new MemoryRateLimitStore();
  sharedStore = undefined;
}

export async function checkRateLimit(
  category: RateLimitCategory,
  identifier: string,
  storeOverride?: RateLimitStore,
): Promise<RateLimitDecision> {
  const limit = readPositiveNumber(`${ENV_PREFIX[category]}_REQUESTS`, DEFAULT_LIMITS[category].limit);
  const windowMs = readPositiveNumber(`${ENV_PREFIX[category]}_WINDOW_MS`, DEFAULT_LIMITS[category].windowMs);
  const key = `rl:${category}:${hashRateLimitIdentifier(identifier)}`;
  const shared = storeOverride ?? resolveSharedStore();
  const mustBeShared = isProductionGateway() && MUTATION_CATEGORIES.has(category);

  let store: RateLimitStore;
  if (shared) {
    store = shared;
  } else if (mustBeShared) {
    // Shared store required but not configured: fail closed.
    return { allowed: false, retryAfterSeconds: 60, unavailable: true };
  } else {
    store = memoryStore;
  }

  try {
    const state = await store.increment(key, windowMs);
    if (state.count > limit) {
      return {
        allowed: false,
        retryAfterSeconds: Math.max(1, Math.ceil((state.resetAtMs - Date.now()) / 1000)),
        unavailable: false,
      };
    }
    return { allowed: true, retryAfterSeconds: 0, unavailable: false };
  } catch (error) {
    if (error instanceof RateLimitStoreUnavailableError) {
      if (MUTATION_CATEGORIES.has(category)) {
        // Redis down: never let mutations through unmetered.
        return { allowed: false, retryAfterSeconds: 30, unavailable: true };
      }
      // Reads degrade to the per-instance fail-safe store.
      const state = await memoryStore.increment(key, windowMs);
      if (state.count > limit) {
        return {
          allowed: false,
          retryAfterSeconds: Math.max(1, Math.ceil((state.resetAtMs - Date.now()) / 1000)),
          unavailable: false,
        };
      }
      return { allowed: true, retryAfterSeconds: 0, unavailable: false };
    }
    throw error;
  }
}

// Backwards-compatible helper used by the legacy gateway path/tests.
export function normalizeRateLimitKey(value: string | null): string {
  const first = value?.split(',')[0]?.trim().toLowerCase() || 'anonymous';
  return first.replace(/[^a-z0-9:.\-[\]]/g, '').slice(0, 128) || 'anonymous';
}
