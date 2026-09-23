/**
 * The Mapping Plan — work-scope responses in, one edit out.
 *
 * What the browser holds, and what it does not
 * --------------------------------------------
 *
 * The plan is SERVER state: `backend/catalog/scope/` builds it, validates it,
 * stores it as immutable revisions and derives its digest. The browser holds
 * three things, none of which is authority:
 *
 *  - a PARSED copy of what the server said, built field by field below so a
 *    value outside the contract never reaches the screen;
 *  - a local DRAFT the person is editing — plain form state that means nothing
 *    until the server has validated it into a new revision;
 *  - the revision and digest it last READ, which every write names back so a
 *    plan that changed underneath the person is refused (`WORK_SCOPE_STALE`)
 *    instead of overwritten.
 *
 * A typed instruction and a saved draft travel to the same server contract and
 * come back as the same kind of revision. There is no browser-side
 * interpretation of an instruction: the words go to the server as words.
 *
 * Parsing is CLOSED, as in `lib/catalogReview.ts`: every field is read by name
 * and type-checked, a wrong type is absent rather than coerced, every string is
 * redacted and bounded, and a required field that is missing makes the whole
 * value `undefined` — "the server said something this release cannot read" —
 * rather than a half-built plan.
 */

import { redactSecretText } from './sanitize';

/** Mirrors `backend/catalog/scope/contract.py`. The server owns the real bounds. */
export const WORK_SCOPE_CONTRACT = 'milo-work-scope/1';
const DIGEST = /^[0-9a-f]{64}$/;
const UNIT_KEY = /^[a-z][a-z0-9_]{0,39}$/;
const MAX_TEXT_CHARS = 500;
const MAX_NAME_CHARS = 80;

export type WorkScopeLimits = {
  maxUnits: number;
  maxItems: number;
  defaultMaxItems: number;
  maxBatchSize: number;
  defaultBatchSize: number;
  minModelYear: number;
  maxModelYear: number;
  maxInstructionChars: number;
};

/**
 * Why the ordinary composer may not create a run in this project, from the
 * server's capability read. `catalog_batch_required`: the project's runs read
 * the Government catalog, and the worker refuses such a run unless it is a
 * prepared Mapping Plan batch, so catalog work starts from the Mapping Plan.
 * `unknown`: the server did not say, and a composer that cannot be confirmed
 * is never offered.
 */
export type DirectRunBlocker = 'catalog_batch_required' | 'run_creation_disabled' | 'unknown';

export type WorkScopeCapabilities = {
  available: boolean;
  reason?: 'mutations_disabled' | 'workflow_not_supported';
  limits: WorkScopeLimits;
  canPrepare: boolean;
  canStartBatches: boolean;
  directRuns: { allowed: boolean; blockedBy?: DirectRunBlocker };
};

export type CoverageState = 'known' | 'unverifiable' | 'unavailable';

export type DirectoryEntry = {
  key: string;
  name: string;
  nameHe?: string;
  origin: string;
  registerMarque?: string;
  registerMarqueVerified: boolean;
  coverageState: CoverageState;
  /** Exact, and possibly zero, only when `coverageState` is `known`. */
  canonicalVariants: number | null;
};

export type WorkScopeDirectory = {
  version: string;
  origins: ReadonlyMap<string, string>;
  entries: DirectoryEntry[];
  coverageAvailable: boolean;
  catalogVariants: number | null;
  attributedVariants: number | null;
};

export type WorkScopeNoteCode =
  | 'WORK_SCOPE_NOTE_DEFAULT_LIMIT'
  | 'WORK_SCOPE_NOTE_UNRECOGNIZED'
  | 'WORK_SCOPE_NOTE_ALREADY_MAPPED'
  | 'WORK_SCOPE_NOTE_COVERAGE_UNKNOWN'
  | 'WORK_SCOPE_NOTE_NOT_IN_PLAN'
  | 'WORK_SCOPE_NOTE_NO_CHANGE';

export type WorkScopeNote = { code: WorkScopeNoteCode; terms: string[]; units: string[] };

export type WorkScopePlan = {
  directoryVersion: string;
  units: string[];
  modelYearFrom: number | null;
  modelYearTo: number | null;
  maxItems: number;
  batchSize: number;
};

export type WorkScopeRevision = {
  revision: number;
  digest: string;
  inputKind: 'instruction' | 'edit';
  instruction?: string;
  notes: WorkScopeNote[];
  createdAt?: string;
};

