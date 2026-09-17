'use client';
import {
  CATALOG_DIMENSION_LABELS,
  catalogUnavailableLabel,
} from '@/lib/catalogReview';
import { safeText } from '@/lib/sanitize';
import {
  CanonicalCatalogItem,
  CanonicalCatalogPage,
  CatalogIdentityDimensions,
  CatalogPageMeta,
  CatalogReviewCandidateItem,
  CatalogReviewPage,
  CatalogReviewSnapshot,
} from '@/lib/types';

/** Which durable question the surface is answering right now. */
export type CatalogReviewView = 'canonical' | 'review';

export type CatalogReviewPanelProps = {
  /** Whether the surface applies at all — a project must be selected. */
  visible: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  view: CatalogReviewView;
  onViewChange: (view: CatalogReviewView) => void;
  loading: boolean;
  /** The caller's own static sentence; never upstream error text. */
  error: string;
  canonical?: CanonicalCatalogPage;
  review?: CatalogReviewPage;
  offset: number;
  onOffsetChange: (offset: number) => void;
  onRetry: () => void;
};

/**
 * CODE-3 — the read-only catalog review surface.
 *
 * What it answers, and what it cannot do
 * --------------------------------------
 *
 * Two durable questions: what the canonical catalog currently holds, and which
 * Government candidates are waiting for a human to look at them.
 *
 * It is an INSPECTION surface. There is no approve, reject, promote, edit,
 * capture, refresh, retry, activate, deactivate or delete control in this file,
 * and there is no code path from it to one: the only callbacks it takes change
 * which view is shown, which page is shown, and whether the panel is open.
 * `tests/catalogReviewSurface.test.tsx` enumerates every rendered control and
 * holds it to that.
 *
 * Why the two views are kept visibly apart
 * ----------------------------------------
 *
 * A candidate is a READING of a Government row. A canonical variant is a
 * PROMOTED fact with verified provenance behind every field. Presenting them in
 * one undifferentiated list would invite exactly the mistake the catalog is
 * built to prevent — treating an unreviewed reading as established truth. So
 * they are separate tabs, each table carries its own caption and heading, and
 * every candidate row carries an explicit "not canonical" marker in TEXT rather
 * than only in colour.
 *
 * What may be rendered
 * --------------------
 *
 * Only values `lib/catalogReview.ts` produced. Nothing here iterates a response,
 * there is no `JSON.stringify` of server data, no `dangerouslySetInnerHTML`, and
 * every durable string goes through `safeText` on the way to the DOM — the same
 * defence in depth the Final Result surface applies, for the same reason.
 */
export function CatalogReviewPanel({
  visible,
  open,
  onOpenChange,
  view,
  onViewChange,
  loading,
  error,
  canonical,
  review,
  offset,
  onOffsetChange,
  onRetry,
}: CatalogReviewPanelProps) {
  if (!visible) return null;

  return (
    <section className="panel catalog-review" aria-labelledby="catalog-review-title">
      <header className="catalog-review-head">
        <div>
          <h3 className="panel-title" id="catalog-review-title">Catalog review</h3>
          <p className="eyebrow">Durable catalog state — read only</p>
        </div>
        <button
          type="button"
          className="disclosure"
          aria-expanded={open}
          aria-controls="catalog-review-body"
          onClick={() => onOpenChange(!open)}
        >
          {open ? 'Hide' : 'Show'}
        </button>
      </header>

      {/*
        The body is rendered only while the panel is OPEN, rather than hidden
        with `hidden`. A hidden subtree still sits in the DOM, and the status
        regions inside it are live: a collapsed panel nobody opened would add
        announcements to an ordinary run, which is exactly what F5's
        "announcements stay rare and meaningful" rule forbids
        (`tests/accessibility.test.tsx`). The wrapper stays so `aria-controls`
        always names a real element.
      */}
      <div id="catalog-review-body">
        {!open ? null : (
        <>
        <p className="note">
          This surface inspects what the catalog already holds. It cannot promote,
          approve, reject, edit or capture anything.
        </p>

        <div className="catalog-tabs" role="tablist" aria-label="Catalog review views">
          <ViewTab
            id="canonical"
            current={view}
            label="Canonical catalog"
            hint="Promoted, with verified provenance"
            onSelect={onViewChange}
          />
          <ViewTab
            id="review"
            current={view}
            label="Ready for review"
            hint="Candidates awaiting a human"
            onSelect={onViewChange}
          />
        </div>

        <div
          className="catalog-view"
          id={`catalog-panel-${view}`}
          role="tabpanel"
          aria-labelledby={`catalog-tab-${view}`}
          data-surface={view}
        >
          <CatalogViewBody
            view={view}
            loading={loading}
            error={error}
            canonical={canonical}
            review={review}
            offset={offset}
            onOffsetChange={onOffsetChange}
            onRetry={onRetry}
          />
        </div>
        </>
        )}
      </div>
    </section>
  );
}

