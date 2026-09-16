/**
 * CODE-2 — the catalog status as an operator actually meets it.
 *
 * Every view model here is produced by the shipped pipeline: the shipped
 * reducer folded over events shaped exactly like `PromotionAttempt.as_event()`,
 * then the shipped selector. There is no second projection in this file, so a
 * regression in the reducer or the selector fails these tests too.
 *
 * What is asserted is BEHAVIOUR, not markup. That the panel renders for a
 * Swarm V2 project is worth little on its own; what matters is that a payload
 * cannot make it render, that a refusal reads as an operational outcome rather
 * than a failure, that state is legible without colour, and that nothing a
 * payload carries reaches the DOM.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { RunInspector } from '../components/inspector/RunInspector';
import { CATALOG_REFUSAL_LABELS, MAX_CANONICAL_FIELDS,
         UNKNOWN_CATALOG_REFUSAL_LABEL } from '../lib/catalogStatus';
import { initialWorkspaceState, reconstructRun } from '../lib/runReducer';
import { buildSwarmRunViewModel } from '../lib/swarmViewModel';
import { Run, RunEvent, WorkspaceState } from '../lib/types';
import { ALL_SECRET_SENTINELS, SECRET_FRAGMENTS } from './secretSentinels';

const RUN_ID = 'a1b2c3d4-1111-4111-8111-000000000001';

let sequence = 0;
function event(eventType: string, payload: unknown = {}, overrides: Partial<RunEvent> = {}): RunEvent {
  sequence += 1;
  return {
    id: String(sequence),
    run_id: RUN_ID,
    event_type: eventType,
    message: eventType,
    payload,
    ...overrides,
  } as RunEvent;
}

function promoted(candidateKey: string, extra: Record<string, unknown> = {}): RunEvent {
  return event('catalog_variant_promoted', {
    candidate_key: candidateKey, promoted: true,
    canonical_key: `${candidateKey}::canonical`,
    promoted_fields: ['engine_capacity', 'fuel_type'], unsupported_fields: ['horsepower'],
    replayed: false, ...extra,
  });
}

function refused(candidateKey: string, reason: string): RunEvent {
  return event('catalog_promotion_refused',
               { candidate_key: candidateKey, promoted: false, reason });
}

/** Render the inspector over a real reconstructed run. */
function renderInspector(options: {
  events?: RunEvent[];
  workflowKey?: string;
  status?: string;
} = {}) {
  const run = { id: RUN_ID, conversation_id: 'ffffffff-1111-4111-8111-000000000001',
                status: options.status ?? 'completed' } as Run;
  const state: WorkspaceState = options.events
    ? reconstructRun(run, options.events)
    : { ...initialWorkspaceState, run };
  const swarm = buildSwarmRunViewModel({
    run, swarm: state.swarm, workflowKey: options.workflowKey ?? 'swarm_v2',
  });
  render(
    <RunInspector
      executionUi
      tab="Workflow"
      onTabChange={() => {}}
      agents={[]}
      state={state}
      swarm={swarm}
    />,
  );
  return state;
}

function catalogPanel(): HTMLElement | null {
  return screen.queryByRole('region', { name: /catalog status/i });
}

/**
 * The top-level "Latest refusal" summary element, and ONLY that element.
 *
 * Reading `panel.textContent` would let an older action still sitting in the
 * bounded history satisfy an assertion about the summary — exactly the
 * false positive that let the stale-reason defect through. This narrows to the
 * one element the summary renders into.
 */
function latestRefusalSummary(): string | null {
  const panel = catalogPanel();
  const node = panel?.querySelector('.catalog-status-reason') ?? null;
  return node === null ? null : (node.textContent ?? '');
}

// ---------------------------------------------------------------------------