export type WorkScopeState = {
  id: string;
  conversationId: string;
  revision: number;
  digest: string;
  /** False when the plan was validated against an older directory. */
  current: boolean;
  plan: WorkScopePlan;
  head: WorkScopeRevision;
  history: WorkScopeRevision[];
};

export type WorkScopeMutation = { applied: boolean; notes: WorkScopeNote[]; state: WorkScopeState };

/**
 * What a person reads for each note. Authored HERE and allowlisted by value,
 * exactly like `lib/errorText.ts`: a code outside this record is dropped by the
 * parser and never rendered.
 */
export const WORK_SCOPE_NOTE_COPY: Readonly<Record<WorkScopeNoteCode, string>> = {
  WORK_SCOPE_NOTE_DEFAULT_LIMIT:
    'No limit was stated, so the plan uses the default candidate limit. Change it below if you meant another.',
  WORK_SCOPE_NOTE_UNRECOGNIZED: 'These words were not understood and changed nothing:',
  WORK_SCOPE_NOTE_ALREADY_MAPPED: 'Left out because the catalog already holds them:',
  WORK_SCOPE_NOTE_COVERAGE_UNKNOWN:
    'Kept, although whether they are already mapped cannot be stated — their register spelling is not verified yet:',
  WORK_SCOPE_NOTE_NOT_IN_PLAN: 'Not in the plan, so there was nothing to remove:',
  WORK_SCOPE_NOTE_NO_CHANGE: 'Understood — the plan already says exactly that, so nothing changed.',
};

const NOTE_CODES = new Set(Object.keys(WORK_SCOPE_NOTE_COPY));

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown, limit = MAX_TEXT_CHARS): string | undefined {
  if (typeof value !== 'string') return undefined;
  const cleaned = redactSecretText(value).trim();
  return cleaned === '' ? undefined : cleaned.slice(0, limit);
}

function whole(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : undefined;
}

function yearOrNull(value: unknown): number | null | undefined {
  if (value === null) return null;
  return whole(value);
}

function units(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const keys = value.filter((key): key is string => typeof key === 'string' && UNIT_KEY.test(key));
  return keys.length === value.length ? keys : undefined;
}

function notes(value: unknown): WorkScopeNote[] {
  if (!Array.isArray(value)) return [];
  const parsed: WorkScopeNote[] = [];
  for (const item of value) {
    const source = asObject(item);
    if (typeof source.code !== 'string' || !NOTE_CODES.has(source.code)) continue;
    const terms = Array.isArray(source.terms)
      ? source.terms.map((term) => text(term, 30)).filter((term): term is string => term !== undefined)
      : [];
    parsed.push({ code: source.code as WorkScopeNoteCode, terms, units: units(source.units) ?? [] });
  }
  return parsed;
}

export function parseCapabilities(body: unknown): WorkScopeCapabilities | undefined {
  const source = asObject(body);
  const limits = asObject(source.limits);
  const read = {
    maxUnits: whole(limits.max_units),
    maxItems: whole(limits.max_items),
    defaultMaxItems: whole(limits.default_max_items),
    maxBatchSize: whole(limits.max_batch_size),
    defaultBatchSize: whole(limits.default_batch_size),
    minModelYear: whole(limits.min_model_year),
    maxModelYear: whole(limits.max_model_year),
    maxInstructionChars: whole(limits.max_instruction_chars),
  };
  if (typeof source.available !== 'boolean'
      || Object.values(read).some((value) => value === undefined || value < 1)) {
    return undefined;
  }
  const reason = source.reason === 'mutations_disabled' || source.reason === 'workflow_not_supported'
    ? source.reason : undefined;
  return {
    available: source.available,
    reason,
    limits: read as WorkScopeLimits,
    // Anything but an explicit `true` is "cannot" — a missing flag never
    // unlocks a control.
    canPrepare: source.can_prepare === true,
    canStartBatches: source.can_start_batches === true,
    directRuns: parseDirectRuns(source.direct_runs),
  };
}

const DIRECT_RUN_BLOCKERS: ReadonlySet<string> = new Set(['catalog_batch_required', 'run_creation_disabled']);

/** The server's answer on ordinary runs. Only an explicit `true` allows one. */
function parseDirectRuns(value: unknown): WorkScopeCapabilities['directRuns'] {
  const source = asObject(value);
  if (source.allowed === true) return { allowed: true };
  const blockedBy = typeof source.blocked_by === 'string' && DIRECT_RUN_BLOCKERS.has(source.blocked_by)
    ? source.blocked_by as DirectRunBlocker : 'unknown';
  return { allowed: false, blockedBy };
}

