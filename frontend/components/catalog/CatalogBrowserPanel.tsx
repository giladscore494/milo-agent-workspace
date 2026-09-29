'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import {
  BrowserFilters, BrowserLevel, COVERAGE_LEVELS, COVERAGE_LEVEL_COPY, Facets, Manufacturer, Model, ModelYear,
  PAGE_SIZE, Page, SEGMENTS, SEGMENT_COPY, Segment, Variant, browserQuery, directoryKeyFor, parseFacets,
  parseManufacturer, parseModel, parseModelYear, parsePage, parseVariant, planAddition,
} from '@/lib/catalogBrowser';
import { safeErrorText } from '@/lib/errorText';
import { safeText } from '@/lib/sanitize';
import {
  WorkScopeCapabilities, WorkScopeDirectory, WorkScopeEdit, WorkScopeState, parseCapabilities, parseDirectory,
  parseOpenWorkScope, parseWorkScopeMutation,
} from '@/lib/workScope';

/** Every call the page makes; injectable so tests need no network. GETs,
 *  except the two EXISTING Mapping Plan writes "Add to plan" uses. */
export type CatalogBrowserClient = {
  browse: (projectId: string, level: BrowserLevel, query: string) => Promise<unknown>;
  capabilities: (projectId: string) => Promise<unknown>;
  directory: (projectId: string) => Promise<unknown>;
  openPlan: (conversationId: string) => Promise<unknown>;
  createPlan: (conversationId: string, edit: WorkScopeEdit) => Promise<unknown>;
  revisePlan: (planId: string, head: { revision: number; digest: string }, edit: WorkScopeEdit) => Promise<unknown>;
};

const defaultClient: CatalogBrowserClient = {
  browse: (projectId, level, query) => api.catalogBrowser(projectId, level, query),
  capabilities: (projectId) => api.workScopeCapabilities(projectId),
  directory: (projectId) => api.workScopeDirectory(projectId),
  openPlan: (conversationId) => api.openWorkScope(conversationId),
  createPlan: (conversationId, edit) => api.createWorkScope(conversationId, { edit }),
  revisePlan: (planId, head, edit) => api.reviseWorkScope(planId, head, { edit }),
};

export type CatalogBrowserPanelProps = { projectId?: string; conversationId?: string; client?: CatalogBrowserClient };

type Path = { tozar?: string; model?: string; year?: number };
type Listing =
  | { level: 'manufacturers'; page: Page<Manufacturer> }
  | { level: 'models'; page: Page<Model> }
  | { level: 'years'; page: Page<ModelYear> }
  | { level: 'variants'; page: Page<Variant> };
type Shown = { kind: 'hidden' } | { kind: 'loading' } | { kind: 'ready'; listing: Listing } | { kind: 'error'; message: string };

const READ_FALLBACK = 'The catalog could not be read. Try again.';
const PLAN_FALLBACK = 'The plan could not be changed. Nothing was added.';
const REFUSAL_COPY = {
  nothing_mappable: 'None of the selected manufacturers is in the plan directory with a verified register spelling.',
  too_many_units: 'The plan would hold more manufacturers than it may.',
  no_change: 'The plan already covers that selection; nothing changed.',
} as const;

function levelOf(path: Path): Exclude<BrowserLevel, 'facets'> {
  if (path.tozar === undefined) return 'manufacturers';
  if (path.model === undefined) return 'models';
  return path.year === undefined ? 'years' : 'variants';
}

function parseListing(level: Listing['level'], body: unknown): Listing | undefined {
  switch (level) {
    case 'manufacturers': { const page = parsePage(body, parseManufacturer); return page && { level, page }; }
    case 'models': { const page = parsePage(body, parseModel); return page && { level, page }; }
    case 'years': { const page = parsePage(body, parseModelYear); return page && { level, page }; }
    default: { const page = parsePage(body, parseVariant); return page && { level: 'variants', page }; }
  }
}

function yearValue(value: string): number | undefined {
  return /^[0-9]{4}$/.test(value.trim()) ? Number(value.trim()) : undefined;
}

/**
 * PR-CAT (D5 + D6) -- the Catalog page: the deterministic catalog variants
 * as a tree, manufacturer -> model -> year -> variants, every level paged by
 * the server. It exists only while the server has the catalog browser on
 * (its first read answers 404 otherwise and this renders nothing). Reading
 * changes nothing; "Add to plan" only writes the conversation's Mapping Plan
 * through its existing routes -- it never prepares, arms or starts anything.
 */
