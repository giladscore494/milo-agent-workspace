/**
 * The closed frontend contract for the Swarm V2 FINAL PRODUCT RESULT.
 *
 * This module is the frontend mirror of `backend/engines/swarm_v2/outcome.py`
 * (`validate_product_outcome`) and it is the ONLY place the browser decides
 * what a run produced. It exists because technical execution finishing is not
 * a product result: the run card reports that the engine ran, and nothing
 * there may be promoted into "here is your answer".
 *
 * Three properties are deliberate and load-bearing:
 *
 * 1. CLOSED. Every value rendered downstream comes from a key named here.
 *    There is no pass-through of the payload, no `Object.keys` walk over
 *    anything a caller supplied, and no `JSON.stringify` of durable data.
 *    An unknown key is not rendered, so a backend that starts emitting a new
 *    key cannot make it appear on the product surface until this contract is
 *    deliberately extended.
 *
 * 2. FAIL-CLOSED. `parseFinalResult` is total: every payload becomes exactly
 *    one of `result`, `absent` or `invalid`. A missing, malformed,
 *    contradictory or unknown payload can only ever become `invalid`, never a
 *    result and never a success. Nothing here repairs a payload, fills in a
 *    default, or infers a kind the backend did not state.
 *
 * 3. NO REASONING, NO EVIDENCE, NO SECRETS. The contract has no field for a
 *    prompt, a chain of thought, a provider error, a source fragment, a
 *    content hash, a locator or a token, so there is no code path that could
 *    carry one to the browser. Redaction stays defense in depth in
 *    lib/sanitize.ts; it is NOT what keeps these out — the typed contract is.
 *
 * `partial_result` is never presented as a completed success: it is a usable
 * result WITH outstanding items, and both halves are always shown together.
 */

import { humanizeKey } from './humanize';
import { isTerminalRunStatus } from './runStatus';

// --- static vocabulary (mirrors backend.engines.swarm_v2.outcome) ------------

/** The public product statuses. */
export const PRODUCT_STATUSES = ['complete', 'partial_success'] as const;
export type ProductStatus = (typeof PRODUCT_STATUSES)[number];

/** The statically allowlisted product-result classification. */
export const RESULT_KINDS = [
  'usable_result',
  'partial_result',
  'no_usable_result',
  'not_found',
] as const;
export type FinalResultKind = (typeof RESULT_KINDS)[number];

/**
 * The only self-consistent pairings. A status and a result kind can never
 * contradict one another because only these four pairs parse.
 */
const ALLOWED_OUTCOMES: ReadonlySet<string> = new Set([
  'complete|usable_result',
  'complete|not_found',
  'partial_success|partial_result',
  'partial_success|no_usable_result',
]);

/** The bounded, static empty-result marker; it carries a code and nothing else. */
export const NO_USABLE_RESULT_CODE = 'NO_USABLE_RESULT';

/**
 * Durable run status -> the product status that status can carry.
 *
 * This is NOT a terminal-status set and must never be read as one:
 * lib/runStatus.ts is the single frontend mirror of
 * `backend.runtime.TERMINAL_STATES`, and the parser asks it. This map answers
 * a different question — which product status a given terminal status may
 * carry. Terminal statuses absent from it (`failed`, `cancelled`, `timed_out`,
 * `budget_exhausted`) reach no product outcome at all in the worker, so a
 * product payload arriving alongside one of them is a contradiction.
 */
const PRODUCT_STATUS_BY_RUN_STATUS: Readonly<Record<string, ProductStatus>> = {
  completed: 'complete',
  partial_success: 'partial_success',
};