/**
 * The manufacturers of a plan that preparation can capture today, and those
 * it cannot. Preparation filters the Government register by a unit's VERIFIED
 * register spelling only; a unit without one is recorded
 * `register_unverified` and queues nothing. This is stated before the plan is
 * prepared, from the server's directory, so a plan never looks more runnable
 * than it is. A key the directory does not know is "cannot", never "can".
 */
export function preparableUnits(units: readonly string[], directory?: WorkScopeDirectory):
    { verified: string[]; unverified: string[] } {
  const entries = new Map((directory?.entries ?? []).map((entry) => [entry.key, entry]));
  const verified: string[] = [];
  const unverified: string[] = [];
  for (const key of units) {
    if (entries.get(key)?.registerMarqueVerified === true) verified.push(key);
    else unverified.push(key);
  }
  return { verified, unverified };
}

function coverageState(value: unknown): CoverageState {
  return value === 'known' || value === 'unverifiable' ? value : 'unavailable';
}

export function parseDirectory(body: unknown): WorkScopeDirectory | undefined {
  const source = asObject(body);
  const version = text(source.directory_version, 80);
  if (version === undefined || !Array.isArray(source.entries)) return undefined;
  const origins = new Map<string, string>();
  for (const item of Array.isArray(source.origins) ? source.origins : []) {
    const origin = asObject(item);
    const key = text(origin.key, 40);
    const label = text(origin.label, MAX_NAME_CHARS);
    if (key !== undefined && label !== undefined) origins.set(key, label);
  }
  const entries: DirectoryEntry[] = [];
  for (const item of source.entries) {
    const entry = asObject(item);
    const key = typeof entry.key === 'string' && UNIT_KEY.test(entry.key) ? entry.key : undefined;
    const name = text(entry.name, MAX_NAME_CHARS);
    const origin = text(entry.origin, 40);
    if (key === undefined || name === undefined || origin === undefined) continue;
    const coverage = asObject(entry.coverage);
    const state = coverageState(coverage.state);
    const count = whole(coverage.canonical_variants);
    entries.push({
      key,
      name,
      nameHe: text(entry.name_he, MAX_NAME_CHARS),
      origin,
      registerMarque: text(entry.register_marque, MAX_NAME_CHARS),
      registerMarqueVerified: entry.register_marque_verified === true,
      coverageState: state,
      // A count is only a fact when the server says the state is known.
      canonicalVariants: state === 'known' && count !== undefined && count >= 0 ? count : null,
    });
  }
  const summary = asObject(source.coverage);
  const catalogVariants = whole(summary.catalog_variants);
  const attributedVariants = whole(summary.attributed_variants);
  return {
    version,
    origins,
    entries,
    coverageAvailable: summary.available === true,
    catalogVariants: catalogVariants === undefined ? null : catalogVariants,
    attributedVariants: attributedVariants === undefined ? null : attributedVariants,
  };
}

function revision(value: unknown): WorkScopeRevision | undefined {
  const source = asObject(value);
  const number = whole(source.revision);
  const digest = typeof source.digest === 'string' && DIGEST.test(source.digest) ? source.digest : undefined;
  const inputKind = source.input_kind === 'instruction' || source.input_kind === 'edit'
    ? source.input_kind : undefined;
  if (number === undefined || number < 1 || digest === undefined || inputKind === undefined) {
    return undefined;
  }
  return {
    revision: number,
    digest,
    inputKind,
    instruction: inputKind === 'instruction' ? text(source.instruction) : undefined,
    notes: notes(source.notes),
    createdAt: text(source.created_at, 64),
  };
}

function plan(value: unknown): WorkScopePlan | undefined {
  const source = asObject(value);
  const unitKeys = units(source.units);
  const from = yearOrNull(source.model_year_from);
  const to = yearOrNull(source.model_year_to);
  const maxItems = whole(source.max_items);
  const batchSize = whole(source.batch_size);
  const directoryVersion = text(source.directory_version, 80);
  if (source.contract !== WORK_SCOPE_CONTRACT || unitKeys === undefined || unitKeys.length === 0
      || from === undefined || to === undefined || maxItems === undefined
      || batchSize === undefined || directoryVersion === undefined) {
    return undefined;
  }
  return { directoryVersion, units: unitKeys, modelYearFrom: from, modelYearTo: to, maxItems, batchSize };
}

