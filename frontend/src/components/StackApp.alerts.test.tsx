import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

const fixture = vi.hoisted(() => ({
    channel: { id: 'channel-id', name: 'Channel', main_stacks: [], nav_stacks: [] },
    refresh: vi.fn(),
    barProps: null as null | Record<string, unknown>,
    shortcuts: {} as Record<string, (e: KeyboardEvent) => void>,
    scene: {
        displayStacks: [{ id: 's1', name: 'One', cells: [] }],
        displaySelectedId: 's1',
        visibleTopIndices: { s1: 0 },
        fanRange: { name: 'Fanned' },
        selectDisplayId: vi.fn(),
        actions: { newEmptyStackRight: vi.fn() },
    },
}));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: fixture.refresh,
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({ selectedWindowId: null, setSelectedWindowId: vi.fn() }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: false }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/lib/keybindings/useShortcut', () => ({
    useShortcut: (id: string, handler: (e: KeyboardEvent) => void) => { fixture.shortcuts[id] = handler; },
}));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({ AppTopBar: (props: {
    onRenameChannel: (name: string) => Promise<void>; onDeleteChannel: () => Promise<void>; onOpenSettings: () => void;
}) => {
    fixture.barProps = props;
    return <div data-testid="top-bar"><button onClick={() => void props.onRenameChannel('New name')}>Rename channel</button>
        <button onClick={() => void props.onDeleteChannel()}>Delete channel</button>
        <button onClick={() => props.onOpenSettings()}>Settings…</button></div>;
} }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
// The settings sections, as the Commands pane's settings view loads them.
vi.mock('@/components/settings/KeyManager', () => ({ KeyManager: () => <div>keys body</div> }));
vi.mock('@/components/settings/EditingSection', () => ({ EditingSection: () => <div>editing body</div> }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./ChannelCreateView', () => ({ ChannelCreateView: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
vi.mock('@/lib/api', async (original) => ({
    ...await original<typeof import('@/lib/api')>(), renameChannel: vi.fn(), deleteChannel: vi.fn(),
}));
import * as api from '@/lib/api';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import StackApp from './StackApp';

beforeEach(() => {
    vi.clearAllMocks();
    fixture.barProps = null;
    useCollimateStore.setState({ isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null, commandPane: defaultCommandPane(), maximizedOverlayCellId: null });
});
afterEach(() => {
    cleanup();
    useCollimateStore.getState().resolveConfirm(false);
});

it.each(['Rename', 'Delete'] as const)('%s refusal opens the server 409 detail alert __P0F27_F0318', async operation => {
    const detail = `${operation} refused: this channel is still in use`;
    const error = Object.assign(new Error('Request failed with status code 409'), {
        response: { status: 409, data: { detail } },
    });
    vi.mocked(operation === 'Rename' ? api.renameChannel : api.deleteChannel).mockRejectedValueOnce(error);
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: `${operation} channel` }));
    await waitFor(() => expect(useCollimateStore.getState().confirmDialog).toMatchObject({
        kind: 'alert', title: `${operation} failed`, body: detail,
    }));
    expect(screen.getByText(detail)).toBeVisible();
    expect(screen.getByText(`${operation} failed`)).toBeVisible();
    expect(fixture.refresh).not.toHaveBeenCalled();
});

it.each(['Rename', 'Delete'] as const)('%s success refreshes without opening an alert __P0F27', async operation => {
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: `${operation} channel` }));
    await waitFor(() => expect(fixture.refresh).toHaveBeenCalledOnce());
    if (operation === 'Rename') expect(api.renameChannel).toHaveBeenCalledWith('channel-id', 'New name');
    else expect(api.deleteChannel).toHaveBeenCalledWith('channel-id');
    expect(useCollimateStore.getState().confirmDialog).toBeNull();
});