export function CatalogBrowserPanel({ projectId, conversationId, client = defaultClient }: CatalogBrowserPanelProps) {
  const [shown, setShown] = useState<Shown>({ kind: 'hidden' });
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState<Path>({});
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState<BrowserFilters>({});
  const [facets, setFacets] = useState<Facets>();
  const [exists, setExists] = useState(false);
  const generation = useRef(0);

  const read = useCallback(async (project: string, where: Path, at: number, by: BrowserFilters, first: boolean) => {
    const mine = ++generation.current;
    const level = levelOf(where);
    try {
      const body = await client.browse(project, level, browserQuery(by, {
        tozar: where.tozar, kinuy_mishari: where.model, shnat_yitzur: where.year, limit: PAGE_SIZE, offset: at }));
      if (mine !== generation.current) return;
      const listing = parseListing(level, body);
      if (listing === undefined) throw new Error('unreadable');
      setExists(true);
      setShown({ kind: 'ready', listing });
    } catch (error) {
      if (mine !== generation.current) return;
      // The page exists only once the server answered it: a first read that
      // fails (404 while the browser is off) shows nothing at all.
      if (first) setShown({ kind: 'hidden' });
      else setShown({ kind: 'error', message: safeErrorText(error, READ_FALLBACK) });
    }
  }, [client]);

  useEffect(() => {
    setExists(false); setPath({}); setOffset(0); setFilters({}); setFacets(undefined);
    setShown({ kind: 'hidden' });
    if (!projectId) return;
    void read(projectId, {}, 0, {}, true);
    void (async () => {
      try {
        setFacets(parseFacets(await client.browse(projectId, 'facets', '')));
      } catch {
        // No facets: the filters offer "Any" only.
      }
    })();
  }, [projectId, client, read]);

  if (!projectId || !exists) return null;

  const go = (where: Path, at = 0, by = filters) => {
    setPath(where); setOffset(at); setFilters(by); setShown({ kind: 'loading' });
    void read(projectId, where, at, by, false);
  };

  return (
    <section className="panel catalog-browser" aria-labelledby="catalog-browser-title">
      <header className="catalog-review-head">
        <div>
          <h3 className="panel-title" id="catalog-browser-title">Catalog</h3>
          <p className="eyebrow">Deterministic catalog variants from the Government register — read-only</p>
        </div>
        <button type="button" className="disclosure" aria-expanded={open} aria-controls="catalog-browser-body"
          onClick={() => setOpen(!open)}>{open ? 'Hide' : 'Show'}</button>
      </header>
      {open && (
        <div id="catalog-browser-body">
          <FilterBar facets={facets} filters={filters} onApply={(next) => go({}, 0, next)} />
          <nav aria-label="Catalog path" className="button-row">
            <button type="button" className="button button--quiet" onClick={() => go({})}>All manufacturers</button>
            {path.tozar !== undefined && (
              <button type="button" className="button button--quiet" onClick={() => go({ tozar: path.tozar })}>
                {safeText(path.tozar)}</button>)}
            {path.model !== undefined && (
              <button type="button" className="button button--quiet"
                onClick={() => go({ tozar: path.tozar, model: path.model })}>{safeText(path.model)}</button>)}
            {path.year !== undefined && <span className="muted">{path.year}</span>}
          </nav>
          {shown.kind === 'loading' && <p className="muted" role="status">Loading…</p>}
          {shown.kind === 'error' && <p className="alert" role="alert">{safeText(shown.message)}</p>}
          {shown.kind === 'ready' && (
            <>
              <Listing listing={shown.listing} path={path} onOpen={(where) => go(where)} projectId={projectId}
                conversationId={conversationId} client={client} filters={filters} />
              <Pager page={shown.listing.page} onPage={(at) => go(path, at)} offset={offset} />
            </>
          )}
        </div>
      )}
    </section>
  );
}

