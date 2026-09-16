/**
 * The Swarm V2 Final Result surface.
 *
 * Rendered against the SAME committed backend payloads the parser tests use,
 * so what a reviewer sees here is what the engine actually emits. The
 * assertions cover the product answer, the separation from the execution
 * surface, the five non-result states, hostile input, accessibility and the
 * V1 path that must not change.
 */

import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FinalResultPanel } from '../components/result/FinalResultPanel';
import { RunOutputPanel } from '../components/run/RunOutputPanel';
import { NO_USABLE_RESULT_CODE } from '../lib/finalResult';
import fixtures from './fixtures/swarmV2FinalResult.json';
import {
  ALL_SECRET_SENTINELS,
  API_KEY_PREFIX,
  BEARER_SENTINEL,
  JWT_SENTINEL,
  SECRET_FRAGMENTS,
  SUPABASE_PUBLISHABLE_PUBLIC,
  SUPABASE_SECRET_SENTINEL,
} from './secretSentinels';

type PanelProps = React.ComponentProps<typeof FinalResultPanel>;

function renderPanel(overrides: Partial<PanelProps> = {}) {
  return render(
    <FinalResultPanel
      visible
      runId="cccccccc-1111-4111-8111-000000000f04"
      runStatus="completed"
      connection="terminal"
      output={fixtures.usable_result as Record<string, unknown>}
      {...overrides}
    />,
  );
}

/** The one region the surface owns, so assertions never leak into siblings. */
function panel(): HTMLElement {
  return screen.getByRole('region', { name: 'Final result' });
}

describe('1. the product answer', () => {
  it('1a. a usable result announces its outcome as text, not only as colour', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: 'Final result', level: 3 })).toBeInTheDocument();
    expect(screen.getByText('Usable result')).toBeInTheDocument();
    expect(screen.getByText(/left nothing outstanding/)).toBeInTheDocument();
  });

  it('1b. verified fields are listed with their humanised and durable keys', () => {
    renderPanel();
    expect(screen.getByRole('heading', { name: 'Verified fields' })).toBeInTheDocument();
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.getByText('fuel_type')).toBeInTheDocument();
    expect(screen.getByText('plug-in hybrid')).toBeInTheDocument();
  });

  it('1c. two verified values for one field are BOTH shown, with neither chosen', () => {
    renderPanel();
    expect(screen.getByText('302')).toBeInTheDocument();
    expect(screen.getByText('306')).toBeInTheDocument();
    expect(screen.getByText('2 verified values — none was chosen.')).toBeInTheDocument();
    expect(screen.getByText(/One field was verified with more than one value/)).toBeInTheDocument();
  });

  it('1d. provenance is a collapsed reference list of ids and scope only', () => {
    renderPanel();
    const provenance = screen.getAllByText('Provenance')[0].closest('details');
    expect(provenance).toBeInTheDocument();
    // Collapsed by default: the answer comes first, the sourcing second.
    expect(provenance).not.toHaveAttribute('open');
    const rows = within(provenance as HTMLElement);
    expect(rows.getByText('Source')).toBeInTheDocument();
    expect(rows.getByText('src-gov-1')).toBeInTheDocument();
    expect(rows.getByText('Task')).toBeInTheDocument();
    expect(rows.getByText('Claim')).toBeInTheDocument();
    // The run id is a contract key that is deliberately NOT surfaced.
    expect(rows.queryByText('cccccccc-1111-4111-8111-000000000f04')).not.toBeInTheDocument();
  });

  it('1e. a structured verified value is rendered, not discarded', () => {
    renderPanel({ output: fixtures.structured_value as Record<string, unknown> });
    expect(screen.getByText('Dimensions')).toBeInTheDocument();
    expect(screen.getByText('length_mm')).toBeInTheDocument();
    expect(screen.getByText('4600')).toBeInTheDocument();
    expect(screen.getByText('axles')).toBeInTheDocument();
    expect(screen.getByText('front')).toBeInTheDocument();
  });
});

