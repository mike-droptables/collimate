import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { installDesktopShell, uninstallDesktopShell, type DesktopShellStub } from '@/test/desktopShell';
import { registerRendererFlusher } from '@/lib/rendererFlush';

// StackApp in the desktop shell: a channel on another server reopens the
// window there only after the page's editors are saved, and File → Servers…
// (the shell's openServers command) opens the channels page on its Servers
// tab, with a channel open or none (plan 1105 P7: Settings has no Servers).
const CHANNEL = { id: 'channel-id', name: 'Channel', main_stacks: [], nav_stacks: [] };
const fixture = vi.hoisted(() => ({
    channel: null as null | { id: string; name: string; main_stacks: never[]; nav_stacks: never[] },
    scene: {
        displayStacks: [{ id: 's1', name: 'One', cells: [] }],
        displaySelectedId: 's1',
        visibleTopIndices: { s1: 0 },
        fanRange: null,
        selectDisplayId: () => {},
        actions: { newEmptyStackRight: () => {} },
    },
}));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: fixture.channel ? [fixture.channel] : [] }, refresh: () => {},
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({ selectedWindowId: null, setSelectedWindowId: () => {} }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: false }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/lib/keybindings/useShortcut', () => ({ useShortcut: () => {} }));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({ AppTopBar: (props: {
    onSelectServerChannel: (serverId: string, channelId: string) => void;
    onRequestNewChannel: () => void;
    onManageChannels: () => void;
}) => (
    <>
        <button onClick={() => props.onSelectServerChannel('lab', 'lab-channel')}>Lab channel</button>
        <button onClick={props.onRequestNewChannel}>New channel</button>
        <button onClick={props.onManageChannels}>Manage channels</button>
    </>
) }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
vi.mock('./SceneBackground', () => ({ SceneBackground: () => null }));
// The settings sections, as the Commands pane's settings view loads them.
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./channelsPage/ChannelsPage', () => ({ ChannelsPage: (props: { tab: string; sourceChannelId: string | null; onClose?: () => void }) =>
    <div data-testid="channels-page" data-tab={props.tab} data-source-channel={props.sourceChannelId ?? ''} data-closable={props.onClose ? 'yes' : 'no'} data-channel-create="" /> }));
vi.mock('./BlurredTopBar', () => ({ BlurredTopBar: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
import StackApp from './StackApp';

let stub: DesktopShellStub;
beforeEach(() => {
    stub = installDesktopShell();
    useCollimateStore.setState({ isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null, commandPane: defaultCommandPane(), maximizedOverlayCellId: null });
    fixture.channel = CHANNEL;
});
afterEach(() => {
    cleanup();
    useCollimateStore.getState().resolveConfirm(false);
    uninstallDesktopShell();
    window.history.replaceState(null, '', '/');
});

const page = () => screen.queryByTestId('channels-page');

it('opens a fresh server on the channels page\'s New tab and consumes the launch marker', () => {
    window.history.replaceState(null, '', '/#new-channel');
    render(<StackApp />);
    expect(page()).toHaveAttribute('data-tab', 'new');
    expect(page()).toHaveAttribute('data-source-channel', 'channel-id');
    expect(page()).toHaveAttribute('data-closable', 'yes');
    expect(window.location.hash).toBe('');
});

it('#channels/<tab> opens that tab, at launch and on a later hash change; an unknown tab is no request (plan 1105 P7)', () => {
    window.history.replaceState(null, '', '/#channels/archived');
    render(<StackApp />);
    expect(page()).toHaveAttribute('data-tab', 'archived');
    expect(window.location.hash).toBe('');
    cleanup();
    window.history.replaceState(null, '', '/#channels/nowhere');
    render(<StackApp />);
    expect(page()).toBeNull();
    act(() => {
        window.history.replaceState(null, '', '/#channels/channels');
        window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(page()).toHaveAttribute('data-tab', 'channels');
    expect(window.location.hash).toBe('');
});

it('the channel menu\'s New channel… and Manage channels… open the page on New and on Channels (plan 1105 P7)', () => {
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'New channel' }));
    expect(page()).toHaveAttribute('data-tab', 'new');
    cleanup();
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Manage channels' }));
    expect(page()).toHaveAttribute('data-tab', 'channels');
});

it('with no channel open the page is the only surface, on New, with no Close (plan 1105 P7)', () => {
    fixture.channel = null;
    render(<StackApp />);
    expect(page()).toHaveAttribute('data-tab', 'new');
    expect(page()).toHaveAttribute('data-closable', 'no');
});

it('shows a local Stop failure on the surviving remote New channel page', () => {
    window.history.replaceState(null, '', '/#new-channel');
    render(<StackApp />);
    expect(document.documentElement).toHaveAttribute('data-shell-command-ready');
    act(() => stub.emitShellCommand({ type: 'localStopFailed', message: 'The local server could not stop.' }));
    expect(useCollimateStore.getState().confirmDialog).toMatchObject({
        kind: 'alert', title: 'Could not stop the local server', body: 'The local server could not stop.',
    });
});

it('a channel on another server flushes editors before openChannel (MS-05)', async () => {
    const order: string[] = [];
    const unregister = registerRendererFlusher('cell', 'notes.md', () => { order.push('flush'); });
    try {
        vi.mocked(stub.shell.servers.openChannel).mockImplementation(async (serverId, channelId) => {
            order.push(`openChannel ${serverId} ${channelId}`);
        });
        render(<StackApp />);
        fireEvent.click(screen.getByRole('button', { name: 'Lab channel' }));
        await waitFor(() => expect(order).toEqual(['flush', 'openChannel lab lab-channel']));
        expect(useCollimateStore.getState().confirmDialog).toBeNull();
    } finally {
        unregister();
    }
});

it('a refused openChannel says why in an alert (MS-05)', async () => {
    vi.mocked(stub.shell.servers.openChannel).mockRejectedValueOnce(new Error('Lab refused the token.'));
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Lab channel' }));
    await waitFor(() => expect(useCollimateStore.getState().confirmDialog).toMatchObject({
        kind: 'alert', title: 'Could not open that channel', body: 'Lab refused the token.',
    }));
});

const pane = () => document.querySelector<HTMLElement>('[data-command-pane]');

it('File → Servers… opens the channels page on its Servers tab, also from another tab (MS-05, plan 1105 P7)', () => {
    render(<StackApp />);
    expect(page()).toBeNull();
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(page()).toHaveAttribute('data-tab', 'servers');
    expect(page()).toHaveAttribute('data-closable', 'yes');
    expect(pane()).toBeNull();
    cleanup();
    window.history.replaceState(null, '', '/#new-channel');
    render(<StackApp />);
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(page()).toHaveAttribute('data-tab', 'servers');
});

it('with no channel, File → Servers… opens the page on Servers; the sceneless pane over it offers only Settings, never Servers (MS-05, CP-3, plan 1105 P7)', () => {
    fixture.channel = null;
    render(<StackApp />);
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(page()).toHaveAttribute('data-tab', 'servers');
    expect(pane()).toBeNull();
    act(() => useCollimateStore.getState().openCommandPane());
    expect(pane()!.parentElement).toBe(document.body);
    expect(pane()).toHaveClass('fixed', 'z-[455]');
    expect([...pane()!.querySelectorAll('[role="option"]')].map(o => o.getAttribute('data-command')))
        .toEqual(['dock', 'settings:keys', 'settings:editing', 'settings:theme', 'settings:device']);
});