/** Why a payload was refused. Static codes; never provider or model text. */
export const INVALID_RESULT_CODES = [
  'NOT_AN_OBJECT',
  'VOCABULARY_NOT_TEXT',
  'VOCABULARY_NOT_ALLOWLISTED',
  'STATUS_CONTRADICTS_KIND',
  'STRUCTURALLY_INVALID',
  'KIND_CONTRADICTS_FIELDS',
  'COMPLETE_WITH_REVIEW_ITEMS',
  'EMPTY_MARKER_INVALID',
  'RUN_STATUS_CONTRADICTS_OUTCOME',
  'FIELD_ENTRY_INVALID',
  'REVIEW_ITEM_INVALID',
  'VALUE_NOT_JSON',
] as const;
export type InvalidResultCode = (typeof INVALID_RESULT_CODES)[number];

// --- the parsed shape --------------------------------------------------------

/**
 * A safe, public provenance REFERENCE. Identifiers and declared scope only.
 *
 * There is deliberately no fragment, no locator, no source version, no hash
 * and no confidence score: this says WHICH durable rows stand behind a value,
 * not what they contain. `time_scope` is structured technical metadata and is
 * not part of this reference — the Inspector is where technical detail lives.
 */
export type ProvenanceReference = {
  claimId?: string;
  sourceId?: string;
  taskId?: string;
  entity?: string;
  field?: string;
  geography?: string;
  market?: string;
};

/**
 * How a durable `value` may be shown.
 *
 * The backend contract genuinely permits structured values: `safe_durable_value`
 * accepts nested mappings and lists, so a verified value is not always a scalar
 * and must not be discarded for being one. It is rendered through an EXPLICITLY
 * BOUNDED representation instead — bounded in depth, in breadth and in text
 * length — and every bound that bites is declared on screen rather than
 * silently truncating the answer.
 *
 * A value of a type the durable contract cannot carry at all (a function, a
 * symbol, a bigint, a non-finite number — none of which survive JSON, and all
 * of which `safe_durable_value` refuses) is not bounded here: it is a contract
 * violation and fails the whole parse.
 */
export type DisplayValue =
  | { display: 'text'; text: string }
  | { display: 'empty' }
  | { display: 'list'; items: DisplayValue[]; hidden: number }
  | { display: 'record'; entries: { key: string; value: DisplayValue }[]; hidden: number }
  /** The depth bound stopped here. The value exists; it is not shown in full. */
  | { display: 'depth_bounded' };

/** Bounds on how much of one durable value the product surface will render. */
export const MAX_VALUE_DEPTH = 3;
export const MAX_VALUE_ITEMS = 20;

export type VerifiedValue = {
  value: DisplayValue;
  provenance: ProvenanceReference;
};

export type VerifiedField = {
  /** The durable field key, exactly as the backend wrote it. */
  key: string;
  /** Deterministic formatting of `key`; no dictionary, no model. */
  label: string;
  /**
   * Every verified value for this field, in payload order. More than one entry
   * means the run verified more than one value and NOTHING chose between them.
   */
  values: VerifiedValue[];
};

/**
 * One outstanding item, classified into a CLOSED set.
 *
 * There is deliberately no "unrecognized" category. An item that matches none
 * of these shapes is not a result with an odd row in it — it is a payload the
 * contract cannot account for, so it invalidates the whole outcome and the
 * surface shows the unavailable state. Accepting it as a displayable category
 * would be exactly the soft failure this contract exists to prevent.
 */
export type ReviewItemKind =
  | 'conflict'
  | 'needs_review'
  | 'coverage_gap'
  | 'task_failure'
  | 'empty_marker';

export type ReviewItem = {
  kind: ReviewItemKind;
  /** Durable field key, when the item is about a field. */
  fieldKey?: string;
  fieldLabel?: string;
  /** Logical task id, when the item is about a task. */
  taskId?: string;
  taskLabel?: string;
  /** A static, backend-owned code. Never free text. */
  code?: string;
  /** A backend-owned reason. Free-form by type, so it is rendered safely. */
  reason?: string;
  value?: DisplayValue;
  provenance?: ProvenanceReference;
};

