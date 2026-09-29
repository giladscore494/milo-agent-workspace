/**
 * PR-CAT: the catalog browser's data (D5) and its "Add to plan" edit (D6).
 *
 * Every read is the server's bounded, paged discovery tree
 * (`GET /projects/{id}/catalog/browser/{level}`, PR-L1): this module never
 * asks for the register in bulk (rule 20), it only states which page. Each
 * answer is `unknown` until parsed here field by field; anything malformed
 * is unreadable, never rendered.
 *
 * "Add to plan" is expressed ONLY through the existing Mapping Plan contract
 * (`milo-work-scope/1`): whole manufacturers (directory keys) and one model-
 * year range. A model or a single variant cannot be added -- the contract has
 * no such selector -- so the browser offers manufacturers and a year range,
 * and says why models stay browse-only. What the coverage ledger already
 * settles (decision 19) is left out by the server when the plan's queue is
 * built; the browser never queues anything itself.
 */

import type { DirectoryEntry, WorkScopeEdit, WorkScopeLimits, WorkScopeState } from './workScope';

export const BROWSER_LEVELS = ['manufacturers', 'models', 'years', 'variants', 'facets'] as const;
export type BrowserLevel = (typeof BROWSER_LEVELS)[number];
export const PAGE_SIZE = 25;
const MAX_TEXT = 200;

export const SEGMENTS = ['private', 'commercial', 'unknown'] as const;
export type Segment = (typeof SEGMENTS)[number];
export const SEGMENT_COPY: Readonly<Record<Segment, string>> = {
  private: 'Private', commercial: 'Commercial', unknown: 'Unknown',
};

export type BrowserFilters = {
  segment?: Segment;
  yearFrom?: number;
  yearTo?: number;
  delekCd?: number;
  merkav?: string;
};

export type Page<T> = { total: number; limit: number; offset: number; items: T[] };
export type Manufacturer = { tozar: string; variants: number };
/** `null` is the register's own "not stated": listed, never opened (the
 *  next level needs a value to ask for). */
export type Model = { kinuyMishari: string | null; variants: number; yearMin: number | null; yearMax: number | null };
export type ModelYear = { year: number | null; variants: number };

export const COVERAGE_LEVELS = ['register', 'identity', 'government_fields'] as const;
export type CoverageLevel = (typeof COVERAGE_LEVELS)[number];
export const COVERAGE_LEVEL_COPY: Readonly<Record<CoverageLevel, string>> = {
  register: 'Register run', identity: 'Identity', government_fields: 'Government fields',
};
const COVERAGE_STATUSES = new Set(['enriched', 'pending', 'failed', 'unresolved_ambiguous', 'unresolved_not_found']);
export type CoverageBadge = { status: string; current: boolean } | null;

/** Level 1.5, in reading order: (field, label). Only these are shown. */
export const VARIANT_FIELDS: ReadonlyArray<readonly [string, string]> = [
  ['degem_nm', 'Model code'], ['ramat_gimur', 'Trim'], ['delek_nm', 'Fuel'],
  ['technologiat_hanaa_nm', 'Propulsion'], ['hanaa_nm', 'Drive'], ['merkav', 'Body'],
  ['automatic_ind', 'Automatic'], ['nefah_manoa', 'Displacement (cc)'], ['koah_sus', 'Power (as stated)'],
  ['mispar_dlatot', 'Doors'], ['mispar_moshavim', 'Seats'], ['mishkal_kolel', 'Total mass'],
  ['kosher_grira_im_blamim', 'Towing (braked)'], ['kosher_grira_bli_blamim', 'Towing (unbraked)'],
  ['kamut_co2_city', 'CO2 city'], ['kamut_co2_hway', 'CO2 highway'], ['co2_wltp', 'CO2 WLTP'],
  ['kvutzat_zihum', 'Pollution group'], ['madad_yarok', 'Green index'], ['nikud_betihut', 'Safety score'],
  ['ramat_eivzur_betihuty', 'Safety equipment level'], ['mispar_kariot_avir', 'Airbags'],
  ['abs_ind', 'ABS'], ['bakarat_yatzivut_ind', 'ESC'], ['sug_tkina_nm', 'Homologation standard'],
  ['nox_wltp', 'NOx WLTP'], ['co_wltp', 'CO WLTP'], ['hc_wltp', 'HC WLTP'], ['sug_mamir_nm', 'Converter type'],
  ['tozeret_nm', 'Manufacturer (register)'], ['tozeret_eretz_nm', 'Country of manufacture'],
];

