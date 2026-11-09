import { create } from 'zustand';
import { useEffect } from 'react';
import { fetchRunners, type RunnerEntry } from '@/lib/runnerApi';
import { useCollimateStore } from './useCollimateStore';

interface RunnerState {
    channel: string; fingerprint: string; data: RunnerEntry[] | null; busy: boolean; error: string | null;
    load: (channel: string, fingerprint: string, force?: boolean) => Promise<void>;
}
let request = 0;
export const useRunnerStore = create<RunnerState>((set, get) => ({
    channel: '', fingerprint: '', data: null, busy: false, error: null,
    load: async (channel, fingerprint, force = false) => {
        if (!channel) return;
        const prev = get();
        // Keyed on (channel, Capabilities fingerprint) in the store, not per hook
        // instance, so N mounted catalogs send one fetch per fingerprint change.
        if (!force && prev.channel === channel && prev.fingerprint === fingerprint && (prev.data !== null || prev.busy)) return;
        const token = ++request;
        set({ channel, fingerprint, busy: true, error: null, data: prev.channel === channel ? prev.data : null });
        try { const data = await fetchRunners(); if (token === request) set({ data, busy: false }); }
        catch (e) { if (token === request) set({ busy: false, error: e instanceof Error ? e.message : 'Could not load runners' }); }
    },
}));

export function useRunnerCatalog() {
    const channel = useCollimateStore(s => s.activeChannel?.id ?? '');
    const fingerprint = useCollimateStore(s => s._capabilitiesFingerprint);
    const state = useRunnerStore();
    useEffect(() => {
        if (channel) void useRunnerStore.getState().load(channel, fingerprint);
    }, [channel, fingerprint]);
    return { ...state, data: state.channel === channel ? state.data : null, refresh: () => state.load(channel, fingerprint, true) };
}
