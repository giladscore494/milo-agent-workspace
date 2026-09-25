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
import { redactSecretText } from './sanitize';
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

/**
 * Field bounds mirrored from `backend/engines/swarm_v2/contracts.py`
 * (`EvidenceReference`, `VerificationVerdict`) and `evidence_bounds.py`.
 *
 * For provenance these are VALIDATION bounds, not display truncation: a
 * durable identifier outside them is a shape the builder cannot have written,
 * so it refuses the whole outcome rather than being trimmed to fit.
 */
export const BACKEND_BOUNDS = {
  claimId: 200,
  sourceId: 200,
  runId: 200,
  taskId: 80,
  entity: 200,
  field: 200,
  geography: 200,
  market: 200,
  reason: 500,
  /** `MAX_TIME_SCOPE_KEYS` / `MAX_FACT_VALUE_JSON_BYTES` in evidence_bounds.py. */
  timeScopeKeys: 8,
  timeScopeJsonBytes: 512,
} as const;

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
  'PROVENANCE_INVALID',
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
  /** Always present: the builder writes all four, and the parser requires them. */
  claimId: string;
  sourceId: string;
  taskId: string;
  entity: string;
  field: string;
  /** `EvidenceReference` types these as `str | None`, so they may be absent. */
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

/**
 * THE redaction boundary for this surface.
 *
 * Every durable string that can reach the product surface goes through here —
 * scalar values, strings nested inside structured values, structured-value
 * keys, field keys, review reasons and codes, task identifiers, provenance
 * identifiers and displayed scope values. There is deliberately one function
 * rather than a call at each render site, because a render site added later
 * would otherwise be a silent gap.
 *
 * Redaction runs BEFORE the length bound: bounding first could cut a
 * credential in half and leave a fragment that no longer matches a pattern.
 *
 * This is defense in depth and is NOT a substitute for the typed contract
 * above. The contract is what stops an unknown key being rendered at all; this
 * is what stops a credential hiding inside a key the contract allows. Neither
 * one makes the other unnecessary.
 */
function safeDurableText(value: string, maxChars: number = MAX_TEXT_CHARS): string {
  const redacted = redactSecretText(value);
  return redacted.length > maxChars ? `${redacted.slice(0, maxChars)}…` : redacted;
}

/**
 * A REQUIRED durable identifier, validated against the backend bound.
 *
 * Returns the redacted text for display, or `null` to refuse. Refusing rather
 * than truncating is the point: a value outside `EvidenceReference`'s bounds is
 * not a long identifier, it is a payload the builder could not have produced.
 */
function requiredId(value: unknown, maxChars: number): Refusable<string> {
  if (typeof value !== 'string' || value.length === 0 || value.length > maxChars) return null;
  return safeDurableText(value, maxChars);
}

/**
 * An OPTIONAL bounded scope string, mirroring `EvidenceReference` exactly:
 *
 *     geography: str | None = Field(default=None, max_length=200)
 *     market:    str | None = Field(default=None, max_length=200)
 *
 * There is no `min_length`, so `""` is backend-valid — `EvidenceReference`
 * accepts it and `FinalBuilder` copies it into the trace verbatim. Refusing it
 * here would make the browser STRICTER than the contract it mirrors and turn a
 * payload the backend legitimately built into `PROVENANCE_INVALID`.
 *
 * `null` and `""` are both "this scope dimension was not stated", so both come
 * back as `undefined` and the row is simply not displayed. The KEY is still
 * required by the closed scope check above; only its value is optional.
 *
 * Still refused: a non-string (a number, an array, an object) and a string past
 * the 200-character bound.
 */
function optionalScopeText(value: unknown, maxChars: number): Refusable<string | undefined> {
  if (value === null) return undefined;
  if (typeof value !== 'string' || value.length > maxChars) return null;
  if (value.length === 0) return undefined;
  return safeDurableText(value, maxChars);
}

