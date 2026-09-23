'use client';
import { useState } from 'react';
import { safeText } from '@/lib/sanitize';
import {
  DirectoryEntry,
  WORK_SCOPE_NOTE_COPY,
  WorkScopeCapabilities,
  WorkScopeDirectory,
  WorkScopeDraft,
  WorkScopeNote,
  WorkScopeState,
  addUnit,
  coverageLabel,
  draftEdit,
  draftMatchesPlan,
  moveUnit,
  preparableUnits,
  removeUnit,
} from '@/lib/workScope';
import { MappingPlanProgress, MappingPlanProgressProps } from './MappingPlanProgress';

export type MappingPlanPanelProps = {
  /** Whether the surface applies at all: execution UI on, a conversation, and
   *  the server's own capability read saying the plan is available. */
  visible: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  capabilities?: WorkScopeCapabilities;
  directory?: WorkScopeDirectory;
  /** `null` is "this conversation has no plan yet"; `undefined` is "not read". */
  state?: WorkScopeState | null;
  loading: boolean;
  busy: boolean;
  /** The caller's own authored sentence; never upstream error text. */
  error: string;
  /** What the server said about the LAST write, including "nothing changed". */
  notes: WorkScopeNote[];
  instruction: string;
  onInstructionChange: (value: string) => void;
  onSubmitInstruction: () => void;
  draft: WorkScopeDraft;
  onDraftChange: (draft: WorkScopeDraft) => void;
  onSaveDraft: () => void;
  onDiscardDraft: () => void;
  onRetry: () => void;
  /** The plan's batches, when this server lets a member start them. */
  batches?: Omit<MappingPlanProgressProps, 'unitName'>;
};

const PROBLEM_COPY = {
  units: 'Add at least one manufacturer to the plan.',
  years: 'The model years must be whole years in range, the first not after the last.',
  maxItems: 'The candidate limit must be a whole number within the server limit.',
  batchSize: 'Choose a batch size within the server limit.',
} as const;

/**
 * The Mapping Plan — what MILO intends to map, stated two ways that are ONE plan.
 *
 * A person can type what they want ("Map Toyota and Lexus, starting with
 * 2018+, up to 800 variants") or build it from the manufacturer directory. Both
 * go to the same server contract (`backend/catalog/scope/`), and what this
 * panel shows afterwards is the server's validated revision — never the words
 * or the clicks themselves. There is no browser-side reading of an instruction.
 *
 * An ordered list, not checkboxes: the order of the plan IS its priority
 * ("Toyota first, then Mazda"), and a set of ticks cannot say that. Each entry
 * is moved with a named button, so priority is changeable from the keyboard.
 *
 * It is a PLAN. Nothing in the plan form prepares Government data, starts a
 * batch, creates a run or spends anything. Once an operator has prepared the
 * plan's current revision, and only where the server's capability read says
 * batches may start, the Batches section shows the plan's progress and lets a
 * person start the NEXT batch -- one confirmed batch, one run, at a time
 * (`MappingPlanProgress`).
 *
 * Everything rendered comes from `lib/workScope.ts`'s parsers, through
 * `safeText`. Coverage is shown exactly as the server stated it: a marque whose
 * register spelling is not verified says its coverage is unknown, never zero.
 */