function ViewTab({
  id,
  current,
  label,
  hint,
  onSelect,
}: {
  id: CatalogReviewView;
  current: CatalogReviewView;
  label: string;
  hint: string;
  onSelect: (view: CatalogReviewView) => void;
}) {
  const selected = current === id;
  return (
    <button
      type="button"
      role="tab"
      id={`catalog-tab-${id}`}
      className="catalog-tab"
      aria-selected={selected}
      aria-controls={`catalog-panel-${id}`}
      onClick={() => onSelect(id)}
    >
      <span className="catalog-tab-label">{label}</span>
      <span className="catalog-tab-hint">{hint}</span>
    </button>
  );
}

function CatalogViewBody({
  view,
  loading,
  error,
  canonical,
  review,
  offset,
  onOffsetChange,
  onRetry,
}: Pick<
  CatalogReviewPanelProps,
  'view' | 'loading' | 'error' | 'canonical' | 'review' | 'offset' | 'onOffsetChange' | 'onRetry'
>) {
  // An error is reported before anything else and never beside stale rows: a
  // page that failed to load must not be read as the current catalog.
  if (error) {
    return (
      <StatusNote tone="negative" symbol="×" label="Not loaded" detail={error}>
        <button type="button" className="button button--quiet" onClick={onRetry}>
          Try again
        </button>
      </StatusNote>
    );
  }

  const page = view === 'canonical' ? canonical : review;
  // `undefined` is "not read yet", which is a different statement from "the
  // catalog is empty" and never borrows its message.
  if (loading || page === undefined) {
    return (
      <StatusNote
        symbol="…"
        label="Loading"
        detail="Reading the durable catalog. Nothing is shown until the server answers."
      />
    );
  }

  if (view === 'review') {
    const reviewPage = review as CatalogReviewPage;
    if (!reviewPage.available) {
      // NOT an empty catalog. The distinction is the whole point of the typed
      // unavailable state: nothing was reviewed, so nothing is claimed.
      return (
        <StatusNote
          symbol="∅"
          label="Nothing to review"
          detail={catalogUnavailableLabel(reviewPage.unavailableReason)}
        />
      );
    }
    return (
      <>
        <ReviewBanner snapshot={reviewPage.snapshot} />
        {reviewPage.items.length === 0 ? (
          <StatusNote
            symbol="∅"
            label="No candidates waiting"
            detail="The active snapshot holds no candidates in ready_for_review on this page."
          />
        ) : (
          <ReviewTable items={reviewPage.items} />
        )}
        <Pagination page={reviewPage.page} offset={offset} onOffsetChange={onOffsetChange} />
      </>
    );
  }

  const canonicalPage = canonical as CanonicalCatalogPage;
  return (
    <>
      <p className="note">
        Every row below was promoted with verified provenance behind each of its
        fields.
      </p>
      {canonicalPage.items.length === 0 ? (
        <StatusNote
          symbol="∅"
          label="No canonical variants"
          detail="The canonical catalog holds nothing on this page. Promotion produces these rows."
        />
      ) : (
        <CanonicalTable items={canonicalPage.items} />
      )}
      <Pagination page={canonicalPage.page} offset={offset} onOffsetChange={onOffsetChange} />
    </>
  );
}

