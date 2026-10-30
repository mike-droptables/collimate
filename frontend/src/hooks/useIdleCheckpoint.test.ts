import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';

vi.mock('@/lib/api', () => ({ checkpointCell: vi.fn() }));

import { checkpointCell } from '@/lib/api';
import { useIdleCheckpoint } from './useIdleCheckpoint';

describe('useIdleCheckpoint', () => {
    it('stays dirty after a failed checkpoint so the next flush retries', async () => {
        const cp = vi.mocked(checkpointCell);
        cp.mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ created: true } as never);
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        const onCp = vi.fn();
        const { result, unmount } = renderHook(() => useIdleCheckpoint('ch', 'c1', true, onCp));
        act(() => result.current.notifyEdit());
        await act(async () => { result.current.flushCheckpoint(); });
        expect(cp).toHaveBeenCalledTimes(1);
        expect(onCp).not.toHaveBeenCalled();
        await act(async () => { result.current.flushCheckpoint(); });
        expect(cp).toHaveBeenCalledTimes(2);
        expect(onCp).toHaveBeenCalledTimes(1);
        unmount();
        warn.mockRestore();
    });
});