export function parseWorkScopeState(body: unknown): WorkScopeState | undefined {
  const source = asObject(body);
  const id = text(source.work_scope_id, 64);
  const conversationId = text(source.conversation_id, 64);
  const number = whole(source.revision);
  const digest = typeof source.digest === 'string' && DIGEST.test(source.digest) ? source.digest : undefined;
  const parsedPlan = plan(source.plan);
  const head = revision(source.head);
  if (id === undefined || conversationId === undefined || number === undefined
      || digest === undefined || parsedPlan === undefined || head === undefined
      || typeof source.current !== 'boolean') {
    return undefined;
  }
  // The head the state names must BE the head it carries; a state that
  // disagrees with itself is not one this release renders.
  if (head.revision !== number || head.digest !== digest) return undefined;
  const history = Array.isArray(source.history)
    ? source.history.map(revision).filter((item): item is WorkScopeRevision => item !== undefined)
    : [];
  return { id, conversationId, revision: number, digest, current: source.current, plan: parsedPlan, head, history };
}

/** `null` is "this conversation has no plan"; `undefined` is "unreadable". */
export function parseOpenWorkScope(body: unknown): WorkScopeState | null | undefined {
  const source = asObject(body);
  if (!('work_scope' in source)) return undefined;
  if (source.work_scope === null) return null;
  return parseWorkScopeState(source.work_scope);
}

export function parseWorkScopeMutation(body: unknown): WorkScopeMutation | undefined {
  const source = asObject(body);
  const state = parseWorkScopeState(source.work_scope);
  if (typeof source.applied !== 'boolean' || state === undefined) return undefined;
  return { applied: source.applied, notes: notes(source.notes), state };
}

// ---------------------------------------------------------------------------
// The local draft.
// ---------------------------------------------------------------------------

/**
 * What the form holds. The years and the limit are the TEXT of their inputs,
 * so a half-typed value is not silently turned into a number; `draftEdit`
 * decides whether the text is an edit at all.
 */
export type WorkScopeDraft = {
  units: string[];
  modelYearFrom: string;
  modelYearTo: string;
  maxItems: string;
  batchSize: number;
};

export function draftFromPlan(source: WorkScopePlan): WorkScopeDraft {
  return {
    units: [...source.units],
    modelYearFrom: source.modelYearFrom === null ? '' : String(source.modelYearFrom),
    modelYearTo: source.modelYearTo === null ? '' : String(source.modelYearTo),
    maxItems: String(source.maxItems),
    batchSize: source.batchSize,
  };
}

export function emptyDraft(limits: WorkScopeLimits): WorkScopeDraft {
  return { units: [], modelYearFrom: '', modelYearTo: '', maxItems: String(limits.defaultMaxItems), batchSize: limits.defaultBatchSize };
}

export type DraftProblem = 'units' | 'years' | 'maxItems' | 'batchSize';

export type WorkScopeEdit = {
  units: string[];
  model_year_from: number | null;
  model_year_to: number | null;
  max_items: number;
  batch_size: number;
};

function wholeText(value: string): number | null | undefined {
  const trimmed = value.trim();
  if (trimmed === '') return null;
  return /^[0-9]{1,6}$/.test(trimmed) ? Number(trimmed) : undefined;
}

/**
 * The edit a draft states, or the first field that cannot be one.
 *
 * A convenience for the form, not a validator of record: it catches what a
 * person can see is wrong before a round trip. The server applies the real
 * bounds to whatever arrives, and its answer is the one shown.
 */
export function draftEdit(draft: WorkScopeDraft, limits: WorkScopeLimits):
  { edit: WorkScopeEdit } | { problem: DraftProblem } {
  if (draft.units.length === 0 || draft.units.length > limits.maxUnits) return { problem: 'units' };
  const from = wholeText(draft.modelYearFrom);
  const to = wholeText(draft.modelYearTo);
  const inRange = (year: number | null) => year === null || (year >= limits.minModelYear && year <= limits.maxModelYear);
  if (from === undefined || to === undefined || !inRange(from) || !inRange(to)
      || (from !== null && to !== null && from > to)) {
    return { problem: 'years' };
  }
  const maxItems = wholeText(draft.maxItems);
  if (maxItems === null || maxItems === undefined || maxItems < 1 || maxItems > limits.maxItems) {
    return { problem: 'maxItems' };
  }
  if (!Number.isSafeInteger(draft.batchSize) || draft.batchSize < 1 || draft.batchSize > limits.maxBatchSize) {
    return { problem: 'batchSize' };
  }
  return { edit: { units: [...draft.units], model_year_from: from, model_year_to: to, max_items: maxItems, batch_size: draft.batchSize } };
}