export type FinalResult = {
  status: ProductStatus;
  kind: FinalResultKind;
  fields: VerifiedField[];
  review: ReviewItem[];
  /** Counts by kind, so the surface never recomputes classification. */
  conflictCount: number;
  coverageGapCount: number;
  taskFailureCount: number;
  /** Fields carrying more than one verified value, none of them chosen. */
  multiValuedFieldCount: number;
};

/** The total outcome of parsing. Exactly one of three shapes. */
export type FinalResultParse =
  | { state: 'result'; result: FinalResult }
  | { state: 'absent' }
  | { state: 'invalid'; code: InvalidResultCode };

// --- helpers -----------------------------------------------------------------

/**
 * True for a plain JSON object.
 *
 * Arrays are excluded deliberately: `typeof [] === 'object'` and an array
 * reaching a mapping check is exactly the kind of lookalike that must fail
 * closed rather than be read as an empty mapping.
 */
function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Own enumerable entries, and nothing inherited.
 *
 * `JSON.parse` materialises `__proto__` as an OWN property rather than
 * mutating a prototype, so reading through `Object.entries` (not by dynamic
 * property access) keeps a hostile key inert data we simply never render.
 */
function ownEntries(value: Record<string, unknown>): [string, unknown][] {
  return Object.entries(value);
}

/** Bounded so a single durable string can never dominate the surface. */
const MAX_TEXT_CHARS = 500;

function boundedText(value: string): string {
  return value.length > MAX_TEXT_CHARS ? `${value.slice(0, MAX_TEXT_CHARS)}…` : value;
}

/** A bounded durable identifier, or undefined. Never a fallback string. */
function reference(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? boundedText(value) : undefined;
}

/**
 * A parse step that can refuse. `null` means "the durable contract cannot
 * carry this", which fails the whole outcome rather than degrading one row.
 */
type Refusable<T> = T | null;

/**
 * Render one durable value under explicit bounds, or refuse it.
 *
 * `null` is a recorded absence, not the string "null". Scalars render as
 * themselves. Lists and records are rendered — the backend contract permits
 * them, so discarding them would drop a verified answer — but only to
 * `MAX_VALUE_DEPTH` levels and `MAX_VALUE_ITEMS` entries per level, with the
 * remainder COUNTED in `hidden` so the surface can say how much it is not
 * showing. Nothing is truncated silently.
 *
 * Anything JSON cannot express is refused: `safe_durable_value` would never
 * have written it, so its presence means the payload is not the durable
 * payload it claims to be.
 */
export function toDisplayValue(value: unknown, depth = 0): Refusable<DisplayValue> {
  if (value === null || value === undefined) return { display: 'empty' };
  if (typeof value === 'string') {
    return value.length === 0 ? { display: 'empty' } : { display: 'text', text: boundedText(value) };
  }
  if (typeof value === 'number') {
    // NaN and +/-Infinity do not survive JSON and `safe_durable_value` never
    // emits them, so they are a contract violation, not a value to bound.
    return Number.isFinite(value) ? { display: 'text', text: String(value) } : null;
  }
  if (typeof value === 'boolean') return { display: 'text', text: value ? 'Yes' : 'No' };

  const structured = Array.isArray(value) || isObject(value);
  if (!structured) return null; // function, symbol, bigint: not JSON.

  // The bound is reached, not exceeded: the value is declared rather than
  // walked further, which keeps rendering cost bounded for any payload.
  if (depth >= MAX_VALUE_DEPTH) return { display: 'depth_bounded' };

  if (Array.isArray(value)) {
    const shown = value.slice(0, MAX_VALUE_ITEMS);
    const items: DisplayValue[] = [];
    for (const item of shown) {
      const rendered = toDisplayValue(item, depth + 1);
      if (rendered === null) return null;
      items.push(rendered);
    }
    return { display: 'list', items, hidden: value.length - shown.length };
  }

  // `Object.entries` reads OWN enumerable keys only, so a `__proto__` key that
  // `JSON.parse` materialised is inert data we render as a row, never a
  // prototype mutation. Nothing here assigns by a payload-controlled key.
  const all = ownEntries(value);
  const shown = all.slice(0, MAX_VALUE_ITEMS);
  const entries: { key: string; value: DisplayValue }[] = [];
  for (const [key, item] of shown) {
    const rendered = toDisplayValue(item, depth + 1);
    if (rendered === null) return null;
    entries.push({ key: boundedText(key), value: rendered });
  }
  return { display: 'record', entries, hidden: all.length - shown.length };
}

