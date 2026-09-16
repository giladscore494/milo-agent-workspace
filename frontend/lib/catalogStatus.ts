/**
 * CODE-2 — the bounded, operator-facing catalog status for a Swarm V2 run.
 *
 * What this answers
 * -----------------
 *
 * "Did this run write anything into the canonical catalog, and if not, why
 * not?" Before this module the only answer available was to read the raw event
 * stream and recognise two type names by eye. That is developer telemetry, not
 * an operational signal, and it is what CAT-13 records.
 *
 * What it is allowed to read
 * --------------------------
 *
 * ONLY the browser-safe event contract `PromotionAttempt.as_event()` produces
 * (`backend/catalog/pipeline.py`). That payload is deliberately narrow — ids,
 * counts, booleans and static reason codes — because the things it could
 * otherwise carry are exactly the things that must never reach a browser: a
 * SQL message quoting a row, an evidence fragment, preserved register data,
 * model text.
 *
 * So the parsing here is closed rather than permissive:
 *
 *  - every field is read by name and checked for its exact type. A field of
 *    the wrong type is DROPPED, not coerced: `promoted_fields: 'engine'` is a
 *    malformed payload, and `['e','n','g','i','n','e']` is a fabrication;
 *  - unknown keys are never copied anywhere. Nothing iterates the payload;
 *  - lists are COUNTED, never retained, so a payload cannot grow this slice;
 *  - strings are truncated to a fixed bound;
 *  - the recent-action list is a fixed-size ring;
 *  - a reason code is kept only if it is in the closed allowlist below, and is
 *    rendered only through `catalogRefusalLabel`, which returns static text.
 *    An unrecognised code becomes a static fallback label and is NOT stored.
 *
 * A refusal is not a failed run
 * -----------------------------
 *
 * There is no failure concept in this state, by construction. A field with no
 * verified evidence, an unresolved conflict and a candidate the ingestion left
 * ambiguous are all legitimate outcomes of a research run — that is what the
 * durable catalog is FOR, and the backend says so at the point it refuses.
 *
 * An INFRASTRUCTURE failure is a different thing and never arrives here at all:
 * a lost lease or a failed pending-promotion read raises out of the worker,
 * which emits NO catalog event and does not finalize the run. Nothing in this
 * module can turn one into a refusal, because it never sees one.
 */

import { EventId } from './eventId';
import { ownsCatalogProjection } from './eventVocabulary';
import { redactSecretText } from './sanitize';
import { RunEvent } from './types';

/** Longest candidate/canonical key retained. Keys are short; this is a bound. */
export const MAX_CATALOG_KEY_CHARS = 120;

/** Fixed-size ring of recent catalog actions. */
export const MAX_CATALOG_ACTIONS = 20;

/**
 * Every refusal code the backend can emit, with the static text shown for it.
 *
 * Mirror of `PROMOTION_REASONS` (`backend/catalog/promotion.py`) plus the two
 * `PIPELINE_REASONS` (`backend/catalog/pipeline.py`) that layer adds. A closed
 * allowlist: an unrecognised code renders `UNKNOWN_CATALOG_REFUSAL_LABEL` and
 * is not retained, so a code invented by a payload can never become text on
 * the surface.
 */
export const CATALOG_REFUSAL_LABELS: Readonly<Record<string, string>> = {
  CATALOG_PROMOTION_CANDIDATE_NOT_READY:
    'The candidate is not in a promotable state',
  CATALOG_PROMOTION_SOURCE_NOT_EVIDENCE:
    'The source cannot support a canonical fact',
  CATALOG_PROMOTION_FIELD_UNSUPPORTED:
    'A field had no verified evidence behind it',
  CATALOG_PROMOTION_FIELD_UNEXPECTED:
    'Evidence named a field the canonical row does not state',
  CATALOG_PROMOTION_VALUE_MISMATCH:
    'Evidence and the candidate disagreed on a value',
  CATALOG_PROMOTION_LINK_CANDIDATE_MISMATCH:
    'An evidence link pointed at a different candidate',
  CATALOG_PROMOTION_LINK_UNVERIFIED:
    'An evidence link had no settled verified verdict',
  CATALOG_PROMOTION_CONFLICT_UNRESOLVED:
    'An unresolved conflict covers one of the fields',
  CATALOG_PROMOTION_REFUSED:
    'The durable catalog refused this promotion',
  CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE:
    'No verified verdict was settled for this candidate',
  CATALOG_PROMOTION_SNAPSHOT_UNUSABLE:
    'The snapshot it was read from cannot support a canonical fact',
};

/** Shown for any code outside the allowlist. Static text, never the code. */
export const UNKNOWN_CATALOG_REFUSAL_LABEL =
  'Refused for a reason this release does not recognise';

export function catalogRefusalLabel(code: string): string {
  return CATALOG_REFUSAL_LABELS[code] ?? UNKNOWN_CATALOG_REFUSAL_LABEL;
}