it('mounts the top bar in the page flow above the scene, fed the display columns (C01)', () => {
    render(<StackApp />);
    const slot = screen.getByTestId('top-bar').parentElement!;
    expect(slot.className.split(/\s+/)).toEqual(expect.arrayContaining(['relative', 'flex-none']));
    expect(slot.className).not.toMatch(/\babsolute\b|\binset-0\b/);
    // Bar, then the scene region filling the rest of the column.
    const column = slot.parentElement!;
    expect(column.className.split(/\s+/)).toEqual(expect.arrayContaining(['flex', 'flex-col']));
    expect(column.children[1].className.split(/\s+/)).toEqual(expect.arrayContaining(['relative', 'flex-1', 'min-h-0']));
    const props = fixture.barProps!;
    expect(props.columns).toBe(fixture.scene.displayStacks);
    expect(props.selectedColumnId).toBe('s1');
    expect(props.fanName).toBe('Fanned');
    expect(props.onSelectColumn).toBe(fixture.scene.selectDisplayId);
    for (const gone of ['isHistoryView', 'toggleHistoryView', 'onOpenHistoryView', 'onSearchCells', 'onNewCellRun', 'mainStacks', 'fanNav']) {
        expect(props).not.toHaveProperty(gone);
    }
});

const pane = () => document.querySelector<HTMLElement>('[data-command-pane]');

it('the channel menu\'s Settings… opens the Commands pane on the settings root (C02, CP-3)', () => {
    render(<StackApp />);
    expect(pane()).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Settings…' }));
    expect(pane()).toHaveAttribute('data-pane-view', 'settings');
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Key manager' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Settings' })).toBeNull();
});

it('Mod+, opens the pane on the settings root whatever it showed; Mod+K closes it from there (CP-3)', async () => {
    render(<StackApp />);
    const press = (id: string) => act(() => fixture.shortcuts[id](new KeyboardEvent('keydown', { key: ',', ctrlKey: true })));
    press(SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS);
    expect(pane()).toHaveAttribute('data-pane-view', 'settings');
    fireEvent.click(screen.getByRole('button', { name: 'Themes' }));
    expect(await screen.findByText('theme body')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Themes' })).toBeInTheDocument();
    press(SHORTCUT_IDS.GLOBAL_OPEN_SETTINGS);
    expect(useCollimateStore.getState().commandPane.view).toEqual({ kind: 'settings', section: null });
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    expect(pane()).toBeNull();
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    expect(pane()).toHaveAttribute('data-pane-view', 'commands');
});

it('/ from the settings view goes back to the commands (CP-3)', () => {
    render(<StackApp />);
    act(() => useCollimateStore.getState().openCommandPaneSettings('editing'));
    act(() => fixture.shortcuts[SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS](new KeyboardEvent('keydown', { key: '/' })));
    expect(pane()).toHaveAttribute('data-pane-view', 'commands');
    expect(document.activeElement).toBe(screen.getByRole('combobox', { name: 'Command' }));
});

it('the Commands button toggles the Commands pane, and reports it open (C01, K01)', () => {
    render(<StackApp />);
    expect(fixture.barProps!.commandsOpen).toBe(false);
    act(() => (fixture.barProps!.onCommands as () => void)());
    expect(useCollimateStore.getState().commandPane.open).toBe(true);
    expect(fixture.barProps!.commandsOpen).toBe(true);
    act(() => (fixture.barProps!.onCommands as () => void)());
    expect(useCollimateStore.getState().commandPane.open).toBe(false);
    expect(fixture.barProps!.commandsOpen).toBe(false);
});

it('the top bar has no right-click new cell + run (C18)', () => {
    render(<StackApp />);
    expect(fixture.barProps).not.toHaveProperty('onNewCellRun');
});

it('Mod+K toggles the pane and / opens it over the full-window view; closing puts the keyboard in the view (polish)', () => {
    const view = document.createElement('div');
    view.setAttribute('data-full-window', '');
    document.body.append(view);
    render(<StackApp />);
    const press = (id: string) => act(() => fixture.shortcuts[id](new KeyboardEvent('keydown', { key: 'k', ctrlKey: true })));
    const open = () => useCollimateStore.getState().commandPane.open;
    useCollimateStore.setState({ maximizedOverlayCellId: 'c1' });
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    expect(open()).toBe(true);
    press(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE);
    expect(open()).toBe(false);
    expect(document.activeElement).toBe(view);
    press(SHORTCUT_IDS.GLOBAL_OPEN_COMMANDS);
    expect(open()).toBe(true);
    expect(useCollimateStore.getState().maximizedOverlayCellId).toBe('c1');
    view.remove();
});
