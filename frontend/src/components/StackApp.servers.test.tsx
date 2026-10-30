import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { installDesktopShell, uninstallDesktopShell, type DesktopShellStub } from '@/test/desktopShell';
import { registerRendererFlusher } from '@/lib/rendererFlush';

// StackApp in the desktop shell: a channel on another server reopens the
// window there only after the page's editors are saved, and File → Servers…
// (the shell's openServers command) opens the Commands pane on its Servers
// section, with a channel open or none.
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
}) => (
    <>
        <button onClick={() => props.onSelectServerChannel('lab', 'lab-channel')}>Lab channel</button>
        <button onClick={props.onRequestNewChannel}>New channel</button>
    </>
) }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
// The settings sections, as the Commands pane's settings view loads them.
vi.mock('@/components/settings/ServersSection', () => ({ ServersSection: () => <div>servers body</div> }));
vi.mock('@/components/settings/ThemeSection', () => ({ ThemeSection: () => <div>theme body</div> }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./ChannelCreateView', () => ({ ChannelCreateView: (props: { sourceChannelId: string | null }) =>
    <div data-testid="create-channel" data-source-channel={props.sourceChannelId ?? ''} /> }));
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

it('opens a fresh server on its New channel page and consumes the launch marker', () => {
    window.history.replaceState(null, '', '/#new-channel');
    render(<StackApp />);
    expect(screen.getByTestId('create-channel')).toHaveAttribute('data-source-channel', 'channel-id');
    expect(window.location.hash).toBe('');
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

it('File → Servers… opens the pane on Servers, also while another section is open (MS-05, CP-3)', async () => {
    render(<StackApp />);
    expect(pane()).toBeNull();
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(pane()).toHaveAttribute('data-pane-view', 'settings');
    expect(screen.getByRole('heading', { name: 'Servers' })).toBeInTheDocument();
    expect(await screen.findByText('servers body')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Back to settings' }));
    fireEvent.click(screen.getByRole('button', { name: 'Themes' }));
    expect(screen.getByRole('heading', { name: 'Themes' })).toBeInTheDocument();
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(screen.getByRole('heading', { name: 'Servers' })).toBeInTheDocument();
});

it('with no channel, File → Servers… opens the pane over the creation view, offering only Settings (MS-05, CP-3)', async () => {
    fixture.channel = null;
    render(<StackApp />);
    expect(pane()).toBeNull();
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(pane()).toHaveAttribute('data-pane-view', 'settings');
    expect(pane()!.parentElement).toBe(document.body);
    expect(pane()).toHaveClass('fixed', 'z-[455]');
    expect(await screen.findByText('servers body')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('button', { name: 'Back to settings' }), { key: 'Escape' });
    fireEvent.keyDown(screen.getByRole('button', { name: 'Key manager' }), { key: 'Escape' });
    expect(pane()).toHaveAttribute('data-pane-view', 'commands');
    expect([...pane()!.querySelectorAll('[role="option"]')].map(o => o.getAttribute('data-command')))
        .toEqual(['dock', 'settings:keys', 'settings:editing', 'settings:theme', 'settings:servers']);
});

it('over the channel-creation view of an open channel, File → Servers… opens the sceneless pane above it on Servers (MS-05, CP-3)', async () => {
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'New channel' }));
    expect(pane()).toBeNull();
    act(() => stub.emitShellCommand({ type: 'openServers' }));
    expect(pane()).toHaveAttribute('data-pane-view', 'settings');
    expect(pane()!.parentElement).toBe(document.body);
    expect(pane()).toHaveClass('fixed', 'z-[455]');
    expect(await screen.findByText('servers body')).toBeInTheDocument();
});
