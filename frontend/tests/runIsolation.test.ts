import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useRunRealtime } from '../lib/useRunRealtime';
import { getTrustedClientIp } from '../lib/server/rateLimit';

const apiMocks = vi.hoisted(() => ({
  run: vi.fn(),
  events: vi.fn(),
}));

vi.mock('../lib/api', () => ({ api: apiMocks }));

const RUN_A = '11111111-1111-4111-8111-00000000000a';
const RUN_B = '11111111-1111-4111-8111-00000000000b';

function runPayload(id: string, status = 'running') {
  return { id, conversation_id: 'c', status };
}

function eventPayload(id: number, runId: string, message: string) {
  return { id, run_id: runId, event_type: 'agent_progress', message, payload: { tokens: 10, cost_usd: 0.5 } };
}

describe('run state isolation', () => {
  beforeEach(() => {
    apiMocks.run.mockReset();
    apiMocks.events.mockReset();
  });

  it('resets events, costs and tokens when switching to another run', async () => {
    apiMocks.run.mockImplementation(async (id: string) => runPayload(id));
    apiMocks.events.mockImplementation(async (id: string) =>
      id === RUN_A ? [eventPayload(1, RUN_A, 'from run A')] : [],
    );
    const { result, rerender } = renderHook(({ runId }) => useRunRealtime(runId), {
      initialProps: { runId: RUN_A },
    });
    await waitFor(() => expect(result.current.state.events).toHaveLength(1));
    expect(result.current.state.tokens).toBe(10);
    expect(result.current.state.cost).toBe(0.5);

    rerender({ runId: RUN_B });
    await waitFor(() => expect(result.current.state.run?.id).toBe(RUN_B));
    expect(result.current.state.events).toHaveLength(0);
    expect(result.current.state.tokens).toBe(0);
    expect(result.current.state.cost).toBe(0);
    expect(result.current.state.agents).toEqual({});
    expect(result.current.state.sources).toEqual([]);
  });

  it('ignores a late response from the previous run', async () => {
    let releaseRunA: (value: unknown) => void = () => {};
    const pendingRunA = new Promise((resolve) => {
      releaseRunA = resolve;
    });
    apiMocks.run.mockImplementation(async (id: string) => {
      if (id === RUN_A) {
        await pendingRunA; // run A's response arrives late
        return runPayload(RUN_A, 'completed');
      }
      return runPayload(RUN_B);
    });
    apiMocks.events.mockResolvedValue([]);

    const { result, rerender } = renderHook(({ runId }) => useRunRealtime(runId), {
      initialProps: { runId: RUN_A },
    });
    rerender({ runId: RUN_B });
    await waitFor(() => expect(result.current.state.run?.id).toBe(RUN_B));
    releaseRunA(undefined); // late A response must be dropped
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(result.current.state.run?.id).toBe(RUN_B);
    expect(result.current.mode).not.toBe('terminal'); // A's terminal state ignored
  });

  it('clears all state on sign-out (runId becomes undefined)', async () => {
    apiMocks.run.mockImplementation(async (id: string) => runPayload(id));
    apiMocks.events.mockImplementation(async () => [eventPayload(2, RUN_A, 'x')]);
    const { result, rerender } = renderHook(({ runId }: { runId?: string }) => useRunRealtime(runId), {
      initialProps: { runId: RUN_A as string | undefined },
    });
    await waitFor(() => expect(result.current.state.events).toHaveLength(1));
    rerender({ runId: undefined });
    await waitFor(() => expect(result.current.mode).toBe('idle'));
    expect(result.current.state.events).toHaveLength(0);
    expect(result.current.state.run).toBeUndefined();
  });

  it('stops polling on every terminal state', async () => {
    for (const status of ['completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted']) {
      apiMocks.run.mockResolvedValue(runPayload(RUN_A, status));
      apiMocks.events.mockResolvedValue([]);
      const { result, unmount } = renderHook(() => useRunRealtime(RUN_A));
      await waitFor(() => expect(result.current.mode).toBe('terminal'));
      unmount();
    }
  });
});

describe('trusted client IP derivation', () => {
  function headers(map: Record<string, string>) {
    return { get: (name: string) => map[name.toLowerCase()] ?? null };
  }

  it('prefers platform-set headers over x-forwarded-for', () => {
    expect(
      getTrustedClientIp(headers({ 'x-real-ip': '203.0.113.9', 'x-forwarded-for': '6.6.6.6' })),
    ).toBe('203.0.113.9');
    expect(
      getTrustedClientIp(headers({ 'x-vercel-forwarded-for': '198.51.100.3', 'x-forwarded-for': '6.6.6.6' })),
    ).toBe('198.51.100.3');
  });

  it('falls back to a validated x-forwarded-for entry', () => {
    expect(getTrustedClientIp(headers({ 'x-forwarded-for': '203.0.113.7, 10.0.0.1' }))).toBe('203.0.113.7');
  });

  it('collapses malformed and spoof-shaped values into one bucket', () => {
    expect(getTrustedClientIp(headers({ 'x-forwarded-for': 'evil"header' }))).toBe('invalid-ip');
    expect(getTrustedClientIp(headers({ 'x-forwarded-for': '999.999.999.999' }))).toBe('invalid-ip');
    expect(getTrustedClientIp(headers({ 'x-real-ip': 'a'.repeat(100) }))).toBe('invalid-ip');
    expect(getTrustedClientIp(headers({}))).toBe('invalid-ip');
  });

  it('accepts valid IPv6', () => {
    expect(getTrustedClientIp(headers({ 'x-real-ip': '2001:db8::1' }))).toBe('2001:db8::1');
  });
});
