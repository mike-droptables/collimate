// @vitest-environment jsdom
//
// Pick-session writes for one cell reach the server in call order: a close
// right after an open must not be overtaken by the open's save.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<Record<string, unknown>>()),
    savePickSession: vi.fn(),
    deletePickSession: vi.fn(async () => {}),
}));

import * as api from '@/lib/api';
import { useCollimateStore } from './useCollimateStore';
import type { Channel } from '@/lib/types';

const A = '11111111-1111-4111-8111-111111111111', B = '22222222-2222-4222-8222-222222222222';
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

describe('useCollimateStore — pick-session write order', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        vi.clearAllMocks();
        useCollimateStore.setState({
            pickSessions: {},
            cellHistory: {},
            activeChannel: { id: A, name: 'Shared label' } as Channel,
        });
    });

    afterEach(async () => { await vi.runOnlyPendingTimersAsync(); vi.useRealTimers(); });

    it('sends the DELETE of a close only after the open\'s PUT settles (P01)', async () => {
        let resolveSave!: () => void;
        vi.mocked(api.savePickSession).mockImplementation(
            () => new Promise<void>((r) => { resolveSave = r; }));
        useCollimateStore.getState().openPickSession('c1', 'runners');
        useCollimateStore.getState().closePickSession('c1', 'runners');
        await flush();
        expect(api.savePickSession).toHaveBeenCalledTimes(1);
        expect(api.deletePickSession).not.toHaveBeenCalled();
        resolveSave();
        await flush();
        expect(api.deletePickSession).toHaveBeenCalledTimes(1);
        expect(api.deletePickSession).toHaveBeenCalledWith(A, 'c1');
    });

    it('a failed save still lets the close go out (P01)', async () => {
        vi.mocked(api.savePickSession).mockRejectedValue(new Error('offline'));
        useCollimateStore.getState().openPickSession('c1', 'runners');
        useCollimateStore.getState().closePickSession('c1', 'runners');
        await flush();
        expect(api.deletePickSession).toHaveBeenCalledWith(A, 'c1');
    });
    it('debounced same-cell edits in same-label channels keep exact UUID targets', async () => {
        vi.mocked(api.savePickSession).mockResolvedValue(undefined);
        const store = () => useCollimateStore.getState();
        store().openPickSession('shared-cell', 'runners');
        store().updatePickSession('shared-cell', 'runners', { query: 'from A' });
        await flush();
        useCollimateStore.setState({ activeChannel: { id: A, name: 'Renamed' } as Channel });
        useCollimateStore.setState({ activeChannel: { id: B, name: 'Shared label' } as Channel, pickSessions: {} });
        store().openPickSession('shared-cell', 'runners');
        store().updatePickSession('shared-cell', 'runners', { query: 'from B' });
        await vi.advanceTimersByTimeAsync(400);
        await flush();
        expect(api.savePickSession).toHaveBeenCalledWith(A, 'shared-cell', expect.objectContaining({ query: 'from A' }));
        expect(api.savePickSession).toHaveBeenCalledWith(B, 'shared-cell', expect.objectContaining({ query: 'from B' }));
        expect(vi.mocked(api.savePickSession).mock.calls.every(([id]) => id === A || id === B)).toBe(true);
    });

    it('closed markers and late hydration do not cross channel UUIDs', async () => {
        vi.mocked(api.savePickSession).mockResolvedValue(undefined);
        const store = () => useCollimateStore.getState();
        store().openPickSession('shared-cell', 'runners');
        store().closePickSession('shared-cell', 'runners');
        useCollimateStore.setState({ activeChannel: { id: B, name: 'Shared label' } as Channel, pickSessions: {} });
        store().hydratePickSessions(A, { 'shared-cell': { kind: 'runners', query: 'late A', at: Date.now() + 1 } });
        expect(store().pickSessions).toEqual({});
        store().hydratePickSessions(B, { 'shared-cell': { kind: 'runners', query: 'B', at: 1 } });
        expect(store().pickSessions['shared-cell'].query).toBe('B');
        await flush();
        expect(api.deletePickSession).toHaveBeenCalledWith(A, 'shared-cell');
    });

});
