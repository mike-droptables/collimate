import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

// StackApp with the real Commands pane over the full-window view (the
// store's maximizedOverlayCellId): the rest of the app is stubbed down to
// two columns, the first selected.
const fixture = vi.hoisted(() => {
    const stacks = [
        { id: 's1', name: 'One', cells: [{ id: 'c1', name: 'Work', files: [] }] },
        { id: 's2', name: 'Two', cells: [{ id: 'c2', name: 'Elsewhere', files: [] }] },
    ];
    return {
        stacks,
        channel: { id: 'channel-id', name: 'Channel', main_stacks: stacks, nav_stacks: [] },
        shortcuts: {} as Record<string, (e: KeyboardEvent) => void>,
        maximizedWhenSelected: [] as (string | null)[],
        scene: {
            displayStacks: stacks,
            displaySelectedId: 's1',
            visibleTopIndices: { s1: 0, s2: 0 },
            fanRange: null,
            selectDisplayId: (_id: string) => {},
            setFannedOutStackId: () => {},
            actions: { newEmptyStackRight: () => {}, winRotate: () => {}, splitCell: async () => {} },
        },
    };
});
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: () => {},
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({ selectedWindowId: 's1', setSelectedWindowId: () => {}, setFocusArea: () => {} }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: false, toggleZoom: () => {} }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/lib/keybindings/useShortcut', () => ({
    useShortcut: (id: string, handler: (e: KeyboardEvent) => void) => { fixture.shortcuts[id] = handler; },
}));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({ AppTopBar: () => null }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./ChannelCreateView', () => ({ ChannelCreateView: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import StackApp from './StackApp';

const store = () => useCollimateStore.getState();
const press = (id: string) => act(() => fixture.shortcuts[id](new KeyboardEvent('keydown', { key: 'k', ctrlKey: true })));

beforeEach(() => {
    fixture.maximizedWhenSelected = [];
    fixture.scene.selectDisplayId = () => { fixture.maximizedWhenSelected.push(store().maximizedOverlayCellId); };
    useCollimateStore.setState({
        isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null,
        commandPane: defaultCommandPane(), maximizedOverlayCellId: 'c1',
        cellLayouts: {}, runs: {}, stoppingRunIds: new Set(), lastFinishedByCell: {},
        activeRunsEverywhere: [], activeChannel: fixture.channel as never,
    } as never);
});
afterEach(() => {
    cleanup();
    useCollimateStore.setState({ maximizedOverlayCellId: null, commandPane: defaultCommandPane() });
});

it('Mod+K opens the pane over the full-window view, above it and the window\'s height (polish)', () => {
    render(<StackApp />);
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    const pane = screen.getByRole('complementary', { name: 'Commands' });
    expect(pane.parentElement).toBe(document.body);
    expect(pane.className).toMatch(/\bfixed\b/);
    expect(pane.className).toMatch(/\bz-\[455\]/);
    expect(pane.className).toMatch(/\btop-0\b/);
    expect(pane.className).toMatch(/\bbottom-0\b/);
    expect(store().maximizedOverlayCellId).toBe('c1');
});

it('a go-to-cell command from the pane closes the full-window view first, then goes (polish)', () => {
    render(<StackApp />);
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    const row = screen.getAllByRole('option').find(o => o.querySelector('span')!.textContent === 'Elsewhere')!;
    fireEvent.click(row);
    expect(fixture.maximizedWhenSelected).toEqual([null]);
    expect(store().maximizedOverlayCellId).toBeNull();
    // The pane stays open, back in the canvas.
    const pane = screen.getByRole('complementary', { name: 'Commands' });
    expect(pane.className).toMatch(/\babsolute\b/);
    expect(pane.parentElement).not.toBe(document.body);
});