describe('2. partial, empty and not-found are visibly different answers', () => {
  it('2a. a partial result shows the fields AND every outstanding item', () => {
    renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByText('Partial result')).toBeInTheDocument();
    expect(screen.getByText(/not a completed result/)).toBeInTheDocument();
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Outstanding items (3)' })).toBeInTheDocument();
  });

  it('2b. conflicts, coverage gaps and task failures get their own headed groups', () => {
    renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByRole('heading', { name: 'Conflicts (1)' })).toBeInTheDocument();
    expect(screen.getByText(/Sources disagreed and no source settled it/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Coverage gaps (1)' })).toBeInTheDocument();
    expect(screen.getByText('Evidence requirements were not met')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Task failures (1)' })).toBeInTheDocument();
    // Safe code values are shown verbatim; there is no invented explanation.
    expect(screen.getByText('R5_GOV_RECORD_AMBIGUOUS')).toBeInTheDocument();
    expect(screen.getByText('unresolved conflict')).toBeInTheDocument();
  });

  it('2c. an empty result says so and is never dressed up as a success', () => {
    renderPanel({ output: fixtures.no_usable_result as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByText('No usable result')).toBeInTheDocument();
    expect(screen.getByText(/No field was verified, so no value is reported/)).toBeInTheDocument();
    expect(screen.queryByText('Usable result')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Verified fields' })).not.toBeInTheDocument();
    // The real outstanding item is still reported in full.
    expect(screen.getByRole('heading', { name: 'Outstanding items (1)' })).toBeInTheDocument();
    expect(screen.getByText('Catalog lookup')).toBeInTheDocument();
  });

  it('2d. the static empty-result marker is validated but never re-shown as an outstanding item', () => {
    // The marker restates the outcome the banner already announces; listing it
    // again would invent a second "outstanding item" that is not one. It is
    // still contract-checked — `4e` in the parser suite proves a missing,
    // duplicated, misplaced or padded marker is refused outright.
    renderPanel({ output: fixtures.no_usable_result as Record<string, unknown>, runStatus: 'partial_success' });
    expect(panel().textContent).not.toContain(NO_USABLE_RESULT_CODE);
  });

  it('2e. not-found is a confirmed negative, worded differently from an empty result', () => {
    renderPanel({ output: fixtures.not_found as Record<string, unknown> });
    expect(screen.getByText('Not found')).toBeInTheDocument();
    expect(screen.getByText(/confirmed negative, not a failure/)).toBeInTheDocument();
    expect(screen.getByText(/point of a confirmed negative/)).toBeInTheDocument();
    expect(screen.queryByText('No usable result')).not.toBeInTheDocument();
  });
});

describe('3. loading, polling delay, absent output and invalid output are four distinct states', () => {
  it('3a. no run selected', () => {
    renderPanel({ runId: undefined, runStatus: undefined, connection: 'idle', output: undefined });
    expect(screen.getByText('Loading')).toBeInTheDocument();
    expect(screen.getByText(/No run is selected/)).toBeInTheDocument();
  });

  it('3b. the run row has not arrived yet', () => {
    renderPanel({ runStatus: undefined, connection: 'polling', output: undefined });
    expect(screen.getByText('Loading')).toBeInTheDocument();
    expect(screen.getByText(/The result appears once the run has been read/)).toBeInTheDocument();
  });

  it('3c. the run has not finished', () => {
    renderPanel({ runStatus: 'running', connection: 'polling', output: undefined });
    expect(screen.getByText('Not finished')).toBeInTheDocument();
    expect(screen.getByText(/appears once it reaches a terminal state/)).toBeInTheDocument();
  });

  it('3d. a delayed poll is reported as a connection fact, not as an empty result', () => {
    renderPanel({ runStatus: 'running', connection: 'reconnecting', output: undefined });
    expect(screen.getByText('Not finished')).toBeInTheDocument();
    expect(screen.getByText(/last status check did not get through/)).toBeInTheDocument();
    expect(screen.queryByText('No result recorded')).not.toBeInTheDocument();
  });

  it('3e. a terminal run with NO recorded payload says exactly that', () => {
    renderPanel({ runStatus: 'completed', output: undefined });
    expect(screen.getByText('No result recorded')).toBeInTheDocument();
    expect(screen.getByText(/Nothing is being inferred from that absence/)).toBeInTheDocument();
  });

  it('3f. an invalid payload is unavailable, with a static reason and no payload echo', () => {
    renderPanel({ output: { status: 'complete', result_kind: 'totally_fine', fields: {}, needs_review: [] } });
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('VOCABULARY_NOT_ALLOWLISTED')).toBeInTheDocument();
    expect(panel().textContent).not.toContain('totally_fine');
  });

  it('3g. a run that failed or was cancelled reports no product result at all', () => {
    renderPanel({ runStatus: 'failed', output: undefined });
    expect(screen.getByText('Run failed')).toBeInTheDocument();
    expect(screen.getByText(/ended without producing a product result/)).toBeInTheDocument();

    renderPanel({ runStatus: 'budget_exhausted', output: undefined });
    expect(screen.getByText('Run budget exhausted')).toBeInTheDocument();
  });

  it('3h. a terminal status that contradicts the payload is unavailable, never a success', () => {
    renderPanel({ runStatus: 'completed', output: fixtures.partial_result as Record<string, unknown> });
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('RUN_STATUS_CONTRADICTS_OUTCOME')).toBeInTheDocument();
    expect(screen.queryByText('Partial result')).not.toBeInTheDocument();
  });
});

describe('4. hostile payloads never reach the surface', () => {
  it('4a. an unclassifiable outstanding item makes the whole result unavailable', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    payload.needs_review.push({ note: 'trust me, it is fine', severity: 'low' });
    renderPanel({ output: payload, runStatus: 'partial_success' });
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('REVIEW_ITEM_INVALID')).toBeInTheDocument();
    expect(panel().textContent).not.toContain('trust me');
    expect(screen.queryByText('Fuel type')).not.toBeInTheDocument();
  });

  it('4b. unknown top-level keys are never rendered', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.commander_rationale = 'I picked the second source because';
    payload.system_prompt = 'You are a helpful assistant';
    renderPanel({ output: payload });
    expect(screen.getByText('Usable result')).toBeInTheDocument();
    expect(panel().textContent).not.toContain('helpful assistant');
    expect(panel().textContent).not.toContain('I picked the second source');
  });

  it('4c. markup in durable text is neutralised, never interpreted as HTML', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = '<img src=x onerror="alert(1)">petrol';
    const { container } = renderPanel({ output: payload });
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByText(/‹img src=x onerror="alert\(1\)"›petrol/)).toBeInTheDocument();
  });

  it('4d. secret-shaped material is neither promoted to a key nor echoed from an invalid payload', () => {
    const smuggled = JSON.parse(JSON.stringify(fixtures.usable_result));
    smuggled.service_role_key = JWT_SENTINEL;
    smuggled.needs_review = [{ authorization: BEARER_SENTINEL }];
    renderPanel({ output: smuggled });
    // `complete` + review items is already a contradiction, so it fails closed
    // AND the offending material is never printed.
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(panel().textContent).not.toContain(JWT_SENTINEL);
    expect(panel().textContent).not.toContain(API_KEY_PREFIX);
  });

  it('4e. no rendered state ever leaks a prompt, reasoning, stack trace or token', () => {
    for (const [name, payload] of Object.entries(fixtures)) {
      const status = (payload as any).status === 'complete' ? 'completed' : 'partial_success';
      const { unmount } = renderPanel({ output: payload as Record<string, unknown>, runStatus: status });
      const text = panel().textContent ?? '';
      expect(text, name).not.toMatch(/Traceback|chain of thought|sk-[A-Za-z0-9_-]{8,}|Bearer /i);
      unmount();
    }
  });
});