export type Variant = {
  upstreamRecordId: string;
  snapshotKey: string;
  segment: Segment;
  fields: ReadonlyArray<readonly [string, string]>;
  coverage: Readonly<Record<CoverageLevel, CoverageBadge>>;
  parseIssues: number;
};

export type Facets = {
  segments: { value: Segment; variants: number }[];
  fuels: { delekCd: number; delekNm: string; variants: number }[];
  bodies: { merkav: string; variants: number }[];
  yearMin: number | null;
  yearMax: number | null;
};

function obj(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}
function count(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}
function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.slice(0, MAX_TEXT) : undefined;
}
function yearOrNull(value: unknown): number | null | undefined {
  if (value === null || value === undefined) return null;
  return count(value);
}
function segment(value: unknown): Segment | undefined {
  return (SEGMENTS as readonly unknown[]).includes(value) ? (value as Segment) : undefined;
}

/** One page, or undefined when the page or ANY item is unreadable. */
export function parsePage<T>(body: unknown, item: (value: unknown) => T | undefined): Page<T> | undefined {
  const source = obj(body);
  const [total, limit, offset] = [count(source.total), count(source.limit), count(source.offset)];
  if (total === undefined || limit === undefined || offset === undefined || !Array.isArray(source.items)) {
    return undefined;
  }
  const items = source.items.map(item);
  return items.every((value) => value !== undefined) ? { total, limit, offset, items: items as T[] } : undefined;
}

export function parseManufacturer(value: unknown): Manufacturer | undefined {
  const source = obj(value);
  const [tozar, variants] = [text(source.tozar), count(source.variants)];
  return tozar === undefined || variants === undefined ? undefined : { tozar, variants };
}

export function parseModel(value: unknown): Model | undefined {
  const source = obj(value);
  const kinuyMishari = source.kinuy_mishari === null ? null : text(source.kinuy_mishari);
  const variants = count(source.variants);
  const [yearMin, yearMax] = [yearOrNull(source.year_min), yearOrNull(source.year_max)];
  if (kinuyMishari === undefined || variants === undefined || yearMin === undefined || yearMax === undefined) {
    return undefined;
  }
  return { kinuyMishari, variants, yearMin, yearMax };
}

export function parseModelYear(value: unknown): ModelYear | undefined {
  const source = obj(value);
  const [year, variants] = [yearOrNull(source.shnat_yitzur), count(source.variants)];
  return year === undefined || variants === undefined ? undefined : { year, variants };
}

function display(field: string, value: unknown): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (field.endsWith('_ind')) return value === 1 ? 'Yes' : value === 0 ? 'No' : undefined;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return text(value);
}

export function parseVariant(value: unknown): Variant | undefined {
  const source = obj(value);
  const upstreamRecordId = text(source.upstream_record_id);
  const snapshotKey = text(source.snapshot_key);
  const seg = segment(source.vehicle_segment);
  if (upstreamRecordId === undefined || snapshotKey === undefined || seg === undefined) return undefined;
  const fields: [string, string][] = [];
  for (const [field, label] of VARIANT_FIELDS) {
    const shown = display(field, source[field]);
    if (shown !== undefined) fields.push([label, shown]);
  }
  const ledger = obj(source.coverage);
  const coverage = {} as Record<CoverageLevel, CoverageBadge>;
  for (const level of COVERAGE_LEVELS) {
    const entry = obj(ledger[level]);
    coverage[level] = typeof entry.status === 'string' && COVERAGE_STATUSES.has(entry.status)
      ? { status: entry.status, current: entry.current === true } : null;
  }
  const equipment = obj(source.equipment);
  // The closed indicators the register marks present (1); sources excluded.
  const assists = Object.entries(equipment).filter(([key, value]) => !key.endsWith('_hatkana') && value === 1).length;
  if (Object.keys(equipment).length > 0) fields.push(['Driver-assistance systems stated', String(assists)]);
  const issues = Array.isArray(source.parse_issues) ? source.parse_issues.length : 0;
  return { upstreamRecordId, snapshotKey, segment: seg, fields, coverage, parseIssues: issues };
}

export function parseFacets(body: unknown): Facets | undefined {
  const source = obj(body);
  if (!Array.isArray(source.segments) || !Array.isArray(source.fuels) || !Array.isArray(source.bodies)) {
    return undefined;
  }
  const [yearMin, yearMax] = [yearOrNull(source.year_min), yearOrNull(source.year_max)];
  if (yearMin === undefined || yearMax === undefined) return undefined;
  return {
    segments: source.segments.flatMap((item) => {
      const [value, variants] = [segment(obj(item).value), count(obj(item).variants)];
      return value && variants !== undefined ? [{ value, variants }] : [];
    }),
    fuels: source.fuels.flatMap((item) => {
      const e = obj(item);
      const [delekCd, variants] = [count(e.delek_cd), count(e.variants)];
      return delekCd !== undefined && variants !== undefined
        ? [{ delekCd, delekNm: text(e.delek_nm) ?? String(delekCd), variants }] : [];
    }),
    bodies: source.bodies.flatMap((item) => {
      const [merkav, variants] = [text(obj(item).merkav), count(obj(item).variants)];
      return merkav !== undefined && variants !== undefined ? [{ merkav, variants }] : [];
    }),
    yearMin,
    yearMax,
  };
}

