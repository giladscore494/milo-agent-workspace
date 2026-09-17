/**
 * CODE-3 — turning a catalog review response into trusted UI state.
 *
 * Why a parser exists at all
 * --------------------------
 *
 * The backend already projects every row through a closed allowlist and
 * validates it against a Pydantic model with `extra="forbid"`. This is the
 * second, independent boundary, and it exists for the reason F5 established:
 * the workspace must not render a value merely because a server it trusts sent
 * it. A durable string is product data as far as the contract is concerned, and
 * that is not proof a credential cannot be inside one.
 *
 * So the parsing here is CLOSED, exactly as `lib/catalogStatus.ts` is:
 *
 *  - every field is read by name and checked for its exact type. Nothing
 *    iterates a response object, so an unexpected key is never copied anywhere
 *    and can never reach the DOM — not even as JSON;
 *  - a field of the wrong type is ABSENT, never coerced. `model_year_start:
 *    '2022'` is a malformed payload and `Number('2022')` would be a fabricated
 *    fact; the honest reading is "not stated";
 *  - every string is redacted and then bounded;
 *  - identity dimensions are read through the closed vocabulary, so a jsonb
 *    column holding something else contributes nothing;
 *  - `total` and `hasMore` stay `null` when the server states none. They are
 *    never inferred from the item count: an unknown total rendered as `0` would
 *    be the claim that the catalog is empty, which is a different — and false —
 *    statement from "we do not know how many there are";
 *  - an unavailable reason is kept only if it is in the closed allowlist below,
 *    and is rendered only through `catalogUnavailableLabel`, which returns
 *    static text authored here. An unrecognised reason becomes a static
 *    fallback and is NOT retained.
 *
 * This is NOT `lib/catalogStatus.ts`
 * ----------------------------------
 *
 * That module answers "what did THIS RUN do to the catalog?" by folding the
 * run's own events. This one answers "what durable catalog state exists now?"
 * from a bounded server page. Different question, different source, different
 * lifetime — so they are two modules and never one reducer.
 */

import { redactSecretText } from './sanitize';
import {
  CanonicalCatalogItem,
  CanonicalCatalogPage,
  CatalogIdentityDimensions,
  CatalogPageMeta,
  CatalogReviewCandidateItem,
  CatalogReviewPage,
  CatalogReviewSnapshot,
  CatalogUnavailableReason,
} from './types';

/** Longest catalog string retained. Keys are 36 characters; this is a bound. */
export const MAX_CATALOG_TEXT_CHARS = 120;

/**
 * The page size the workspace asks for, and the largest it may ask for.
 *
 * Mirrors `DEFAULT_REVIEW_PAGE_ITEMS` / `MAX_REVIEW_PAGE_ITEMS` in
 * `backend/catalog/review.py`. The SERVER owns the real bound and refuses
 * anything above it; this is the client keeping its own requests inside it, so
 * a paging control cannot produce a request the server will reject.
 * `tests/test_catalog_review_surface.py::test_the_frontend_page_bounds_match_the_backend`
 * is the drift alarm.
 */
export const CATALOG_PAGE_SIZE = 25;
export const MAX_CATALOG_PAGE_SIZE = 100;

/**
 * The only identity dimensions that may be read, and the static label each
 * one renders under. Closed: a key outside this record is dropped, so the
 * vocabulary is a property of this module rather than of a response.
 *
 * Mirrors `CANDIDATE_IDENTITY_DIMENSIONS` in `backend/catalog/contracts.py`.
 */
export const CATALOG_DIMENSION_LABELS: Readonly<Record<string, string>> = {
  body_style: 'Body style',
  drivetrain: 'Drivetrain',
  engine_code: 'Engine code',
  fuel_type: 'Fuel',
  generation: 'Generation',
  market: 'Market',
  propulsion_technology: 'Propulsion',
  transmission: 'Transmission',
};

const DIMENSION_KEYS = Object.keys(
  CATALOG_DIMENSION_LABELS,
) as (keyof CatalogIdentityDimensions)[];

/** The ONE status this surface presents. Mirrors `REVIEW_CANDIDATE_STATUS`. */
export const REVIEW_STATUS = 'ready_for_review';

/**
 * Why there is nothing to review, with the static text shown for each.
 *
 * A closed allowlist mirroring `REVIEW_UNAVAILABLE_REASONS`. Each sentence says
 * what is true of the durable state without suggesting an action the operator
 * is not authorized to take here — this surface inspects, it does not fix.
 */