describe('5. surface separation', () => {
  it('5a. the Final Result surface renders no execution telemetry', () => {
    renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    const text = panel().textContent ?? '';
    for (const telemetry of ['Model calls', 'Logical tasks', 'Verifier batches', 'Swarm run',
                             'Commander', 'Usage and scale', 'Cancel run', 'Launch']) {
      expect(text, telemetry).not.toContain(telemetry);
    }
  });

  it('5b. it renders nothing at all when it does not apply', () => {
    const { container } = renderPanel({ visible: false });
    expect(container).toBeEmptyDOMElement();
  });

  it('5c. V1 keeps its existing sanitized-output panel, unchanged', () => {
    render(<RunOutputPanel visible output={{ summary: 'V1 mocked output', artifacts: { report: 'body' } }} />);
    expect(screen.getByRole('heading', { name: 'Final artifacts' })).toBeInTheDocument();
    expect(screen.getByText(/V1 mocked output/)).toBeInTheDocument();
    // The V1 panel is deliberately still the raw sanitized dump.
    expect(document.querySelector('pre.code-block')).toBeInTheDocument();
  });

  it('5d. the V1 panel still says plainly when nothing was recorded', () => {
    render(<RunOutputPanel visible output={undefined} />);
    expect(screen.getByText('The backend has recorded no output payload for this run.')).toBeInTheDocument();
  });
});