/** Keys a durable provenance trace may carry. Anything else is a violation. */
const PROVENANCE_KEYS: ReadonlySet<string> = new Set([
  'claim_id', 'source_id', 'run_id', 'task_id', 'scope',
]);
const PROVENANCE_SCOPE_KEYS: ReadonlySet<string> = new Set([
  'entity', 'field', 'geography', 'market', 'time_scope',
]);

/**
 * Read a durable provenance trace, or refuse it.
 *
 * Closed on both levels: a key the builder never writes means this is not a
 * builder trace. `run_id` and `time_scope` are accepted as valid contract keys
 * and deliberately NOT surfaced — the run is the one the user is already on,
 * and a time scope is structured technical metadata that belongs in the
 * Inspector, not in a product-surface provenance reference.
 */
function toProvenance(value: unknown): Refusable<ProvenanceReference> {
  if (!isObject(value)) return null;
  for (const [key, item] of ownEntries(value)) {
    if (!PROVENANCE_KEYS.has(key)) return null;
    if (key === 'scope') {
      if (!isObject(item)) return null;
      for (const [scopeKey] of ownEntries(item)) {
        if (!PROVENANCE_SCOPE_KEYS.has(scopeKey)) return null;
      }
    }
  }
  const scope = isObject(value.scope) ? value.scope : undefined;
  return {
    claimId: reference(value.claim_id),
    sourceId: reference(value.source_id),
    taskId: reference(value.task_id),
    entity: scope ? reference(scope.entity) : undefined,
    field: scope ? reference(scope.field) : undefined,
    geography: scope ? reference(scope.geography) : undefined,
    market: scope ? reference(scope.market) : undefined,
  };
}

/** Exactly these keys, no more and no fewer. */
function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => keys.includes(key));
}

/**
 * Count field keys that actually carry a verified entry.
 *
 * Mirrors `_usable_field_count`: usefulness is the presence of at least one
 * entry, never the presence of a key, and a non-array value carries none.
 */
function usableFieldCount(fields: Record<string, unknown>): number {
  return ownEntries(fields).filter(([, value]) => Array.isArray(value) && value.length > 0).length;
}

/** Exactly the static marker: a lone `code`, with no extra key beside it. */
function isEmptyMarker(item: Record<string, unknown>): boolean {
  const keys = Object.keys(item);
  return keys.length === 1 && keys[0] === 'code' && item.code === NO_USABLE_RESULT_CODE;
}

/** Reason strings the verifier contract reserves for an unresolved conflict. */
const CONFLICT_REASONS: ReadonlySet<string> = new Set(['unresolved conflict']);

/** Codes the engine emits for a coverage gap, as opposed to a task failure. */
const COVERAGE_GAP_CODES: ReadonlySet<string> = new Set([
  'REQUIRED_OUTPUT_MISSING',
  'EVIDENCE_REQUIREMENTS_UNMET',
]);

/** The exact key sets `FinalBuilder` and the engine write. */
const VERDICT_REVIEW_KEYS = ['field', 'value', 'reason', 'provenance'] as const;
const TASK_SCOPED_KEYS = ['task_id', 'code'] as const;

