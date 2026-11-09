import { beforeEach, expect, it } from 'vitest';
import { useCollimateStore } from './useCollimateStore';
import type { Channel, PickSession, RunRecord } from '@/lib/types';

const channel = (id: string): Channel => ({
    id, name: id, nav_stacks: [], nav_selection_id: null, main_selection_id: null,
    main_stacks: [{ id: `stack-${id}`, name: '', cells: [{ id: `cell-${id}`, name: id, files: [] }] }],
});

beforeEach(() => useCollimateStore.setState({
    state: null, activeChannel: null, cellCache: {}, cellDiffs: {}, pickSessions: {},
    confirmDialog: null,
    _capabilitiesFingerprint: '', runs: {}, endedRuns: {},
    pendingReveals: {},
}));

it('publishes channel, cache and invalidations atomically while preserving global runs __F0509', () => {
    const a = channel('a');
    const b = channel('b');
    const store = useCollimateStore.getState();
    store.applyServerState({ active_channel_id: 'a', channels: [a, b] });
    const run = { runId: 'run-a', cellId: 'cell-a', channel: 'a' } as RunRecord;
    useCollimateStore.setState({
        runs: { 'run-a': run }, cellDiffs: { 'cell-a': 'cell-b' },
        pickSessions: { 'cell-a': { at: 1 } as PickSession },
    });
    const observed: ReturnType<typeof useCollimateStore.getState>[] = [];
    const unsubscribe = useCollimateStore.subscribe(s => s.activeChannel?.id, () => {
        observed.push(useCollimateStore.getState());
    });
    try {
        store.applyServerState({ active_channel_id: 'b', channels: [a, b] });
    } finally { unsubscribe(); }
    expect(observed).toHaveLength(1);
    expect(observed[0].activeChannel?.id).toBe('b');
    expect(observed[0].cellCache['cell-b'].id).toBe('cell-b');
    expect(observed[0].pickSessions).toEqual({});
    expect(observed[0].cellDiffs).toEqual({});
    expect(observed[0].runs['run-a']).toBe(run);
});

it('settles replaced dialogs with each existing cancel type and keeps only the current resolver __F0509', async () => {
    const store = useCollimateStore.getState();
    const first = store.openChoice({ title: 'first', options: [{ id: 'yes', label: 'Yes' }] });
    const confirm = store.openConfirm({ title: 'second', body: '' });
    expect(await first).toBeNull();
    const choice = store.openChoice({ title: 'third', options: [{ id: 'yes', label: 'Yes' }], body: '' });
    expect(await confirm).toBe(false);
    store.resolveChoice('yes');
    expect(await choice).toBe('yes');
    expect(useCollimateStore.getState().confirmDialog).toBeNull();
});