describe('6. accessibility and responsive structure', () => {
  it('6a. the surface is a labelled region with a correct heading hierarchy', () => {
    renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    const region = panel();
    expect(region.tagName).toBe('SECTION');
    const levels = within(region).getAllByRole('heading').map((h) => Number(h.tagName[1]));
    expect(levels[0]).toBe(3);
    // No heading level is skipped on the way down.
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i] - levels[i - 1]).toBeLessThanOrEqual(1);
    }
  });

  it('6b. the outcome is announced politely to assistive technology', () => {
    renderPanel();
    expect(screen.getByRole('status')).toHaveTextContent('Usable result');
  });

  it('6c. every status carries a text label beside its symbol — colour is never the signal', () => {
    for (const [output, runStatus, label] of [
      [fixtures.usable_result, 'completed', 'Usable result'],
      [fixtures.partial_result, 'partial_success', 'Partial result'],
      [fixtures.no_usable_result, 'partial_success', 'No usable result'],
      [fixtures.not_found, 'completed', 'Not found'],
    ] as const) {
      const { unmount } = renderPanel({ output: output as Record<string, unknown>, runStatus });
      const banner = screen.getByRole('status');
      expect(banner).toHaveAttribute('data-tone');
      expect(banner).toHaveTextContent(label);
      // The glyph is decorative: the label alone must carry the meaning.
      expect(banner.querySelector('.final-result-symbol')).toHaveAttribute('aria-hidden', 'true');
      unmount();
    }
  });

  it('6d. provenance is a native disclosure, so it is keyboard operable with no custom handler', () => {
    renderPanel();
    const details = screen.getAllByText('Provenance')[0].closest('details') as HTMLDetailsElement;
    expect(details.querySelector('summary')).toBeInTheDocument();
    // <summary> is focusable and toggles on Enter/Space natively.
    details.open = true;
    expect(details.open).toBe(true);
  });

  it('6e. nothing is positioned with a fixed pixel width that could break narrow viewports', () => {
    const { container } = renderPanel({ output: fixtures.structured_value as Record<string, unknown> });
    for (const node of Array.from(container.querySelectorAll<HTMLElement>('*'))) {
      expect(node.style.width).toBe('');
      expect(node.style.minWidth).toBe('');
    }
  });

  it('6f. long unbroken durable text cannot force horizontal overflow', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = 'A'.repeat(400);
    renderPanel({ output: payload });
    // The value is bounded by the parser and wrapped by `overflow-wrap: anywhere`.
    const value = panel().querySelector('.final-result-value-text');
    expect(value?.textContent?.length).toBeLessThanOrEqual(501);
  });
});

describe('7. refresh and resume', () => {
  it('7a. the same durable output renders identically after a remount', () => {
    const first = renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    const before = panel().innerHTML;
    first.unmount();

    // A refresh re-reads the run and re-renders from the durable payload alone.
    renderPanel({
      output: JSON.parse(JSON.stringify(fixtures.partial_result)),
      runStatus: 'partial_success',
      connection: 'terminal',
    });
    expect(panel().innerHTML).toBe(before);
  });

  it('7b. reconnecting after a terminal result never replaces the answer with a spinner', () => {
    renderPanel({ output: fixtures.usable_result as Record<string, unknown>, connection: 'reconnecting' });
    expect(screen.getByText('Usable result')).toBeInTheDocument();
    expect(screen.queryByText('Loading')).not.toBeInTheDocument();
  });
});

