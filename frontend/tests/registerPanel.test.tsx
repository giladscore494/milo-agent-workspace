/**
 * PR-D1: the Register page.
 *
 * What is asserted: the page is parsed STRICTLY (a failure without a code, a
 * capture without a verified snapshot, an unknown state is unreadable, never
 * rendered); every state renders (not captured / capturing / captured with its
 * snapshot, rows, verification and bytes per row / failed with its code); the
 * totals and the capacity bar show the server's numbers; a capacity refusal
 * shows current, projected and limit; a group over the cap cannot be sent; a
 * tozar goes back to the server exactly as it was read; the page does not
 * exist while the server answers 404 (flag off); and the gateway proxies the
 * read as a read and the two writes as execution routes that never start a run.
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RegisterPanel, RegisterClient } from '../components/register/RegisterPanel';
import { ApiError } from '../lib/api';
import { capacityRefusal, capturable, groupFits, parseRegister, parseSyncSummary } from '../lib/register';
import { isGatewayRequestAllowed, isRunCreationRequest } from '../lib/server/gatewayPolicy';

const PROJECT = '00000000-0000-4000-8000-000000000001';
const CONVERSATION = '00000000-0000-4000-8000-000000000002';
const VERSION = 'a'.repeat(64);
const OLD_VERSION = 'b'.repeat(64);
const RESOURCE = '142afde2-6228-49f9-8a29-9b6c3a0cbe40';
// Exact register spellings, including a trailing space: never normalized.
const TOYOTA = 'טויוטה יפן';
const MAZDA = 'מאזדה ';

function unitBody(tozar: string, expected: number, state: string, extra: Record<string, unknown> = {}) {
  return { tozar, expected_rows: expected, state, ...extra };
}

function registerBody(overrides: Record<string, unknown> = {}) {
  return {
    available: true, can_capture: true, can_refresh_directory: true,
    directory: { register_version: VERSION, resource_id: RESOURCE, fetched_at: '2026-09-29T10:00:00+00:00',
                 unit_count: 5, total_rows: 26000 },
    units: [
      unitBody(TOYOTA, 3000, 'captured', { snapshot_key: 'cs1.toyota', captured_rows: 3000, api_total: 3000,
                                           verified: true, failure_code: null, measured_bytes: 9_000_000,
                                           measured_bytes_per_row: 3000, register_version: VERSION }),
      unitBody(MAZDA, 2000, 'not_captured'),
      unitBody('קיה', 4000, 'capturing', { snapshot_key: null, captured_rows: null, api_total: null,
                                          verified: null, failure_code: null, register_version: VERSION }),
      unitBody('יונדאי', 5000, 'failed', { snapshot_key: 'cs1.hyundai', captured_rows: 4990, api_total: 5000,
                                           verified: false, failure_code: 'CATALOG_CAPTURE_COUNT_MISMATCH',
                                           register_version: VERSION }),
      unitBody('פולקסווגן', 12000, 'not_captured'),
    ],
    totals: { units_total: 5, units_captured: 1, rows_total: 26000, rows_captured: 3000 },
    capacity: { capacity_bytes: 500_000_000, threshold: 0.8, limit_bytes: 400_000_000,
                bytes_per_row_estimate: 3500, group_max_rows: 10000, current_bytes: 120_000_000,
                over_threshold: false },
    group_max_rows: 10000,
    ...overrides,
  };
}

function client(body: unknown = registerBody(), overrides: Partial<RegisterClient> = {}): RegisterClient {
  return {
    register: vi.fn(async () => body),
    requestCapture: vi.fn(async () => ({ decision: 'claimed' })),
    requestDirectory: vi.fn(async () => ({ started: true })),
    requestSync: vi.fn(async () => ({ started: true })),
    ...overrides,
  };
}

async function openPanel(api: RegisterClient, conversationId: string | null = CONVERSATION) {
  render(<RegisterPanel projectId={PROJECT} conversationId={conversationId ?? undefined} client={api} />);
  const toggle = await screen.findByRole('button', { name: 'Show' });
  fireEvent.click(toggle);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('parseRegister', () => {
  it('reads the whole page', () => {
    const view = parseRegister(registerBody());
    expect(view?.units.map((item) => item.state)).toEqual(
      ['captured', 'not_captured', 'capturing', 'failed', 'not_captured']);
    expect(view?.units[1].tozar).toBe(MAZDA);
    expect(view?.capacity.limitBytes).toBe(400_000_000);
    expect(view?.groupMaxRows).toBe(10000);
  });

  it.each([
    ['a failure without its code', { failure_code: null }, 3],
    ['a capture without a verified snapshot', { verified: false }, 0],
    ['an unknown state', { state: 'queued' }, 1],
    ['a negative expected count', { expected_rows: -1 }, 1],
    ['a malformed code', { failure_code: 'not a code' }, 3],
  ])('refuses %s', (_label, patch, index) => {
    const body = registerBody();
    (body.units as Record<string, unknown>[])[index] = { ...(body.units as Record<string, unknown>[])[index], ...patch };
    expect(parseRegister(body)).toBeUndefined();
  });

  it('refuses a malformed directory or capacity', () => {
    expect(parseRegister(registerBody({ directory: { register_version: 'x' } }))).toBeUndefined();
    expect(parseRegister(registerBody({ capacity: { threshold: 2 } }))).toBeUndefined();
    expect(parseRegister({ available: false })).toBeUndefined();
  });

  it('accepts a register that has not been read yet', () => {
    const view = parseRegister(registerBody({ directory: null, units: [], can_capture: false,
      totals: { units_total: 0, units_captured: 0, rows_total: 0, rows_captured: 0 } }));
    expect(view?.directory).toBeUndefined();
    expect(view?.canRefreshDirectory).toBe(true);
  });
});

describe('group cap and capturability', () => {
  const view = parseRegister(registerBody())!;
  it('lets one tozar over the cap go alone, never in a group', () => {
    const big = { ...view.units[4], expectedRows: 25000 };
    expect(groupFits([big], 10000)).toBe(true);
    expect(groupFits([big, view.units[1]], 10000)).toBe(false);
    expect(groupFits([view.units[1], view.units[3]], 10000)).toBe(true);
    expect(groupFits([], 10000)).toBe(false);
  });

  it('offers a capture for the not captured, the failed and an older version only', () => {
    expect(view.units.map((item) => capturable(item, VERSION))).toEqual([false, true, false, true, true]);
    expect(capturable(view.units[0], OLD_VERSION)).toBe(true);
  });
});

describe('RegisterPanel', () => {
  it('renders every state with its facts, the totals and the capacity bar', async () => {
    await openPanel(client());
    const table = await screen.findByRole('table');
    expect(table.textContent).toContain(TOYOTA);
    expect(table.textContent).toContain('Captured — 3,000 rows, verified');
    expect(table.textContent).toContain('cs1.toyota');
    expect(table.textContent).toContain('Not captured');
    expect(table.textContent).toContain('Capturing');
    expect(table.textContent).toContain('Failed CATALOG_CAPTURE_COUNT_MISMATCH');
    expect(table.textContent).toContain('3,000');  // measured bytes per row
    expect(screen.getByLabelText('Register totals').textContent).toContain('Captured 1 of 5 tozars, 3,000 of 26,000 rows');
    expect(screen.getByLabelText('Database capacity').textContent).toContain('Database 120.0 MB of the 400.0 MB threshold');
    expect(screen.getByLabelText('Capacity used')).toHaveProperty('value', 30);
  });

  it('does not exist while the server answers 404 (register capture off)', async () => {
    const api = client(undefined, {
      register: vi.fn(async () => { throw new ApiError(404, 'CATALOG_REGISTER_DISABLED', 'x'); }),
    });
    const { container } = render(<RegisterPanel projectId={PROJECT} conversationId={CONVERSATION} client={api} />);
    await waitFor(() => expect(api.register).toHaveBeenCalled());
    expect(container.innerHTML).toBe('');
    expect(screen.queryByText('Register')).toBeNull();
  });

  it('reads nothing without a project', () => {
    const api = client();
    const { container } = render(<RegisterPanel client={api} />);
    expect(container.innerHTML).toBe('');
    expect(api.register).not.toHaveBeenCalled();
  });

  it('captures ONE tozar with its exact spelling and this directory version', async () => {
    const api = client();
    await openPanel(api);
    const row = (await screen.findByText(MAZDA.trim())).closest('tr')!;
    await act(async () => {
      fireEvent.click(row.querySelector('button')!);
    });
    expect(api.requestCapture).toHaveBeenCalledWith(PROJECT, VERSION, [MAZDA], CONVERSATION);
    expect(api.register).toHaveBeenCalledTimes(2);  // read again after the action
  });

  it('captures a group within the cap and refuses to send one over it', async () => {
    const api = client();
    await openPanel(api);
    // Testing Library trims the accessible name; the request below keeps the space.
    fireEvent.click(await screen.findByLabelText(`Select ${MAZDA.trim()}`));
    fireEvent.click(screen.getByLabelText('Select פולקסווגן'));
    // 2,000 + 12,000 > 10,000: the group cannot be sent.
    expect(screen.getByLabelText('Selection').textContent).toContain('14,000 expected rows (group cap 10,000)');
    expect(screen.getByRole('button', { name: 'Capture selected (2)' })).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByLabelText('Select פולקסווגן'));
    fireEvent.click(screen.getByLabelText('Select יונדאי'));
    const send = screen.getByRole('button', { name: 'Capture selected (2)' });
    expect(send).toHaveProperty('disabled', false);
    await act(async () => {
      fireEvent.click(send);
    });
    expect(api.requestCapture).toHaveBeenCalledWith(PROJECT, VERSION, [MAZDA, 'יונדאי'], CONVERSATION);
  });

  it('shows the exact numbers of a capacity refusal', async () => {
    const refusal = new ApiError(409, 'CATALOG_CAPACITY_THRESHOLD_EXCEEDED', 'x',
      { current_bytes: 390_000_000, projected_bytes: 397_000_000 + 7_000_000, limit_bytes: 400_000_000 });
    const api = client(registerBody(), { requestCapture: vi.fn(async () => { throw refusal; }) });
    await openPanel(api);
    const row = (await screen.findByText(MAZDA.trim())).closest('tr')!;
    await act(async () => {
      fireEvent.click(row.querySelector('button')!);
    });
    expect(screen.getByRole('alert').textContent).toContain('above its capacity threshold');
    const numbers = screen.getByLabelText('Capacity refusal').textContent ?? '';
    expect(numbers).toContain('390,000,000 bytes');
    expect(numbers).toContain('404,000,000 bytes');
    expect(numbers).toContain('400,000,000 bytes');
  });

  it('says why capture is refused above the threshold', async () => {
    await openPanel(client(registerBody({ capacity: { capacity_bytes: 500_000_000, threshold: 0.8,
      limit_bytes: 400_000_000, bytes_per_row_estimate: 3500, group_max_rows: 10000,
      current_bytes: 410_000_000, over_threshold: true } })));
    expect((await screen.findAllByRole('alert'))[0].textContent).toContain('above its capacity threshold');
  });

  it('offers no capture without a conversation, and none when the server cannot capture', async () => {
    await openPanel(client(), null);
    expect(await screen.findByText(/Open a conversation of this project to capture/)).toBeTruthy();
    for (const button of screen.getAllByRole('button', { name: /^Capture/ })) {
      expect(button).toHaveProperty('disabled', true);
    }
  });

  it('hides every capture control when the server cannot capture', async () => {
    await openPanel(client(registerBody({ can_capture: false, can_refresh_directory: false })));
    await screen.findByRole('table');
    expect(screen.queryAllByRole('button', { name: /Capture/ })).toHaveLength(0);
    expect(screen.queryByRole('button', { name: 'Refresh directory' })).toBeNull();
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  it('refreshes the directory under the open conversation', async () => {
    const api = client();
    await openPanel(api);
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Refresh directory' }));
    });
    expect(api.requestDirectory).toHaveBeenCalledWith(PROJECT, CONVERSATION);
  });

  it('syncs under the open conversation and shows the last sync', async () => {
    const summary = 'SYNC_SUMMARY|changed=true|directory_version=9e2ae1e5da80|work=93|captured=21|reused=0|'
      + 'failed=0|deferred=72|requests=78/80|stop=budget|backlog=72/2900|coverage=98793/101693';
    const api = client(registerBody({ can_sync: true, last_sync: { summary, finished_at: '2026-10-05T10:00:00Z' } }));
    await openPanel(api);
    const shown = await screen.findByLabelText('Last sync');
    expect(shown.textContent).toContain('backlog72/2900');
    expect(shown.textContent).toContain('stopbudget');
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Sync register' }));
    });
    expect(api.requestSync).toHaveBeenCalledWith(PROJECT, CONVERSATION);
    expect(parseSyncSummary(summary)?.map((field) => field.name)).toEqual(['changed', 'directory_version', 'work',
      'captured', 'reused', 'failed', 'deferred', 'requests', 'stop', 'backlog', 'coverage']);
    for (const bad of [summary.replace('stop=budget', 'stop=<b>'), summary.replace('|work=93', ''), `${summary}|x=1`]) {
      expect(parseSyncSummary(bad)).toBeUndefined();
    }
  });

  it('offers no sync where the server cannot sync', async () => {
    await openPanel(client());
    await screen.findByRole('table');
    expect(screen.queryByRole('button', { name: 'Sync register' })).toBeNull();
    expect(screen.queryByLabelText('Last sync')).toBeNull();
  });

  it('shows nothing when the first read is unreadable or fails, never a partial page', async () => {
    for (const api of [client({ available: true, units: 'nope' }),
      client(undefined, { register: vi.fn(async () => { throw new ApiError(502, 'REPOSITORY_ERROR', 'x'); }) })]) {
      const { container, unmount } = render(<RegisterPanel projectId={PROJECT} conversationId={CONVERSATION} client={api} />);
      await waitFor(() => expect(api.register).toHaveBeenCalled());
      expect(container.innerHTML).toBe('');
      unmount();
    }
  });

  it('keeps the last page and says so when a later read fails', async () => {
    let reads = 0;
    const api = client(undefined, {
      register: vi.fn(async () => {
        reads += 1;
        if (reads === 1) return registerBody();
        return { available: true, units: 'nope' };
      }),
    });
    await openPanel(api);
    await screen.findByRole('table');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Reload' }));
    });
    expect((await screen.findByRole('alert')).textContent).toContain('The register could not be read.');
    expect(screen.getByRole('table')).toBeTruthy();
  });
});

describe('capacityRefusal', () => {
  it('reads only a well-formed refusal', () => {
    expect(capacityRefusal(new ApiError(409, 'CATALOG_CAPACITY_THRESHOLD_EXCEEDED', 'x',
      { current_bytes: 1, projected_bytes: 2, limit_bytes: 3 }))).toEqual(
      { currentBytes: 1, projectedBytes: 2, limitBytes: 3 });
    expect(capacityRefusal(new ApiError(409, 'CATALOG_CAPACITY_THRESHOLD_EXCEEDED', 'x', { current_bytes: -1 })))
      .toBeUndefined();
    expect(capacityRefusal(new ApiError(409, 'OTHER', 'x', { current_bytes: 1, projected_bytes: 2, limit_bytes: 3 })))
      .toBeUndefined();
  });
});

describe('gateway policy for the Register page', () => {
  const read = `/projects/${PROJECT}/register`;
  const writes = [`/projects/${PROJECT}/register/captures`, `/projects/${PROJECT}/register/directory`,
    `/projects/${PROJECT}/register/sync`];
  afterEach(() => {
    delete process.env.GATEWAY_ALLOW_EXECUTION_ROUTES;
    delete process.env.GATEWAY_ALLOW_RUN_START_ROUTES;
  });

  it('proxies the read in every posture, and never a write to it', () => {
    expect(isGatewayRequestAllowed('GET', read)).toBe(true);
    expect(isGatewayRequestAllowed('POST', read)).toBe(false);
  });

  it('proxies the writes as execution routes that are never run starts', () => {
    for (const path of writes) {
      expect(isGatewayRequestAllowed('POST', path)).toBe(false);
      expect(isRunCreationRequest('POST', path)).toBe(false);
    }
    process.env.GATEWAY_ALLOW_EXECUTION_ROUTES = 'true';
    for (const path of writes) {
      // The run-start flag is NOT needed: capture is not a run.
      expect(isGatewayRequestAllowed('POST', path)).toBe(true);
      expect(isRunCreationRequest('POST', path)).toBe(false);
      expect(isGatewayRequestAllowed('GET', path)).toBe(false);
    }
    expect(isGatewayRequestAllowed('POST', `/projects/${PROJECT}/register/other`)).toBe(false);
    expect(isGatewayRequestAllowed('POST', '/projects/not-a-uuid/register/captures')).toBe(false);
  });
});