/**
 * Classify one `needs_review` entry, or refuse it.
 *
 * The list is heterogeneous BY CONTRACT, but each entry is one of exactly
 * three literal shapes the backend constructs:
 *
 *   verdict review  {field, value, reason, provenance}   (FinalBuilder)
 *   task-scoped     {task_id, code}                      (task failure / gap)
 *   empty marker    {code: NO_USABLE_RESULT}             (outcome policy)
 *
 * An entry matching none of them — an unknown shape, a missing key, an extra
 * key, a wrong type — is refused, and refusing one entry invalidates the whole
 * outcome. There is no lenient path: an outstanding item nobody can classify
 * must not be rendered beside verified fields as though the result were sound.
 */
function toReviewItem(item: Record<string, unknown>): Refusable<ReviewItem> {
  if (isEmptyMarker(item)) return { kind: 'empty_marker', code: NO_USABLE_RESULT_CODE };

  if (hasExactKeys(item, VERDICT_REVIEW_KEYS)) {
    const fieldKey = reference(item.field);
    const reason = reference(item.reason);
    if (fieldKey === undefined || reason === undefined) return null;
    const value = toDisplayValue(item.value);
    if (value === null) return null;
    const provenance = toProvenance(item.provenance);
    if (provenance === null) return null;
    return {
      kind: CONFLICT_REASONS.has(reason) ? 'conflict' : 'needs_review',
      fieldKey,
      fieldLabel: humanizeKey(fieldKey),
      reason,
      value,
      provenance,
    };
  }

  if (hasExactKeys(item, TASK_SCOPED_KEYS)) {
    const taskId = reference(item.task_id);
    const code = reference(item.code);
    if (taskId === undefined || code === undefined) return null;
    return {
      kind: COVERAGE_GAP_CODES.has(code) ? 'coverage_gap' : 'task_failure',
      taskId,
      taskLabel: humanizeKey(taskId),
      code,
    };
  }

  return null;
}

/** One verified value entry: exactly `{value, provenance}`, or a refusal. */
const FIELD_ENTRY_KEYS = ['value', 'provenance'] as const;

function toVerifiedValue(entry: unknown): Refusable<VerifiedValue> {
  if (!isObject(entry) || !hasExactKeys(entry, FIELD_ENTRY_KEYS)) return null;
  const value = toDisplayValue(entry.value);
  if (value === null) return null;
  const provenance = toProvenance(entry.provenance);
  if (provenance === null) return null;
  return { value, provenance };
}

// --- the parser --------------------------------------------------------------

export type ParseOptions = {
  /**
   * The durable run status, from trusted run state. When supplied, the run's
   * terminal status and the payload's product status must agree: a `completed`
   * run cannot carry a `partial_success` payload, and a run that failed, was
   * cancelled, timed out or exhausted its budget reaches no product outcome at
   * all, so carrying one is a contradiction rather than a result.
   */
  runStatus?: string;
};

/**
 * Parse a durable Swarm V2 `run.output` into the closed product contract.
 *
 * TOTAL by construction: every input maps to `result`, `absent` or `invalid`.
 * The invariants below are the same ones `validate_product_outcome` enforces
 * on the way out of the worker, checked again here because the browser must
 * never depend on a server-side check it cannot see.
 */