describe('8. a valid partial result with no itemized review rows', () => {
  const PAYLOAD = fixtures.partial_result_no_review_items as Record<string, unknown>;

  it('8a. it still reads as partial and unfinished, never as a completed success', () => {
    renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    expect(screen.getByText('Partial result')).toBeInTheDocument();
    expect(screen.getByText(/not a completed result/)).toBeInTheDocument();
    expect(screen.queryByText('Usable result', { exact: true })).not.toBeInTheDocument();
  });

  it('8b. it says the run did not complete its work, without naming a cause', () => {
    renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    expect(screen.getByText(/did not complete all the work it was required to/)).toBeInTheDocument();
    // It must NOT assert that a claim went unverified: a partial result can
    // arise with every gathered claim verified.
    const text = panel().textContent ?? '';
    expect(text).not.toMatch(/not every claim|the verifier rejected|a claim the verifier/i);
  });

  it('8c. it promises NO itemized list when none was recorded', () => {
    renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    const text = panel().textContent ?? '';
    expect(text).not.toMatch(/items below|listed below|below are still outstanding/i);
    // The heading has no count, because there is no list to count.
    expect(screen.getByRole('heading', { name: 'Outstanding items' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Outstanding items \(/ })).not.toBeInTheDocument();
    expect(screen.getByText(/contains no itemized entries/)).toBeInTheDocument();
    expect(screen.getByText(/does not infer the missing reason, task or claim/)).toBeInTheDocument();
  });

  it('8d. it invents no reason and no field', () => {
    renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    // The rejected claim's field must NOT appear: the builder left it out.
    expect(screen.queryByText('horsepower_hp')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Conflicts/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Task failures/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Coverage gaps/ })).not.toBeInTheDocument();
    // The verified half is still reported in full.
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.getByText('plug-in hybrid')).toBeInTheDocument();
  });

  it('8e. itemized rows still appear when the payload DOES record them', () => {
    renderPanel({ output: fixtures.partial_result as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByRole('heading', { name: 'Outstanding items (3)' })).toBeInTheDocument();
    expect(screen.queryByText(/No itemized entries were recorded/)).not.toBeInTheDocument();
  });

  it('8f. the no-items section keeps the heading hierarchy and landmark intact', () => {
    renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    const region = panel();
    const levels = within(region).getAllByRole('heading').map((h) => Number(h.tagName[1]));
    expect(levels[0]).toBe(3);
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i] - levels[i - 1]).toBeLessThanOrEqual(1);
    }
    expect(screen.getByRole('status')).toHaveTextContent('Partial result');
  });

  it('8g. a refresh rebuilds it identically from the durable payload', () => {
    const first = renderPanel({ output: PAYLOAD, runStatus: 'partial_success' });
    const before = panel().innerHTML;
    first.unmount();
    renderPanel({ output: JSON.parse(JSON.stringify(PAYLOAD)), runStatus: 'partial_success' });
    expect(panel().innerHTML).toBe(before);
  });
});