describe('when the catalog surface renders', () => {
  it('renders for a Swarm V2 project once a catalog event is observed', () => {
    renderInspector({ events: [promoted('mazda_3_2021')] });
    expect(catalogPanel()).not.toBeNull();
  });

  it('does not render for a V1 project, whatever the events say', () => {
    renderInspector({ events: [promoted('mazda_3_2021')], workflowKey: 'vehicle_catalog_v1' });
    expect(catalogPanel()).toBeNull();
  });

  it('does not render for an unknown workflow key', () => {
    renderInspector({ events: [promoted('mazda_3_2021')], workflowKey: 'swarm_v3' });
    expect(catalogPanel()).toBeNull();
  });

  it('cannot be summoned by an event payload claiming a workflow', () => {
    /**
     * The trusted `workflow_key` comes from the PROJECT row. A payload naming
     * `swarm_v2` is a model-reachable field asserting a routing decision it
     * does not get to make.
     */
    renderInspector({
      workflowKey: 'vehicle_catalog_v1',
      events: [
        event('catalog_variant_promoted', {
          candidate_key: 'k', promoted: true, canonical_key: 'c',
          promoted_fields: [], unsupported_fields: [], replayed: false,
          workflow_key: 'swarm_v2', workflow: 'swarm_v2', isSwarmV2: true,
        }),
      ],
    });
    expect(catalogPanel()).toBeNull();
  });

  it('stays absent for a Swarm V2 run with no catalog event at all', () => {
    renderInspector({ events: [event('run_started'), event('task_completed', { task_id: 't1' })] });
    expect(catalogPanel()).toBeNull();
  });

  it('stays absent when only an unrecognised catalog-looking type arrived', () => {
    renderInspector({ events: [event('catalog_variant_promoted_v2', { candidate_key: 'k' })] });
    expect(catalogPanel()).toBeNull();
  });
});

