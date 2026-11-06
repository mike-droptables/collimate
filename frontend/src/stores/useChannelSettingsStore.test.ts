/**
 * The channel settings store: which channel the section writers save to
 * while a load is in flight or failing, and which load's result applies
 * (F0483); and the no-fan-out patch "Set current" uses (F0903).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('@/lib/api', () => ({
    fetchChannelSettings: vi.fn(),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));
const { adoptOverrides, adoptCommandBindings, adoptEditing, adoptRendererStyles, writeBootCache } = vi.hoisted(() => ({
    adoptOverrides: vi.fn(),
    adoptCommandBindings: vi.fn(),
    adoptEditing: vi.fn(),
    adoptRendererStyles: vi.fn(),
    writeBootCache: vi.fn(),
}));
vi.mock('@/lib/keybindings/store', () => ({
    useKeybindingsStore: { getState: () => ({ adoptOverrides, adoptCommandBindings }) },
}));
vi.mock('@/stores/useEditingStore', () => ({
    useEditingStore: { getState: () => ({ adoptEditing }) },
}));
vi.mock('@/stores/useRendererStylesStore', () => ({
    useRendererStylesStore: { getState: () => ({ adoptRendererStyles }) },
}));
vi.mock('@/lib/settingsCache', () => ({ writeBootCache }));

import { fetchChannelSettings, type ChannelSettings } from '@/lib/api';
import { useChannelSettingsStore, LOAD_RETRY_MS } from './useChannelSettingsStore';
import { useCollimateStore } from './useCollimateStore';
import type { Channel } from '@/lib/types';

const fetchMock = vi.mocked(fetchChannelSettings);

const settingsDoc = (tag: string): ChannelSettings => ({
    version: 1,
    kind: 'channel-settings',
    keyboard: { overrides: { [tag]: 'Ctrl+K' }, commands: { [tag]: 'Alt+L' } },
    theme: { activeThemeId: null, userThemes: [] },
    editing: { spellcheck: tag !== 'B' },
    layout: { defaultStackWidth: null },
    remove_zones: { zones: [] },
    renderer_styles: { milkdown: { theme: tag, font: null, textSize: null } },
} as unknown as ChannelSettings);

const A = '11111111-1111-4111-8111-111111111111', B = '22222222-2222-4222-8222-222222222222', C = '33333333-3333-4333-8333-333333333333';
const channel = (id: string): Channel => ({
    id, name: 'Shared label', main_stacks: [], nav_stacks: [], main_selection_id: null, nav_selection_id: null,
} as unknown as Channel);

/** Switch the active channel; the store's subscription starts the load. */
const activate = (name: string) => useCollimateStore.setState({ activeChannel: channel(name) });