/** Exactly the query parameters the browser route reads. Nothing else. */
export function browserQuery(filters: BrowserFilters, extra: Record<string, string | number | undefined> = {}):
  string {
  const params = new URLSearchParams();
  const put = (key: string, value: string | number | undefined) => {
    if (value !== undefined && value !== '') params.set(key, String(value));
  };
  put('segment', filters.segment);
  put('year_from', filters.yearFrom);
  put('year_to', filters.yearTo);
  put('delek_cd', filters.delekCd);
  put('merkav', filters.merkav);
  for (const [key, value] of Object.entries(extra)) put(key, value);
  const query = params.toString();
  return query ? `?${query}` : '';
}

// --- D6: "Add to plan" --------------------------------------------------------

export type PlanSelection = { tozars: string[]; yearFrom: number | null; yearTo: number | null };

export type PlanAddition =
  | { kind: 'create'; edit: WorkScopeEdit; unmapped: string[] }
  | { kind: 'revise'; edit: WorkScopeEdit; unmapped: string[]; head: { revision: number; digest: string } }
  | { kind: 'refused'; reason: 'nothing_mappable' | 'too_many_units' | 'no_change' | 'years_differ';
      unmapped: string[] };

/** A tozar's directory key: only a VERIFIED exact register spelling maps. */
export function directoryKeyFor(tozar: string, entries: readonly DirectoryEntry[]): string | undefined {
  return entries.find((entry) => entry.registerMarqueVerified && entry.registerMarque === tozar)?.key;
}

/**
 * The ONE edit that adds a selection to the conversation's plan.
 *
 * No plan yet: a new plan of the selected manufacturers and year range, with
 * the server's default limit and batch size. An open plan: its units plus the
 * selected ones (its order kept, new ones after). The plan has ONE year range
 * for all its manufacturers, so adding keeps it: a selection without a year
 * range takes the plan's, and a DIFFERENT range is refused (it would change
 * what the plan already covers -- that is the Mapping Plan's own edit). Its
 * limit, batch size and include_unresolved are kept as they are.
 */
export function planAddition(selection: PlanSelection, plan: WorkScopeState | null,
  entries: readonly DirectoryEntry[], limits: WorkScopeLimits): PlanAddition {
  const unmapped = selection.tozars.filter((tozar) => directoryKeyFor(tozar, entries) === undefined);
  const keys = selection.tozars.map((tozar) => directoryKeyFor(tozar, entries))
    .filter((key): key is string => key !== undefined);
  if (keys.length === 0) return { kind: 'refused', reason: 'nothing_mappable', unmapped };
  if (plan === null) {
    const units = [...new Set(keys)];
    if (units.length > limits.maxUnits) return { kind: 'refused', reason: 'too_many_units', unmapped };
    return {
      kind: 'create', unmapped,
      edit: { units, model_year_from: selection.yearFrom, model_year_to: selection.yearTo,
              max_items: limits.defaultMaxItems, batch_size: limits.defaultBatchSize },
    };
  }
  const current = plan.plan;
  const units = [...current.units, ...keys.filter((key) => !current.units.includes(key))];
  const unique = [...new Set(units)];
  if (unique.length > limits.maxUnits) return { kind: 'refused', reason: 'too_many_units', unmapped };
  const stated = selection.yearFrom !== null || selection.yearTo !== null;
  if (stated && (selection.yearFrom !== current.modelYearFrom || selection.yearTo !== current.modelYearTo)) {
    return { kind: 'refused', reason: 'years_differ', unmapped };
  }
  if (unique.length === current.units.length) return { kind: 'refused', reason: 'no_change', unmapped };
  const [from, to] = [current.modelYearFrom, current.modelYearTo];
  return {
    kind: 'revise', unmapped, head: { revision: plan.revision, digest: plan.digest },
    edit: { units: unique, model_year_from: from, model_year_to: to, max_items: current.maxItems,
            batch_size: current.batchSize, ...(current.includeUnresolved ? { include_unresolved: true } : {}) },
  };
}