export function draftMatchesPlan(draft: WorkScopeDraft, source: WorkScopePlan | undefined): boolean {
  if (source === undefined) return draft.units.length === 0;
  const saved = draftFromPlan(source);
  return saved.units.join(',') === draft.units.join(',')
    && saved.modelYearFrom === draft.modelYearFrom.trim()
    && saved.modelYearTo === draft.modelYearTo.trim()
    && saved.maxItems === draft.maxItems.trim()
    && saved.batchSize === draft.batchSize;
}

export function addUnit(draft: WorkScopeDraft, key: string): WorkScopeDraft {
  return draft.units.includes(key) ? draft : { ...draft, units: [...draft.units, key] };
}

export function removeUnit(draft: WorkScopeDraft, key: string): WorkScopeDraft {
  return { ...draft, units: draft.units.filter((unit) => unit !== key) };
}

/** Move one unit up (-1) or down (+1). Order IS priority. */
export function moveUnit(draft: WorkScopeDraft, key: string, delta: -1 | 1): WorkScopeDraft {
  const index = draft.units.indexOf(key);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= draft.units.length) return draft;
  const next = [...draft.units];
  [next[index], next[target]] = [next[target], next[index]];
  return { ...draft, units: next };
}

/** What a person reads about one marque's coverage. Never a fabricated zero. */
export function coverageLabel(entry: Pick<DirectoryEntry, 'coverageState' | 'canonicalVariants'>): string {
  if (entry.coverageState === 'known' && entry.canonicalVariants !== null) {
    return entry.canonicalVariants === 0
      ? 'Not mapped yet'
      : `${entry.canonicalVariants} variant${entry.canonicalVariants === 1 ? '' : 's'} mapped`;
  }
  if (entry.coverageState === 'unverifiable') return 'Coverage unknown — register spelling not verified';
  return 'Coverage unavailable';
}

// ---------------------------------------------------------------------------
// Batches (scoped catalog PR3): progress in, one start / pause / resume out.
// ---------------------------------------------------------------------------
//
// A prepared plan is cut into bounded batches. Each batch is started by ONE
// person's request and runs as ONE product run; nothing starts the next batch.
// Everything below is a parsed copy of `GET /work-scopes/{id}/progress`, which
// the server derives from the bound runs' own durable status and promotion
// events. The browser never counts, never decides which batch is next and
// never decides whether a start is allowed: `controls` is the server's answer,
// and the database checks every start again under the plan's row lock.

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type BatchState = 'pending' | 'active' | 'completed' | 'partial' | 'interrupted';
export type UnitProgressState =
  'not_queued' | 'nothing_queued' | 'pending' | 'active' | 'in_progress' | 'completed';
export type PlanProgressStatus = 'not_prepared' | 'nothing_queued' | 'ready' | 'running' | 'complete';
export type StartBlocker =
  'batches_disabled' | 'closed' | 'paused' | 'not_prepared' | 'nothing_queued' | 'batch_running'
  | 'complete';

export type ProgressBatch = {
  batchId: string;
  batchNumber: number;
  unitKey: string;
  itemCount: number;
  state: BatchState;
  attempts: number;
  /** Present on a batch that has run: its latest run and its outcome counts. */
  runId?: string;
  runStatus?: string;
  promoted?: number;
  refused?: number;
  unresolved?: number;
};

export type ProgressUnit = {
  unitKey: string;
  name: string;
  priority: number;
  state: 'prepared' | 'register_unverified' | 'snapshot_unusable' | 'vocabulary_insufficient';
  reasonCode?: string;
  progress: UnitProgressState;
  queuedCount: number;
  batchCount: number;
  settledBatches: number;
  active: boolean;
  promoted: number;
  refused: number;
  unresolved: number;
};

export type ProgressLive = {
  batchId: string;
  batchNumber: number;
  revision: number;
  unitKey: string;
  itemCount: number;
  attempt: number;
  runId: string;
  runStatus: string;
  launchState?: string;
};