export function parseFinalResult(output: unknown, options: ParseOptions = {}): FinalResultParse {
  // Nothing recorded is a state of its own, distinct from a broken payload:
  // an absent output has not made a claim that could be wrong.
  if (output === undefined || output === null) return { state: 'absent' };
  if (!isObject(output)) return { state: 'invalid', code: 'NOT_AN_OBJECT' };
  if (Object.keys(output).length === 0) return { state: 'absent' };

  const { status, result_kind: resultKind, fields, needs_review: needsReview } = output;

  // Types first: an unhashable-lookalike (`status: []`) must fail closed
  // rather than slip through a membership test.
  if (typeof status !== 'string' || typeof resultKind !== 'string') {
    return { state: 'invalid', code: 'VOCABULARY_NOT_TEXT' };
  }
  if (!(PRODUCT_STATUSES as readonly string[]).includes(status) ||
      !(RESULT_KINDS as readonly string[]).includes(resultKind)) {
    return { state: 'invalid', code: 'VOCABULARY_NOT_ALLOWLISTED' };
  }
  if (!ALLOWED_OUTCOMES.has(`${status}|${resultKind}`)) {
    return { state: 'invalid', code: 'STATUS_CONTRADICTS_KIND' };
  }
  const productStatus = status as ProductStatus;
  const kind = resultKind as FinalResultKind;

  if (!isObject(fields) || !Array.isArray(needsReview) || !needsReview.every(isObject)) {
    return { state: 'invalid', code: 'STRUCTURALLY_INVALID' };
  }

  // A kind that claims usable content must have some, and a kind that claims
  // none must have none. This is the check that stops an empty result being
  // displayed as a successful one.
  const hasUsableFields = usableFieldCount(fields) > 0;
  if (hasUsableFields !== (kind === 'usable_result' || kind === 'partial_result')) {
    return { state: 'invalid', code: 'KIND_CONTRADICTS_FIELDS' };
  }

  // `complete` is reachable only with nothing outstanding.
  if (productStatus === 'complete' && needsReview.length > 0) {
    return { state: 'invalid', code: 'COMPLETE_WITH_REVIEW_ITEMS' };
  }

  // The empty-result marker: exactly one, last, and byte-for-byte the static
  // entry, so no extra key can smuggle prose in beside the code.
  const markerIndexes = needsReview
    .map((item, index) => (item.code === NO_USABLE_RESULT_CODE ? index : -1))
    .filter((index) => index >= 0);
  if (kind === 'no_usable_result') {
    const last = needsReview.length - 1;
    if (markerIndexes.length !== 1 || markerIndexes[0] !== last || !isEmptyMarker(needsReview[last])) {
      return { state: 'invalid', code: 'EMPTY_MARKER_INVALID' };
    }
  } else if (markerIndexes.length > 0) {
    return { state: 'invalid', code: 'EMPTY_MARKER_INVALID' };
  }

  // Trusted run state has the last word. A payload that disagrees with the
  // durable terminal status is a contradiction, whichever side is wrong.
  const runStatus = options.runStatus;
  if (runStatus !== undefined && isTerminalRunStatus(runStatus) &&
      PRODUCT_STATUS_BY_RUN_STATUS[runStatus] !== productStatus) {
    return { state: 'invalid', code: 'RUN_STATUS_CONTRADICTS_OUTCOME' };
  }

  // Everything above mirrors `validate_product_outcome`. Everything below is
  // the CLOSED read of the payload's interior: each field entry and each
  // review item must be one of the literal shapes the backend constructs, and
  // a single refusal invalidates the whole outcome rather than degrading a row.
  //
  // Payload order is preserved throughout: the backend already sorted fields
  // and verdict review items deterministically, so a refresh or a resume
  // rebuilds the identical surface from the same durable output.
  const parsedFields: VerifiedField[] = [];
  for (const [key, value] of ownEntries(fields)) {
    // `FinalBuilder` appends into a list per field, so a key always carries a
    // non-empty array. An empty or non-array value is a shape it cannot write.
    if (!Array.isArray(value) || value.length === 0) {
      return { state: 'invalid', code: 'FIELD_ENTRY_INVALID' };
    }
    const values: VerifiedValue[] = [];
    for (const entry of value) {
      const parsedEntry = toVerifiedValue(entry);
      if (parsedEntry === null) {
        // Tell "this is not a builder entry" apart from "this value is not
        // something JSON could ever have carried": both are refusals, but the
        // second says the payload was not written by the durable path at all.
        const badValue = isObject(entry) && hasExactKeys(entry, FIELD_ENTRY_KEYS) &&
          toDisplayValue(entry.value) === null;
        return { state: 'invalid', code: badValue ? 'VALUE_NOT_JSON' : 'FIELD_ENTRY_INVALID' };
      }
      values.push(parsedEntry);
    }
    parsedFields.push({ key, label: humanizeKey(key), values });
  }

  const review: ReviewItem[] = [];
  for (const item of needsReview) {
    const parsedItem = toReviewItem(item);
    if (parsedItem === null) return { state: 'invalid', code: 'REVIEW_ITEM_INVALID' };
    review.push(parsedItem);
  }

  const countOf = (kindName: ReviewItemKind) => review.filter((item) => item.kind === kindName).length;

  return {
    state: 'result',
    result: {
      status: productStatus,
      kind,
      fields: parsedFields,
      review,
      conflictCount: countOf('conflict'),
      coverageGapCount: countOf('coverage_gap'),
      taskFailureCount: countOf('task_failure'),
      multiValuedFieldCount: parsedFields.filter((field) => field.values.length > 1).length,
    },
  };
}

