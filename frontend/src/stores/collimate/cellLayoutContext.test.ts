// @vitest-environment jsdom
import 'fake-indexeddb/auto';
import { beforeEach, expect, it, vi } from 'vitest';
vi.mock('@/lib/api/client', async (original) => ({ ...await original<Record<string, unknown>>(), requestJson: vi.fn() }));
import { requestJson } from '@/lib/api/client';
import { useCollimateStore } from '../useCollimateStore';
import { cellLayoutContext } from './layoutSlice';
import { encodeCellDisplay } from '@/lib/cellDisplay';
import { allTabs, createDefaultLayout } from '@/lib/cellLayout';
import { revealCellFile } from '@/lib/revealFile';
import { fileFixture } from '@/test/fileFixtures';
import type { Cell, Channel } from '@/lib/types';

const channelId = '11111111-1111-4111-8111-111111111111';
const capsCell: Cell = { id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', name: 'Kept', files: [fileFixture('a.md', 'A')], pinned: 'gone.md' };
const workCell: Cell = { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', name: 'Work', files: [fileFixture('b.md', 'B')], pinned: 'b.md' };
const hiddenCell: Cell = { id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', name: 'Hidden', files: [] };
const channel = (capsShown: boolean): Channel => {
    const caps = { id: 'caps', stack_type: 'capabilities' as const, name: 'Capabilities', cells: [capsCell, hiddenCell] };
    const work = { id: 'work', stack_type: 'standard' as const, name: '', cells: [workCell] };
    return { id: channelId, name: 'Ctx', main_stacks: capsShown ? [caps, work] : [work], nav_stacks: capsShown ? [] : [caps], nav_selection_id: null, main_selection_id: 'work' };
};

beforeEach(() => {
    vi.clearAllMocks();
    useCollimateStore.setState({ state: null, activeChannel: null, cellCache: {}, runs: {}, cellLayouts: {}, cellHistory: {}, _cellDisplayEdits: {}, _cellDisplayValues: {}, _cellDisplayFetch: 0, cellDisplaysReady: false, cellDisplayErrors: {}, cellDisplayLoadError: null });
    useCollimateStore.getState().applyServerState({ active_channel_id: channelId, channels: [channel(true)] });
});

it('a Capabilities cell\'s context asks for the Manage tab, shown or hidden; any other cell\'s does not', () => {
    expect(cellLayoutContext(useCollimateStore.getState(), capsCell.id)).toEqual({ filePaths: ['a.md'], pinnedPath: null, manageTab: true });
    expect(cellLayoutContext(useCollimateStore.getState(), workCell.id)).toEqual({ filePaths: ['b.md'], pinnedPath: 'b.md', manageTab: false });
    useCollimateStore.getState().applyServerState({ active_channel_id: channelId, channels: [channel(false)] });
    expect(cellLayoutContext(useCollimateStore.getState(), hiddenCell.id)?.manageTab).toBe(true);
    expect(cellLayoutContext(useCollimateStore.getState(), 'no-such-cell')).toBeNull();
});

it('hydration gives a stored Capabilities display its Manage tab and leaves other cells without one', async () => {
    const stored = encodeCellDisplay(createDefaultLayout({ filePaths: ['a.md'], pinnedPath: null }), null);
    const work = encodeCellDisplay(createDefaultLayout({ filePaths: ['b.md'], pinnedPath: 'b.md' }), null);
    vi.mocked(requestJson).mockResolvedValue({ [capsCell.id]: stored, [workCell.id]: work });
    await useCollimateStore.getState().hydrateCellDisplays();
    const layouts = useCollimateStore.getState().cellLayouts;
    expect(allTabs(layouts[capsCell.id]).map(t => t.kind)).toEqual(['manage', 'empty']);
    expect(allTabs(layouts[workCell.id]).map(t => t.kind)).toEqual(['file']);
});

it('a reveal into a Capabilities cell with no layout yet seeds the Manage tab and opens the file beside it', () => {
    useCollimateStore.setState({ requestSelectCell: vi.fn() } as never);
    revealCellFile(capsCell.id, 'a.md');
    const tabs = allTabs(useCollimateStore.getState().cellLayouts[capsCell.id]);
    expect(tabs.map(t => (t.kind === 'file' ? t.path : t.kind))).toEqual(['manage', 'a.md']);
});
