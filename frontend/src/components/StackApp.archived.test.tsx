import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { useChannelReadOnly } from '@/contexts/ChannelReadOnlyContext';
import { reportArchivedRefusal } from '@/lib/channelArchived';
import { unarchiveChannel } from '@/lib/api/storage';

// StackApp with an archived channel open (plan 1105 P7): the read-only
// context reaches the window, the banner sits under the top bar with
// Unarchive acting at once (D11) and Manage channels opening the page, and
// a refusal from a missed control shows the server's sentence once.
const fixture = vi.hoisted(() => ({
    channel: null as null | { id: string; name: string; archived?: boolean; main_stacks: never[]; nav_stacks: never[] },
    refresh: vi.fn(async () => {}),
    scene: {
        displayStacks: [{ id: 's1', name: 'One', cells: [] }],
        displaySelectedId: 's1',
        visibleTopIndices: { s1: 0 },
        fanRange: null,
        selectDisplayId: () => {},
        actions: { newEmptyStackRight: () => {} },
    },
}));
vi.mock('@/lib/api/storage', () => ({ unarchiveChannel: vi.fn(async () => ({})) }));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: fixture.channel ? [fixture.channel] : [], active_channel_id: fixture.channel?.id ?? null }, refresh: fixture.refresh,
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
vi.mock('./AppTopBar', () => ({ AppTopBar: () => <div data-testid="top-bar" data-read-only={String(useChannelReadOnly())} /> }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => <div data-testid="scene" data-read-only={String(useChannelReadOnly())} /> }));
vi.mock('./SceneBackground', () => ({ SceneBackground: () => null }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: (props: { isEmpty: boolean }) => (props.isEmpty ? <button>New cell</button> : null) }));
vi.mock('./channelsPage/ChannelsPage', () => ({ ChannelsPage: (props: { tab: string }) => <div data-testid="channels-page" data-tab={props.tab} /> }));
vi.mock('./BlurredTopBar', () => ({ BlurredTopBar: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
import StackApp from './StackApp';

const CHANNEL = { id: 'channel-id', name: 'Channel', main_stacks: [] as never[], nav_stacks: [] as never[] };
beforeEach(() => {
    vi.clearAllMocks();
    useCollimateStore.setState({ isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null, commandPane: defaultCommandPane(), maximizedOverlayCellId: null });
    useCollimateStore.setState({ activeChannel: { ...CHANNEL, archived: true, nav_selection_id: null, main_selection_id: null } });
    fixture.channel = { ...CHANNEL, archived: true };
});
afterEach(() => {
    cleanup();
    useCollimateStore.getState().resolveConfirm(false);
});

const banner = () => screen.queryByRole('region', { name: 'Archived channel' });

it('an open channel that is not archived has no banner and nothing read-only (plan 1105 P7)', () => {
    fixture.channel = CHANNEL;
    render(<StackApp />);
    expect(banner()).toBeNull();
    expect(screen.getByTestId('top-bar')).toHaveAttribute('data-read-only', 'false');
    expect(screen.getByTestId('scene')).toHaveAttribute('data-read-only', 'false');
    expect(screen.getByRole('button', { name: 'New cell' })).toBeInTheDocument();
});

it('an archived channel opens read-only under the banner: no New cell, the context on the top bar and the scene (plan 1105 P7)', () => {
    render(<StackApp />);
    expect(banner()).toHaveTextContent('Archived · read-only.');
    expect(screen.getByTestId('top-bar')).toHaveAttribute('data-read-only', 'true');
    expect(screen.getByTestId('scene')).toHaveAttribute('data-read-only', 'true');
    expect(screen.queryByRole('button', { name: 'New cell' })).toBeNull();
});

it('the banner\'s Unarchive acts at once; Manage channels opens the page on Archived (plan 1105 P7, D11)', async () => {
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Unarchive' }));
    await waitFor(() => expect(fixture.refresh).toHaveBeenCalledOnce());
    expect(unarchiveChannel).toHaveBeenCalledExactlyOnceWith('channel-id');
    expect(useCollimateStore.getState().confirmDialog).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Manage channels' }));
    expect(screen.getByTestId('channels-page')).toHaveAttribute('data-tab', 'archived');
});

it('a refusal from a missed control shows the server\'s sentence once (plan 1105 P7)', () => {
    render(<StackApp />);
    act(() => reportArchivedRefusal('This channel is archived and read-only. Unarchive it to change it.'));
    expect(useCollimateStore.getState().confirmDialog).toMatchObject({
        kind: 'alert', title: 'This channel is archived', body: 'This channel is archived and read-only. Unarchive it to change it.',
    });
    act(() => useCollimateStore.getState().resolveConfirm(false));
    act(() => reportArchivedRefusal('This channel is archived and read-only. Unarchive it to change it.'));
    expect(useCollimateStore.getState().confirmDialog).toBeNull();
});