/**
 * A parse step that can refuse. `null` means "the durable contract cannot
 * carry this", which fails the whole outcome rather than degrading one row.
 */
type Refusable<T> = T | null;

/**
 * Render one durable value under explicit bounds, or refuse it.
 *
 * `null` is a recorded absence. `undefined` is NOT: it cannot survive JSON and
 * `safe_durable_value` can never emit it, so a value key that carries it — at
 * the top of an entry, inside a list, or inside a structured object — means
 * the payload did not come from the durable path, and it refuses.
 *
 * Scalars render as themselves. Lists and records are rendered — the backend
 * contract permits them, so discarding them would drop a verified answer — but
 * only to `MAX_VALUE_DEPTH` levels and `MAX_VALUE_ITEMS` entries per level,
 * with the remainder COUNTED in `hidden` so the surface can say how much it is
 * not showing. Nothing is truncated silently.
 *
 * Every string that comes out of here — a scalar value and a record KEY alike
 * — has been through the redaction boundary.
 */
export function toDisplayValue(value: unknown, depth = 0): Refusable<DisplayValue> {
  // `undefined` is refused; only an explicit JSON null is a recorded absence.
  if (value === undefined) return null;
  if (value === null) return { display: 'empty' };
  if (typeof value === 'string') {
    if (value.length === 0) return { display: 'empty' };
    const text = safeDurableText(value);
    return text.length === 0 ? { display: 'empty' } : { display: 'text', text };
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
    // A structured-value KEY is durable text too, and is redacted like one.
    entries.push({ key: safeDurableText(key), value: rendered });
  }
  return { display: 'record', entries, hidden: all.length - shown.length };
}

/**
 * A `time_scope` the durable contract could have carried.
 *
 * Validated even though the product surface deliberately does not display it:
 * a malformed scope means the trace is not a builder trace, and the surface
 * must not present a field as verified on the strength of one.
 */
function isValidTimeScope(value: unknown): boolean {
  if (!isObject(value)) return false;
  const entries = ownEntries(value);
  if (entries.length > BACKEND_BOUNDS.timeScopeKeys) return false;
  // Every member must be JSON-expressible; `undefined` and non-finite numbers
  // are refused here for exactly the reason they are refused in a value.
  for (const [, item] of entries) {
    if (toDisplayValue(item) === null) return false;
  }
  let json: string | undefined;
  try {
    json = JSON.stringify(value);
  } catch {
    return false; // a cycle, or a throwing `toJSON`.
  }
  if (typeof json !== 'string') return false;
  return new TextEncoder().encode(json).length <= BACKEND_BOUNDS.timeScopeJsonBytes;
}

/**
 * The EXACT keys `FinalBuilder` writes into a provenance trace, and into its
 * `scope`. Both levels are closed: a missing key, an extra key, a wrong type,
 * an empty required identifier or a malformed scope refuses the outcome.
 */
const PROVENANCE_KEYS = ['claim_id', 'source_id', 'run_id', 'task_id', 'scope'] as const;
const PROVENANCE_SCOPE_KEYS = ['entity', 'field', 'geography', 'market', 'time_scope'] as const;

/**
 * Read a durable provenance trace, or refuse it.
 *
 * This mirrors the literal dictionary `FinalBuilder.build` constructs, key for
 * key. An incomplete trace is not "a field with less provenance" — it is a
 * field whose sourcing cannot be established, and showing it as verified would
 * be the exact claim this surface exists to avoid making.
 *
 * `run_id` and `time_scope` are REQUIRED and VALIDATED, and deliberately not
 * surfaced: the run is the one the user is already on (`SwarmRunCard` shows
 * it), and a time scope is structured technical metadata that belongs in the
 * Inspector. Validating what is not displayed is the point — a trace that
 * fails there is not a builder trace.
 */
