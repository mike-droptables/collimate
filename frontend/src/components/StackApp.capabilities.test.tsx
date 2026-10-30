import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import type { Channel, Stack } from '@/lib/types';

// StackApp's Capabilities wiring: the pill's toggle, and the manage-request
// consumer (restore when hidden, then select, then front the manage card).
// The rest of the app is stubbed down to what those read.
const CHANNEL = '11111111-1111-4111-8111-111111111111';
const caps: Stack = {
    id: 'caps', name: 'Capabilities', stack_type: 'capabilities',
    cells: [{ id: 'card', name: 'Definitions', files: [], builtin: true }, { id: 'u', name: 'Mine', files: [] }],
};
const work: Stack = { id: 's1', name: 'Work', cells: [{ id: 'w', name: 'W', files: [] }] };
const channelWith = (shown: boolean): Channel => ({
    id: CHANNEL, name: 'Channel', nav_selection_id: null, main_selection_id: null,
    main_stacks: shown ? [work, caps] : [work], nav_stacks: shown ? [] : [caps],
});

const fixture = vi.hoisted(() => ({
    channel: null as unknown as Channel,
    log: [] as string[],
    barProps: null as null | Record<string, unknown>,
    paneScene: null as null | { capabilities: { shown: boolean; toggle: () => void; create: () => void } },
    performAction: null as unknown as (p: Promise<unknown>) => Promise<unknown>,
    scene: null as unknown as Record<string, unknown>,
    focus: null as unknown as Record<string, unknown>,
    setIsZoomedOut: null as unknown as (v: boolean) => void,
}));
vi.mock('@/hooks/useAppState', () => ({ useAppState: () => ({
    activeChannel: fixture.channel, state: { channels: [fixture.channel] }, refresh: () => {}, performAction: fixture.performAction,
}) }));
vi.mock('@/contexts/ThemeContext', () => ({ ThemeProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock('@/contexts/FocusContext', () => ({
    FocusProvider: ({ children }: { children: ReactNode }) => children,
    useFocus: () => fixture.focus,
}));
vi.mock('@/hooks/useSceneActions', () => ({ useSceneActions: () => fixture.scene }));
vi.mock('@/hooks/useZoomActions', () => ({ useZoomActions: () => ({ isZoomedOut: true, setIsZoomedOut: fixture.setIsZoomedOut }) }));
vi.mock('@/hooks/useKeyboardHandler', () => ({ useKeyboardHandler: () => {} }));
vi.mock('@/hooks/useChannelSSE', () => ({ useChannelSSE: () => {} }));
vi.mock('@/hooks/useCanvasFileDrop', () => ({ useCanvasFileDrop: () => ({ dragProps: {} }) }));
vi.mock('./AppTopBar', () => ({ AppTopBar: (props: Record<string, unknown>) => { fixture.barProps = props; return null; } }));
vi.mock('./ApiSettingsModal', () => ({ ApiSettingsModal: () => null }));
vi.mock('./StackScene', () => ({ StackScene: () => null }));
vi.mock('./CommandPane', () => ({
    CommandPane: ({ scene }: { scene: typeof fixture.paneScene }) => { if (scene) fixture.paneScene = scene; return null; },
}));
vi.mock('@/hooks/useCommandPanePersistence', () => ({ useCommandPanePersistence: () => {} }));
vi.mock('./EmptyChannelNewCell', () => ({ EmptyChannelNewCell: () => null }));
vi.mock('./ChannelCreateView', () => ({ ChannelCreateView: () => null }));
vi.mock('./DesktopTitleBar', () => ({ DesktopTitleBar: () => null }));
vi.mock('@/lib/api', async (original) => ({
    ...await original<typeof import('@/lib/api')>(),
    minimizeStack: vi.fn(async () => { fixture.log.push('minimize'); return {}; }),
    restoreMinimizedStack: vi.fn(async (_c: string, _s: string, after?: string | null) => { fixture.log.push(`restore after ${after}`); return {}; }),
}));
import * as api from '@/lib/api';
import StackApp from './StackApp';

const mount = (shown: boolean, vti: Record<string, number> = {}) => {
    fixture.channel = channelWith(shown);
    const stacks = fixture.channel.main_stacks as Stack[];
    fixture.scene = {
        displayStacks: stacks, displaySelectedId: 's1', visibleTopIndices: vti, fanRange: null,
        selectDisplayId: () => {}, setVisibleTopIndices: () => {},
        actions: {
            newEmptyStackRight: () => {}, winSetTopCell: () => {},
            winShowManage: vi.fn((id: string) => { fixture.log.push(`manage ${id}`); }),
        },
    };
    return render(<StackApp />);
};

beforeEach(() => {
    vi.clearAllMocks();
    fixture.log = [];
    fixture.barProps = null;
    fixture.paneScene = null;
    fixture.performAction = vi.fn(async (p: Promise<unknown>) => { try { return await p; } catch { return null; } });
    fixture.setIsZoomedOut = vi.fn((v: boolean) => { fixture.log.push(`zoomed out ${v}`); });
    fixture.focus = {
        selectedWindowId: 's1',
        setSelectedWindowId: vi.fn((id: string | null) => { fixture.log.push(`select ${id}`); }),
        setFocusArea: vi.fn((area: string) => { fixture.log.push(`focus ${area}`); }),
        setMainFocusMode: vi.fn(),
    };
    useCollimateStore.setState({
        isShuttingDown: false, hasInitialized: true, confirmDialog: null, pendingSelectCellId: null,
        commandPane: defaultCommandPane(), maximizedOverlayCellId: null, capabilitiesManageRequest: null,
        pickSessions: {}, cellHistory: {}, cellDiffs: {},
    } as never);
});
afterEach(() => { cleanup(); });

describe('the Capabilities pill', () => {
    it('hands the top bar the stack and whether it is shown', () => {
        mount(false);
        expect(fixture.barProps).toMatchObject({ capabilitiesStack: caps, capabilitiesShown: false });
        cleanup();
        mount(true);
        expect(fixture.barProps).toMatchObject({ capabilitiesStack: caps, capabilitiesShown: true });
    });

    it('while hidden, restores the stack right of the selected stack, then selects it', async () => {
        mount(false);
        await act(async () => { await (fixture.barProps!.onToggleCapabilities as () => Promise<void>)(); });
        expect(api.restoreMinimizedStack).toHaveBeenCalledExactlyOnceWith(CHANNEL, 'caps', 's1');
        expect(fixture.log).toEqual(['restore after s1', 'focus main', 'select caps']);
        expect(api.minimizeStack).not.toHaveBeenCalled();
    });

    it('while shown, hides the stack and writes no selection (the layout hands it on)', async () => {
        mount(true);
        await act(async () => { await (fixture.barProps!.onToggleCapabilities as () => Promise<void>)(); });
        expect(api.minimizeStack).toHaveBeenCalledExactlyOnceWith(CHANNEL, 'caps');
        expect(fixture.focus.setSelectedWindowId).not.toHaveBeenCalled();
    });

    it('a failed restore selects nothing', async () => {
        vi.mocked(api.restoreMinimizedStack).mockRejectedValueOnce(new Error('refused'));
        mount(false);
        await act(async () => { await (fixture.barProps!.onToggleCapabilities as () => Promise<void>)(); });
        expect(fixture.focus.setSelectedWindowId).not.toHaveBeenCalled();
    });
});

describe('a Capabilities manage request', () => {
    it('restores a hidden stack, then selects it, then fronts its manage card in the main view', async () => {
        mount(false);
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ create: true }); });
        await waitFor(() => expect(fixture.log).toContain('manage caps'));
        expect(fixture.log).toEqual(['restore after s1', 'focus main', 'select caps', 'manage caps', 'zoomed out false']);
        // The card applies the payload and clears it.
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ create: true });
    });

    it('on a shown stack selects it and fronts the manage card without a restore, once per request', async () => {
        const view = mount(true, { caps: 2 });
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ kind: 'runner' }); });
        await waitFor(() => expect(fixture.log).toContain('manage caps'));
        expect(api.restoreMinimizedStack).not.toHaveBeenCalled();
        expect(fixture.log).toEqual(['focus main', 'select caps', 'manage caps', 'zoomed out false']);
        view.rerender(<StackApp />);
        await act(async () => { await Promise.resolve(); });
        expect(fixture.log.filter(entry => entry === 'manage caps')).toHaveLength(1);
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({}); });
        await waitFor(() => expect(fixture.log.filter(entry => entry === 'manage caps')).toHaveLength(2));
    });

    it('a failed restore fronts nothing and drops the request, so a later restore does not apply it', async () => {
        vi.mocked(api.restoreMinimizedStack).mockRejectedValueOnce(new Error('refused'));
        mount(false);
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ create: true }); });
        await waitFor(() => expect(api.restoreMinimizedStack).toHaveBeenCalled());
        await act(async () => { await Promise.resolve(); });
        expect(fixture.log).not.toContain('manage caps');
        await waitFor(() => expect(useCollimateStore.getState().capabilitiesManageRequest).toBeNull());
    });

    it('a failed restore drops only its own request, never a newer one', async () => {
        let refuse!: (error: Error) => void;
        vi.mocked(api.restoreMinimizedStack).mockImplementationOnce(() => new Promise((_resolve, reject) => { refuse = reject; }));
        mount(false);
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ create: true }); });
        await waitFor(() => expect(api.restoreMinimizedStack).toHaveBeenCalled());
        act(() => { useCollimateStore.getState().requestCapabilitiesManage({ addFromCatalog: true }); });
        await act(async () => { refuse(new Error('refused')); await Promise.resolve(); });
        await waitFor(() => expect(fixture.log).toContain('manage caps'));
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ addFromCatalog: true });
    });
});