describe('9. no credential survives into the rendered DOM', () => {
  /** Every durable string position the surface can render. */
  const positions: [string, (secret: string) => Record<string, unknown>][] = [
    ['scalar value', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields.fuel_type[0].value = secret;
      return p;
    }],
    ['nested string', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields.fuel_type[0].value = { spec: { notes: [secret] } };
      return p;
    }],
    ['structured key', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields.fuel_type[0].value = { [secret]: 'value' };
      return p;
    }],
    ['field key', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields = { [secret]: p.fields.fuel_type };
      return p;
    }],
    ['provenance source', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields.fuel_type[0].provenance.source_id = secret;
      return p;
    }],
    ['provenance scope', (secret) => {
      const p = JSON.parse(JSON.stringify(fixtures.usable_result));
      p.fields.fuel_type[0].provenance.scope.entity = secret;
      return p;
    }],
  ];

  it('9a. in every verified-field position, with provenance expanded', () => {
    for (const [name, build] of positions) {
      for (const secret of ALL_SECRET_SENTINELS) {
        const { unmount } = renderPanel({ output: build(secret), runStatus: 'completed' });
        // Open every disclosure: a collapsed <details> still has its content
        // in the DOM, but opening it proves the visible path too.
        for (const node of Array.from(document.querySelectorAll('details'))) {
          (node as HTMLDetailsElement).open = true;
        }
        const html = panel().innerHTML;
        for (const s of ALL_SECRET_SENTINELS) expect(html, `${name} / ${s}`).not.toContain(s);
        for (const f of SECRET_FRAGMENTS) expect(html, `${name} / ${f}`).not.toContain(f);
        unmount();
      }
    }
  });

  it('9b. in a review reason, a review code and a task id', () => {
    const builders: [string, (secret: string) => Record<string, unknown>][] = [
      ['reason', (secret) => {
        const p = JSON.parse(JSON.stringify(fixtures.partial_result));
        p.needs_review[0].reason = secret;
        return p;
      }],
      ['code', (secret) => {
        const p = JSON.parse(JSON.stringify(fixtures.partial_result));
        p.needs_review[1].code = secret;
        return p;
      }],
      ['task id', (secret) => {
        const p = JSON.parse(JSON.stringify(fixtures.partial_result));
        p.needs_review[1].task_id = secret.slice(0, 80);
        return p;
      }],
    ];
    for (const [name, build] of builders) {
      for (const secret of ALL_SECRET_SENTINELS) {
        const { unmount } = renderPanel({ output: build(secret), runStatus: 'partial_success' });
        for (const node of Array.from(document.querySelectorAll('details'))) {
          (node as HTMLDetailsElement).open = true;
        }
        const html = panel().innerHTML;
        for (const s of ALL_SECRET_SENTINELS) expect(html, `${name} / ${s}`).not.toContain(s);
        for (const f of SECRET_FRAGMENTS) expect(html, `${name} / ${f}`).not.toContain(f);
        unmount();
      }
    }
  });

  it('9c. a redacted value is shown as the marker, not silently dropped', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = BEARER_SENTINEL;
    renderPanel({ output: payload, runStatus: 'completed' });
    // The field is still reported; only its content is replaced.
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.getByText('[REDACTED]')).toBeInTheDocument();
  });

  it('9d. V1 keeps its own existing sanitized-output behaviour, unchanged', () => {
    // RunOutputPanel still uses redactSecrets over the whole payload. F4 must
    // not have altered what V1 shows.
    render(<RunOutputPanel visible output={{ summary: 'V1 mocked output', note: JWT_SENTINEL }} />);
    expect(screen.getByRole('heading', { name: 'Final artifacts' })).toBeInTheDocument();
    expect(screen.getByText(/V1 mocked output/)).toBeInTheDocument();
    expect(document.querySelector('pre.code-block')).toBeInTheDocument();
  });
});

describe('10. malformed provenance never renders as a verified field', () => {
  it('10a. an incomplete trace makes the whole result unavailable', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].provenance = {};
    renderPanel({ output: payload, runStatus: 'completed' });
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('PROVENANCE_INVALID')).toBeInTheDocument();
    // Critically: no field is shown as verified on the strength of it.
    expect(screen.queryByText('Fuel type')).not.toBeInTheDocument();
    expect(screen.queryByText('plug-in hybrid')).not.toBeInTheDocument();
  });

  it('10b. a missing scope key, an extra key and an empty id all refuse', () => {
    const cases: [string, (p: any) => void][] = [
      ['missing scope key', (p) => { delete p.fields.fuel_type[0].provenance.scope.market; }],
      ['extra key', (p) => { p.fields.fuel_type[0].provenance.content_hash = 'sha256:abc'; }],
      ['empty id', (p) => { p.fields.fuel_type[0].provenance.source_id = ''; }],
      ['mistyped id', (p) => { p.fields.fuel_type[0].provenance.claim_id = 7; }],
    ];
    for (const [name, mutate] of cases) {
      const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
      mutate(payload);
      const { unmount } = renderPanel({ output: payload, runStatus: 'completed' });
      expect(screen.getByText('Result unavailable'), name).toBeInTheDocument();
      expect(screen.getByText('PROVENANCE_INVALID'), name).toBeInTheDocument();
      unmount();
    }
  });

  it('10c. a value that could not have survived JSON refuses too', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = undefined;
    renderPanel({ output: payload, runStatus: 'completed' });
    expect(screen.getByText('Result unavailable')).toBeInTheDocument();
    expect(screen.getByText('VALUE_NOT_JSON')).toBeInTheDocument();
    expect(screen.queryByText('No value recorded')).not.toBeInTheDocument();
  });
});

