'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api } from '@/lib/api';
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

export type CatalogBrowserPanelProps = {
  projectId?: string;
  conversationId?: string;
  /** The page's own answer to "may this person edit a Mapping Plan here"
   *  (execution UI on AND plan writes on); nothing is offered otherwise. */
  planWrites?: boolean;
  /** Called after the plan was written, so the page's Mapping Plan reloads. */
  onPlanChanged?: () => void;
  client?: CatalogBrowserClient;
};

type Path = { tozar?: string; model?: string; year?: number };
type Listing =
  | { level: 'manufacturers'; page: Page<Manufacturer> }
  | { level: 'models'; page: Page<Model> }
  | { level: 'years'; page: Page<ModelYear> }
  | { level: 'variants'; page: Page<Variant> };
type Shown = { kind: 'hidden' } | { kind: 'loading' } | { kind: 'ready'; listing: Listing } | { kind: 'error'; message: string };
/** `plan` undefined = not (yet) read, null = the conversation has none. */
type PlanState = { capabilities?: WorkScopeCapabilities; directory?: WorkScopeDirectory; plan?: WorkScopeState | null };

const READ_FALLBACK = 'The catalog could not be read. Try again.';
const PLAN_FALLBACK = 'The plan could not be changed. Nothing was added.';
const REFUSAL_COPY = {
  nothing_mappable: 'None of the selected manufacturers is in the plan directory with a verified register spelling.',
  too_many_units: 'The plan would hold more manufacturers than it may.',
  no_change: 'The plan already holds every selected manufacturer; nothing changed.',
  years_differ: 'The plan has one model-year range for all its manufacturers. Clear the year filter, or set it to the '
    + 'plan\'s range, to add to it; change the range itself in the Mapping Plan.',
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

/**
 * PR-CAT (D5 + D6) -- the Catalog page: the deterministic catalog variants
 * as a tree, manufacturer -> model -> year -> variants, every level paged by
 * the server. It exists only while the server has the catalog browser on
 * (its first read answers 404 otherwise and this renders nothing; any other
 * failure of that read is shown as an error). Reading
 * changes nothing; "Add to plan" only writes the conversation's Mapping Plan
 * through its existing routes -- it never prepares, arms or starts anything.
 */
export function CatalogBrowserPanel({ projectId, conversationId, planWrites = false, onPlanChanged,
  client = defaultClient }: CatalogBrowserPanelProps) {
  const [shown, setShown] = useState<Shown>({ kind: 'hidden' });
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState<Path>({});
  const [offset, setOffset] = useState(0);
  const [filters, setFilters] = useState<BrowserFilters>({});
  const [facets, setFacets] = useState<Facets>();
  const [exists, setExists] = useState(false);
  const [plan, setPlan] = useState<PlanState>({});
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  const planGeneration = useRef(0);
  // The facets' own guard: only a project change drops them, never a read.
  const facetGeneration = useRef(0);

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
      // The page exists only once the server answered it: a 404 (the browser
      // is off) shows nothing at all; any other failure is an error.
      if (first && error instanceof ApiError && error.status === 404) {
        setShown({ kind: 'hidden' });
        return;
      }
      setExists(true);
      setShown({ kind: 'error', message: safeErrorText(error, READ_FALLBACK) });
    }
  }, [client]);

  useEffect(() => {
    setExists(false); setPath({}); setOffset(0); setFilters({}); setFacets(undefined);
    setShown({ kind: 'hidden' });
    const mine = ++facetGeneration.current;
    if (!projectId) return;
    void read(projectId, {}, 0, {}, true);
    void (async () => {
      try {
        const body = await client.browse(projectId, 'facets', '');
        // Facets of a project the page has since left are dropped.
        if (facetGeneration.current === mine) setFacets(parseFacets(body));
      } catch {
        // No facets: the filters offer "Any" only.
      }
    })();
  }, [projectId, client, read]);

  // The conversation's plan state, read only once the page exists (the
  // browser answered). Everything about the previous conversation is
  // dropped FIRST, so nothing is ever written to another conversation's plan.
  const loadPlan = useCallback(async () => {
    const mine = ++planGeneration.current;
    setPlan({});
    if (!projectId || !conversationId || !planWrites || !exists) return;
    try {
      const [caps, dir, openPlan] = await Promise.all([
        client.capabilities(projectId), client.directory(projectId), client.openPlan(conversationId)]);
      if (mine !== planGeneration.current) return;
      // An unreadable open plan is not "no plan": Add to plan stays off.
      setPlan({ capabilities: parseCapabilities(caps), directory: parseDirectory(dir),
                plan: parseOpenWorkScope(openPlan) });
    } catch {
      // No plan state: Add to plan stays off.
    }
  }, [client, projectId, conversationId, planWrites, exists]);

  useEffect(() => {
    setSelected(new Set());
    setMessage('');
    void loadPlan();
  }, [loadPlan]);

  if (!projectId || !exists) return null;

  const go = (where: Path, at = 0, by = filters) => {
    setPath(where); setOffset(at); setFilters(by); setShown({ kind: 'loading' });
    void read(projectId, where, at, by, false);
  };

  const canPlan = planWrites && conversationId !== undefined && plan.capabilities?.available === true
    && plan.directory !== undefined && plan.plan !== undefined;
  const otherFilters = filters.segment !== undefined || filters.delekCd !== undefined || filters.merkav !== undefined;
  // An open plan keeps ITS year range (planAddition); only a new plan takes the filter's.
  const openPlan = plan.plan?.plan;
  const yearsDiffer = openPlan !== undefined && (filters.yearFrom !== undefined || filters.yearTo !== undefined)
    && ((filters.yearFrom ?? null) !== openPlan.modelYearFrom || (filters.yearTo ?? null) !== openPlan.modelYearTo);
  const years = openPlan
    ? `the plan's own ${openPlan.modelYearFrom ?? 'any'}–${openPlan.modelYearTo ?? 'any'}, kept when it is revised`
      + (yearsDiffer ? ' (clear the year filter, or set it to that range, to add)' : '')
    : `${filters.yearFrom ?? 'any'}–${filters.yearTo ?? 'any'}`;

  const add = async () => {
    if (!canPlan || !conversationId || busy || plan.capabilities === undefined || plan.plan === undefined) return;
    const addition = planAddition({ tozars: [...selected], yearFrom: filters.yearFrom ?? null,
                                    yearTo: filters.yearTo ?? null }, plan.plan, plan.directory?.entries ?? [],
                                  plan.capabilities.limits);
    if (addition.kind === 'refused') { setMessage(REFUSAL_COPY[addition.reason]); return; }
    setBusy(true); setMessage('');
    try {
      const answer = addition.kind === 'create'
        ? await client.createPlan(conversationId, addition.edit)
        : await client.revisePlan(plan.plan!.id, addition.head, addition.edit);
      if (parseWorkScopeMutation(answer) === undefined) throw new Error('unreadable');
      setSelected(new Set());
      setMessage(`Added to the Mapping Plan${addition.unmapped.length ? ` (left out: ${addition.unmapped.length} `
        + 'without a verified register spelling)' : ''}. Prepare it from the Mapping Plan; nothing was started here.`);
      onPlanChanged?.();
    } catch (error) {
      // A stale head or a plan opened elsewhere (409) is answered by reading
      // the plan again: the next attempt is made against the current head.
      setMessage(safeErrorText(error, PLAN_FALLBACK));
    } finally {
      setBusy(false);
      await loadPlan();
    }
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
              <Listing listing={shown.listing} path={path} onOpen={(where) => go(where)} canPlan={canPlan}
                entries={plan.directory?.entries ?? []} selected={selected} busy={busy}
                onToggle={(tozar) => setSelected((current) => {
                  const next = new Set(current);
                  if (next.has(tozar)) next.delete(tozar); else next.add(tozar);
                  return next;
                })} />
              <Pager page={shown.listing.page} onPage={(at) => go(path, at)} offset={offset} />
            </>
          )}
          {canPlan && (
            <div className="button-row">
              <button type="button" className="button button--primary"
                disabled={busy || selected.size === 0 || otherFilters} onClick={() => void add()}>
                {busy ? 'Adding…' : `Add to plan (${selected.size})`}</button>
              {selected.size > 0 && (
                <span>Selected: {[...selected].map(safeText).join(', ')}</span>)}
              <span className="muted">
                {otherFilters
                  ? 'A plan selects whole manufacturers by model year: clear the segment, fuel and body filters to add.'
                  : `Year range: ${years}. Variants the catalog already enriched are left out when the plan is `
                    + 'prepared.'}
              </span>
            </div>)}
          {message && <p className="muted" role="status">{safeText(message)}</p>}
        </div>
      )}
    </section>
  );
}

