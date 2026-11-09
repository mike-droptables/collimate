import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { MOVE_POLL_MS, useStorage } from './useStorage';
import { fetchLocations, fetchManagedChannels, fetchStorageJob, moveChannel, type ManagedChannel, type MoveJob } from '@/lib/api/storage';

vi.mock('@/lib/api/storage', async importOriginal => ({
    ...await importOriginal<typeof import('@/lib/api/storage')>(),
    fetchManagedChannels: vi.fn(), fetchLocations: vi.fn(), moveChannel: vi.fn(), fetchStorageJob: vi.fn(),
}));

const row = (id: string): ManagedChannel => ({ id, name: id, archived: false, location_id: 'l', path: `/x/${id}`, state: 'ok', active: false });
const job = (over: Partial<MoveJob>): MoveJob => ({
    id: 'j', kind: 'move', channel_id: 'a', from_location_id: 'l', to_location_id: 'm', state: 'copying', method: 'copy',
    bytes_done: 0, bytes_total: 10, files_done: 0, files_total: 1, started_at: 'now', finished_at: null, ...over,
});
beforeEach(() => vi.mocked(fetchLocations).mockResolvedValue({ locations: [] }));
afterEach(() => { vi.clearAllMocks(); vi.useRealTimers(); });

describe('useStorage (plan 1105 P8): the locations and the move job', () => {
    it('reads the locations beside the listing, on mount and on refresh()', async () => {
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('a')] });
        vi.mocked(fetchLocations).mockRejectedValueOnce(new Error('down')).mockResolvedValue({ locations: [] });
        const { result } = renderHook(() => useStorage());
        await waitFor(() => expect(result.current.locations).toEqual({ list: null, error: 'down', loading: false }));
        await act(() => result.current.refresh());
        expect(result.current.locations).toEqual({ list: [], error: null, loading: false });
        expect(fetchManagedChannels).toHaveBeenCalledTimes(2);
    });

    it('startMove resolves once the job is taken, polls it every 500 ms until it ends, then reads the lists again', async () => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('a')] });
        vi.mocked(moveChannel).mockResolvedValue({ job_id: 'j' });
        vi.mocked(fetchStorageJob).mockResolvedValueOnce(job({ bytes_done: 5 })).mockResolvedValueOnce(job({ state: 'done', bytes_done: 10 }));
        const { result } = renderHook(() => useStorage());
        await waitFor(() => expect(result.current.manage.channels).not.toBeNull());
        await act(() => result.current.startMove({ id: 'a', name: 'A' }, 'm'));
        expect(moveChannel).toHaveBeenCalledWith('a', 'm');
        expect(result.current.move).toMatchObject({ channelId: 'a', toLocationId: 'm', job: null });
        expect(fetchStorageJob).not.toHaveBeenCalled();
        await act(() => vi.advanceTimersByTimeAsync(MOVE_POLL_MS));
        expect(result.current.move?.job?.bytes_done).toBe(5);
        await act(() => vi.advanceTimersByTimeAsync(MOVE_POLL_MS));
        expect(result.current.move?.job?.state).toBe('done');
        await waitFor(() => expect(fetchLocations).toHaveBeenCalledTimes(2));
        act(() => result.current.dismissMove());
        expect(result.current.move).toBeNull();
    });

    it('a refused move rejects and starts nothing; a job that cannot be read stops with why', async () => {
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('a')] });
        vi.mocked(moveChannel).mockRejectedValueOnce(new Error('refused')).mockResolvedValue({ job_id: 'j' });
        vi.mocked(fetchStorageJob).mockRejectedValue(new Error('No such job'));
        const { result } = renderHook(() => useStorage());
        await expect(result.current.startMove({ id: 'a', name: 'A' }, 'm')).rejects.toThrow('refused');
        expect(result.current.move).toBeNull();
        await act(() => result.current.startMove({ id: 'a', name: 'A' }, 'm'));
        await waitFor(() => expect(result.current.move?.error).toBe('No such job'), { timeout: 2000 });
    });
});

describe('useStorage (plan 1105 P7)', () => {
    it('reads the manage listing on mount, again when the revision changes and on refresh()', async () => {
        vi.mocked(fetchManagedChannels).mockResolvedValue({ channels: [row('a')] });
        const { result, rerender } = renderHook(({ revision }) => useStorage(revision), { initialProps: { revision: 1 } });
        expect(result.current.manage).toMatchObject({ channels: null, loading: true });
        await waitFor(() => expect(result.current.manage).toEqual({ channels: [row('a')], error: null, loading: false }));
        rerender({ revision: 2 });
        await waitFor(() => expect(fetchManagedChannels).toHaveBeenCalledTimes(2));
        await act(() => result.current.refresh());
        expect(fetchManagedChannels).toHaveBeenCalledTimes(3);
    });

    it('keeps the last listing and says why when a read fails; only the latest answer lands', async () => {
        let resolveSlow!: (value: { channels: ManagedChannel[] }) => void;
        vi.mocked(fetchManagedChannels)
            .mockResolvedValueOnce({ channels: [row('a')] })
            .mockRejectedValueOnce(new Error('offline'))
            .mockReturnValueOnce(new Promise(resolve => { resolveSlow = resolve; }))
            .mockResolvedValueOnce({ channels: [row('c')] });
        const { result } = renderHook(() => useStorage());
        await waitFor(() => expect(result.current.manage.channels).toEqual([row('a')]));
        await act(() => result.current.refresh());
        expect(result.current.manage).toEqual({ channels: [row('a')], error: 'offline', loading: false });
        let slow!: Promise<void>;
        act(() => { slow = result.current.refresh(); });
        await act(() => result.current.refresh());
        expect(result.current.manage.channels).toEqual([row('c')]);
        await act(async () => { resolveSlow({ channels: [row('b')] }); await slow; });
        expect(result.current.manage.channels).toEqual([row('c')]);
    });
});
