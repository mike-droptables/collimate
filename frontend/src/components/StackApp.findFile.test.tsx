import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';

// StackApp's real Mod+P registration (useShortcut on window): the rest of
// the app is stubbed down to one column whose front cell is selected.
const fixture = vi.hoisted(() => ({
    channel: { id: 'channel-id', name: 'Channel', main_stacks: [], nav_stacks: [] },
    zoomedOut: false,
    scene: {
        displayStacks: [{ id: 's1', name: 'One', cells: [{ id: 'cell', name: 'Work', files: [] }] }],
        displaySelectedId: 's1',
        visibleTopIndices: { s1: 0 },
        fanRange: null,
        selectDisplayId: () => {},
        actions: { newEmptyStackRight: () => {} },
    },
}));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: () => {},
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({ selectedWindowId: null, setSelectedWindowId: () => {} }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: fixture.zoomedOut }) }));
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

/** Ctrl+P and Cmd+P on window: one of them is Mod+P on this platform. */
function pressModP() {
    const events = [{ ctrlKey: true }, { metaKey: true }].map(mods => new KeyboardEvent('keydown', { key: 'p', code: 'KeyP', bubbles: true, cancelable: true, ...mods }));
    for (const event of events) window.dispatchEvent(event);
    return events.some(event => event.defaultPrevented);
}

beforeEach(() => {
    fixture.zoomedOut = false;
    useCollimateStore.setState({
        isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null,
        commandPane: { open: false, side: 'left', query: '', recents: [], view: { kind: 'commands' } }, maximizedOverlayCellId: null,
        pickSessions: {}, cellHistory: {}, cellDiffs: {},
    });
});
afterEach(() => { cleanup(); });

it('Mod+P on window opens the Files panel on the selected cell and keeps the print dialog away (P03)', () => {
    render(<StackApp />);
    expect(pressModP()).toBe(true);
    expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
});

it('Mod+P opens the Files panel in the full-window view, which hosts the cell\'s panels (polish)', () => {
    render(<StackApp />);
    useCollimateStore.setState({ maximizedOverlayCellId: 'cell' });
    expect(pressModP()).toBe(true);
    expect(useCollimateStore.getState().pickSessions.cell?.kind).toBe('files');
    expect(useCollimateStore.getState().maximizedOverlayCellId).toBe('cell');
});

it('Mod+P opens nothing in the overview (P03)', () => {
    fixture.zoomedOut = true;
    render(<StackApp />);
    expect(pressModP()).toBe(true);
    expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
});

it('Mod+P opens nothing while Capabilities shows its manage card, which is no cell', () => {
    const scene = fixture.scene as unknown as { displayStacks: unknown[]; displaySelectedId: string; visibleTopIndices: Record<string, number> };
    const before = { ...scene };
    scene.displayStacks = [{ id: 'caps', name: 'Capabilities', stack_type: 'capabilities', cells: [{ id: 'cell', name: 'Work', files: [] }] }];
    scene.displaySelectedId = 'caps';
    scene.visibleTopIndices = { caps: 0 };
    try {
        render(<StackApp />);
        expect(pressModP()).toBe(true);
        expect(useCollimateStore.getState().pickSessions.cell).toBeUndefined();
    } finally { Object.assign(scene, before); }
});