export type WorkScopeProgress = {
  workScopeId: string;
  revision: number;
  digest: string;
  closed: boolean;
  paused: boolean;
  status: PlanProgressStatus;
  live?: ProgressLive;
  preparation?: {
    revision: number;
    preparedAt?: string;
    unitCount: number;
    preparedUnitCount: number;
    units: ProgressUnit[];
    next?: ProgressBatch;
    recent: ProgressBatch[];
    batches: { total: number; settled: number; active: number; interrupted: number; remaining: number };
    items: { total: number; promoted: number; refused: number; unresolved: number; completed: number; remaining: number };
  };
  controls: {
    start: { available: boolean; blockedBy?: StartBlocker; batch?: ProgressBatch; retry: boolean; relaunch: boolean };
    pause: { available: boolean };
    resume: { available: boolean };
    cancel: { available: boolean; runId?: string };
  };
};

export type BatchStart = {
  runId: string;
  status: string;
  batchId: string;
  attempt: number;
  /** False for a replay, or for a second request for the batch already running. */
  created: boolean;
};

export type PauseResult = { changed: boolean; paused: boolean; progress: WorkScopeProgress };

/** What a person reads for each batch state. Authored here; nothing else renders. */
export const BATCH_STATE_COPY: Readonly<Record<BatchState, string>> = {
  pending: 'Not started',
  active: 'Running',
  completed: 'Finished',
  partial: 'Finished — some candidates unresolved',
  interrupted: 'Stopped before finishing',
};

export const UNIT_PROGRESS_COPY: Readonly<Record<UnitProgressState, string>> = {
  not_queued: 'Nothing queued',
  nothing_queued: 'Nothing queued',
  pending: 'Not started',
  active: 'Running now',
  in_progress: 'Partly done',
  completed: 'Done',
};

/** Why a prepared manufacturer queued nothing. Codes outside this are generic. */
export const UNIT_REASON_COPY: Readonly<Record<string, string>> = {
  WORK_SCOPE_REGISTER_UNVERIFIED: 'Its register spelling is not verified, so it was not captured.',
  WORK_SCOPE_VOCABULARY_INSUFFICIENT:
    'Most of its register rows use codes the catalog cannot read yet, so nothing was queued.',
  GOV_PROJECTION_SNAPSHOT_INCOMPLETE: 'Its register capture was incomplete, so nothing was queued.',
  GOV_PROJECTION_SNAPSHOT_NOT_READ: 'Its register capture could not be read, so nothing was queued.',
  GOV_PROJECTION_RESOURCE_NOT_NORMALIZED: 'Its register capture could not be read, so nothing was queued.',
  GOV_PROJECTION_SNAPSHOT_STATE_INVALID: 'Its register capture could not be read, so nothing was queued.',
};

export const START_BLOCKER_COPY: Readonly<Record<StartBlocker, string>> = {
  batches_disabled: 'Starting batches is turned off at the current activation stage.',
  closed: 'This mapping plan is closed.',
  paused: 'The plan is paused. Resume it to start the next batch.',
  not_prepared:
    'This revision of the plan has not been prepared yet. An operator prepares a revision; no batch can start until then.',
  nothing_queued: 'Preparing this revision queued no candidates, so there is no batch to start.',
  batch_running: 'A batch is running. The next one can start when it finishes.',
  complete: 'Every batch of this plan has finished.',
};

const BATCH_STATES: ReadonlySet<string> = new Set(Object.keys(BATCH_STATE_COPY));
const UNIT_PROGRESS_STATES: ReadonlySet<string> = new Set(Object.keys(UNIT_PROGRESS_COPY));
const START_BLOCKERS: ReadonlySet<string> = new Set(Object.keys(START_BLOCKER_COPY));
const PLAN_STATUSES: ReadonlySet<string> = new Set(['not_prepared', 'nothing_queued', 'ready', 'running', 'complete']);
const UNIT_STATES: ReadonlySet<string> = new Set(['prepared', 'register_unverified', 'snapshot_unusable', 'vocabulary_insufficient']);
const RUN_STATUS = /^[a-z_]{1,40}$/;

function uuid(value: unknown): string | undefined {
  return typeof value === 'string' && UUID_PATTERN.test(value) ? value.toLowerCase() : undefined;
}

function count(value: unknown): number | undefined {
  const number = whole(value);
  return number !== undefined && number >= 0 ? number : undefined;
}