const MIN_YEAR = 1900;
const MAX_YEAR = 2100;

function yearValue(value: string): number | undefined {
  const year = /^[0-9]{4}$/.test(value.trim()) ? Number(value.trim()) : undefined;
  return year !== undefined && year >= MIN_YEAR && year <= MAX_YEAR ? year : undefined;
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
      {invalid && <span className="alert" role="alert">Years are {MIN_YEAR}–{MAX_YEAR}, the first not after the last.</span>}
    </form>
  );
}

function Pager({ page, offset, onPage }: { page: Page<unknown>; offset: number; onPage: (at: number) => void }) {
  if (page.total <= page.limit) return null;
  const last = Math.min(offset + page.items.length, page.total);
  return (
    <div className="button-row" role="group" aria-label="Pages">
      <button type="button" className="button button--quiet" disabled={offset === 0}
        onClick={() => onPage(Math.max(0, offset - page.limit))}>Previous</button>
      <span className="muted">{offset + 1}–{last} of {page.total}</span>
      <button type="button" className="button button--quiet" disabled={last >= page.total}
        onClick={() => onPage(offset + page.limit)}>Next</button>
    </div>
  );
}

const NOT_STATED = '(not stated)';

type ListingProps = {
  listing: Listing; path: Path; onOpen: (where: Path) => void; canPlan: boolean;
  entries: WorkScopeDirectory['entries']; selected: ReadonlySet<string>; busy: boolean;
  onToggle: (tozar: string) => void;
};

