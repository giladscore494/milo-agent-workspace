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

export type CatalogActionOutcome = 'promoted' | 'refused';

/** One bounded record of what the run did about one candidate. */
export type CatalogAction = {
  eventId: EventId;
  outcome: CatalogActionOutcome;
  /** Bounded; absent when the payload did not state a usable string. */
  candidateKey?: string;
  canonicalKey?: string;
  /** Counts, never the field names themselves. */
  promotedFieldCount: number;
  unsupportedFieldCount: number;
  /** A promotion an earlier attempt already made; not a second write. */
  replayed: boolean;
  /** Present only for an allowlisted refusal code. */
  reasonCode?: string;
};

export type CatalogRunState = {
  /** True once any catalog event has been folded. */
  observed: boolean;
  promotedCount: number;
  refusedCount: number;
  replayedCount: number;
  /** Most recent ALLOWLISTED refusal code; unknown codes are not stored. */
  lastRefusalCode?: string;
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

/** How many entries a declared array holds. Anything else counts as none. */
function listLength(payload: Record<string, unknown>, key: string): number {
  const value = payload[key];
  return Array.isArray(value) ? value.length : 0;
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
  // A reason travels only with a refusal, and only an allowlisted code is kept.
  const reasonRaw = promoted ? undefined : boundedString(payload, 'reason');
  const reasonCode = reasonRaw !== undefined
    && Object.prototype.hasOwnProperty.call(CATALOG_REFUSAL_LABELS, reasonRaw)
    ? reasonRaw
    : undefined;
  const replayed = promoted && readBoolean(payload, 'replayed');

  const action: CatalogAction = {
    eventId,
    outcome: promoted ? 'promoted' : 'refused',
    candidateKey: boundedString(payload, 'candidate_key'),
    canonicalKey: promoted ? boundedString(payload, 'canonical_key') : undefined,
    promotedFieldCount: listLength(payload, 'promoted_fields'),
    unsupportedFieldCount: listLength(payload, 'unsupported_fields'),
    replayed,
    reasonCode,
  };

  return {
    observed: true,
    promotedCount: state.promotedCount + (promoted ? 1 : 0),
    refusedCount: state.refusedCount + (promoted ? 0 : 1),
    replayedCount: state.replayedCount + (replayed ? 1 : 0),
    lastRefusalCode: promoted ? state.lastRefusalCode : (reasonCode ?? state.lastRefusalCode),
    actions: push(state, action),
  };
}