describe('the Commands pane\'s Capabilities rows', () => {
    it('Show raises a manage request while hidden; Create raises one for the questionnaire', () => {
        mount(false);
        expect(fixture.paneScene!.capabilities.shown).toBe(false);
        act(() => fixture.paneScene!.capabilities.toggle());
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({});
        expect(useCollimateStore.getState().capabilitiesManageRequest?.create).toBeUndefined();
        act(() => fixture.paneScene!.capabilities.create());
        expect(useCollimateStore.getState().capabilitiesManageRequest).toMatchObject({ create: true });
    });

    it('Hide hides a shown stack', async () => {
        mount(true);
        expect(fixture.paneScene!.capabilities.shown).toBe(true);
        await act(async () => { await fixture.paneScene!.capabilities.toggle(); });
        expect(api.minimizeStack).toHaveBeenCalledExactlyOnceWith(CHANNEL, 'caps');
    });
});

describe('the selected cell mirror', () => {
    it('is null while the manage card is in front, and the front cell otherwise', () => {
        const view = mount(true, { caps: 0 });
        (fixture.scene as { displaySelectedId: string }).displaySelectedId = 'caps';
        view.rerender(<StackApp />);
        expect(useCollimateStore.getState().selectedCellId).toBeNull();
        (fixture.scene as { visibleTopIndices: Record<string, number> }).visibleTopIndices = { caps: 2 };
        view.rerender(<StackApp />);
        expect(useCollimateStore.getState().selectedCellId).toBe('u');
    });
});
