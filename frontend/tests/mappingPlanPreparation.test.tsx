/**
 * E': preparing ONE plan revision from the website, and the include_unresolved
 * toggle (E'-5).
 *
 * What is asserted: the status is parsed STRICTLY (an unknown state, a
 * failure without a code, a self-contradicting answer is unreadable, never
 * rendered); the Prepare button follows the server's `canPrepare` and nothing
 * else; a prepared revision shows PR-Z's per-unit counts; the toggle writes
 * `include_unresolved: true` into the next revision only when on, so an edit
 * with it off is byte for byte what it was before; and the gateway proxies the
 * status as a read and the Prepare as an execution route that is never a run
 * start.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MappingPlanPreparation } from '../components/scope/MappingPlanPreparation';
import { MappingPlanPanel } from '../components/scope/MappingPlanPanel';
import {
  draftEdit, draftFromPlan, draftMatchesPlan, emptyDraft, parseCapabilities, parsePreparation,
  parseWorkScopeState, preparationReasonText,
} from '../lib/workScope';
import { isGatewayRequestAllowed, isRunCreationRequest } from '../lib/server/gatewayPolicy';
import { CAPABILITIES, DIGEST, PLAN, stateBody } from './fixtures/workScope';

function preparationBody(overrides: Record<string, unknown> = {}) {
  return {
    work_scope_id: PLAN, revision: 1, digest: DIGEST, state: 'not_requested', reason_code: null,
    attempt: 0, can_prepare: true, blocked_by: null, preparation: null, units: [],
    known_unresolved: null, ...overrides,
  };
}

const PREPARED_UNIT = { unit_key: 'toyota', name: 'Toyota', state: 'prepared', queued_count: 5,
                        coverage: { enriched: 8, ambiguous: 4, pending: 0, queued: 5 } };

const PREPARED = preparationBody({
  state: 'prepared', attempt: 1, can_prepare: false, blocked_by: 'prepared',
  preparation: { unit_count: 1, prepared_unit_count: 1, queued_item_count: 5, batch_count: 1,
                 prepared_at: '2026-09-27T10:00:00+00:00' },
  units: [PREPARED_UNIT],
  known_unresolved: { revision: 1, count: 4 },
});

describe('the preparation status is parsed strictly', () => {
  it('reads every state the server derives', () => {
    expect(parsePreparation(preparationBody())?.state).toBe('not_requested');
    expect(parsePreparation(preparationBody({ state: 'preparing', can_prepare: false,
      blocked_by: 'in_flight', attempt: 1 }))?.blockedBy).toBe('in_flight');
    const prepared = parsePreparation(PREPARED)!;
    expect(prepared.figures?.queuedItemCount).toBe(5);
    expect(prepared.units[0].coverage).toEqual({ enriched: 8, ambiguous: 4, pending: 0, queued: 5 });
    expect(prepared.knownUnresolved).toEqual({ revision: 1, count: 4 });
    const failed = parsePreparation(preparationBody({ state: 'failed', attempt: 2,
      reason_code: 'GOV_TRANSPORT_FAILED' }))!;
    expect(failed.reasonCode).toBe('GOV_TRANSPORT_FAILED');
    expect(parsePreparation(preparationBody({ state: 'stale', can_prepare: false,
      blocked_by: 'stale' }))?.state).toBe('stale');
  });

  it.each([
    ['an unknown state', { state: 'queued' }],
    ['a failure without a code', { state: 'failed', reason_code: null }],
    ['a code on a state that is not a failure', { reason_code: 'PREPARATION_FAILED' }],
    ['a reason that is not a static code', { state: 'failed', reason_code: 'select * from runs' }],
    ['Prepare offered beside a blocker', { can_prepare: true, blocked_by: 'in_flight' }],
    ['an unknown blocker', { can_prepare: false, blocked_by: 'whatever' }],
    ['a missing can_prepare', { can_prepare: undefined }],
    ['a can_prepare that is not a boolean', { can_prepare: 'true' }],
    ['a prepared state without figures', { state: 'prepared', can_prepare: false, blocked_by: 'prepared' }],
    ['figures on a state that is not prepared', { preparation: PREPARED.preparation }],
    ['a malformed unit', { ...PREPARED, units: [{ unit_key: 'toyota', name: 'Toyota', state: 'prepared' }] }],
    ['a unit count that is negative', { ...PREPARED, units: [{ ...PREPARED_UNIT, queued_count: -1 }] }],
    ['coverage missing a count', { ...PREPARED, units: [{ ...PREPARED_UNIT, coverage: { enriched: 1 } }] }],
    ['a bad digest', { digest: 'x' }],
    ['a revision of zero', { revision: 0 }],
    ['units that are not a list', { units: null }],
    ['a known-unresolved count that is not a count', { known_unresolved: { revision: 1, count: 'four' } }],
  ])('refuses %s', (_label, overrides) => {
    expect(parsePreparation(preparationBody(overrides as Record<string, unknown>))).toBeUndefined();
  });

  it('authors the copy for a failure and shows an unknown static code as itself', () => {
    expect(preparationReasonText('PREPARATION_NOT_STARTED')).toContain('never started');
    expect(preparationReasonText('CAPTURE_SOMETHING_NEW')).toContain('CAPTURE_SOMETHING_NEW');
    expect(preparationReasonText(undefined)).toBe('');
  });

  it('never unlocks Prepare on the server capability without an explicit true', () => {
    expect(parseCapabilities({ ...CAPABILITIES, can_prepare: true })?.canPrepare).toBe(true);
    expect(parseCapabilities({ ...CAPABILITIES, can_prepare: 'true' })?.canPrepare).toBe(false);
    expect(parseCapabilities({ ...CAPABILITIES, can_prepare: undefined })?.canPrepare).toBe(false);
  });
});

describe('the Prepare button follows the server', () => {
  const noop = () => {};

  function renderPreparation(body: unknown, props: Partial<Parameters<typeof MappingPlanPreparation>[0]> = {}) {
    const onPrepare = vi.fn();
    render(<MappingPlanPreparation serverCanPrepare revision={1} preparation={parsePreparation(body)}
      loading={false} busy={false} error="" unitName={(key) => key.toUpperCase()}
      onPrepare={onPrepare} onRefresh={noop} {...props} />);
    return onPrepare;
  }

  it('is enabled only when the server says can_prepare, and sends one request', () => {
    const onPrepare = renderPreparation(preparationBody());
    const button = screen.getByRole('button', { name: 'Prepare this revision' });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(onPrepare).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['in flight', preparationBody({ state: 'preparing', can_prepare: false, blocked_by: 'in_flight', attempt: 1 })],
    ['stale', preparationBody({ state: 'stale', can_prepare: false, blocked_by: 'stale' })],
    ['needing an operator', preparationBody({ state: 'failed', reason_code: 'PREPARATION_INTERRUPTED',
      can_prepare: false, blocked_by: 'needs_operator', attempt: 1 })],
    ['prepared', PREPARED],
  ])('is disabled when the revision is %s', (_label, body) => {
    renderPreparation(body);
    for (const button of screen.getAllByRole('button').filter((item) => item.textContent?.startsWith('Prepare'))) {
      expect(button).toBeDisabled();
    }
  });

  it('is disabled while unread, busy, for another revision, or when the server cannot prepare', () => {
    const { unmount } = render(<MappingPlanPreparation serverCanPrepare revision={1} preparation={undefined}
      loading busy={false} error="" unitName={(key) => key} onPrepare={noop} onRefresh={noop} />);
    expect(screen.getByRole('button', { name: 'Prepare this revision' })).toBeDisabled();
    unmount();
    renderPreparation(preparationBody(), { revision: 2 });
    expect(screen.getByRole('button', { name: 'Prepare this revision' })).toBeDisabled();
  });

  it('offers no Prepare at all when the server cannot prepare', () => {
    renderPreparation(preparationBody(), { serverCanPrepare: false });
    expect(screen.queryByRole('button', { name: /Prepare/ })).toBeNull();
  });

  it('shows the per-unit counts PR-Z recorded once prepared', () => {
    renderPreparation(PREPARED);
    expect(screen.getByRole('status', { name: 'Preparation status' }).textContent).toContain('Prepared');
    expect(screen.getByLabelText('Variant coverage').textContent)
      .toMatch(/8 enriched, 4 ambiguous,\s+0 pending, 5 queued/);
  });

  it('shows a failure as its static reason code, never raw text', () => {
    renderPreparation(preparationBody({ state: 'failed', attempt: 1, reason_code: 'GOV_TRANSPORT_FAILED' }));
    expect(screen.getByText('GOV_TRANSPORT_FAILED')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Prepare again' })).toBeEnabled();
  });
});

describe('the include_unresolved toggle (E\'-5)', () => {
  const limits = parseCapabilities(CAPABILITIES)!.limits;

  it('is off by default and keeps the edit exactly as before', () => {
    const draft = { ...emptyDraft(limits), units: ['toyota'], modelYearFrom: '2018' };
    expect(draft.includeUnresolved).toBe(false);
    const checked = draftEdit(draft, limits);
    expect('edit' in checked && checked.edit).toEqual({
      units: ['toyota'], model_year_from: 2018, model_year_to: null, max_items: 100, batch_size: 10,
    });
    expect(JSON.stringify('edit' in checked && checked.edit)).not.toContain('include_unresolved');
  });

  it('writes include_unresolved: true into the next revision only when on', () => {
    const draft = { ...emptyDraft(limits), units: ['toyota'], includeUnresolved: true };
    const checked = draftEdit(draft, limits);
    expect('edit' in checked && checked.edit.include_unresolved).toBe(true);
  });

  it('reads the flag back from the server and counts it as a change', () => {
    const on = parseWorkScopeState(stateBody({ plan: { ...stateBody().plan, include_unresolved: true } }))!;
    expect(on.plan.includeUnresolved).toBe(true);
    const off = parseWorkScopeState(stateBody())!;
    expect(off.plan.includeUnresolved).toBe(false);
    const draft = draftFromPlan(off.plan);
    expect(draftMatchesPlan(draft, off.plan)).toBe(true);
    expect(draftMatchesPlan({ ...draft, includeUnresolved: true }, off.plan)).toBe(false);
  });

  it('shows how many known-unresolved rows it would re-queue, from the server', () => {
    const state = parseWorkScopeState(stateBody())!;
    const onDraftChange = vi.fn();
    render(<MappingPlanPanel visible open onOpenChange={() => {}}
      capabilities={parseCapabilities({ ...CAPABILITIES, can_prepare: true })} state={state}
      loading={false} busy={false} error="" notes={[]} instruction="" onInstructionChange={() => {}}
      onSubmitInstruction={() => {}} draft={draftFromPlan(state.plan)} onDraftChange={onDraftChange}
      onSaveDraft={() => {}} onDiscardDraft={() => {}} onRetry={() => {}}
      preparation={{ preparation: parsePreparation(PREPARED), loading: false, busy: false, error: '',
                     onPrepare: () => {}, onRefresh: () => {} }} />);
    expect(screen.getByLabelText('Known-unresolved variants').textContent)
      .toContain('4 known-unresolved variants the preparation of revision 1 left out');
    const toggle = screen.getByLabelText(/Retry known-unresolved variants/);
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ includeUnresolved: true }));
  });
});

describe('the gateway', () => {
  const previous = { ...process.env };
  afterEach(() => {
    process.env = { ...previous };
  });

  it('proxies the status as a read in every posture', () => {
    delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    expect(isGatewayRequestAllowed('GET', `/work-scopes/${PLAN}/preparation`)).toBe(true);
    expect(isGatewayRequestAllowed('POST', `/work-scopes/${PLAN}/preparation`)).toBe(false);
    expect(isGatewayRequestAllowed('GET', '/work-scopes/not-a-uuid/preparation')).toBe(false);
  });

  it('proxies Prepare only with the execution routes open, and never as a run start', () => {
    delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    expect(isGatewayRequestAllowed('POST', `/work-scopes/${PLAN}/preparations`)).toBe(false);
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    delete process.env.GATEWAY_ALLOW_RUN_START_ROUTES;
    expect(isGatewayRequestAllowed('POST', `/work-scopes/${PLAN}/preparations`)).toBe(true);
    expect(isRunCreationRequest('POST', `/work-scopes/${PLAN}/preparations`)).toBe(false);
    expect(isGatewayRequestAllowed('GET', `/work-scopes/${PLAN}/preparations`)).toBe(false);
  });
});