function FilterBar({ facets, filters, onApply }: { facets?: Facets; filters: BrowserFilters;
  onApply: (next: BrowserFilters) => void }) {
  const [segment, setSegment] = useState<string>(filters.segment ?? '');
  const [yearFrom, setYearFrom] = useState(filters.yearFrom !== undefined ? String(filters.yearFrom) : '');
  const [yearTo, setYearTo] = useState(filters.yearTo !== undefined ? String(filters.yearTo) : '');
  const [fuel, setFuel] = useState(filters.delekCd !== undefined ? String(filters.delekCd) : '');
  const [body, setBody] = useState(filters.merkav ?? '');
  const from = yearValue(yearFrom);
  const to = yearValue(yearTo);
  const invalid = (yearFrom !== '' && from === undefined) || (yearTo !== '' && to === undefined)
    || (from !== undefined && to !== undefined && from > to);
  return (
    <form className="catalog-filters" aria-label="Catalog filters" onSubmit={(event) => {
      event.preventDefault();
      if (invalid) return;
      onApply({ segment: segment === '' ? undefined : (segment as Segment), yearFrom: from, yearTo: to,
                delekCd: fuel === '' ? undefined : Number(fuel), merkav: body === '' ? undefined : body });
    }}>
      <label>Segment <select value={segment} onChange={(event) => setSegment(event.target.value)}>
        <option value="">Any</option>
        {SEGMENTS.map((value) => <option key={value} value={value}>{SEGMENT_COPY[value]}</option>)}
      </select></label>
      <label>From year <input inputMode="numeric" value={yearFrom} placeholder={String(facets?.yearMin ?? '')}
        onChange={(event) => setYearFrom(event.target.value)} /></label>
      <label>To year <input inputMode="numeric" value={yearTo} placeholder={String(facets?.yearMax ?? '')}
        onChange={(event) => setYearTo(event.target.value)} /></label>
      <label>Fuel <select value={fuel} onChange={(event) => setFuel(event.target.value)}>
        <option value="">Any</option>
        {(facets?.fuels ?? []).map((item) => (
          <option key={item.delekCd} value={String(item.delekCd)}>{safeText(item.delekNm)}</option>))}
      </select></label>
      <label>Body <select value={body} onChange={(event) => setBody(event.target.value)}>
        <option value="">Any</option>
        {(facets?.bodies ?? []).map((item) => (
          <option key={item.merkav} value={item.merkav}>{safeText(item.merkav)}</option>))}
      </select></label>
      <button type="submit" className="button button--quiet" disabled={invalid}>Apply filters</button>
      {invalid && <span className="alert" role="alert">Years are four digits, the first not after the last.</span>}
    </form>
  );
}

function Pager({ page, offset, onPage }: { page: Page<unknown>; offset: number; onPage: (at: number) => void }) {
  if (page.total <= page.limit) return null;
  const last = Math.min(offset + page.items.length, page.total);
  return (
    <div className="button-row" aria-label="Pages">
      <button type="button" className="button button--quiet" disabled={offset === 0}
        onClick={() => onPage(Math.max(0, offset - page.limit))}>Previous</button>
      <span className="muted">{offset + 1}–{last} of {page.total}</span>
      <button type="button" className="button button--quiet" disabled={last >= page.total}
        onClick={() => onPage(offset + page.limit)}>Next</button>
    </div>
  );
}

type ListingProps = { listing: Listing; path: Path; onOpen: (where: Path) => void; projectId: string;
  conversationId?: string; client: CatalogBrowserClient; filters: BrowserFilters };

function Listing({ listing, path, onOpen, projectId, conversationId, client, filters }: ListingProps) {
  if (listing.page.items.length === 0) {
    return <p className="muted">No catalog variants match. Built snapshots appear here once their build completes.</p>;
  }
  switch (listing.level) {
    case 'manufacturers':
      return <Manufacturers page={listing.page} onOpen={(tozar) => onOpen({ tozar })} projectId={projectId}
        conversationId={conversationId} client={client} filters={filters} />;
    case 'models':
      return (
        <>
          <p className="note">A Mapping Plan selects whole manufacturers by model year; a model is browsed here but
            cannot be added to a plan on its own.</p>
          <ul className="catalog-list">{listing.page.items.map((item) => (
            <li key={item.kinuyMishari}><button type="button" className="button button--quiet"
              onClick={() => onOpen({ tozar: path.tozar, model: item.kinuyMishari })}>{safeText(item.kinuyMishari)}</button>
              {' '}<span className="muted">{item.variants} variants, {item.yearMin ?? '—'}–{item.yearMax ?? '—'}</span></li>))}
          </ul>
        </>);
    case 'years':
      return <ul className="catalog-list">{listing.page.items.map((item) => (
        <li key={item.year}><button type="button" className="button button--quiet"
          onClick={() => onOpen({ ...path, year: item.year })}>{item.year}</button>
          {' '}<span className="muted">{item.variants} variants</span></li>))}</ul>;
    default:
      return <Variants items={listing.page.items} />;
  }
}