function batch(value: unknown, withOutcome: boolean): ProgressBatch | undefined {
  const source = asObject(value);
  const batchId = uuid(source.batch_id);
  const batchNumber = count(source.batch_number);
  const itemCount = count(source.item_count);
  const attempts = count(source.attempts);
  const unitKey = typeof source.unit_key === 'string' && UNIT_KEY.test(source.unit_key) ? source.unit_key : undefined;
  if (batchId === undefined || batchNumber === undefined || itemCount === undefined
      || attempts === undefined || unitKey === undefined
      || typeof source.state !== 'string' || !BATCH_STATES.has(source.state)) {
    return undefined;
  }
  const parsed: ProgressBatch = {
    batchId, batchNumber, unitKey, itemCount, attempts, state: source.state as BatchState,
  };
  if (withOutcome) {
    const runId = uuid(source.run_id);
    const promoted = count(source.promoted);
    const refused = count(source.refused);
    const unresolved = count(source.unresolved);
    if (runId === undefined || promoted === undefined || refused === undefined || unresolved === undefined) {
      return undefined;
    }
    Object.assign(parsed, {
      runId,
      runStatus: typeof source.run_status === 'string' && RUN_STATUS.test(source.run_status)
        ? source.run_status : undefined,
      promoted, refused, unresolved,
    });
  }
  return parsed;
}

function progressUnit(value: unknown): ProgressUnit | undefined {
  const source = asObject(value);
  const unitKey = typeof source.unit_key === 'string' && UNIT_KEY.test(source.unit_key) ? source.unit_key : undefined;
  const numbers = {
    priority: count(source.priority), queuedCount: count(source.queued_count),
    batchCount: count(source.batch_count), settledBatches: count(source.settled_batches),
    promoted: count(source.promoted), refused: count(source.refused), unresolved: count(source.unresolved),
  };
  const name = text(source.name, MAX_NAME_CHARS);
  if (unitKey === undefined || name === undefined || Object.values(numbers).some((item) => item === undefined)
      || typeof source.state !== 'string' || !UNIT_STATES.has(source.state)
      || typeof source.progress !== 'string' || !UNIT_PROGRESS_STATES.has(source.progress)) {
    return undefined;
  }
  const reason = typeof source.reason_code === 'string' && /^[A-Z][A-Z0-9_]{2,79}$/.test(source.reason_code)
    ? source.reason_code : undefined;
  return {
    unitKey, name, state: source.state as ProgressUnit['state'], reasonCode: reason,
    progress: source.progress as UnitProgressState, active: source.active === true,
    ...(numbers as Omit<ProgressUnit, 'unitKey' | 'name' | 'state' | 'reasonCode' | 'progress' | 'active'>),
  };
}

function live(value: unknown): ProgressLive | undefined {
  const source = asObject(value);
  const read = {
    batchId: uuid(source.batch_id), runId: uuid(source.run_id),
    batchNumber: count(source.batch_number), revision: count(source.revision),
    itemCount: count(source.item_count), attempt: count(source.attempt),
  };
  const unitKey = typeof source.unit_key === 'string' && UNIT_KEY.test(source.unit_key) ? source.unit_key : undefined;
  if (Object.values(read).some((item) => item === undefined) || unitKey === undefined
      || typeof source.run_status !== 'string' || !RUN_STATUS.test(source.run_status)) {
    return undefined;
  }
  return {
    ...(read as Omit<ProgressLive, 'unitKey' | 'runStatus' | 'launchState'>),
    unitKey,
    runStatus: source.run_status,
    launchState: typeof source.launch_state === 'string' && RUN_STATUS.test(source.launch_state)
      ? source.launch_state : undefined,
  };
}

function totals<K extends string>(value: unknown, keys: readonly K[]): Record<K, number> | undefined {
  const source = asObject(value);
  const read: Partial<Record<K, number>> = {};
  for (const key of keys) {
    const number = count(source[key]);
    if (number === undefined) return undefined;
    read[key] = number;
  }
  return read as Record<K, number>;
}

/**
 * The plan's progress, or `undefined` when any part of it cannot be read.
 * A count shown wrong is worse than a count not shown, so a malformed unit,
 * batch or total refuses the whole answer rather than rendering around it.
 */
