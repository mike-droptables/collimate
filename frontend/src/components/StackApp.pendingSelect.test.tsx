import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';

// StackApp's pending-select consumer selects the cell's stack and never
// recentres the camera (owner, 2026-10-13). The rest of the app is stubbed
// down to one stack holding the cell.
const fixture = vi.hoisted(() => ({
    channel: { id: 'channel-id', name: 'Channel', nav_stacks: [], main_stacks: [{ id: 's1', name: 'One', stack_type: 'standard', cells: [{ id: 'cell', name: 'Work', files: [] }] }] },
    selected: [] as (string | null)[],
    fronted: [] as [string, number][],
    areas: [] as string[],
    scene: {
        displayStacks: [{ id: 's1', name: 'One', cells: [{ id: 'cell', name: 'Work', files: [] }] }],
        displaySelectedId: 's1',
        visibleTopIndices: { s1: 0 },
        fanRange: null,
        selectDisplayId: () => {},
        setVisibleTopIndices: () => {},
        actions: { newEmptyStackRight: () => {}, winSetTopCell: (sid: string, idx: number) => { fixture.fronted.push([sid, idx]); } },
    },
}));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: () => {},
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({
        selectedWindowId: 's1', setSelectedWindowId: (id: string | null) => { fixture.selected.push(id); },
        setFocusArea: (area: string) => { fixture.areas.push(area); },
    }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: false }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({ AppTopBar: () => null }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
vi.mock('./CommandPane', () => ({ CommandPane: () => null }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./ChannelCreateView', () => ({ ChannelCreateView: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
import StackApp from './StackApp';

beforeEach(() => {
    fixture.selected = [];
    fixture.fronted = [];
    fixture.areas = [];
    useSpatialStore.setState({ centerRequested: 0 });
    useCollimateStore.setState({
        isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null, pendingSelectFocus: false,
        commandPane: { open: false, side: 'left', query: '', recents: [], view: { kind: 'commands' } }, maximizedOverlayCellId: null,
        pickSessions: {}, cellHistory: {}, cellDiffs: {},
    });
});
afterEach(() => { cleanup(); });

it('a selection request selects the cell\'s stack and never recentres the camera (owner, 2026-10-13)', () => {
    render(<StackApp />);
    act(() => { useCollimateStore.getState().requestSelectCell('cell'); });
    expect(fixture.selected).toContain('s1');
    // The cell is fronted by its REAL index (the scene stores its slot).
    expect(fixture.fronted).toEqual([['s1', 0]]);
    expect(useSpatialStore.getState().centerRequested).toBe(0);
    expect(useCollimateStore.getState().pendingSelectCellId).toBeNull();
    // A plain request leaves the focus area where it is.
    expect(fixture.areas).toEqual([]);
});

it('a request with focus also gives the canvas the main focus area, once, before selecting', () => {
    render(<StackApp />);
    act(() => { useCollimateStore.getState().requestSelectCell('cell', { focus: true }); });
    expect(fixture.areas).toEqual(['main']);
    expect(fixture.selected).toContain('s1');
    const state = useCollimateStore.getState();
    expect(state.pendingSelectCellId).toBeNull();
    expect(state.pendingSelectFocus).toBe(false);
});