function Listing({ listing, path, onOpen, canPlan, entries, selected, busy, onToggle }: ListingProps) {
  if (listing.page.items.length === 0) {
    return <p className="muted">No catalog variants match. Built snapshots appear here once their build completes.</p>;
  }
  switch (listing.level) {
    case 'manufacturers':
      return (
        <ul className="catalog-list">{listing.page.items.map((item) => {
          const mapped = directoryKeyFor(item.tozar, entries) !== undefined;
          return (
            <li key={item.tozar}>
              {canPlan && (
                <input type="checkbox" aria-label={`Select ${item.tozar}`} disabled={!mapped || busy}
                  checked={selected.has(item.tozar)} onChange={() => onToggle(item.tozar)} />)}
              <button type="button" className="button button--quiet" onClick={() => onOpen({ tozar: item.tozar })}>
                {safeText(item.tozar)}</button>
              {item.canonical && <span className="note"> ({safeText(item.canonical)})</span>}
              {' '}<span className="muted">{item.variants} variants</span>
            </li>);
        })}</ul>);
    case 'models':
      return (
        <>
          <p className="note">A Mapping Plan selects whole manufacturers by model year; a model is browsed here but
            cannot be added to a plan on its own.</p>
          <ul className="catalog-list">{listing.page.items.map((item) => (
            <li key={item.kinuyMishari ?? NOT_STATED}>
              {item.kinuyMishari === null ? <span>{NOT_STATED}</span> : (
                <button type="button" className="button button--quiet"
                  onClick={() => onOpen({ tozar: path.tozar, model: item.kinuyMishari! })}>
                  {safeText(item.kinuyMishari)}</button>)}
              {' '}<span className="muted">{item.variants} variants, {item.yearMin ?? '—'}–{item.yearMax ?? '—'}</span></li>))}
          </ul>
        </>);
    case 'years':
      return <ul className="catalog-list">{listing.page.items.map((item) => (
        <li key={item.year ?? NOT_STATED}>
          {item.year === null ? <span>{NOT_STATED}</span> : (
            <button type="button" className="button button--quiet"
              onClick={() => onOpen({ ...path, year: item.year! })}>{item.year}</button>)}
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
        <p role="group" aria-label="Coverage">{COVERAGE_LEVELS.map((level) => {
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