describe('useChannelSettingsStore', () => {
    beforeEach(async () => {
        vi.useFakeTimers();
        fetchMock.mockReset();
        adoptOverrides.mockClear();
        adoptCommandBindings.mockClear();
        adoptEditing.mockClear();
        adoptRendererStyles.mockClear();
        writeBootCache.mockClear();
        // Channel A loaded and active.
        fetchMock.mockResolvedValueOnce(settingsDoc('A'));
        activate(A);
        await vi.advanceTimersByTimeAsync(0);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(A);
        fetchMock.mockReset();
    });
    afterEach(async () => {
        // Leave no retry pending for the next test.
        useCollimateStore.setState({ activeChannel: null });
        await vi.advanceTimersByTimeAsync(60_000);
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
        vi.useRealTimers();
    });

    it("a failed load of the new channel stops saving into the previous channel's doc (F0483)", async () => {
        const docA = useChannelSettingsStore.getState().doc;
        fetchMock.mockRejectedValue(new Error('HTTP 503'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        const s = useChannelSettingsStore.getState();
        // Writers read loadedFor: null means cache-only, never channel A.
        expect(s.loadedFor).toBeNull();
        // A's doc keeps painting until B's arrives.
        expect(s.doc).toBe(docA);
    });

    it("while the new channel's load is in flight, the writers do not save into the previous channel (F0483)", async () => {
        const docA = useChannelSettingsStore.getState().doc;
        let resolveB: (d: ChannelSettings) => void = () => {};
        fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveB = resolve; }));
        activate(B);
        // Before the GET settles: cache-only writers, A's doc still painting.
        expect(useChannelSettingsStore.getState().loadedFor).toBeNull();
        expect(useChannelSettingsStore.getState().doc).toBe(docA);
        resolveB(settingsDoc('B'));
        await vi.advanceTimersByTimeAsync(0);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(B);
    });

    it("a loaded doc fans its editing section out to the editing store and the boot cache", async () => {
        fetchMock.mockResolvedValueOnce(settingsDoc('B'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        await vi.dynamicImportSettled();
        expect(adoptEditing).toHaveBeenLastCalledWith({ spellcheck: false });
        expect(writeBootCache).toHaveBeenLastCalledWith(expect.objectContaining({ editing: { spellcheck: false } }));
    });

    it('a loaded doc fans its renderer_styles section out to the renderer styles store and the boot cache (RS-02)', async () => {
        fetchMock.mockResolvedValueOnce(settingsDoc('B'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        await vi.dynamicImportSettled();
        const styles = { milkdown: { theme: 'B', font: null, textSize: null } };
        expect(adoptRendererStyles).toHaveBeenLastCalledWith(styles);
        expect(writeBootCache).toHaveBeenLastCalledWith(expect.objectContaining({ renderer_styles: styles }));
    });

    it('a failed forced reload of the loaded channel keeps it as the save target and retries the reload (F0483)', async () => {
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockResolvedValueOnce(settingsDoc('A2'));
        await useChannelSettingsStore.getState().reloadAfterExternalWrite();
        // `doc` is still A's own: its writers keep saving to A.
        expect(useChannelSettingsStore.getState().loadedFor).toBe(A);
        await vi.advanceTimersByTimeAsync(LOAD_RETRY_MS[0]);
        expect(fetchMock.mock.calls.map(c => c[0])).toEqual([A, A]);
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ A2: 'Ctrl+K' });
    });

    it('a retry GET overtaken by a forced reload cannot re-apply the doc it read (F0483)', async () => {
        let resolveRetry: (d: ChannelSettings) => void = () => {};
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve; }));
        fetchMock.mockResolvedValueOnce(settingsDoc('B-new'));
        activate(B);
        await vi.advanceTimersByTimeAsync(LOAD_RETRY_MS[0]);
        expect(fetchMock).toHaveBeenCalledTimes(2);            // the retry's GET is in flight
        await useChannelSettingsStore.getState().reloadAfterExternalWrite();
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ 'B-new': 'Ctrl+K' });
        resolveRetry(settingsDoc('B-old'));
        await vi.advanceTimersByTimeAsync(0);
        const s = useChannelSettingsStore.getState();
        expect(s.loadedFor).toBe(B);
        expect(s.doc?.keyboard.overrides).toEqual({ 'B-new': 'Ctrl+K' });
    });

    it("a second load of the switching channel joins the first: a later failure cannot discard its good doc (F0483)", async () => {
        // Every editor mount nudges a load while the switch lands. Had the
        // nudge issued its own GET, that GET failing would supersede the
        // first one's success and leave B on A's look.
        let resolveB: (d: ChannelSettings) => void = () => {};
        fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolveB = resolve; }));
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        activate(B);
        const nudge = useChannelSettingsStore.getState().loadForChannel(B);
        resolveB(settingsDoc('B'));
        await nudge;
        // The joined call resolves once the doc is adopted.
        expect(useChannelSettingsStore.getState().loadedFor).toBe(B);
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ B: 'Ctrl+K' });
    });

    it('a forced reload cancels the retry a failed forced reload left pending (F0483)', async () => {
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockResolvedValueOnce(settingsDoc('A2'));
        fetchMock.mockResolvedValueOnce(settingsDoc('A-retry'));
        await useChannelSettingsStore.getState().reloadAfterExternalWrite();
        await useChannelSettingsStore.getState().reloadAfterExternalWrite();
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ A2: 'Ctrl+K' });
        // The first reload's retry would re-fetch (it is forced): it is gone.
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetchMock).toHaveBeenCalledTimes(2);
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ A2: 'Ctrl+K' });
    });

    it('retries a failed load with backoff while the channel stays active, then adopts it (F0483)', async () => {
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockResolvedValueOnce(settingsDoc('B'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        expect(fetchMock).toHaveBeenCalledTimes(1);
        await vi.advanceTimersByTimeAsync(LOAD_RETRY_MS[0]);
        expect(fetchMock).toHaveBeenCalledTimes(2);
        expect(useChannelSettingsStore.getState().loadedFor).toBeNull();
        await vi.advanceTimersByTimeAsync(LOAD_RETRY_MS[1]);
        expect(fetchMock).toHaveBeenCalledTimes(3);
        const s = useChannelSettingsStore.getState();
        expect(s.loadedFor).toBe(B);
        expect(s.doc?.keyboard.overrides).toEqual({ B: 'Ctrl+K' });
        expect(adoptOverrides).toHaveBeenLastCalledWith({ B: 'Ctrl+K' });
        expect(adoptCommandBindings).toHaveBeenLastCalledWith({ B: 'Alt+L' });
    });

    it('the retries are bounded (F0483)', async () => {
        fetchMock.mockRejectedValue(new Error('HTTP 503'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        const total = LOAD_RETRY_MS.reduce((a, b) => a + b, 0);
        await vi.advanceTimersByTimeAsync(total + 60_000);
        expect(fetchMock).toHaveBeenCalledTimes(1 + LOAD_RETRY_MS.length);
        expect(useChannelSettingsStore.getState().loadedFor).toBeNull();
    });

    it('switching away cancels the pending retry (F0483)', async () => {
        fetchMock.mockRejectedValueOnce(new Error('HTTP 503'));
        fetchMock.mockResolvedValueOnce(settingsDoc('C'));
        activate(B);
        await vi.advanceTimersByTimeAsync(0);
        activate(C);
        await vi.advanceTimersByTimeAsync(0);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(C);
        await vi.advanceTimersByTimeAsync(60_000);
        expect(fetchMock.mock.calls.map(c => c[0])).toEqual([B, C]);
    });

    it("a failed load for a channel already left changes nothing (F0483)", async () => {
        let rejectB: (e: Error) => void = () => {};
        fetchMock.mockImplementationOnce(() => new Promise((_, reject) => { rejectB = reject; }));
        fetchMock.mockResolvedValueOnce(settingsDoc('C'));
        activate(B);
        activate(C);
        await vi.advanceTimersByTimeAsync(0);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(C);
        rejectB(new Error('HTTP 503'));
        await vi.advanceTimersByTimeAsync(60_000);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(C);
        expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('patchDoc merges a saved section into the loaded doc without fanning out (F0903)', () => {
        const before = useChannelSettingsStore.getState().doc!;
        adoptOverrides.mockClear();
        adoptEditing.mockClear();
        adoptRendererStyles.mockClear();
        writeBootCache.mockClear();
        useChannelSettingsStore.getState().patchDoc(A, { layout: { defaultStackWidth: 640 } });
        const after = useChannelSettingsStore.getState();
        expect(after.loadedFor).toBe(A);
        expect(after.doc?.layout?.defaultStackWidth).toBe(640);
        // Every other section keeps its identity: no consumer re-adopts.
        expect(after.doc?.theme).toBe(before.theme);
        expect(after.doc?.keyboard).toBe(before.keyboard);
        expect(after.doc?.editing).toBe(before.editing);
        expect(adoptOverrides).not.toHaveBeenCalled();
        expect(adoptEditing).not.toHaveBeenCalled();
        expect(adoptRendererStyles).not.toHaveBeenCalled();
        expect(writeBootCache).not.toHaveBeenCalled();
    });

    it("patchDoc for a channel other than the loaded doc's is a no-op (F0903)", () => {
        const before = useChannelSettingsStore.getState().doc;
        useChannelSettingsStore.getState().patchDoc(B, { layout: { defaultStackWidth: 640 } });
        expect(useChannelSettingsStore.getState().doc).toBe(before);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(A);
    });
    it('renaming the display label preserves the UUID load and save target', async () => {
        activate(A);
        useCollimateStore.setState({ activeChannel: { ...channel(A), name: 'Renamed' } });
        await vi.advanceTimersByTimeAsync(0);
        expect(fetchMock).not.toHaveBeenCalled();
        expect(useChannelSettingsStore.getState().loadedFor).toBe(A);
    });

    it('a deleted channel response cannot populate a new UUID with the same label', async () => {
        let resolveOld!: (doc: ChannelSettings) => void;
        fetchMock.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
        const old = useChannelSettingsStore.getState().loadForChannel(A, true);
        useCollimateStore.setState({ activeChannel: null });
        fetchMock.mockResolvedValueOnce(settingsDoc('C'));
        activate(C);
        await vi.advanceTimersByTimeAsync(0);
        resolveOld(settingsDoc('old'));
        await old;
        await vi.dynamicImportSettled();
        expect(fetchMock.mock.calls.map(call => call[0])).toEqual([A, C]);
        expect(useChannelSettingsStore.getState().loadedFor).toBe(C);
        expect(useChannelSettingsStore.getState().doc?.keyboard.overrides).toEqual({ C: 'Ctrl+K' });
        expect(adoptOverrides).not.toHaveBeenCalledWith({ old: 'Ctrl+K' });
    });

    it('deferred section adoption is rejected after a switch before imports resolve', async () => {
        let resolveOld!: (doc: ChannelSettings) => void;
        fetchMock.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
        const old = useChannelSettingsStore.getState().loadForChannel(A, true);
        resolveOld(settingsDoc('old'));
        await old; // fanOut has scheduled lazy imports, not adopted their sections yet
        adoptOverrides.mockClear();
        adoptEditing.mockClear();
        adoptRendererStyles.mockClear();
        fetchMock.mockImplementationOnce(() => new Promise(() => {}));
        activate(B);
        await vi.dynamicImportSettled();
        expect(adoptOverrides).not.toHaveBeenCalledWith({ old: 'Ctrl+K' });
        expect(adoptEditing).not.toHaveBeenCalled();
        expect(adoptRendererStyles).not.toHaveBeenCalled();
    });

});
