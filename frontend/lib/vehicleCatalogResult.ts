/**
 * The vehicle_catalog_v1 FINAL RESULT contract, as the browser reads it.
 *
 * V1's durable `run.output` is the engine's run envelope
 * (`backend/engines/vehicle_catalog_v1/engine.py`): `{ status, result,
 * summary, ... }` where `result` is the deterministic final document from
 * `core.build_final_json_python` — `manufacturer`, `market`, `period`,
 * `status`, `models[]` (each with a `verification_status`), `needs_review[]`,
 * `rejected[]`, `failed_agents[]` and `pipeline_quality{}`.
 *
 * This module is the typed, fail-closed reader of that document, so the
 * product surface renders vehicles, verdicts and quality facts instead of a
 * JSON dump. It mirrors what `backend/product_outcome.py`'s
 * `derive_vehicle_catalog_v1_outcome` counts, and nothing else: an envelope
 * that is not a catalog document parses as `invalid`, an empty payload as
 * `absent`, and free-form text is bounded before it can reach `safeText`.
 *
 * Nothing here decides the run's verdict. The canonical verdict is the
 * ProductOutcome the finalizer recorded (`lib/productOutcome.ts`); this
 * module only lays out the product that verdict is about.
 */

export const V1_VERIFICATION_STATUSES = ['verified', 'partial', 'needs_review', 'rejected'] as const;
export type V1VerificationStatus = (typeof V1_VERIFICATION_STATUSES)[number];

export const V1_STAGE_STATUSES = ['success', 'partial', 'failed', 'needs_review', 'rejected'] as const;
export type V1StageStatus = (typeof V1_STAGE_STATUSES)[number];

/** The stages `pipeline_quality` reports, in pipeline order. */
export const V1_STAGES = ['discovery', 'normalizer', 'technical_enrichment', 'verifier', 'final_builder'] as const;
export type V1Stage = (typeof V1_STAGES)[number];

export const V1_DATA_DEPTHS = ['full_technical', 'partial_technical', 'model_list_only'] as const;

/**
 * The technical fields a model row may state. A closed list: a key the
 * document carries outside it is not rendered, however plausible it looks.
 */
export const V1_MODEL_FIELDS = [
  'years', 'engine', 'fuel_type', 'power_hp', 'transmission', 'drivetrain',
  'body_style', 'seats', 'trims', 'currently_sold', 'safety_rating',
] as const;
export type V1ModelField = (typeof V1_MODEL_FIELDS)[number];

const FIELD_LABELS: Record<V1ModelField, string> = {
  years: 'Years', engine: 'Engine', fuel_type: 'Fuel type', power_hp: 'Power (hp)',
  transmission: 'Transmission', drivetrain: 'Drivetrain', body_style: 'Body style',
  seats: 'Seats', trims: 'Trims', currently_sold: 'Currently sold', safety_rating: 'Safety rating',
};

export const MAX_V1_MODELS = 200;
export const MAX_V1_SOURCES_PER_MODEL = 8;
export const MAX_V1_TEXT = 200;
export const MAX_V1_SUMMARY = 2_000;

export type V1ModelSource = { url: string; host: string };

export type V1Model = {
  name: string;
  nameHe?: string;
  verification: V1VerificationStatus;
  confidence?: 'high' | 'medium' | 'low';
  sourceStrength?: string;
  fields: { key: V1ModelField; label: string; value: string }[];
  sources: V1ModelSource[];
};

export type V1StageQuality = { stage: V1Stage; label: string; status: V1StageStatus };

export type VehicleCatalogResult = {
  manufacturer: string;
  market: string;
  period: string;
  /** The engine's declared document status, shown as a fact, never as the verdict. */
  declaredStatus: string;
  summary?: string;
  models: V1Model[];
  counts: { verified: number; needsReview: number; rejected: number; failedAgents: number };
  quality: V1StageQuality[];
  dataDepth?: (typeof V1_DATA_DEPTHS)[number];
};

export type VehicleCatalogParse =
  | { state: 'absent' }
  | { state: 'invalid'; code: 'V1_NOT_A_CATALOG_DOCUMENT' | 'V1_MODELS_MALFORMED' | 'V1_TOO_MANY_MODELS' }
  | { state: 'result'; result: VehicleCatalogResult };

const STAGE_LABELS: Record<V1Stage, string> = {
  discovery: 'Discovery', normalizer: 'Normalization', technical_enrichment: 'Technical enrichment',
  verifier: 'Verification', final_builder: 'Final assembly',
};

function text(value: unknown, max = MAX_V1_TEXT): string | undefined {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed ? trimmed.slice(0, max) : undefined;
  }
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  return undefined;
}

