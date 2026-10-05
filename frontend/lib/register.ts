import { ApiError } from './api';

/**
 * PR-D1 — the Register page's data, validated before anything renders.
 *
 * The server (`backend/catalog/register/service.py`) derives every state from
 * durable rows; this module only checks the shape and turns it into what the
 * page shows. A body that does not match is `undefined` -- never partially
 * rendered.
 *
 * A tozar is kept EXACTLY as the server sent it: it is the register's own
 * spelling and goes back to the server verbatim in a capture request, so it
 * is never trimmed, normalized or redacted here. Only rendering escapes it.
 */

export type RegisterUnitState = 'not_captured' | 'capturing' | 'captured' | 'failed';

export type RegisterUnit = {
  tozar: string;
  /** PR-D3: the approved canonical manufacturer (the tozar stays exact). */
  canonicalManufacturer?: string;
  expectedRows: number;
  state: RegisterUnitState;
  snapshotKey?: string;
  capturedRows?: number;
  apiTotal?: number;
  verified?: boolean;
  failureCode?: string;
  measuredBytes?: number;
  measuredBytesPerRow?: number;
  registerVersion?: string;
};

export type RegisterCapacity = {
  capacityBytes: number;
  threshold: number;
  limitBytes: number;
  bytesPerRowEstimate: number;
  currentBytes: number;
  overThreshold: boolean;
};

/** PR-SYNC-1: the last sync's summary fields, in the server's order. */
export type SyncField = { name: string; value: string };

/** PR-SYNC-2: the Auto sync block -- the switch, a pause and its next step, the ticks. */
export type AutoSyncTick = {
  at: string; decision: 'start' | 'skip'; reason: string; backlog: string; dbMb: string; warning?: string;
};

export type AutoSync = {
  enabled: boolean;
  pausedReason?: string;
  pausedAt?: string;
  nextStep?: string;
  consecutiveThrottles: number;
  consecutiveFailures: number;
  lastTick?: AutoSyncTick;
  next: { decision: 'start' | 'skip'; reason: string };
  schedulerStale: boolean;
  dbWarning: boolean;
};

export type RegisterView = {
  canCapture: boolean;
  autoSync?: AutoSync;
  canRefreshDirectory: boolean;
  canSync: boolean;
  lastSync?: { fields: SyncField[]; finishedAt?: string };
  directory?: {
    registerVersion: string;
    resourceId: string;
    fetchedAt: string;
    unitCount: number;
    totalRows: number;
  };
  units: RegisterUnit[];
  totals: { unitsTotal: number; unitsCaptured: number; rowsTotal: number; rowsCaptured: number };
  capacity: RegisterCapacity;
  groupMaxRows: number;
};

export type CapacityNumbers = { currentBytes: number; projectedBytes: number; limitBytes: number };