function Variants({ items }: { items: Variant[] }) {
  return (
    <ul className="catalog-variants">{items.map((item) => (
      <li key={item.upstreamRecordId} aria-label={`Variant ${item.upstreamRecordId}`}>
        <p>Source: Government register (Ministry of Transport), record{' '}
          <span className="identifier">{safeText(item.upstreamRecordId)}</span> of snapshot{' '}
          <span className="identifier">{safeText(item.snapshotKey)}</span> — {SEGMENT_COPY[item.segment]}</p>
        <p aria-label="Coverage">{COVERAGE_LEVELS.map((level) => {
          const badge = item.coverage[level];
          return (
            <span key={level} className="badge" data-level={level}>
              {COVERAGE_LEVEL_COPY[level]}: {badge === null ? 'not covered'
                : `${badge.status.replace(/_/g, ' ')}${badge.current ? '' : ' (older content)'}`}
            </span>);
        })}</p>
        <dl className="catalog-fields">{item.fields.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{safeText(value)}</dd></div>))}</dl>
        {item.parseIssues > 0 && <p className="note">{item.parseIssues} register value(s) could not be read and are shown as not stated.</p>}
      </li>))}
    </ul>);
}

type PlanState = { capabilities?: WorkScopeCapabilities; directory?: WorkScopeDirectory; plan?: WorkScopeState | null };

function Manufacturers({ page, onOpen, projectId, conversationId, client, filters }: {
  page: Page<Manufacturer>; onOpen: (tozar: string) => void; projectId: string; conversationId?: string;
  client: CatalogBrowserClient; filters: BrowserFilters }) {
  const [plan, setPlan] = useState<PlanState>({});
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    if (!conversationId) return;
    let live = true;
    void (async () => {
      try {
        const [caps, dir, open] = await Promise.all([client.capabilities(projectId), client.directory(projectId),
                                                     client.openPlan(conversationId)]);
        // An unreadable open plan is not "no plan": Add to plan stays off.
        if (live) setPlan({ capabilities: parseCapabilities(caps), directory: parseDirectory(dir),
                            plan: parseOpenWorkScope(open) });
      } catch {
        // No plan state: Add to plan stays off.
      }
    })();
    return () => { live = false; };
  }, [client, projectId, conversationId]);

  const canPlan = conversationId !== undefined && plan.capabilities?.available === true
    && plan.directory !== undefined && plan.plan !== undefined;
  const entries = plan.directory?.entries ?? [];

  const add = async () => {
    if (!canPlan || !conversationId || busy || plan.capabilities === undefined) return;
    if (plan.plan === undefined) return;
    const addition = planAddition({ tozars: [...selected], yearFrom: filters.yearFrom ?? null,
                                    yearTo: filters.yearTo ?? null }, plan.plan, entries,
                                  plan.capabilities.limits);
    if (addition.kind === 'refused') { setMessage(REFUSAL_COPY[addition.reason]); return; }
    setBusy(true); setMessage('');
    try {
      const answer = addition.kind === 'create'
        ? await client.createPlan(conversationId, addition.edit)
        : await client.revisePlan(plan.plan!.id, addition.head, addition.edit);
      // The saved head; an unreadable answer leaves the plan unknown (off).
      setPlan((current) => ({ ...current, plan: parseWorkScopeMutation(answer)?.state }));
      setSelected(new Set());
      setMessage(`Added to the Mapping Plan${addition.unmapped.length ? ` (left out: ${addition.unmapped.length} without a verified register spelling)` : ''}. Prepare it from the Mapping Plan; nothing was started here.`);
    } catch (error) {
      setMessage(safeErrorText(error, PLAN_FALLBACK));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <ul className="catalog-list">{page.items.map((item) => {
        const mapped = directoryKeyFor(item.tozar, entries) !== undefined;
        return (
          <li key={item.tozar}>
            {canPlan && (
              <input type="checkbox" aria-label={`Select ${item.tozar}`} disabled={!mapped || busy}
                checked={selected.has(item.tozar)} onChange={() => setSelected((current) => {
                  const next = new Set(current);
                  if (next.has(item.tozar)) next.delete(item.tozar); else next.add(item.tozar);
                  return next;
                })} />)}
            <button type="button" className="button button--quiet" onClick={() => onOpen(item.tozar)}>
              {safeText(item.tozar)}</button>
            {' '}<span className="muted">{item.variants} variants</span>
          </li>);
      })}</ul>
      {canPlan && (
        <div className="button-row">
          <button type="button" className="button button--primary" disabled={busy || selected.size === 0}
            onClick={() => void add()}>{busy ? 'Adding…' : `Add to plan (${selected.size})`}</button>
          <span className="muted">Year range: {filters.yearFrom ?? 'any'}–{filters.yearTo ?? 'any'}. Variants the
            catalog already enriched are left out when the plan is prepared.</span>
        </div>)}
      {message && <p className="muted" role="status">{safeText(message)}</p>}
    </>
  );
}