function scalarOrList(value: unknown): string | undefined {
  if (Array.isArray(value)) {
    const parts = value.map((item) => text(item, 60)).filter((item): item is string => item !== undefined);
    return parts.length ? parts.slice(0, 12).join(', ') : undefined;
  }
  return text(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function sourcesOf(value: unknown): V1ModelSource[] {
  if (!Array.isArray(value)) return [];
  const out: V1ModelSource[] = [];
  for (const item of value) {
    if (out.length >= MAX_V1_SOURCES_PER_MODEL) break;
    const candidate = typeof item === 'string' ? item : isRecord(item) ? item.url : undefined;
    if (typeof candidate !== 'string') continue;
    try {
      const url = new URL(candidate);
      if (url.protocol !== 'https:' && url.protocol !== 'http:') continue;
      out.push({ url: url.toString().slice(0, 500), host: url.hostname });
    } catch {
      // Not a URL; a source that cannot be linked is not listed as one.
    }
  }
  return out;
}

function modelOf(raw: unknown): V1Model | undefined {
  if (!isRecord(raw)) return undefined;
  const name = text(raw.canonical_model_name) ?? text(raw.model_name_en) ?? text(raw.model) ?? text(raw.name);
  if (!name) return undefined;
  const verificationRaw = raw.verification_status;
  const verification: V1VerificationStatus =
    typeof verificationRaw === 'string' && (V1_VERIFICATION_STATUSES as readonly string[]).includes(verificationRaw)
      ? (verificationRaw as V1VerificationStatus)
      : 'partial'; // the same downward default the backend applies to an unknown verdict
  const confidenceRaw = raw.confidence;
  const confidence = confidenceRaw === 'high' || confidenceRaw === 'medium' || confidenceRaw === 'low' ? confidenceRaw : undefined;
  const fields: V1Model['fields'] = [];
  for (const key of V1_MODEL_FIELDS) {
    const value = scalarOrList(raw[key]);
    if (value !== undefined) fields.push({ key, label: FIELD_LABELS[key], value });
  }
  return {
    name,
    nameHe: text(raw.model_name_he),
    verification,
    confidence,
    sourceStrength: text(raw.source_strength, 40),
    fields,
    sources: sourcesOf(raw.sources),
  };
}

function countOf(value: unknown): number {
  return Array.isArray(value) ? value.length : isRecord(value) ? Object.keys(value).length : 0;
}

/**
 * Total. Accepts the run envelope (`{ status, result, summary }`) or the bare
 * final document, exactly like the backend reader, and refuses anything that
 * is not a catalog document rather than rendering it as one.
 */
export function parseVehicleCatalogResult(output: unknown): VehicleCatalogParse {
  if (!isRecord(output) || Object.keys(output).length === 0) return { state: 'absent' };
  const envelopeSummary = text(output.summary, MAX_V1_SUMMARY);
  const document = isRecord(output.result) ? output.result : output;
  if (!isRecord(document) || !Array.isArray(document.models) || !isRecord(document.pipeline_quality)) {
    return { state: 'invalid', code: 'V1_NOT_A_CATALOG_DOCUMENT' };
  }
  if (document.models.length > MAX_V1_MODELS) return { state: 'invalid', code: 'V1_TOO_MANY_MODELS' };
  const models: V1Model[] = [];
  for (const raw of document.models) {
    const model = modelOf(raw);
    if (!model) return { state: 'invalid', code: 'V1_MODELS_MALFORMED' };
    models.push(model);
  }
  const verified = models.filter((m) => m.verification === 'verified').length;
  const unsettled = models.filter((m) => m.verification === 'partial' || m.verification === 'needs_review').length;
  const rejected = models.filter((m) => m.verification === 'rejected').length;
  const quality: V1StageQuality[] = [];
  const pq = document.pipeline_quality as Record<string, unknown>;
  for (const stage of V1_STAGES) {
    const status = pq[stage];
    if (typeof status === 'string' && (V1_STAGE_STATUSES as readonly string[]).includes(status)) {
      quality.push({ stage, label: STAGE_LABELS[stage], status: status as V1StageStatus });
    }
  }
  const depthRaw = pq.data_depth;
  const dataDepth = typeof depthRaw === 'string' && (V1_DATA_DEPTHS as readonly string[]).includes(depthRaw)
    ? (depthRaw as (typeof V1_DATA_DEPTHS)[number]) : undefined;
  return {
    state: 'result',
    result: {
      manufacturer: text(document.manufacturer) ?? 'Unknown manufacturer',
      market: text(document.market) ?? 'unknown',
      period: text(document.period) ?? 'unknown',
      declaredStatus: text(document.status, 40) ?? 'unknown',
      summary: envelopeSummary,
      models,
      counts: {
        verified,
        // The review/rejected VIEWS can carry items the per-model verdicts do
        // not (the backend recomputes them after the builder), so the larger
        // count is shown — the same rule the outcome reader applies.
        needsReview: Math.max(unsettled, countOf(document.needs_review)),
        rejected: Math.max(rejected, countOf(document.rejected)),
        failedAgents: countOf(document.failed_agents),
      },
      quality,
      dataDepth,
    },
  };
}

export function verificationLabel(status: V1VerificationStatus): string {
  switch (status) {
    case 'verified': return 'Verified';
    case 'partial': return 'Partially verified';
    case 'needs_review': return 'Needs review';
    default: return 'Rejected';
  }
}

export function dataDepthLabel(depth: (typeof V1_DATA_DEPTHS)[number] | undefined): string | undefined {
  switch (depth) {
    case 'full_technical': return 'Full technical detail';
    case 'partial_technical': return 'Partial technical detail';
    case 'model_list_only': return 'Model list only';
    default: return undefined;
  }
}