function toProvenance(value: unknown): Refusable<ProvenanceReference> {
  if (!isObject(value) || !hasExactKeys(value, PROVENANCE_KEYS)) return null;

  const claimId = requiredId(value.claim_id, BACKEND_BOUNDS.claimId);
  const sourceId = requiredId(value.source_id, BACKEND_BOUNDS.sourceId);
  const runId = requiredId(value.run_id, BACKEND_BOUNDS.runId);
  const taskId = requiredId(value.task_id, BACKEND_BOUNDS.taskId);
  if (claimId === null || sourceId === null || runId === null || taskId === null) return null;

  const scope = value.scope;
  if (!isObject(scope) || !hasExactKeys(scope, PROVENANCE_SCOPE_KEYS)) return null;

  const entity = requiredId(scope.entity, BACKEND_BOUNDS.entity);
  const field = requiredId(scope.field, BACKEND_BOUNDS.field);
  if (entity === null || field === null) return null;

  const geography = optionalScopeText(scope.geography, BACKEND_BOUNDS.geography);
  const market = optionalScopeText(scope.market, BACKEND_BOUNDS.market);
  if (geography === null || market === null) return null;

  if (!isValidTimeScope(scope.time_scope)) return null;

  return { claimId, sourceId, taskId, entity, field, geography, market };
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
  // A register candidate the task truthfully could not resolve: an answer,
  // listed for review, never a task failure.
  'CANDIDATE_UNRESOLVED_AMBIGUOUS',
  'CANDIDATE_UNRESOLVED_NOT_FOUND',
]);

/** The exact key sets `FinalBuilder` and the engine write. */
const VERDICT_REVIEW_KEYS = ['field', 'value', 'reason', 'provenance'] as const;
const TASK_SCOPED_KEYS = ['task_id', 'code'] as const;
/** One verified value entry: exactly `{value, provenance}`. */
const FIELD_ENTRY_KEYS = ['value', 'provenance'] as const;

/**
 * Why a payload's interior was refused.
 *
 * Returned in place of the parsed shape so the caller reports the RIGHT
 * reason. Telling "this is not a builder entry" apart from "this value could
 * never have survived JSON" and "this provenance is not a builder trace" is
 * what makes an invalid result diagnosable instead of merely refused.
 */
type EntryRefusal = 'FIELD_ENTRY_INVALID' | 'REVIEW_ITEM_INVALID' | 'VALUE_NOT_JSON'
  | 'PROVENANCE_INVALID';

function isRefusal<T>(value: T | EntryRefusal): value is EntryRefusal {
  return typeof value === 'string';
}

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
 *
 * Classification reads the RAW reason and code; only the DISPLAY text is
 * redacted, so a redaction can never change which group an item lands in.
 */
function toReviewItem(item: Record<string, unknown>): ReviewItem | EntryRefusal {
  if (isEmptyMarker(item)) return { kind: 'empty_marker', code: NO_USABLE_RESULT_CODE };

  if (hasExactKeys(item, VERDICT_REVIEW_KEYS)) {
    const rawField = item.field;
    const rawReason = item.reason;
    if (typeof rawField !== 'string' || rawField.length === 0 ||
        rawField.length > BACKEND_BOUNDS.field) return 'REVIEW_ITEM_INVALID';
    if (typeof rawReason !== 'string' || rawReason.length === 0 ||
        rawReason.length > BACKEND_BOUNDS.reason) return 'REVIEW_ITEM_INVALID';
    const value = toDisplayValue(item.value);
    if (value === null) return 'VALUE_NOT_JSON';
    const provenance = toProvenance(item.provenance);
    if (provenance === null) return 'PROVENANCE_INVALID';
    const fieldKey = safeDurableText(rawField, BACKEND_BOUNDS.field);
    return {
      kind: CONFLICT_REASONS.has(rawReason) ? 'conflict' : 'needs_review',
      fieldKey,
      fieldLabel: humanizeKey(fieldKey),
      reason: safeDurableText(rawReason, BACKEND_BOUNDS.reason),
      value,
      provenance,
    };
  }

  if (hasExactKeys(item, TASK_SCOPED_KEYS)) {
    const rawTaskId = item.task_id;
    const rawCode = item.code;
    if (typeof rawTaskId !== 'string' || rawTaskId.length === 0 ||
        rawTaskId.length > BACKEND_BOUNDS.taskId) return 'REVIEW_ITEM_INVALID';
    if (typeof rawCode !== 'string' || rawCode.length === 0) return 'REVIEW_ITEM_INVALID';
    const taskId = safeDurableText(rawTaskId, BACKEND_BOUNDS.taskId);
    return {
      kind: COVERAGE_GAP_CODES.has(rawCode) ? 'coverage_gap' : 'task_failure',
      taskId,
      taskLabel: humanizeKey(taskId),
      code: safeDurableText(rawCode),
    };
  }

  return 'REVIEW_ITEM_INVALID';
}

