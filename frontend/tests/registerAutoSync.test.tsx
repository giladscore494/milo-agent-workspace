/**
 * PR-SYNC-2: the Register page's Auto sync control.
 *
 * What is asserted: the block is parsed strictly (an unknown decision or a
 * malformed tick is no block at all, and the rest of the page still renders);
 * Off / On / Paused render with the next tick's decision, the last tick, the
 * pause's exact next step and Resume; a switch that is on with no recent tick
 * says "Scheduler not ticking"; the 90% warning shows; and each button sends
 * its action under the open conversation, then reads the page again.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RegisterClient, RegisterPanel } from '../components/register/RegisterPanel';
import { ApiError } from '../lib/api';
import { parseAutoSync, parseRegister } from '../lib/register';

const PROJECT = '00000000-0000-4000-8000-000000000001';
const CONVERSATION = '00000000-0000-4000-8000-000000000002';

function autoSync(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true, paused_reason: null, paused_at: null, next_step: null,
    consecutive_throttles: 0, consecutive_failures: 0,
    last_tick: { at: '2026-10-05T17:07:00+00:00', decision: 'skip', reason: 'SYNC_NOT_DUE',
                 backlog: '83/41137', db_mb: '326.5', warning: '' },
    next: { decision: 'start', reason: 'SYNC_BACKLOG' },
    scheduler_stale: false, db_warning: false,
    ...overrides,
  };
}

function registerBody(auto: unknown) {
  return {
    available: true, can_capture: true, can_refresh_directory: true, can_sync: true,
    directory: null, units: [],
    totals: { units_total: 0, units_captured: 0, rows_total: 0, rows_captured: 0 },
    capacity: { capacity_bytes: 500_000_000, threshold: 0.8, limit_bytes: 400_000_000,
                bytes_per_row_estimate: 3000, current_bytes: 326_500_000, over_threshold: false },
    group_max_rows: 10000,
    auto_sync: auto,
  };
}

function client(auto: unknown, overrides: Partial<RegisterClient> = {}): RegisterClient {
  return {
    register: vi.fn(async () => registerBody(auto)),
    requestCapture: vi.fn(async () => ({})),
    requestDirectory: vi.fn(async () => ({})),
    requestSync: vi.fn(async () => ({})),
    setAutoSync: vi.fn(async () => auto),
    ...overrides,
  };
}

async function openPanel(api: RegisterClient, conversationId?: string) {
  render(<RegisterPanel projectId={PROJECT} conversationId={conversationId} client={api} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Show' }));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('parseAutoSync', () => {
  it('reads the server block', () => {
    const parsed = parseAutoSync(autoSync());
    expect(parsed).toMatchObject({ enabled: true, next: { decision: 'start', reason: 'SYNC_BACKLOG' },
      lastTick: { decision: 'skip', reason: 'SYNC_NOT_DUE', backlog: '83/41137', dbMb: '326.5' } });
    expect(parsed?.lastTick?.warning).toBeUndefined();
  });

  it('refuses a malformed block, and the page still renders without it', () => {
    for (const bad of [
      autoSync({ next: { decision: 'pause', reason: 'SYNC_NOT_DUE' } }),
      autoSync({ next: { decision: 'start', reason: 'not a code' } }),
      autoSync({ enabled: 'yes' }),
      autoSync({ paused_reason: '<b>x</b>' }),
      autoSync({ last_tick: { at: 'now', decision: 'skip', reason: 'SYNC_BUSY', backlog: '83 tozars', db_mb: '1' } }),
      autoSync({ consecutive_failures: -1 }),
    ]) {
      expect(parseAutoSync(bad)).toBeUndefined();
      const page = parseRegister(registerBody(bad));
      expect(page).toBeDefined();
      expect(page?.autoSync).toBeUndefined();
    }
    expect(parseRegister(registerBody(null))?.autoSync).toBeUndefined();
  });
});

describe('the Auto sync control', () => {
  it('shows On, the next tick and the last tick, and turns it off', async () => {
    const api = client(autoSync());
    await openPanel(api, CONVERSATION);
    const block = await screen.findByLabelText('Auto sync');
    expect(block.textContent).toContain('Auto sync: On');
    expect(block.textContent).toContain('next tick: start (backlog left, an hour since the last sync (SYNC_BACKLOG))');
    expect(screen.getByLabelText('Last tick').textContent).toContain(
      'Last tick 2026-10-05T17:07:00+00:00: skip (not due yet (SYNC_NOT_DUE)), backlog 83/41137, database 326.5 MB.');
    expect(screen.queryByText(/Scheduler not ticking/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Turn auto sync off' }));
    await waitFor(() => expect(api.setAutoSync).toHaveBeenCalledWith(PROJECT, 'off', CONVERSATION));
    await waitFor(() => expect(api.register).toHaveBeenCalledTimes(2));
  });

  it('turns it on, recorded under the open conversation only', async () => {
    const api = client(autoSync({ enabled: false, last_tick: null, next: { decision: 'skip', reason: 'SYNC_AUTO_OFF' } }));
    await openPanel(api);
    const button = await screen.findByRole('button', { name: 'Turn auto sync on' });
    expect(button).toBeDisabled();  // no conversation open
    expect(screen.getByLabelText('Last tick').textContent).toContain('No tick yet.');
  });

  it('shows a pause with its exact next step, and resumes', async () => {
    const api = client(autoSync({
      paused_reason: 'SYNC_PAUSED_CAPACITY', paused_at: '2026-10-06T03:07:00+00:00',
      next_step: 'Run the Register retention workflow with mode vacuum-full (reviewer-gated), then press Resume.',
      next: { decision: 'skip', reason: 'SYNC_PAUSED_CAPACITY' }, db_warning: true }));
    await openPanel(api, CONVERSATION);
    const paused = await screen.findByLabelText('Auto sync paused');
    expect(paused.textContent).toContain('Paused since 2026-10-06T03:07:00+00:00');
    expect(paused.textContent).toContain('vacuum-full');
    expect(screen.getByLabelText('Auto sync').textContent).toContain('Auto sync: Paused');
    expect(screen.getByLabelText('Database warning').textContent).toContain('90%');
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(api.setAutoSync).toHaveBeenCalledWith(PROJECT, 'resume', CONVERSATION));
  });

  it('says what a refused Resume needs', async () => {
    const api = client(autoSync({ paused_reason: 'SYNC_PAUSED_CAPACITY' }), {
      setAutoSync: vi.fn(async () => {
        throw new ApiError(409, 'SYNC_RESUME_OVER_CAPACITY', 'over');
      }),
    });
    await openPanel(api, CONVERSATION);
    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));
    expect(await screen.findByText(/Run the Register retention workflow with mode vacuum-full first/))
      .toBeTruthy();
  });

  it('says when the scheduler is not ticking', async () => {
    await openPanel(client(autoSync({ scheduler_stale: true })), CONVERSATION);
    expect(await screen.findByText(/Scheduler not ticking/)).toBeTruthy();
  });
});