export const CATALOG_UNAVAILABLE_LABELS: Readonly<
  Record<CatalogUnavailableReason, string>
> = {
  no_active_snapshot:
    'No Government catalog snapshot is active, so there is nothing to review yet.',
  snapshot_not_read:
    'The active snapshot records no reading of its rows, so it cannot state candidates.',
  snapshot_not_normalized:
    'The active snapshot was captured raw-only and states no vehicle identities.',
  snapshot_incomplete:
    'The active snapshot holds rows its reviewed vocabulary could not read, so it does not answer here.',
  snapshot_state_invalid:
    "The active snapshot's recorded reading is malformed or disagrees with its own rows.",
  snapshot_unknown: 'No active snapshot carries the requested identity.',
  snapshot_unavailable: 'The catalog review source is unavailable.',
};

/** Shown for any reason outside the allowlist. Static text, never the code. */
export const UNKNOWN_CATALOG_UNAVAILABLE_LABEL =
  'There is nothing to review, for a reason this release does not recognise.';

export function catalogUnavailableLabel(
  reason: CatalogUnavailableReason | undefined,
): string {
  if (reason === undefined) return UNKNOWN_CATALOG_UNAVAILABLE_LABEL;
  return CATALOG_UNAVAILABLE_LABELS[reason] ?? UNKNOWN_CATALOG_UNAVAILABLE_LABEL;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/**
 * A non-empty string: sanitized, then truncated.
 *
 * Redaction runs BEFORE truncation on purpose — truncating first could split a
 * credential across the bound and leave a fragment the patterns no longer
 * match. The same ordering `lib/catalogStatus.ts` uses, for the same reason.
 */
function text(source: Record<string, unknown>, key: string): string | undefined {
  const value = source[key];
  if (typeof value !== 'string') return undefined;
  const cleaned = redactSecretText(value).trim();
  if (cleaned === '') return undefined;
  return cleaned.slice(0, MAX_CATALOG_TEXT_CHARS);
}

/**
 * A whole number, or absent.
 *
 * `true` is a `number`-adjacent value in JavaScript comparisons and is not a
 * model year, so booleans are excluded explicitly rather than by hoping no
 * payload carries one. A non-integer is absent rather than rounded.
 */
function whole(source: Record<string, unknown>, key: string): number | undefined {
  const value = source[key];
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) return undefined;
  return value;
}

/** A boolean, or `null` for "the server did not state one". Never defaulted. */
function tristate(source: Record<string, unknown>, key: string): boolean | null {
  return typeof source[key] === 'boolean' ? (source[key] as boolean) : null;
}

/** A whole number, or `null` for "not stated". Distinct from zero. */
function optionalWhole(source: Record<string, unknown>, key: string): number | null {
  const value = whole(source, key);
  return value === undefined ? null : value;
}

function dimensions(value: unknown): CatalogIdentityDimensions {
  const source = asObject(value);
  const stated: CatalogIdentityDimensions = {};
  for (const key of DIMENSION_KEYS) {
    const stringValue = text(source, key);
    if (stringValue !== undefined) stated[key] = stringValue;
  }
  return stated;
}

/**
 * Page metadata as trusted state.
 *
 * `limit` and `offset` fall back to the client's own request shape rather than
 * to zero when the server states nothing usable, because a page that claimed
 * `limit: 0` would render as a broken control. `total` and `hasMore` have no
 * such fallback: their absence is a real answer.
 */
function pageMeta(value: unknown, requested: { limit: number; offset: number }): CatalogPageMeta {
  const source = asObject(value);
  return {
    limit: whole(source, 'limit') ?? requested.limit,
    offset: whole(source, 'offset') ?? requested.offset,
    total: optionalWhole(source, 'total'),
    hasMore: tristate(source, 'has_more'),
  };
}

/**
 * One canonical item, or `undefined` when it has no identity to show under.
 *
 * The canonical key is required: it is what the row IS, and an item without one
 * is not a row this surface can present or key a list by. Dropping it is safer
 * than rendering an anonymous row that looks like catalog content.
 */
