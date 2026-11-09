// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { activeTab } from '@/lib/cellLayout';
import { fileFixture } from '@/test/fileFixtures';
import type { Cell, Channel, ChannelLayout } from '@/lib/types';
import type { SpawnResult } from '@/lib/api/capabilitiesStack';
import { openSpawnedCell } from './capabilitySpawn';

const CH = '11111111-1111-4111-8111-111111111111';
const caps = { id: 'k', name: 'Capabilities', stack_type: 'capabilities' as const, cells: [] as Cell[] };
const channel = (main: Channel['main_stacks']): Channel =>
    ({ id: CH, name: 'ch', nav_stacks: [], main_stacks: main, nav_selection_id: null, main_selection_id: 'k' });
const workCell = (files: Cell['files']): Cell => ({ id: 'work-cell', name: 'Build ‘Memo’', files, pinned: 'brief.md' });

const spawn = (): SpawnResult => {
    const layout: ChannelLayout = {
        ...channel([]),
        main_stacks: [caps, { id: 'work', name: '', cells: [{ id: 'work-cell', name: 'Build ‘Memo’', pinned: 'brief.md' }] }],
    };
    return { ...layout, stack_id: 'work', cell_id: 'work-cell', brief_path: 'brief.md' };
};

beforeEach(() => {
    const ch = channel([caps]);
    useCollimateStore.setState({
        state: { active_channel_id: CH, channels: [ch] }, activeChannel: ch,
        cellCache: {}, cellLayouts: {}, cellHistory: {}, pickSessions: {}, pendingSelectCellId: null,
    } as never);
});

describe('openSpawnedCell', () => {
    it('applies the layout, selects the work cell and opens the runners panel', () => {
        openSpawnedCell(spawn());
        const state = useCollimateStore.getState();
        expect(state.activeChannel?.main_stacks.map(s => s.id)).toEqual(['k', 'work']);
        expect(state.pendingSelectCellId).toBe('work-cell');
        expect(state.pickSessions['work-cell']?.kind).toBe('runners');
    });

    it('opens the brief as the active tab once the cell\'s files arrive', () => {
        openSpawnedCell(spawn());
        expect(useCollimateStore.getState().cellLayouts['work-cell']).toBeUndefined();
        useCollimateStore.getState().applyServerState({
            active_channel_id: CH,
            channels: [channel([caps, { id: 'work', name: '', cells: [workCell([fileFixture('brief.md', '# Brief'), fileFixture('memo/memo.handler', 'id: memo')])] }])],
        });
        const tab = activeTab(useCollimateStore.getState().cellLayouts['work-cell']);
        expect(tab?.kind === 'file' && tab.path).toBe('brief.md');
    });

    it('drops the wait when the channel switches first', () => {
        openSpawnedCell(spawn());
        const other = { ...channel([]), id: '22222222-2222-4222-8222-222222222222' };
        useCollimateStore.getState().applyServerState({ active_channel_id: other.id, channels: [channel([caps]), other] });
        useCollimateStore.getState().applyServerState({
            active_channel_id: CH,
            channels: [channel([caps, { id: 'work', name: '', cells: [workCell([fileFixture('brief.md', '# Brief')])] }]), other],
        });
        expect(useCollimateStore.getState().cellLayouts['work-cell']).toBeUndefined();
    });
});