/**
 * What is being reviewed, stated once for the whole page.
 *
 * The reading gap is called out when the snapshot records one, because a page
 * read from a snapshot with unread rows is not a complete review of that
 * capture and should not look like one.
 */
function ReviewBanner({ snapshot }: { snapshot?: CatalogReviewSnapshot }) {
  if (!snapshot) return null;
  const issues = snapshot.normalizationIssueCount;
  return (
    <div className="catalog-banner" role="note">
      <p className="catalog-banner-title">
        <span className="catalog-badge" data-kind="review">Not canonical</span>
        Government candidates awaiting review
      </p>
      <dl className="catalog-meta">
        <MetaEntry label="Snapshot" value={snapshot.snapshotKey} mono />
        <MetaEntry label="Dataset" value={snapshot.datasetTitle} />
        <MetaEntry label="Publisher" value={snapshot.publisher} />
        <MetaEntry label="Upstream version" value={snapshot.upstreamVersion} mono />
        <MetaEntry label="Activated" value={snapshot.activatedAt} />
        <MetaEntry
          label="Rows captured"
          value={snapshot.storedRecordCount === undefined ? undefined : String(snapshot.storedRecordCount)}
        />
      </dl>
      {issues !== undefined && issues > 0 && (
        <p className="note">
          {issues === 1
            ? 'This snapshot records 1 row its reviewed vocabulary could not read.'
            : `This snapshot records ${issues} rows its reviewed vocabulary could not read.`}
        </p>
      )}
    </div>
  );
}

function MetaEntry({ label, value, mono }: { label: string; value?: string; mono?: boolean }) {
  // An absent value is an absent entry. Rendering "—" for one would present a
  // field the snapshot does not state as a field it states as nothing.
  if (value === undefined) return null;
  return (
    <div className="catalog-meta-entry">
      <dt>{label}</dt>
      <dd className={mono ? 'identifier' : undefined}>{safeText(value)}</dd>
    </div>
  );
}