/**
 * What a refusal's reason is, as TRUSTED state — a closed tri-state.
 *
 * The first implementation stored `lastRefusalCode?: string` and, when a later
 * refusal had no allowlisted code, kept the previous one
 * (`reasonCode ?? state.lastRefusalCode`). Two conditions were therefore
 * indistinguishable — "the latest refusal has no code I can name" and "the
 * latest refusal is the older one I can name" — and the surface resolved the
 * ambiguity the wrong way: it went on presenting a stale known reason as *the
 * latest refusal*.
 *
 * So the reason is a value with three states, not a nullable string:
 *
 *   absent                    no refusal has been observed at all
 *   { known: true, code }     the latest refusal's code is on the allowlist
 *   { known: false }          the latest refusal's reason is unknown, missing
 *                             or malformed
 *
 * The `known: false` variant carries NO code property. There is nowhere in the
 * type for an untrusted string to sit, so "we saw a reason we cannot name" is
 * recorded without retaining one byte of what it said.
 */
export type CatalogRefusalReason =
  | { readonly known: true; readonly code: string }
  | { readonly known: false };

/**
 * The trusted sentinel for the third state.
 *
 * Frozen and shared: it is repository-authored data, never anything a payload
 * produced, and `catalogRefusalLabel` resolves it to static text.
 */
export const UNKNOWN_CATALOG_REFUSAL: CatalogRefusalReason = Object.freeze({ known: false });

/** The static text for a refusal reason in any of its known states. */
export function catalogRefusalReasonLabel(reason: CatalogRefusalReason): string {
  return reason.known ? catalogRefusalLabel(reason.code) : UNKNOWN_CATALOG_REFUSAL_LABEL;
}

/**
 * The largest number of promoted or unsupported fields one canonical promotion
 * can state. Mirror of `MAX_CANONICAL_FIELDS` in
 * `backend/catalog/contracts.py`, where it is `len(CANONICAL_VARIANT_FIELDS)`:
 * the two required identity columns, the two optional ones, and one per closed
 * identity dimension.
 *
 * Both lists `PromotionAttempt.as_event()` carries are subsets of that tuple —
 * `promoted_fields` is one entry per planned field, `unsupported_fields` is the
 * unsupported subset of the namespaced dimensions — so this one number bounds
 * both. A longer list did not come from that contract, and this module will not
 * present its length as a field count.
 *
 * Declared here rather than fetched, so the browser stays free of any runtime
 * coupling to the backend package;
 * `tests/test_catalog_execution_flag.py::test_the_frontend_bounds_promoted_field_counts_at_the_backend_maximum`
 * is the drift alarm that keeps the two numbers equal.
 */
export const MAX_CANONICAL_FIELDS = 12;

export type CatalogActionOutcome = 'promoted' | 'refused';

/** One bounded record of what the run did about one candidate. */
export type CatalogAction = {
  eventId: EventId;
  outcome: CatalogActionOutcome;
  /** Bounded; absent when the payload did not state a usable string. */
  candidateKey?: string;
  canonicalKey?: string;
  /**
   * Counts, never the field names themselves.
   *
   * ABSENT rather than zero when the payload's list broke its declared
   * contract — not an array, not an array of strings, or longer than
   * `MAX_CANONICAL_FIELDS`. A malformed list is not evidence of zero fields,
   * and rendering it as `0 fields promoted` would be a false statement of the
   * same kind the stale-reason defect made.
   */
  promotedFieldCount?: number;
  unsupportedFieldCount?: number;
  /** A promotion an earlier attempt already made; not a second write. */
  replayed: boolean;
  /** Present only on a refusal; the tri-state, never an untrusted string. */
  reason?: CatalogRefusalReason;
};

export type CatalogRunState = {
  /** True once any catalog event has been folded. */
  observed: boolean;
  promotedCount: number;
  refusedCount: number;
  replayedCount: number;
  /**
   * The LATEST refusal's reason, as the tri-state above.
   *
   * Absent means exactly one thing: no refusal has been observed. It is never
   * used to mean "the latest refusal had a reason I could not name" — that is
   * `UNKNOWN_CATALOG_REFUSAL`, and conflating the two is the defect this
   * field's type exists to make unrepresentable.
   */
  lastRefusalReason?: CatalogRefusalReason;
  /** Fixed-size ring, oldest first. */
  actions: CatalogAction[];
};

export const initialCatalogState: CatalogRunState = {
  observed: false,
  promotedCount: 0,
  refusedCount: 0,
  replayedCount: 0,
  actions: [],
};

function readPayload(event: RunEvent): Record<string, unknown> {
  const payload: unknown = event.payload;
  return payload && typeof payload === 'object' && !Array.isArray(payload)
    ? (payload as Record<string, unknown>)
    : {};
}

/**
 * A non-empty string of the declared type: sanitized, then truncated.
 *
 * `redactSecretText` is the existing per-string boundary the Swarm V2
 * final-result surface already runs every durable string through, and this is
 * the same defense in depth for the same reason: a candidate key is ordinary
 * product data as far as the contract is concerned, and that is not proof that
 * a credential cannot be inside one.
 *
 * Redaction runs BEFORE truncation on purpose — truncating first could split a
 * credential across the bound and leave a fragment the patterns no longer
 * match.
 */