const DIGEST = /^[0-9a-f]{64}$/;
const RESOURCE = /^[0-9a-f-]{36}$/;
const CODE = /^[A-Z][A-Z0-9_]{2,79}$/;
const SNAPSHOT_KEY = /^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$/;
const STATES: ReadonlySet<string> = new Set(['not_captured', 'capturing', 'captured', 'failed']);
const MAX_TOZAR_CHARS = 200;

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function count(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function optionalCount(value: unknown): number | undefined | null {
  if (value === null || value === undefined) return undefined;
  const read = count(value);
  return read === undefined ? null : read;
}

function unit(value: unknown): RegisterUnit | undefined {
  const source = asObject(value);
  const tozar = source.tozar;
  const expectedRows = count(source.expected_rows);
  if (typeof tozar !== 'string' || tozar.length < 1 || tozar.length > MAX_TOZAR_CHARS
      || expectedRows === undefined || typeof source.state !== 'string' || !STATES.has(source.state)) {
    return undefined;
  }
  const out: RegisterUnit = { tozar, expectedRows, state: source.state as RegisterUnitState };
  if (typeof source.canonical_manufacturer === 'string' && source.canonical_manufacturer.length >= 1
      && source.canonical_manufacturer.length <= 120) {
    out.canonicalManufacturer = source.canonical_manufacturer;
  }
  if (out.state === 'not_captured') return out;
  const numbers = {
    capturedRows: optionalCount(source.captured_rows), apiTotal: optionalCount(source.api_total),
    measuredBytes: optionalCount(source.measured_bytes),
    measuredBytesPerRow: optionalCount(source.measured_bytes_per_row),
  };
  if (Object.values(numbers).some((item) => item === null)) return undefined;
  Object.assign(out, Object.fromEntries(Object.entries(numbers).filter(([, item]) => item !== undefined)));
  if (source.snapshot_key !== null && source.snapshot_key !== undefined) {
    if (typeof source.snapshot_key !== 'string' || !SNAPSHOT_KEY.test(source.snapshot_key)) return undefined;
    out.snapshotKey = source.snapshot_key;
  }
  if (source.verified !== null && source.verified !== undefined) {
    if (typeof source.verified !== 'boolean') return undefined;
    out.verified = source.verified;
  }
  if (source.failure_code !== null && source.failure_code !== undefined) {
    if (typeof source.failure_code !== 'string' || !CODE.test(source.failure_code)) return undefined;
    out.failureCode = source.failure_code;
  }
  if (source.register_version !== null && source.register_version !== undefined) {
    if (typeof source.register_version !== 'string' || !DIGEST.test(source.register_version)) return undefined;
    out.registerVersion = source.register_version;
  }
  // Self-consistency: a failure always names its code; a capture always
  // names its snapshot and is verified (the database refuses anything else).
  if ((out.state === 'failed') !== (out.failureCode !== undefined)) return undefined;
  if (out.state === 'captured' && (out.snapshotKey === undefined || out.verified !== true)) return undefined;
  return out;
}

const SYNC_FIELDS = ['changed', 'directory_version', 'work', 'captured', 'reused', 'failed', 'deferred',
  'requests', 'stop', 'backlog', 'coverage'];

/** `SYNC_SUMMARY|name=value|...`: exactly the known names, in order, each a bounded token. */
export function parseSyncSummary(line: unknown): SyncField[] | undefined {
  if (typeof line !== 'string' || line.length > 400) return undefined;
  const [head, ...parts] = line.split('|');
  const fields = parts.map((part) => {
    const [name, value, extra] = part.split('=');
    return { name, value, extra };
  });
  if (head !== 'SYNC_SUMMARY' || fields.length !== SYNC_FIELDS.length
      || fields.some((field, index) => field.name !== SYNC_FIELDS[index] || field.extra !== undefined
        || !/^[a-z0-9/]{1,40}$/.test(field.value ?? ''))) {
    return undefined;
  }
  return fields.map(({ name, value }) => ({ name, value }));
}

const DECISIONS: ReadonlySet<string> = new Set(['start', 'skip']);
const TICK_VALUE = /^[A-Za-z0-9./-]{1,40}$/;

function shortText(value: unknown, max: number): string | undefined {
  return typeof value === 'string' && value.length >= 1 && value.length <= max ? value : undefined;
}

/** The Auto sync block, or `undefined` (the page then shows no Auto sync control). */
export function parseAutoSync(body: unknown): AutoSync | undefined {
  const source = asObject(body);
  const next = asObject(source.next);
  const throttles = count(source.consecutive_throttles);
  const failures = count(source.consecutive_failures);
  if (typeof source.enabled !== 'boolean' || typeof source.scheduler_stale !== 'boolean'
      || typeof source.db_warning !== 'boolean' || throttles === undefined || failures === undefined
      || typeof next.decision !== 'string' || !DECISIONS.has(next.decision)
      || typeof next.reason !== 'string' || !CODE.test(next.reason)) {
    return undefined;
  }
  const out: AutoSync = {
    enabled: source.enabled, consecutiveThrottles: throttles, consecutiveFailures: failures,
    next: { decision: next.decision as 'start' | 'skip', reason: next.reason },
    schedulerStale: source.scheduler_stale, dbWarning: source.db_warning,
  };
  if (source.paused_reason !== null && source.paused_reason !== undefined) {
    if (typeof source.paused_reason !== 'string' || !CODE.test(source.paused_reason)) return undefined;
    out.pausedReason = source.paused_reason;
    out.pausedAt = shortText(source.paused_at, 64);
    out.nextStep = shortText(source.next_step, 400);
  }
  if (source.last_tick !== null && source.last_tick !== undefined) {
    const tick = asObject(source.last_tick);
    const at = shortText(tick.at, 64);
    if (at === undefined || typeof tick.decision !== 'string' || !DECISIONS.has(tick.decision)
        || typeof tick.reason !== 'string' || !CODE.test(tick.reason)
        || typeof tick.backlog !== 'string' || !TICK_VALUE.test(tick.backlog)
        || typeof tick.db_mb !== 'string' || !TICK_VALUE.test(tick.db_mb)) {
      return undefined;
    }
    out.lastTick = { at, decision: tick.decision as 'start' | 'skip', reason: tick.reason,
      backlog: tick.backlog, dbMb: tick.db_mb };
    if (typeof tick.warning === 'string' && CODE.test(tick.warning)) out.lastTick.warning = tick.warning;
  }
  return out;
}

/** The Register page, or `undefined` when the body is not the expected shape. */
export function parseRegister(body: unknown): RegisterView | undefined {
  const source = asObject(body);
  if (source.available !== true || typeof source.can_capture !== 'boolean'
      || typeof source.can_refresh_directory !== 'boolean' || !Array.isArray(source.units)) {
    return undefined;
  }
  let directory: RegisterView['directory'];
  if (source.directory !== null && source.directory !== undefined) {
    const raw = asObject(source.directory);
    const unitCount = count(raw.unit_count);
    const totalRows = count(raw.total_rows);
    if (typeof raw.register_version !== 'string' || !DIGEST.test(raw.register_version)
        || typeof raw.resource_id !== 'string' || !RESOURCE.test(raw.resource_id)
        || typeof raw.fetched_at !== 'string' || raw.fetched_at.length > 64
        || unitCount === undefined || totalRows === undefined) {
      return undefined;
    }
    directory = { registerVersion: raw.register_version, resourceId: raw.resource_id,
      fetchedAt: raw.fetched_at, unitCount, totalRows };
  }
  const units = source.units.map(unit);
  if (units.some((item) => item === undefined)) return undefined;
  const totalsRaw = asObject(source.totals);
  const totals = {
    unitsTotal: count(totalsRaw.units_total), unitsCaptured: count(totalsRaw.units_captured),
    rowsTotal: count(totalsRaw.rows_total), rowsCaptured: count(totalsRaw.rows_captured),
  };
  const capacityRaw = asObject(source.capacity);
  const capacity = {
    capacityBytes: count(capacityRaw.capacity_bytes), limitBytes: count(capacityRaw.limit_bytes),
    bytesPerRowEstimate: count(capacityRaw.bytes_per_row_estimate), currentBytes: count(capacityRaw.current_bytes),
  };
  const threshold = capacityRaw.threshold;
  const groupMaxRows = count(source.group_max_rows);
  if (Object.values(totals).some((item) => item === undefined)
      || Object.values(capacity).some((item) => item === undefined)
      || typeof threshold !== 'number' || !(threshold > 0 && threshold <= 1)
      || typeof capacityRaw.over_threshold !== 'boolean' || groupMaxRows === undefined || groupMaxRows < 1) {
    return undefined;
  }
  const lastSyncRaw = asObject(source.last_sync);
  const syncFields = parseSyncSummary(lastSyncRaw.summary);
  return {
    canCapture: source.can_capture,
    canRefreshDirectory: source.can_refresh_directory,
    canSync: source.can_sync === true,
    autoSync: parseAutoSync(source.auto_sync),
    lastSync: syncFields && { fields: syncFields,
      finishedAt: typeof lastSyncRaw.finished_at === 'string' ? lastSyncRaw.finished_at.slice(0, 64) : undefined },
    directory,
    units: units as RegisterUnit[],
    totals: totals as RegisterView['totals'],
    capacity: { ...(capacity as Omit<RegisterCapacity, 'threshold' | 'overThreshold'>), threshold,
      overThreshold: capacityRaw.over_threshold },
    groupMaxRows,
  };
}

/** The numbers of a capacity refusal, validated, or `undefined`. */
export function capacityRefusal(error: unknown): CapacityNumbers | undefined {
  if (!(error instanceof ApiError) || error.code !== 'CATALOG_CAPACITY_THRESHOLD_EXCEEDED') return undefined;
  const raw = asObject(error.details);
  const numbers = {
    currentBytes: count(raw.current_bytes), projectedBytes: count(raw.projected_bytes), limitBytes: count(raw.limit_bytes),
  };
  return Object.values(numbers).some((item) => item === undefined) ? undefined : (numbers as CapacityNumbers);
}

/** May this unit be captured now (the server decides again)? */
export function capturable(item: RegisterUnit, registerVersion: string | undefined): boolean {
  if (item.state === 'not_captured' || item.state === 'failed') return true;
  // Captured under an older directory version: this version is a new request.
  return item.state === 'captured' && registerVersion !== undefined && item.registerVersion !== undefined
    && item.registerVersion !== registerVersion;
}

/**
 * Can these tozars go in ONE request? A group covers at most the cap of
 * expected rows; a single tozar larger than the cap is captured alone.
 */
export function groupFits(selected: readonly RegisterUnit[], groupMaxRows: number): boolean {
  if (selected.length === 0) return false;
  if (selected.length === 1) return true;
  return selected.reduce((sum, item) => sum + item.expectedRows, 0) <= groupMaxRows;
}

export const REGISTER_STATE_COPY: Readonly<Record<RegisterUnitState, string>> = {
  not_captured: 'Not captured',
  capturing: 'Capturing',
  captured: 'Captured',
  failed: 'Failed',
};

/** Bytes as a short decimal figure (MB = 1,000,000 bytes, the capacity's unit). */
export function formatBytes(bytes: number): string {
  if (bytes >= 1_000_000_000) return `${(bytes / 1_000_000_000).toFixed(2)} GB`;
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${(bytes / 1_000).toFixed(1)} kB`;
  return `${bytes} B`;
}

export function formatCount(value: number): string {
  return value.toLocaleString('en-US');
}

/** PR-SYNC-2: each tick reason in a few words (an unknown code shows as itself). */
export const AUTO_SYNC_REASON_COPY: Readonly<Record<string, string>> = {
  SYNC_FIRST: 'no sync has run yet',
  SYNC_BACKLOG: 'backlog left, an hour since the last sync',
  SYNC_DAILY_CHECK: 'complete; the daily change check',
  SYNC_NOT_DUE: 'not due yet',
  SYNC_BUSY: 'a capture, refresh or sync is running',
  SYNC_COOLING_DOWN: 'cooling down after a firewall block',
  SYNC_AUTO_OFF: 'Auto sync is off',
  SYNC_REGISTER_DISABLED: 'register capture is off on the server',
  SYNC_PAUSED_CAPACITY: 'the database reached its capacity threshold',
  SYNC_PAUSED_FAILING: 'two syncs in a row failed',
};