function CanonicalTable({ items }: { items: CanonicalCatalogItem[] }) {
  return (
    <div className="catalog-table-scroll">
      <table className="catalog-table">
        <caption className="sr-only">
          Canonical catalog variants, one bounded page
        </caption>
        <thead>
          <tr>
            <th scope="col">Manufacturer</th>
            <th scope="col">Model</th>
            <th scope="col">Years</th>
            <th scope="col">Model code</th>
            <th scope="col">Trim</th>
            <th scope="col">Dimensions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.canonicalKey}>
              <td>
                <Value value={item.manufacturer} />
                <span className="identifier catalog-row-key">{safeText(item.canonicalKey)}</span>
              </td>
              <td><Value value={item.commercialModel} /></td>
              <td><YearRange start={item.modelYearStart} end={item.modelYearEnd} /></td>
              <td><Value value={item.officialModelCode} mono /></td>
              <td><Value value={item.trim} /></td>
              <td><Dimensions dimensions={item.identityDimensions} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReviewTable({ items }: { items: CatalogReviewCandidateItem[] }) {
  return (
    <div className="catalog-table-scroll">
      <table className="catalog-table catalog-table--review">
        <caption className="sr-only">
          Government candidates with status ready for review. These are not
          canonical catalog rows.
        </caption>
        <thead>
          <tr>
            <th scope="col">State</th>
            <th scope="col">Manufacturer</th>
            <th scope="col">Model</th>
            <th scope="col">Years</th>
            <th scope="col">Model code</th>
            <th scope="col">Trim</th>
            <th scope="col">Dimensions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.candidateKey}>
              {/* Text, not colour: the distinction survives a monochrome
                  screen, a screen reader and a stylesheet that failed. */}
              <td>
                <span className="catalog-badge" data-kind="review">Candidate</span>
                <span className="catalog-row-note">not canonical</span>
              </td>
              <td>
                <Value value={item.manufacturer} />
                <span className="identifier catalog-row-key">{safeText(item.candidateKey)}</span>
              </td>
              <td><Value value={item.commercialModel} /></td>
              <td><YearRange start={item.modelYearStart} end={item.modelYearEnd} /></td>
              <td><Value value={item.officialModelCode} mono /></td>
              <td><Value value={item.trim} /></td>
              <td><Dimensions dimensions={item.identityDimensions} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * One cell. An unstated value renders as static text saying so, never as an
 * empty cell and never as a coerced value.
 */
function Value({ value, mono }: { value?: string; mono?: boolean }) {
  if (value === undefined) return <span className="muted">Not stated</span>;
  return <span className={mono ? 'identifier' : undefined}>{safeText(value)}</span>;
}

function YearRange({ start, end }: { start?: number; end?: number }) {
  if (start === undefined || end === undefined) {
    return <span className="muted">Not stated</span>;
  }
  return <span>{start === end ? String(start) : `${start}–${end}`}</span>;
}

/**
 * The dimensions a row states, each under static authored copy.
 *
 * The labels come from this module's closed vocabulary, so a dimension name is
 * never itself rendered — only the label the release authored for it.
 */
function Dimensions({ dimensions }: { dimensions: CatalogIdentityDimensions }) {
  const entries = Object.entries(dimensions).filter(
    ([name, value]) =>
      typeof value === 'string' &&
      Object.prototype.hasOwnProperty.call(CATALOG_DIMENSION_LABELS, name),
  ) as [string, string][];
  if (entries.length === 0) return <span className="muted">None stated</span>;
  return (
    <ul className="catalog-dimensions">
      {entries.map(([name, value]) => (
        <li key={name}>
          <span className="catalog-dimension-label">{CATALOG_DIMENSION_LABELS[name]}</span>
          <span>{safeText(value)}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * Page navigation, and nothing else.
 *
 * The counts come from the server's own page metadata. `total: null` means the
 * server did not state one, and the control says so rather than showing a
 * number it does not have — `hasMore` alone still decides whether Next is
 * available, because "we do not know the total" is not "there is no next page".
 */
function Pagination({
  page,
  offset,
  onOffsetChange,
}: {
  page: CatalogPageMeta;
  offset: number;
  onOffsetChange: (offset: number) => void;
}) {
  const first = offset + 1;
  const hasPrevious = offset > 0;
  const hasNext = page.hasMore === true;
  return (
    <nav className="catalog-pagination" aria-label="Catalog page navigation">
      <p className="note" role="status">
        {page.total === null
          ? `Showing from row ${first}. The server did not state a total.`
          : `Showing from row ${first} of ${page.total}.`}
      </p>
      <div className="catalog-pagination-controls">
        <button
          type="button"
          className="button button--quiet"
          disabled={!hasPrevious}
          onClick={() => onOffsetChange(Math.max(0, offset - page.limit))}
        >
          Previous page
        </button>
        <button
          type="button"
          className="button button--quiet"
          disabled={!hasNext}
          onClick={() => onOffsetChange(offset + page.limit)}
        >
          Next page
        </button>
      </div>
    </nav>
  );
}

function StatusNote({
  tone,
  symbol,
  label,
  detail,
  children,
}: {
  tone?: 'negative';
  symbol: string;
  label: string;
  detail: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="catalog-status" data-tone={tone ?? 'neutral'} role="status">
      <p className="catalog-status-label">
        <span aria-hidden="true">{symbol}</span>
        <span>{label}</span>
      </p>
      <p className="muted">{detail}</p>
      {children}
    </div>
  );
}