function boundedString(payload: Record<string, unknown>, key: string): string | undefined {
  const value = payload[key];
  if (typeof value !== 'string') return undefined;
  const trimmed = redactSecretText(value).trim();
  if (trimmed === '') return undefined;
  return trimmed.slice(0, MAX_CATALOG_KEY_CHARS);
}

/**
 * How many entries a field list holds — or `undefined` when it is not a field
 * list this contract can have produced.
 *
 * The declared type is `list[str]`, bounded by `MAX_CANONICAL_FIELDS`. Three
 * things are therefore rejected outright, and rejection means an ABSENT count
 * rather than a zero:
 *
 *  - not an array at all. `'model_year_start'` is a malformed payload, and
 *    counting its 16 characters, or an object's `length` property, would be
 *    fabricating telemetry out of a type error;
 *  - an array with a non-string member. The backend emits field KEYS; an array
 *    holding an object or a number is not the list it claims to be, and the
 *    honest count of it is "none available";
 *  - longer than the contract allows. A canonical promotion cannot state more
 *    fields than `CANONICAL_VARIANT_FIELDS` has entries, so a longer list did
 *    not come from `as_event()` and its length is not a fact about a promotion.
 *
 * The length check runs BEFORE the per-member check so a hostile 100 000-entry
 * array is rejected without being walked.
 */
function fieldListCount(payload: Record<string, unknown>, key: string): number | undefined {
  const value = payload[key];
  if (!Array.isArray(value)) return undefined;
  if (value.length > MAX_CANONICAL_FIELDS) return undefined;
  if (!value.every((item) => typeof item === 'string')) return undefined;
  return value.length;
}

/**
 * The refusal reason as trusted state.
 *
 * Every path that is not an allowlisted code lands on the same sentinel:
 * absent key, wrong type, empty or whitespace-only string, and a well-formed
 * string that simply is not on the allowlist. The caller cannot tell those
 * apart, and does not need to — what it needs is that none of them can be
 * mistaken for a code, and that none of them retains the payload's value.
 */
function readRefusalReason(payload: Record<string, unknown>): CatalogRefusalReason {
  const raw = boundedString(payload, 'reason');
  if (raw !== undefined && Object.prototype.hasOwnProperty.call(CATALOG_REFUSAL_LABELS, raw)) {
    return { known: true, code: raw };
  }
  return UNKNOWN_CATALOG_REFUSAL;
}

function readBoolean(payload: Record<string, unknown>, key: string): boolean {
  return payload[key] === true;
}

function push(state: CatalogRunState, action: CatalogAction): CatalogAction[] {
  const actions = [...state.actions, action];
  return actions.length > MAX_CATALOG_ACTIONS
    ? actions.slice(actions.length - MAX_CATALOG_ACTIONS)
    : actions;
}

/**
 * Fold ONE recognised catalog event into the slice.
 *
 * The caller (`lib/swarmReducer.ts`) has already established that the type is
 * exactly one of the two catalog types and that the event id advances the
 * slice, so replay and duplication are no-ops by construction — the same rule
 * that makes every other Swarm V2 counter safe.
 *
 * The OUTCOME is decided by the event TYPE, never by `payload.promoted`: the
 * type is what the trusted emitter chose, the payload field is data travelling
 * beside it. They agree in production; if they ever disagreed, the type wins.
 */
export function reduceCatalogEvent(
  state: CatalogRunState,
  event: RunEvent,
  eventId: EventId,
): CatalogRunState {
  if (!ownsCatalogProjection(event.event_type)) return state;

  const payload = readPayload(event);
  const promoted = event.event_type === 'catalog_variant_promoted';
  // A reason travels only with a refusal. On a refusal it is ALWAYS resolved
  // to the tri-state, so a refusal always has a reason of its own to report.
  const reason = promoted ? undefined : readRefusalReason(payload);
  const replayed = promoted && readBoolean(payload, 'replayed');

  const action: CatalogAction = {
    eventId,
    outcome: promoted ? 'promoted' : 'refused',
    candidateKey: boundedString(payload, 'candidate_key'),
    canonicalKey: promoted ? boundedString(payload, 'canonical_key') : undefined,
    promotedFieldCount: fieldListCount(payload, 'promoted_fields'),
    unsupportedFieldCount: fieldListCount(payload, 'unsupported_fields'),
    replayed,
    reason,
  };

  return {
    observed: true,
    promotedCount: state.promotedCount + (promoted ? 1 : 0),
    refusedCount: state.refusedCount + (promoted ? 0 : 1),
    replayedCount: state.replayedCount + (replayed ? 1 : 0),
    // A REFUSAL always replaces the summary with its own reason — including
    // when that reason is the unknown sentinel. Falling back to the previous
    // value here is what made the surface claim an older known reason was the
    // latest refusal.
    //
    // A PROMOTION preserves it, because a promotion is not a newer refusal and
    // does not make the most recent one stop having happened.
    lastRefusalReason: promoted ? state.lastRefusalReason : reason,
    actions: push(state, action),
  };
}
