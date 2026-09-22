/**
 * The canonical ProductOutcome, as `GET /runs/{id}` states it.
 *
 * This is the browser-side mirror of `backend/product_outcome.py`'s
 * `as_record` form: a closed vocabulary of semantic statuses, a usability
 * derived from it, a coverage pair, allowlisted blocking codes with counts and
 * a payload REFERENCE (presence, digest, size, top-level keys — never content).
 *
 * The record is produced by the canonical finalizer in the same transaction as
 * the terminal status and projected by the API from that terminal event. The
 * browser never derives it from `output`: a run whose response carries no
 * trustworthy record renders "no canonical verdict recorded", not a guess.
 *
 * Parsing is total and fail-closed. Anything outside the contract yields
 * `undefined`, and nothing outside it can reach the screen.
 */

export const SEMANTIC_STATUSES = ['complete', 'partial', 'unusable', 'refused', 'not_produced'] as const;
export type SemanticStatus = (typeof SEMANTIC_STATUSES)[number];

export const USABILITY = ['usable', 'partial', 'unusable', 'refused', 'none'] as const;
export type Usability = (typeof USABILITY)[number];

/** Mirrors `BLOCKING_CODES` in backend/product_outcome.py, exactly. */
export const BLOCKING_CODES = [
  'OUTSTANDING_REVIEW_ITEMS',
  'TASK_FAILURES',
  'COVERAGE_GAPS',
  'UNVERIFIED_CLAIMS',
  'CONFLICTING_CLAIMS',
  'REJECTED_ITEMS',
  'FAILED_AGENTS',
  'DEGRADED_VERIFICATION',
  'DEGRADED_ENRICHMENT',
  'NO_USABLE_RESULT',
  'NO_PRODUCT_PAYLOAD',
  'OUTCOME_CONTRACT_VIOLATION',
  'ENGINE_REPORTED_FAILURE',
  'REFUSED_BEFORE_EXECUTION',
  'RUN_NOT_PRODUCED',
] as const;
export type BlockingCode = (typeof BLOCKING_CODES)[number];

const BLOCKING_LABELS: Record<BlockingCode, string> = {
  OUTSTANDING_REVIEW_ITEMS: 'items awaiting review',
  TASK_FAILURES: 'planned tasks that did not complete',
  COVERAGE_GAPS: 'planned scope with no result',
  UNVERIFIED_CLAIMS: 'claims without a verified verdict',
  CONFLICTING_CLAIMS: 'contradicting claims left unresolved',
  REJECTED_ITEMS: 'rejected items',
  FAILED_AGENTS: 'failed agents',
  DEGRADED_VERIFICATION: 'verification degraded',
  DEGRADED_ENRICHMENT: 'technical enrichment degraded',
  NO_USABLE_RESULT: 'nothing usable was produced or disproved',
  NO_PRODUCT_PAYLOAD: 'no product payload recorded',
  OUTCOME_CONTRACT_VIOLATION: 'result contract violated',
  ENGINE_REPORTED_FAILURE: 'engine reported failure',
  REFUSED_BEFORE_EXECUTION: 'refused before execution',
  RUN_NOT_PRODUCED: 'run ended without a product',
};

export type ProductOutcome = {
  engine: string;
  semanticStatus: SemanticStatus;
  usability: Usability;
  resultKind?: string;
  coverage: { produced: number; outstanding: number; ratio?: number };
  blocking: { code: BlockingCode; count: number; label: string }[];
  payload: { present: boolean; digest?: string; byteSize?: number };
};

const MAX_BLOCKING = 16;

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

/** Total: an untrustworthy record is `undefined`, never partially believed. */
export function parseProductOutcome(raw: unknown): ProductOutcome | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const record = raw as Record<string, unknown>;
  const semantic = record.semantic_status;
  if (typeof semantic !== 'string' || !(SEMANTIC_STATUSES as readonly string[]).includes(semantic)) return undefined;
  const usability = record.usability;
  if (typeof usability !== 'string' || !(USABILITY as readonly string[]).includes(usability)) return undefined;
  const engine = record.engine;
  if (typeof engine !== 'string' || !engine || engine.length > 64) return undefined;
  const coverage = record.coverage;
  if (!coverage || typeof coverage !== 'object') return undefined;
  const { produced, outstanding, ratio } = coverage as Record<string, unknown>;
  if (!isCount(produced) || !isCount(outstanding)) return undefined;
  if (ratio !== undefined && ratio !== null && !(typeof ratio === 'number' && ratio >= 0 && ratio <= 1)) return undefined;
  const blockingRaw = Array.isArray(record.blocking) ? record.blocking : [];
  if (blockingRaw.length > MAX_BLOCKING) return undefined;
  const blocking: ProductOutcome['blocking'] = [];
  for (const item of blockingRaw) {
    if (!item || typeof item !== 'object') return undefined;
    const { code, count } = item as Record<string, unknown>;
    if (typeof code !== 'string' || !(BLOCKING_CODES as readonly string[]).includes(code)) return undefined;
    if (!isCount(count) || count < 1) return undefined;
    blocking.push({ code: code as BlockingCode, count, label: BLOCKING_LABELS[code as BlockingCode] });
  }
  const payload = record.payload;
  if (!payload || typeof payload !== 'object') return undefined;
  const { present, digest, byte_size: byteSize } = payload as Record<string, unknown>;
  if (typeof present !== 'boolean') return undefined;
  const resultKind = typeof record.result_kind === 'string' ? record.result_kind : undefined;
  return {
    engine,
    semanticStatus: semantic as SemanticStatus,
    usability: usability as Usability,
    resultKind,
    coverage: {
      produced,
      outstanding,
      ratio: typeof ratio === 'number' ? ratio : undefined,
    },
    blocking,
    payload: {
      present,
      digest: typeof digest === 'string' && /^[0-9a-f]{64}$/.test(digest) ? digest : undefined,
      byteSize: isCount(byteSize) ? byteSize : undefined,
    },
  };
}

export type OutcomeTone = 'positive' | 'caution' | 'negative';

/** Static, authored text per semantic status. Nothing from the payload. */
export function describeProductOutcome(outcome: ProductOutcome): { label: string; summary: string; tone: OutcomeTone; symbol: string } {
  switch (outcome.semanticStatus) {
    case 'complete':
      return { label: 'Complete', tone: 'positive', symbol: '✓',
        summary: 'The canonical finalizer recorded a complete, usable product with nothing outstanding.' };
    case 'partial':
      return { label: 'Partial', tone: 'caution', symbol: '◐',
        summary: 'A usable product exists, but the finalizer recorded outstanding items. They are listed below.' };
    case 'unusable':
      return { label: 'Unusable', tone: 'negative', symbol: '∅',
        summary: 'The run finished but produced nothing a consumer can act on.' };
    case 'refused':
      return { label: 'Refused', tone: 'negative', symbol: '×',
        summary: 'The product was refused: the engine failed or the recorded result did not satisfy its contract.' };
    default:
      return { label: 'Not produced', tone: 'negative', symbol: '×',
        summary: 'The run ended without a product. The terminal status says why.' };
  }
}
