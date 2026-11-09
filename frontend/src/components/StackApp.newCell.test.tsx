import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

// StackApp's mouse entry points to a plain new cell: the top bar's + and the
// empty channel's New stack. Each starts the new cell in its name field
// (newEmptyStackRight with editTitle). The real empty-channel button; the
// top bar stubbed to a button wired to its onNewStack.
const fixture = vi.hoisted(() => {
    const channel = { id: 'channel-id', name: 'Channel', main_stacks: [], nav_stacks: [] };
    return {
        channel,
        selectedWindowId: null as string | null,
        scene: {
            displayStacks: [], displaySelectedId: null, visibleTopIndices: {}, fanRange: null,
            selectDisplayId: () => {}, setFannedOutStackId: () => {},
            actions: { newEmptyStackRight: vi.fn() },
        },
    };
});
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: () => {},
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => ({ selectedWindowId: fixture.selectedWindowId, setSelectedWindowId: () => {}, setFocusArea: () => {} }),
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: false, toggleZoom: () => {} }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({
    AppTopBar: ({ onNewStack }: { onNewStack: () => void }) => <button type="button" onClick={onNewStack}>Top bar +</button>,
}));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
vi.mock('./SceneBackground', () => ({ SceneBackground: () => null }));
vi.mock('./CommandPane', () => ({ CommandPane: () => null }));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./channelsPage/ChannelsPage', () => ({ ChannelsPage: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
import StackApp from './StackApp';

beforeEach(() => {
    fixture.scene.actions.newEmptyStackRight.mockClear();
    fixture.selectedWindowId = null;
    useCollimateStore.setState({
        isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null,
        commandPane: defaultCommandPane(), maximizedOverlayCellId: null,
    } as never);
});
afterEach(cleanup);

it('the top bar\'s + opens a new cell right of the selection, in its name field', () => {
    fixture.selectedWindowId = 's1';
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: 'Top bar +' }));
    expect(fixture.scene.actions.newEmptyStackRight).toHaveBeenCalledExactlyOnceWith('s1', { editTitle: true });
});

it('the empty channel\'s New stack opens a new cell in its name field', () => {
    render(<StackApp />);
    fireEvent.click(screen.getByRole('button', { name: /^New stack/ }));
    expect(fixture.scene.actions.newEmptyStackRight).toHaveBeenCalledExactlyOnceWith(null, { editTitle: true });
});