function toVerifiedValue(entry: unknown): VerifiedValue | EntryRefusal {
  if (!isObject(entry) || !hasExactKeys(entry, FIELD_ENTRY_KEYS)) return 'FIELD_ENTRY_INVALID';
  const value = toDisplayValue(entry.value);
  if (value === null) return 'VALUE_NOT_JSON';
  const provenance = toProvenance(entry.provenance);
  if (provenance === null) return 'PROVENANCE_INVALID';
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
    if (key.length === 0 || key.length > BACKEND_BOUNDS.field) {
      return { state: 'invalid', code: 'FIELD_ENTRY_INVALID' };
    }
    const values: VerifiedValue[] = [];
    for (const entry of value) {
      const parsedEntry = toVerifiedValue(entry);
      if (isRefusal(parsedEntry)) return { state: 'invalid', code: parsedEntry };
      values.push(parsedEntry);
    }
    // The field KEY is durable text and is redacted like any other; the label
    // is derived from the redacted key so the two can never disagree.
    const displayKey = safeDurableText(key, BACKEND_BOUNDS.field);
    parsedFields.push({ key: displayKey, label: humanizeKey(displayKey), values });
  }

  const review: ReviewItem[] = [];
  for (const item of needsReview) {
    const parsedItem = toReviewItem(item);
    if (isRefusal(parsedItem)) return { state: 'invalid', code: parsedItem };
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
    // SOURCE-AGNOSTIC, because `decide_outcome` reaches `partial_result` from
    // ANY blocking condition: an unverified or rejected claim, a task failure,
    // a coverage gap, or a conflict. A run whose every gathered claim verified
    // can still be partial because a separate task failed — so the summary may
    // not say a claim went unverified, and may not promise a list either,
    // since a rejected verdict or a bare conflict produces no `needs_review`
    // row of its own. What is always true is exactly this: there are verified
    // fields, and the run did not complete all the work it was required to.
    summary:
      'The run verified the fields below but did not complete all the work it was required to. This is not a completed result.',
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
const REVIEW_CODE_LABELS: ReadonlyMap<string, string> = new Map([
  ['NO_USABLE_RESULT', 'No usable result was produced'],
  ['REQUIRED_OUTPUT_MISSING', 'A required output was missing'],
  ['EVIDENCE_REQUIREMENTS_UNMET', 'Evidence requirements were not met'],
  ['CANDIDATE_UNRESOLVED_AMBIGUOUS', 'The register matched more than one variant; left unresolved'],
  ['CANDIDATE_UNRESOLVED_NOT_FOUND', 'The register matched no variant'],
  ['TASK_FAILED', 'The task did not complete'],
]);

/**
 * A Map, not a plain object, because the key comes from the PAYLOAD.
 * `labels['constructor']` on an object literal returns a function off the
 * prototype chain, and React throws when handed a function as a child — so a
 * payload carrying `code: "constructor"` would have crashed the surface it was
 * meant to fail closed on. A Map has no prototype keys to reach.
 */
export function describeReviewCode(code?: string): string | undefined {
  if (code === undefined) return undefined;
  return REVIEW_CODE_LABELS.get(code);
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