export function MappingPlanPanel({
  visible,
  open,
  onOpenChange,
  capabilities,
  directory,
  state,
  loading,
  busy,
  error,
  notes,
  instruction,
  onInstructionChange,
  onSubmitInstruction,
  draft,
  onDraftChange,
  onSaveDraft,
  onDiscardDraft,
  onRetry,
  batches,
}: MappingPlanPanelProps) {
  const [filter, setFilter] = useState('');
  if (!visible || capabilities === undefined) return null;

  const limits = capabilities.limits;
  const entries = new Map((directory?.entries ?? []).map((entry) => [entry.key, entry]));
  const dirty = !draftMatchesPlan(draft, state?.plan);
  const checked = draftEdit(draft, limits);
  const problem = 'problem' in checked ? checked.problem : undefined;
  const hasPlan = state !== undefined && state !== null;
  // Nothing is authored before the server has said whether this conversation
  // already has a plan (`state === undefined` is "not read yet"). An edit made
  // earlier would be silently replaced when that answer arrives -- or, if the
  // read failed, written over a plan nobody has seen.
  const locked = busy || state === undefined;
  const needle = filter.trim().toLowerCase();
  const matches = (directory?.entries ?? []).filter((entry) =>
    needle === ''
    || entry.name.toLowerCase().includes(needle)
    || (entry.nameHe ?? '').includes(filter.trim())
    || entry.key.includes(needle));

  function unitLabel(key: string): string {
    return entries.get(key)?.name ?? key;
  }

  // What preparation can capture for this plan, from the server's directory:
  // only a unit with a VERIFIED register spelling is captured and queued.
  const preparable = directory !== undefined ? preparableUnits(draft.units, directory) : undefined;
  const verifiedInDirectory = (directory?.entries ?? []).filter((entry) => entry.registerMarqueVerified);

  return (
    <section className="panel mapping-plan" aria-labelledby="mapping-plan-title">
      <header className="catalog-review-head">
        <div>
          <h3 className="panel-title" id="mapping-plan-title">Mapping plan</h3>
          <p className="eyebrow">What MILO plans to map — a draft plan</p>
        </div>
        <button
          type="button"
          className="disclosure"
          aria-expanded={open}
          aria-controls="mapping-plan-body"
          onClick={() => onOpenChange(!open)}
        >
          {open ? 'Hide' : 'Show'}
        </button>
      </header>

      <div id="mapping-plan-body">
        {!open ? null : (
          <div className="panel-body">
            <p className="note">
              {capabilities.canStartBatches
                ? 'Each batch starts only when you start it, one at a time, and runs as one paid run. Preparing a revision’s Government data is an operator step.'
                : 'Planning only: preparing Government data and starting batches are not available yet. Nothing here runs or spends anything.'}
            </p>
            {error && <p className="alert" role="alert">{safeText(error)}</p>}
            {loading && <p className="muted">Loading the mapping plan…</p>}
            {!loading && error && (
              <div className="button-row">
                <button type="button" className="button button--quiet" onClick={onRetry}>Reload plan</button>
              </div>
            )}

            {hasPlan && capabilities.canStartBatches && batches !== undefined && (
              <MappingPlanProgress {...batches} unitName={unitLabel} />
            )}

            <div className="field">
              <label className="field-label" htmlFor="mapping-plan-instruction">Tell MILO what to map</label>
              <textarea
                id="mapping-plan-instruction"
                value={instruction}
                maxLength={limits.maxInstructionChars}
                rows={2}
                disabled={locked}
                onChange={(event) => onInstructionChange(event.target.value)}
                placeholder="Map Toyota and Lexus, starting with 2018+, up to 800 variants."
              />
            </div>
            <div className="button-row">
              <button
                type="button"
                className="button button--primary"
                onClick={onSubmitInstruction}
                disabled={locked || dirty || instruction.trim() === ''}
              >
                {busy ? 'Working…' : hasPlan ? 'Update plan' : 'Create plan'}
              </button>
            </div>
            {dirty && (
              <p className="muted">Save or discard your edits below before sending an instruction.</p>
            )}

            {notes.length > 0 && (
              <ul className="mapping-plan-notes" aria-label="How the plan was read">
                {notes.map((note, index) => (
                  <li key={`${note.code}-${index}`}>
                    {WORK_SCOPE_NOTE_COPY[note.code]}
                    {note.units.length > 0 && <> {note.units.map((key) => safeText(unitLabel(key))).join(', ')}</>}
                    {note.terms.length > 0 && <> {note.terms.map((term) => `“${safeText(term)}”`).join(', ')}</>}
                  </li>
                ))}
              </ul>
            )}

            <h4 className="section-title">Plan</h4>
            {hasPlan ? (
              <p className="muted">
                Revision {state.revision} · fingerprint <span className="identifier">{state.digest.slice(0, 12)}</span>
                {!state.current && ' · made against an older manufacturer directory; save it again to renew it'}
              </p>
            ) : (
              <p className="muted">This conversation has no mapping plan yet.</p>
            )}

            {draft.units.length === 0 ? (
              <p className="muted">No manufacturers in the plan. Add them from the directory below, or describe them above.</p>
            ) : (
              <ol className="mapping-plan-units" aria-label="Manufacturers in priority order">
                {draft.units.map((key, index) => {
                  const entry = entries.get(key);
                  const name = unitLabel(key);
                  return (
                    <li key={key} className="mapping-plan-unit">
                      <span className="mapping-plan-unit-name">
                        {index + 1}. {safeText(name)}
                        {entry?.nameHe && <span className="muted" lang="he" dir="rtl"> {safeText(entry.nameHe)}</span>}
                      </span>
                      {entry && <span className="note">{safeText(coverageLabel(entry))}</span>}
                      {entry && !entry.registerMarqueVerified && (
                        <span className="note">Register spelling not verified — cannot be prepared until it is.</span>
                      )}
                      {!entry && <span className="note">Not in the current manufacturer directory.</span>}
                      <span className="button-row">
                        <button type="button" className="button button--quiet" aria-label={`Move ${name} up`}
                          disabled={locked || index === 0} onClick={() => onDraftChange(moveUnit(draft, key, -1))}>↑</button>
                        <button type="button" className="button button--quiet" aria-label={`Move ${name} down`}
                          disabled={locked || index === draft.units.length - 1} onClick={() => onDraftChange(moveUnit(draft, key, 1))}>↓</button>
                        <button type="button" className="button button--quiet" aria-label={`Remove ${name}`}
                          disabled={locked} onClick={() => onDraftChange(removeUnit(draft, key))}>Remove</button>
                      </span>
                    </li>
                  );
                })}
              </ol>
            )}

            {preparable !== undefined && draft.units.length > 0 && (
              <div className="note" role="note" aria-label="What can be prepared">
                {preparable.verified.length === 0 ? (
                  <p>
                    None of these manufacturers has a verified Government-register spelling, so preparing this plan
                    would queue nothing and no batch could run.
                  </p>
                ) : (
                  <p>Can be prepared from the Government register: {preparable.verified.map((key) => safeText(unitLabel(key))).join(', ')}.</p>
                )}
                {preparable.unverified.length > 0 && (
                  <p>
                    Not preparable until their register spelling is verified (recorded as register-unverified; nothing
                    is queued for them): {preparable.unverified.map((key) => safeText(unitLabel(key))).join(', ')}.
                  </p>
                )}
              </div>
            )}

            <div className="mapping-plan-fields">
              <div className="field">
                <label className="field-label" htmlFor="mapping-plan-year-from">From model year (optional)</label>
                <input id="mapping-plan-year-from" inputMode="numeric" value={draft.modelYearFrom}
                  disabled={locked}
                  onChange={(event) => onDraftChange({ ...draft, modelYearFrom: event.target.value })} />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="mapping-plan-year-to">To model year (optional)</label>
                <input id="mapping-plan-year-to" inputMode="numeric" value={draft.modelYearTo}
                  disabled={locked}
                  onChange={(event) => onDraftChange({ ...draft, modelYearTo: event.target.value })} />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="mapping-plan-max-items">Candidate limit (at most {limits.maxItems})</label>
                <input id="mapping-plan-max-items" inputMode="numeric" value={draft.maxItems}
                  disabled={locked}
                  onChange={(event) => onDraftChange({ ...draft, maxItems: event.target.value })} />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="mapping-plan-batch-size">Candidates per batch (at most {limits.maxBatchSize})</label>
                <select id="mapping-plan-batch-size" value={draft.batchSize} disabled={locked}
                  onChange={(event) => onDraftChange({ ...draft, batchSize: Number(event.target.value) })}>
                  {Array.from({ length: limits.maxBatchSize }, (_, index) => index + 1).map((size) => (
                    <option key={size} value={size}>{size}{size === limits.defaultBatchSize ? ' (default)' : ''}</option>
                  ))}
                </select>
              </div>
            </div>
            {dirty && problem && <p className="muted">{PROBLEM_COPY[problem]}</p>}
            <div className="button-row">
              <button type="button" className="button button--primary" onClick={onSaveDraft}
                disabled={locked || !dirty || problem !== undefined}>
                {hasPlan ? 'Save plan' : 'Create plan from these choices'}
              </button>
              <button type="button" className="button button--quiet" onClick={onDiscardDraft} disabled={locked || !dirty}>
                Discard changes
              </button>
            </div>

            <h4 className="section-title">Manufacturer directory</h4>
            {directory === undefined ? (
              <p className="muted">The manufacturer directory is not available.</p>
            ) : (
              <>
                <p className="note">
                  {directory.coverageAvailable && directory.catalogVariants !== null
                    ? `The canonical catalog holds ${directory.catalogVariants} variant${directory.catalogVariants === 1 ? '' : 's'}${directory.attributedVariants !== null && directory.attributedVariants !== directory.catalogVariants ? `, ${directory.attributedVariants} of them under a verified register spelling` : ''}.`
                    : 'Catalog coverage is unavailable right now; no count is shown rather than a guessed one.'}
                </p>
                <p className="note">
                  {verifiedInDirectory.length === 0
                    ? 'No manufacturer in the directory has a verified Government-register spelling yet, so no plan can be prepared.'
                    : safeText(`Verified Government-register spelling: ${verifiedInDirectory.length} of ${directory.entries.length} manufacturers (${verifiedInDirectory.map((entry) => entry.name).join(', ')}). Only those can be prepared and run; the others can be planned but are not captured.`)}
                </p>
                <div className="field">
                  <label className="field-label" htmlFor="mapping-plan-filter">Find a manufacturer</label>
                  <input id="mapping-plan-filter" type="search" value={filter} onChange={(event) => setFilter(event.target.value)} />
                </div>
                <ul className="mapping-plan-directory" aria-label="Manufacturer directory">
                  {matches.map((entry: DirectoryEntry) => {
                    const inPlan = draft.units.includes(entry.key);
                    return (
                      <li key={entry.key} className="mapping-plan-entry">
                        <span>
                          {safeText(entry.name)}
                          {entry.nameHe && <span className="muted" lang="he" dir="rtl"> {safeText(entry.nameHe)}</span>}
                          <span className="note"> · {safeText(directory.origins.get(entry.origin) ?? entry.origin)}</span>
                        </span>
                        <span className="note">{safeText(coverageLabel(entry))}</span>
                        <button type="button" className="button button--quiet" aria-label={`Add ${entry.name}`}
                          disabled={locked || inPlan || draft.units.length >= limits.maxUnits}
                          onClick={() => onDraftChange(addUnit(draft, entry.key))}>
                          {inPlan ? 'In plan' : 'Add'}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}

            {hasPlan && state.history.length > 0 && (
              <>
                <h4 className="section-title">Revisions</h4>
                <ol className="mapping-plan-history" aria-label="Plan revisions, newest first">
                  {[state.head, ...state.history].map((revision) => (
                    <li key={revision.revision}>
                      <span className="identifier">#{revision.revision}</span>{' '}
                      {revision.inputKind === 'instruction' && revision.instruction
                        ? <>You asked: “{safeText(revision.instruction)}”</>
                        : 'Edited in the plan'}
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