// --- static presentation vocabulary -----------------------------------------

/**
 * How an outcome is announced.
 *
 * `tone` drives styling only. Every distinction it makes is ALSO carried by
 * `label`, `symbol` and `summary`, so status meaning never depends on colour:
 * removing the stylesheet leaves the outcome fully readable.
 */
export type OutcomeTone = 'positive' | 'caution' | 'neutral' | 'negative';

export type OutcomeDescriptor = {
  tone: OutcomeTone;
  /** Short status word, shown as text next to the symbol. */
  label: string;
  /** A non-colour, non-decorative glyph, always paired with `label`. */
  symbol: string;
  /** One sentence stating exactly what the kind means. */
  summary: string;
};

const OUTCOMES: Readonly<Record<FinalResultKind, OutcomeDescriptor>> = {
  usable_result: {
    tone: 'positive',
    label: 'Usable result',
    symbol: '✔',
    summary: 'The run verified every field it reports and left nothing outstanding.',
  },
  partial_result: {
    tone: 'caution',
    label: 'Partial result',
    symbol: '!',
    summary:
      'The run verified some fields but did not finish: the items below are still outstanding. This is not a completed result.',
  },
  no_usable_result: {
    tone: 'negative',
    label: 'No usable result',
    symbol: '∅',
    summary:
      'The run finished without verifying anything usable. Nothing was disproved either — there is simply no result to report.',
  },
  not_found: {
    tone: 'neutral',
    label: 'Not found',
    symbol: '−',
    summary: 'A trusted source established that no matching record exists. This is a confirmed negative, not a failure.',
  },
};

export function describeOutcome(kind: FinalResultKind): OutcomeDescriptor {
  return OUTCOMES[kind];
}

/**
 * Safe, human-readable labels for the codes the contract can carry.
 *
 * A code NOT named here is still displayed — as the raw code, rendered safely
 * — because hiding an outstanding item would misreport the result. Nothing is
 * invented for an unknown code.
 */
const REVIEW_CODE_LABELS: Readonly<Record<string, string>> = {
  NO_USABLE_RESULT: 'No usable result was produced',
  REQUIRED_OUTPUT_MISSING: 'A required output was missing',
  EVIDENCE_REQUIREMENTS_UNMET: 'Evidence requirements were not met',
  TASK_FAILED: 'The task did not complete',
};

export function describeReviewCode(code?: string): string | undefined {
  if (code === undefined) return undefined;
  return REVIEW_CODE_LABELS[code];
}

/** Section headings for the outstanding-item groups, in display order. */
export const REVIEW_GROUPS: readonly { kind: ReviewItemKind; title: string; note: string }[] = [
  {
    kind: 'conflict',
    title: 'Conflicts',
    note: 'Sources disagreed and no source settled it. No value was chosen.',
  },
  {
    kind: 'needs_review',
    title: 'Needs review',
    note: 'The evidence was insufficient or ambiguous, so these values are not reported as verified.',
  },
  {
    kind: 'coverage_gap',
    title: 'Coverage gaps',
    note: 'A task completed without meeting its stated output or evidence requirements.',
  },
  {
    kind: 'task_failure',
    title: 'Task failures',
    note: 'A task did not complete. Its part of the result is missing.',
  },
];
