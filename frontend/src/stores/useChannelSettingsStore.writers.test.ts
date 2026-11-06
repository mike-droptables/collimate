/**
 * The section writers against the real settings store (F0483): while a
 * switch from A to B is loading or failing, a shortcut edit and an editing
 * edit save nothing to A's settings doc; once B's doc lands they save to
 * B. The store tests pin `loadedFor`; this pins what the user sees.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

import { fetchChannelSettings, putChannelSettings, type ChannelSettings } from '@/lib/api';
import { useChannelSettingsStore, LOAD_RETRY_MS } from './useChannelSettingsStore';
import { useCollimateStore } from './useCollimateStore';
import { useEditingStore } from './useEditingStore';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import type { Channel } from '@/lib/types';

const fetchMock = vi.mocked(fetchChannelSettings);
const putMock = vi.mocked(putChannelSettings);

const settingsDoc = (): ChannelSettings => ({
    version: 1,
    kind: 'channel-settings',
    editing: { spellcheck: true },
    keyboard: { overrides: {}, commands: {} },
    theme: { activeThemeId: null, userThemes: [] },
    layout: { defaultStackWidth: null },
    remove_zones: { zones: [] },
} as unknown as ChannelSettings);

const A = '11111111-1111-4111-8111-111111111111', B = '22222222-2222-4222-8222-222222222222';
const channel = (id: string): Channel => ({
    id, name: 'Shared label', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null,
} as unknown as Channel);

/** Switch the active channel; the store's subscription starts the load. */
const activate = (name: string) => useCollimateStore.setState({ activeChannel: channel(name) });

/** A shortcut edit and an editing edit, run to completion (the keyboard
 *  writer reaches the settings store through a dynamic import). */
const editShortcutAndEditing = async () => {
    useKeybindingsStore.getState().setOverride('editor.save', 'Mod+Shift+s');
    await useEditingStore.getState().commitEditing({ spellcheck: false });
    await vi.dynamicImportSettled();
    await vi.advanceTimersByTimeAsync(0);
};

const savedTo = () => putMock.mock.calls.map(([name, patch]) => [name, Object.keys(patch)]);

describe('section writers during a channel switch (F0483)', () => {
    beforeEach(async () => {
        vi.useFakeTimers();
        fetchMock.mockReset();
        fetchMock.mockResolvedValueOnce(settingsDoc());
        activate(A);
        await vi.advanceTimersByTimeAsync(0);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(A);
        fetchMock.mockReset();
        putMock.mockClear();
    });
    afterEach(async () => {
        useCollimateStore.setState({ activeChannel: null });
        await vi.advanceTimersByTimeAsync(60_000);
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
        vi.useRealTimers();
    });

    it("edits made while B's load is in flight save nothing to A, and save to B once it lands", async () => {
        let resolveB: (d: ChannelSettings) => void = () => {};
        fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveB = resolve; }));
        activate(B);
        await editShortcutAndEditing();
        expect(savedTo()).toEqual([]);

        resolveB(settingsDoc());
        await vi.advanceTimersByTimeAsync(0);
        await editShortcutAndEditing();
        expect(savedTo()).toEqual([[B, ['keyboard']], [B, ['editing']]]);
    });

    it("edits made while B's load is failing save nothing to A, and save to B once a retry lands", async () => {
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockResolvedValueOnce(settingsDoc());
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        await editShortcutAndEditing();
        expect(savedTo()).toEqual([]);

        await vi.advanceTimersByTimeAsync(LOAD_RETRY_MS[0]);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(B);
        await editShortcutAndEditing();
        expect(savedTo()).toEqual([[B, ['keyboard']], [B, ['editing']]]);
    });
    it('settings writes capture the adopted UUID before a same-label switch', async () => {
        useKeybindingsStore.getState().setOverride('editor.save', 'Mod+Shift+s');
        const editing = useEditingStore.getState().commitEditing({ spellcheck: false });
        fetchMock.mockResolvedValueOnce(settingsDoc());
        activate(B);
        await editing;
        await vi.dynamicImportSettled();
        expect(savedTo()).toEqual([[A, ['keyboard']], [A, ['editing']]]);
    });

});