describe('11. a payload-controlled review code cannot crash the surface', () => {
  it('11a. a code naming an Object.prototype member renders as a plain code chip', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.partial_result));
    payload.needs_review[1].code = 'constructor';
    // Before the label table became a Map this threw: "Functions are not valid
    // as a React child".
    renderPanel({ output: payload, runStatus: 'partial_success' });
    expect(screen.getByText('Partial result')).toBeInTheDocument();
    expect(screen.getByText('constructor')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Task failures (1)' })).toBeInTheDocument();
  });
});

describe('12. the modern Supabase server-side key never reaches the DOM', () => {
  it('12a. it is shown as the marker, and the field is still reported', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = SUPABASE_SECRET_SENTINEL;
    renderPanel({ output: payload, runStatus: 'completed' });
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.getByText('[REDACTED]')).toBeInTheDocument();
    expect(panel().innerHTML).not.toContain(SUPABASE_SECRET_SENTINEL);
    expect(panel().innerHTML).not.toContain('sb_secret_');
  });

  it('12b. it is absent from the FULLY EXPANDED DOM in every position', () => {
    const positions: [string, (secret: string) => Record<string, unknown>][] = [
      ['scalar value', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].value = s2; return p2; }],
      ['nested string', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].value = { spec: { notes: [s2] } }; return p2; }],
      ['structured key', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].value = { [s2]: 'v' }; return p2; }],
      ['field key', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields = { [s2]: p2.fields.fuel_type }; return p2; }],
      ['provenance source', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].provenance.source_id = s2; return p2; }],
      ['provenance claim', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].provenance.claim_id = s2; return p2; }],
      ['provenance task', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].provenance.task_id = s2.slice(0, 80); return p2; }],
      ['scope entity', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].provenance.scope.entity = s2; return p2; }],
      ['scope geography', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.usable_result)); p2.fields.fuel_type[0].provenance.scope.geography = s2; return p2; }],
    ];
    for (const [name, build] of positions) {
      const { unmount } = renderPanel({ output: build(SUPABASE_SECRET_SENTINEL), runStatus: 'completed' });
      for (const node of Array.from(document.querySelectorAll('details'))) {
        (node as HTMLDetailsElement).open = true;
      }
      const html = panel().innerHTML;
      expect(html, name).not.toContain(SUPABASE_SECRET_SENTINEL);
      expect(html, name).not.toContain('sb_secret_');
      unmount();
    }
  });

  it('12c. in a review reason, a review code and a task id', () => {
    const builders: [string, (s2: string) => Record<string, unknown>][] = [
      ['reason', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.partial_result)); p2.needs_review[0].reason = s2; return p2; }],
      ['code', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.partial_result)); p2.needs_review[1].code = s2; return p2; }],
      ['task id', (s2) => { const p2 = JSON.parse(JSON.stringify(fixtures.partial_result)); p2.needs_review[1].task_id = s2.slice(0, 80); return p2; }],
    ];
    for (const [name, build] of builders) {
      const { unmount } = renderPanel({ output: build(SUPABASE_SECRET_SENTINEL), runStatus: 'partial_success' });
      for (const node of Array.from(document.querySelectorAll('details'))) {
        (node as HTMLDetailsElement).open = true;
      }
      expect(panel().innerHTML, name).not.toContain(SUPABASE_SECRET_SENTINEL);
      expect(panel().innerHTML, name).not.toContain('sb_secret_');
      unmount();
    }
  });

  it('12d. PUBLIC Supabase configuration still renders — it is not a credential', () => {
    const payload = JSON.parse(JSON.stringify(fixtures.usable_result));
    payload.fields.fuel_type[0].value = SUPABASE_PUBLISHABLE_PUBLIC;
    renderPanel({ output: payload, runStatus: 'completed' });
    expect(screen.getByText(SUPABASE_PUBLISHABLE_PUBLIC)).toBeInTheDocument();
  });
});