export function parseProgress(body: unknown): WorkScopeProgress | undefined {
  const source = asObject(body);
  const workScopeId = uuid(source.work_scope_id);
  const revisionNumber = whole(source.revision);
  const digest = typeof source.digest === 'string' && DIGEST.test(source.digest) ? source.digest : undefined;
  if (workScopeId === undefined || revisionNumber === undefined || revisionNumber < 1 || digest === undefined
      || typeof source.closed !== 'boolean' || typeof source.paused !== 'boolean'
      || typeof source.status !== 'string' || !PLAN_STATUSES.has(source.status)) {
    return undefined;
  }
  let parsedLive: ProgressLive | undefined;
  if (source.live !== null && source.live !== undefined) {
    parsedLive = live(source.live);
    if (parsedLive === undefined) return undefined;
  }
  let preparation: WorkScopeProgress['preparation'];
  if (source.preparation !== null && source.preparation !== undefined) {
    const raw = asObject(source.preparation);
    const unitsRead = Array.isArray(raw.units) ? raw.units.map(progressUnit) : undefined;
    const recentRead = Array.isArray(raw.recent) ? raw.recent.map((item) => batch(item, true)) : undefined;
    const nextRead = raw.next === null || raw.next === undefined ? null : batch(raw.next, false);
    const batchTotals = totals(raw.batches, ['total', 'settled', 'active', 'interrupted', 'remaining'] as const);
    const itemTotals = totals(raw.items, ['total', 'promoted', 'refused', 'unresolved', 'completed', 'remaining'] as const);
    const revisionRead = count(raw.revision);
    const unitCount = count(raw.unit_count);
    const preparedUnitCount = count(raw.prepared_unit_count);
    if (unitsRead === undefined || unitsRead.some((unit) => unit === undefined)
        || recentRead === undefined || recentRead.some((item) => item === undefined)
        || nextRead === undefined || batchTotals === undefined || itemTotals === undefined
        || revisionRead === undefined || unitCount === undefined || preparedUnitCount === undefined) {
      return undefined;
    }
    preparation = {
      revision: revisionRead,
      preparedAt: text(raw.prepared_at, 64),
      unitCount,
      preparedUnitCount,
      units: unitsRead as ProgressUnit[],
      next: nextRead ?? undefined,
      recent: recentRead as ProgressBatch[],
      batches: batchTotals,
      items: itemTotals,
    };
  }
  const controls = asObject(source.controls);
  const start = asObject(controls.start);
  const startBatch = start.batch === null || start.batch === undefined ? null : batch(start.batch, false);
  if (typeof start.available !== 'boolean' || startBatch === undefined) return undefined;
  const blockedBy = typeof start.blocked_by === 'string' && START_BLOCKERS.has(start.blocked_by)
    ? start.blocked_by as StartBlocker : undefined;
  const cancel = asObject(controls.cancel);
  return {
    workScopeId,
    revision: revisionNumber,
    digest,
    closed: source.closed,
    paused: source.paused,
    status: source.status as PlanProgressStatus,
    live: parsedLive,
    preparation,
    controls: {
      // Anything but an explicit `true`, with a batch to start, is "cannot":
      // a missing or malformed control never unlocks a button.
      start: {
        available: start.available === true && startBatch !== null,
        blockedBy,
        batch: startBatch ?? undefined,
        retry: start.retry === true,
        relaunch: start.relaunch === true,
      },
      pause: { available: asObject(controls.pause).available === true },
      resume: { available: asObject(controls.resume).available === true },
      cancel: { available: cancel.available === true && uuid(cancel.run_id) !== undefined, runId: uuid(cancel.run_id) },
    },
  };
}

export function parseBatchStart(body: unknown): BatchStart | undefined {
  const source = asObject(body);
  const runId = uuid(source.run_id);
  const batchId = uuid(source.batch_id);
  const attempt = count(source.attempt);
  if (runId === undefined || batchId === undefined || attempt === undefined
      || typeof source.status !== 'string' || !RUN_STATUS.test(source.status)
      || typeof source.created !== 'boolean') {
    return undefined;
  }
  return { runId, status: source.status, batchId, attempt, created: source.created };
}

export function parsePauseResult(body: unknown): PauseResult | undefined {
  const source = asObject(body);
  const progress = parseProgress(source.progress);
  if (typeof source.changed !== 'boolean' || typeof source.paused !== 'boolean' || progress === undefined) {
    return undefined;
  }
  return { changed: source.changed, paused: source.paused, progress };
}

/** "Candidate" or "candidates", for a count a person reads. */
export function candidatesLabel(total: number): string {
  return `${total} candidate${total === 1 ? '' : 's'}`;
}