describe('what the surface says', () => {
  it('states promotion and refusal counts in words and numbers', () => {
    renderInspector({
      events: [promoted('mazda_3_2021'),
               refused('kia_niro_2020', 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED')],
    });
    const counts = catalogPanel()!.querySelector('.catalog-status-counts')!;
    // Each count is legible as a WORD plus a number — never by badge colour.
    const pairs = [...counts.querySelectorAll('div')].map(
      (row) => [row.querySelector('dt')!.textContent, row.querySelector('dd')!.textContent]);
    expect(pairs).toEqual([['Promoted', '1'], ['Refused', '1'], ['Replayed', '0']]);
  });

  it('presents a refusal as an operational outcome, not a failed run', () => {
    renderInspector({
      events: [refused('kia_niro_2020', 'CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE')],
      status: 'completed',
    });
    const panel = catalogPanel()!;
    // The surface says so in words, and says WHY in allowlisted static text.
    expect(panel.textContent).toContain('not a failed run');
    expect(panel.textContent).toContain(
      CATALOG_REFUSAL_LABELS.CATALOG_PROMOTION_NO_VERIFIED_EVIDENCE);
    // And it never reaches for failure vocabulary of its own.
    for (const word of ['error', 'failure', 'failed to', 'crashed', 'exception']) {
      expect(panel.textContent!.toLowerCase(), word).not.toContain(word);
    }
  });

  it('renders a refusal code only through the closed allowlist', () => {
    renderInspector({
      events: [refused('k', 'ERROR: relation "catalog_canonical_variants" does not exist')],
    });
    const panel = catalogPanel()!;
    expect(panel.textContent).toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
    expect(panel.textContent).not.toContain('relation');
    expect(panel.textContent).not.toContain('ERROR');
  });

  it('marks a replayed promotion as a replay rather than a second write', () => {
    renderInspector({ events: [promoted('mazda_3_2021', { replayed: true })] });
    const panel = catalogPanel()!;
    expect(panel.textContent).toContain('replay');
    expect(within(panel).getByText('Replayed')).toBeTruthy();
  });

  it('reports field counts, never field names', () => {
    renderInspector({ events: [promoted('mazda_3_2021')] });
    const panel = catalogPanel()!;
    expect(panel.textContent).toContain('2 fields promoted');
    expect(panel.textContent).toContain('1 unsupported');
    expect(panel.textContent).not.toContain('engine_capacity');
    expect(panel.textContent).not.toContain('horsepower');
  });

  it('never renders a raw payload object', () => {
    renderInspector({
      events: [promoted('mazda_3_2021', {
        raw_row: { register_row: 'preserved' }, sql: 'select * from catalog_raw_records',
      })],
    });
    const panel = catalogPanel()!;
    for (const leak of ['raw_row', 'register_row', 'preserved', 'select *',
                        'catalog_raw_records', '[object Object]', '{"']) {
      expect(panel.textContent, leak).not.toContain(leak);
    }
  });
});

describe('the rendered "Latest refusal" summary', () => {
  const KNOWN = 'CATALOG_PROMOTION_VALUE_MISMATCH';
  const OTHER_KNOWN = 'CATALOG_PROMOTION_CONFLICT_UNRESOLVED';

  function refusedWith(payload: Record<string, unknown>): RunEvent {
    return event('catalog_promotion_refused', { candidate_key: 'k', promoted: false, ...payload });
  }

  it('shows the unknown fallback after known -> unknown, not the stale reason', () => {
    renderInspector({
      events: [refusedWith({ reason: KNOWN }),
               refusedWith({ reason: 'CATALOG_PROMOTION_INVENTED' })],
    });
    const summary = latestRefusalSummary();
    expect(summary).toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
    // The regression: the older known label must not be what the SUMMARY says.
    expect(summary).not.toContain(CATALOG_REFUSAL_LABELS[KNOWN]);
    // It is still legitimately present in the action history below it — which
    // is precisely why this test reads the summary element and not the panel.
    expect(catalogPanel()!.textContent).toContain(CATALOG_REFUSAL_LABELS[KNOWN]);
  });

  it('shows the unknown fallback after known -> missing reason', () => {
    renderInspector({ events: [refusedWith({ reason: KNOWN }), refusedWith({})] });
    const summary = latestRefusalSummary();
    expect(summary).toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
    expect(summary).not.toContain(CATALOG_REFUSAL_LABELS[KNOWN]);
  });

  it('shows the unknown fallback after known -> malformed reason type', () => {
    renderInspector({ events: [refusedWith({ reason: KNOWN }), refusedWith({ reason: 42 })] });
    const summary = latestRefusalSummary();
    expect(summary).toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
    expect(summary).not.toContain(CATALOG_REFUSAL_LABELS[KNOWN]);
  });

  it('shows the new static label after unknown -> known', () => {
    renderInspector({
      events: [refusedWith({ reason: 'CATALOG_PROMOTION_INVENTED' }),
               refusedWith({ reason: OTHER_KNOWN })],
    });
    const summary = latestRefusalSummary();
    expect(summary).toContain(CATALOG_REFUSAL_LABELS[OTHER_KNOWN]);
    expect(summary).not.toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
  });

  it('keeps the refusal summary after a later promotion', () => {
    renderInspector({ events: [refusedWith({ reason: KNOWN }), promoted('mazda_3_2021')] });
    expect(latestRefusalSummary()).toContain(CATALOG_REFUSAL_LABELS[KNOWN]);
  });

  it('omits the summary entirely when no refusal was observed', () => {
    renderInspector({ events: [promoted('mazda_3_2021')] });
    expect(catalogPanel()).not.toBeNull();
    expect(latestRefusalSummary()).toBeNull();
    expect(catalogPanel()!.textContent).not.toContain('Latest refusal');
  });

  it('never renders an untrusted reason string in the summary', () => {
    renderInspector({
      events: [refusedWith({ reason: KNOWN }),
               refusedWith({ reason: 'ERROR: relation "catalog_canonical_variants" does not exist' })],
    });
    const panel = catalogPanel()!.textContent!;
    expect(panel).not.toContain('relation');
    expect(panel).not.toContain('does not exist');
    expect(latestRefusalSummary()).toContain(UNKNOWN_CATALOG_REFUSAL_LABEL);
  });
});

describe('the rendered field counts stay honest', () => {
  it('reports real counts for a well-formed list', () => {
    renderInspector({ events: [promoted('mazda_3_2021')] });
    expect(catalogPanel()!.textContent).toContain('2 fields promoted');
    expect(catalogPanel()!.textContent).toContain('1 unsupported');
  });

  it('says the count is unavailable rather than claiming zero, for a malformed list', () => {
    renderInspector({
      events: [event('catalog_variant_promoted', {
        candidate_key: 'k', promoted: true, canonical_key: 'c',
        promoted_fields: ['model_year_start', { field: 'trim' }],
        unsupported_fields: 'not-an-array', replayed: false,
      })],
    });
    const panel = catalogPanel()!.textContent!;
    expect(panel).toContain('Field count unavailable');
    expect(panel).not.toContain('0 fields promoted');
    expect(panel).not.toContain('trim');
  });

  it('says the count is unavailable for an over-contract list', () => {
    const over = Array.from({ length: MAX_CANONICAL_FIELDS + 1 }, (_, i) => `field_${i}`);
    renderInspector({
      events: [event('catalog_variant_promoted', {
        candidate_key: 'k', promoted: true, canonical_key: 'c',
        promoted_fields: over, unsupported_fields: [], replayed: false,
      })],
    });
    const panel = catalogPanel()!.textContent!;
    expect(panel).toContain('Field count unavailable');
    expect(panel).not.toContain(String(MAX_CANONICAL_FIELDS + 1));
  });
});

describe('accessibility and resilience', () => {
  it('is a labelled region with a real heading, reachable without a pointer', () => {
    renderInspector({ events: [promoted('mazda_3_2021')] });
    const panel = catalogPanel()!;
    const heading = within(panel).getByRole('heading', { name: /catalog status/i });
    expect(heading).toBeTruthy();
    expect(panel.getAttribute('aria-labelledby')).toBe(heading.id);
    // Nothing here can trap focus: the panel introduces no interactive control.
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    expect(within(panel).queryAllByRole('textbox')).toHaveLength(0);
  });

  it('keeps every existing inspector surface intact beside it', () => {
    renderInspector({ events: [promoted('mazda_3_2021'), event('run_completed')] });
    expect(screen.getByText('Live event stream')).toBeTruthy();
    expect(screen.getByRole('tablist', { name: /inspector sections/i })).toBeTruthy();
  });

  it('leaves the raw event stream showing recognised and unrecognised events', () => {
    const state = renderInspector({
      events: [event('run_started'), promoted('mazda_3_2021'),
               refused('kia_niro_2020', 'CATALOG_PROMOTION_REFUSED'),
               event('some_future_type')],
    });
    expect(state.events).toHaveLength(4);
    const stream = screen.getByText('Live event stream').closest('section')!;
    for (const type of ['run_started', 'catalog_variant_promoted',
                        'catalog_promotion_refused', 'some_future_type']) {
      expect(stream.textContent, type).toContain(type);
    }
  });

  it('renders a bounded list however many catalog events arrive', () => {
    const events = Array.from({ length: 200 }, (_, i) => promoted(`candidate_${i}`));
    renderInspector({ events });
    const panel = catalogPanel()!;
    const items = within(panel).getAllByRole('listitem');
    expect(items.length).toBeLessThanOrEqual(20);
    // The counts still report every event that was folded.
    expect(panel.textContent).toContain('200');
  });

  it('survives a long candidate key without breaking the layout contract', () => {
    renderInspector({ events: [promoted('k'.repeat(5_000))] });
    const panel = catalogPanel()!;
    // Bounded in the projection, so the DOM cannot carry the full string.
    expect(panel.textContent!.length).toBeLessThan(1_500);
  });

  it('renders no secret sentinel placed in any payload string', () => {
    const events = ALL_SECRET_SENTINELS.map((sentinel) =>
      event('catalog_variant_promoted', {
        candidate_key: sentinel, promoted: true, canonical_key: sentinel,
        promoted_fields: [sentinel], unsupported_fields: [], replayed: false,
        reason: sentinel,
      }));
    renderInspector({ events });
    const rendered = document.body.textContent ?? '';
    for (const fragment of SECRET_FRAGMENTS) {
      expect(rendered, fragment).not.toContain(fragment);
    }
  });
});
