import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { useRunRealtime } from '../lib/useRunRealtime';

vi.mock('../lib/api', () => ({
  api: {
    run: vi.fn(() => Promise.reject(new Error('disabled'))),
    events: vi.fn(() => Promise.reject(new Error('disabled'))),
  },
}));

describe('authenticated Realtime hardening', () => {
  it('keeps Realtime disabled until an authenticated channel is implemented', async () => {
    const { result } = renderHook(() => useRunRealtime('11111111-1111-4111-8111-111111111111'));
    await waitFor(() => expect(result.current.mode).toBe('polling'));
  });
});
