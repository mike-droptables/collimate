// A generator-fingerprint change reloads the runner catalog once, however
// many useRunnerCatalog consumers are mounted.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

vi.mock('@/lib/runnerApi', () => ({ fetchRunners: vi.fn(async () => []) }));

import { fetchRunners } from '@/lib/runnerApi';
import { useCollimateStore } from './useCollimateStore';
import { useRunnerCatalog, useRunnerStore } from './useRunnerStore';
import type { Channel } from '@/lib/types';

describe('useRunnerCatalog', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        useRunnerStore.setState({ channel: '', fingerprint: '', data: null, busy: false, error: null });
        useCollimateStore.setState({ activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'Shared label' } as Channel, _capabilitiesFingerprint: 'fp1' });
    });

    it('fetches once per fingerprint change across several mounted consumers', async () => {
        renderHook(() => useRunnerCatalog());
        renderHook(() => useRunnerCatalog());
        await act(async () => {});
        expect(fetchRunners).toHaveBeenCalledTimes(1);
        await act(async () => { useCollimateStore.setState({ _capabilitiesFingerprint: 'fp2' }); });
        expect(fetchRunners).toHaveBeenCalledTimes(2);
    });
    it('same-label channels have independent catalog keys and reject late results', async () => {
        let first!: (value: Awaited<ReturnType<typeof fetchRunners>>) => void;
        vi.mocked(fetchRunners).mockImplementationOnce(() => new Promise(resolve => { first = resolve; }));
        vi.mocked(fetchRunners).mockResolvedValueOnce([]);
        const { result } = renderHook(() => useRunnerCatalog());
        await act(async () => {});
        await act(async () => { useCollimateStore.setState({ activeChannel: { id: '22222222-2222-4222-8222-222222222222', name: 'Shared label' } as Channel }); });
        expect(fetchRunners).toHaveBeenCalledTimes(2);
        expect(result.current.channel).toBe('22222222-2222-4222-8222-222222222222');
        const data = result.current.data;
        await act(async () => { first([{ id: 'old', name: 'Old catalog' } as never]); });
        expect(result.current.data).toBe(data);
        await act(async () => { useCollimateStore.setState({ activeChannel: { id: '22222222-2222-4222-8222-222222222222', name: 'Renamed' } as Channel }); });
        expect(fetchRunners).toHaveBeenCalledTimes(2);
    });

});
