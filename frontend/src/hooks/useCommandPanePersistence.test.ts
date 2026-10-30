import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

vi.mock('@/lib/api', async (original) => ({
    ...await original<typeof import('@/lib/api')>(),
    putChannelSettings: vi.fn(async () => ({})),
}));

import { putChannelSettings, type ChannelSettings } from '@/lib/api';
import { useCommandPanePersistence, COMMAND_PANE_SAVE_MS } from './useCommandPanePersistence';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { defaultCommandPane, type PersistedCommandPane } from '@/stores/collimate/commandPaneSlice';

const doc = (commandPane?: PersistedCommandPane): ChannelSettings => ({
    version: 1, kind: 'channel-settings',
    keyboard: { overrides: {}, commands: {} },
    theme: { activeThemeId: null, userThemes: [] },
    editing: { spellcheck: true },
    layout: { defaultStackWidth: 640, ...(commandPane ? { commandPane } : {}) },
    remove_zones: { zones: [] },
});

beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(putChannelSettings).mockClear();
    useCollimateStore.setState({ commandPane: defaultCommandPane() });
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
});
afterEach(() => { vi.useRealTimers(); });

it('adopts each channel\'s saved pane when its settings load (K01)', () => {
    renderHook(() => useCommandPanePersistence());
    act(() => useChannelSettingsStore.setState({
        doc: doc({ open: true, side: 'right', query: 'fan', recents: ['log'] }), loadedFor: '11111111-1111-4111-8111-111111111111',
    }));
    expect(useCollimateStore.getState().commandPane).toEqual({ open: true, side: 'right', query: 'fan', recents: ['log'], view: { kind: 'commands' } });
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: '22222222-2222-4222-8222-222222222222' }));
    expect(useCollimateStore.getState().commandPane).toEqual(defaultCommandPane());
    vi.runOnlyPendingTimers();
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('saves changes to the channel\'s layout section with the stack width kept, once the keys pause (K01)', () => {
    renderHook(() => useCommandPanePersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }));
    act(() => {
        useCollimateStore.getState().openCommandPane();
        useCollimateStore.getState().setCommandPaneQuery('f');
        useCollimateStore.getState().setCommandPaneQuery('fa');
    });
    vi.advanceTimersByTime(COMMAND_PANE_SAVE_MS - 1);
    expect(putChannelSettings).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    const pane: PersistedCommandPane = { open: true, side: 'left', query: 'fa', recents: [] };
    expect(putChannelSettings).toHaveBeenCalledExactlyOnceWith('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 640, commandPane: pane } });
    expect(useChannelSettingsStore.getState().doc?.layout?.commandPane).toEqual(pane);
});

it('a pending save still reaches its channel when the user switches away (K01)', () => {
    renderHook(() => useCommandPanePersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }));
    act(() => useCollimateStore.getState().setCommandPaneSide('right'));
    act(() => useChannelSettingsStore.setState({ loadedFor: null }));
    expect(putChannelSettings).toHaveBeenCalledExactlyOnceWith('11111111-1111-4111-8111-111111111111', {
        layout: { defaultStackWidth: 640, commandPane: { open: false, side: 'right', query: '', recents: [] } },
    });
});

it('never saves the view, and a saved doc adopts the commands view (CP-3)', () => {
    useCollimateStore.getState().openCommandPaneSettings('theme');
    renderHook(() => useCommandPanePersistence());
    act(() => useChannelSettingsStore.setState({
        doc: doc({ open: true, side: 'left', query: '', recents: [] }), loadedFor: '11111111-1111-4111-8111-111111111111',
    }));
    expect(useCollimateStore.getState().commandPane.view).toEqual({ kind: 'commands' });
    act(() => useCollimateStore.getState().openCommandPaneSettings('keys'));
    vi.runOnlyPendingTimers();
    expect(putChannelSettings).not.toHaveBeenCalled();
    act(() => useCollimateStore.getState().setCommandPaneSide('right'));
    vi.runOnlyPendingTimers();
    const saved = vi.mocked(putChannelSettings).mock.calls[0][1].layout!.commandPane!;
    expect(saved).toEqual({ open: true, side: 'right', query: '', recents: [] });
    expect(saved).not.toHaveProperty('view');
    expect(useChannelSettingsStore.getState().doc?.layout?.commandPane).not.toHaveProperty('view');
});