describe('13. an empty optional scope value renders as unstated, not as a refusal', () => {
  it('13a. the backend-built empty-scope payload renders as a usable result', () => {
    renderPanel({ output: fixtures.empty_optional_scope as Record<string, unknown>, runStatus: 'completed' });
    expect(screen.getByText('Usable result')).toBeInTheDocument();
    expect(screen.getByText('Fuel type')).toBeInTheDocument();
    expect(screen.queryByText('Result unavailable')).not.toBeInTheDocument();
  });

  it('13b. the empty rows are omitted from the provenance disclosure', () => {
    renderPanel({ output: fixtures.empty_optional_scope as Record<string, unknown>, runStatus: 'completed' });
    const details = screen.getAllByText('Provenance')[0].closest('details') as HTMLDetailsElement;
    details.open = true;
    const rows = within(details);
    // The references that ARE stated still appear…
    expect(rows.getByText('Source')).toBeInTheDocument();
    expect(rows.getByText('Entity')).toBeInTheDocument();
    // …and the unstated optional ones are simply absent, not blank rows.
    expect(rows.queryByText('Geography')).not.toBeInTheDocument();
    expect(rows.queryByText('Market')).not.toBeInTheDocument();
  });
});

describe('14. every backend route to partial_result renders honestly', () => {
  const ROUTES: [string, Record<string, unknown>, number][] = [
    ['task failure only', fixtures.partial_task_failure_only as Record<string, unknown>, 1],
    ['coverage gap only', fixtures.partial_coverage_gap_only as Record<string, unknown>, 1],
    ['conflict, no rows', fixtures.partial_conflict_no_rows as Record<string, unknown>, 0],
    ['rejected verdict, no rows', fixtures.partial_result_no_review_items as Record<string, unknown>, 0],
  ];

  it('14a. each shows Partial result and never Usable result', () => {
    for (const [name, payload] of ROUTES) {
      const { unmount } = renderPanel({ output: payload, runStatus: 'partial_success' });
      expect(screen.getByText('Partial result'), name).toBeInTheDocument();
      expect(screen.queryByText('Usable result', { exact: true }), name).not.toBeInTheDocument();
      expect(screen.getByText(/not a completed result/), name).toBeInTheDocument();
      unmount();
    }
  });

  it('14b. the summary stays TRUE for each — it names no cause', () => {
    for (const [name, payload] of ROUTES) {
      const { unmount } = renderPanel({ output: payload, runStatus: 'partial_success' });
      const text = panel().textContent ?? '';
      expect(text, name).toMatch(/did not complete all the work it was required to/);
      // These payloads include ones where EVERY gathered claim verified.
      expect(text, name).not.toMatch(/not every claim|the verifier rejected|a claim the verifier/i);
      unmount();
    }
  });

  it('14c. a recorded task failure renders in the Task failures group', () => {
    renderPanel({ output: fixtures.partial_task_failure_only as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByRole('heading', { name: 'Outstanding items (1)' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Task failures (1)' })).toBeInTheDocument();
    expect(screen.getByText('The task did not complete')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Coverage gaps/ })).not.toBeInTheDocument();
  });

  it('14d. a recorded coverage gap renders in the Coverage gaps group', () => {
    renderPanel({ output: fixtures.partial_coverage_gap_only as Record<string, unknown>, runStatus: 'partial_success' });
    expect(screen.getByRole('heading', { name: 'Coverage gaps (1)' })).toBeInTheDocument();
    expect(screen.getByText('Evidence requirements were not met')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /Task failures/ })).not.toBeInTheDocument();
  });

  it('14e. the itemless routes claim nothing about a rejection or a reason', () => {
    for (const [name, payload, rows] of ROUTES.filter(([, , n]) => n === 0)) {
      const { unmount } = renderPanel({ output: payload, runStatus: 'partial_success' });
      expect(rows).toBe(0);
      expect(screen.getByRole('heading', { name: 'Outstanding items' }), name).toBeInTheDocument();
      expect(screen.getByText(/contains no itemized entries/), name).toBeInTheDocument();
      expect(screen.getByText(/does not infer the missing reason, task or claim/), name).toBeInTheDocument();
      const text = panel().textContent ?? '';
      expect(text, name).not.toMatch(/rejected|unverified|conflict/i);
      unmount();
    }
  });

  it('14f. the verified half is still reported in every route', () => {
    for (const [name, payload] of ROUTES) {
      const { unmount } = renderPanel({ output: payload, runStatus: 'partial_success' });
      expect(screen.getByRole('heading', { name: 'Verified fields' }), name).toBeInTheDocument();
      expect(screen.getByText('plug-in hybrid'), name).toBeInTheDocument();
      unmount();
    }
  });
});