function canonicalItem(value: unknown): CanonicalCatalogItem | undefined {
  const source = asObject(value);
  const canonicalKey = text(source, 'canonical_key');
  if (canonicalKey === undefined) return undefined;
  return {
    canonicalKey,
    modelCanonicalKey: text(source, 'model_canonical_key'),
    manufacturer: text(source, 'manufacturer'),
    commercialModel: text(source, 'commercial_model'),
    modelYearStart: whole(source, 'model_year_start'),
    modelYearEnd: whole(source, 'model_year_end'),
    officialModelCode: text(source, 'official_model_code'),
    trim: text(source, 'trim'),
    identityDimensions: dimensions(source.identity_dimensions),
    promotedAt: text(source, 'promoted_at'),
    revisedAt: text(source, 'revised_at'),
  };
}

/**
 * One review candidate, or `undefined`.
 *
 * BOTH the key and the status are required, and the status must be exactly
 * `ready_for_review`. The server filters in the database and asserts the
 * answer; this refuses to display anything else regardless, so no row can be
 * shown on the review surface under a status that surface does not mean.
 */
function reviewItem(value: unknown): CatalogReviewCandidateItem | undefined {
  const source = asObject(value);
  const candidateKey = text(source, 'candidate_key');
  const status = text(source, 'status');
  if (candidateKey === undefined || status !== REVIEW_STATUS) return undefined;
  return {
    candidateKey,
    status,
    manufacturer: text(source, 'manufacturer'),
    commercialModel: text(source, 'commercial_model'),
    modelYearStart: whole(source, 'model_year_start'),
    modelYearEnd: whole(source, 'model_year_end'),
    officialModelCode: text(source, 'official_model_code'),
    trim: text(source, 'trim'),
    identityDimensions: dimensions(source.identity_dimensions),
  };
}

function snapshot(value: unknown): CatalogReviewSnapshot | undefined {
  const source = asObject(value);
  const snapshotKey = text(source, 'snapshot_key');
  if (snapshotKey === undefined) return undefined;
  return {
    snapshotKey,
    resourceId: text(source, 'resource_id'),
    packageId: text(source, 'package_id'),
    publisher: text(source, 'publisher'),
    datasetTitle: text(source, 'dataset_title'),
    datasetMarketScope: text(source, 'dataset_market_scope'),
    upstreamVersion: text(source, 'upstream_version'),
    upstreamVersionKind: text(source, 'upstream_version_kind'),
    activatedAt: text(source, 'activated_at'),
    declaredRecordCount: whole(source, 'declared_record_count'),
    storedRecordCount: whole(source, 'stored_record_count'),
    normalizationContract: text(source, 'normalization_contract'),
    normalizationIssueCount: whole(source, 'normalization_issue_count'),
  };
}

function unavailableReason(
  source: Record<string, unknown>,
): CatalogUnavailableReason | undefined {
  const raw = text(source, 'unavailable_reason');
  if (raw === undefined) return undefined;
  return Object.prototype.hasOwnProperty.call(CATALOG_UNAVAILABLE_LABELS, raw)
    ? (raw as CatalogUnavailableReason)
    : undefined;
}

/**
 * How many items a page may render, whatever it was sent.
 *
 * The server bounds its own page; this bounds what the browser will lay out
 * even if that bound is ever wrong, so a response cannot turn one screen into
 * an unbounded render.
 */
function bounded<T>(items: T[]): T[] {
  return items.slice(0, MAX_CATALOG_PAGE_SIZE);
}

export function parseCanonicalPage(
  value: unknown,
  requested: { limit: number; offset: number },
): CanonicalCatalogPage {
  const source = asObject(value);
  const items = Array.isArray(source.items) ? source.items : [];
  return {
    page: pageMeta(source.page, requested),
    items: bounded(
      items.map(canonicalItem).filter((item): item is CanonicalCatalogItem => item !== undefined),
    ),
  };
}

export function parseReviewPage(
  value: unknown,
  requested: { limit: number; offset: number },
): CatalogReviewPage {
  const source = asObject(value);
  const available = source.available === true;
  const items = Array.isArray(source.items) ? source.items : [];
  return {
    available,
    // A reason travels only with an unavailable page, and an available one
    // never carries a leftover reason into the UI.
    unavailableReason: available ? undefined : unavailableReason(source),
    status: REVIEW_STATUS,
    // No snapshot is shown for an unavailable page even if one was sent: an
    // unavailable page states no source, and displaying one would suggest
    // something was reviewed.
    snapshot: available ? snapshot(source.snapshot) : undefined,
    page: pageMeta(source.page, requested),
    items: available
      ? bounded(
          items
            .map(reviewItem)
            .filter((item): item is CatalogReviewCandidateItem => item !== undefined),
        )
      : [],
  };
}
