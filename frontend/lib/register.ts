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

export type RegisterView = {
  canCapture: boolean;
  canRefreshDirectory: boolean;
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
  return {
    canCapture: source.can_capture,
    canRefreshDirectory: source.can_refresh_directory,
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
