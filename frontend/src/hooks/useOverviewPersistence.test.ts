import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

vi.mock('@/lib/api', async (original) => ({
    ...await original<typeof import('@/lib/api')>(),
    putChannelSettings: vi.fn(async () => ({})),
}));

import { putChannelSettings, type ChannelSettings, type RemoveZone } from '@/lib/api';
import { REMOVE_ZONES_SAVE_MS, useOverviewPersistence } from './useOverviewPersistence';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { SHELF_HEIGHT_DEFAULT, defaultOverviewView } from '@/stores/collimate/overviewSlice';

const doc = (removedShelfHeight?: number, zones?: RemoveZone[], overviewShowTags?: boolean): ChannelSettings => ({
    version: 1, kind: 'channel-settings',
    keyboard: { overrides: {}, commands: {} },
    theme: { activeThemeId: null, userThemes: [] },
    editing: { spellcheck: true },
    layout: {
        defaultStackWidth: 640,
        ...(removedShelfHeight ? { removedShelfHeight } : {}),
        ...(overviewShowTags === undefined ? {} : { overviewShowTags }),
    },
    remove_zones: { zones: zones ?? [] },
});

const A = '11111111-1111-4111-8111-111111111111';
const B = '22222222-2222-4222-8222-222222222222';
const TWO: RemoveZone[] = [
    { id: 'z1', width: 0.5, action: { kind: 'add_tag', tag: 'done' } },
    { id: 'z2', width: 0.5, action: { kind: 'remove' } },
];

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

beforeEach(() => {
    vi.mocked(putChannelSettings).mockClear().mockResolvedValue(doc());
    useCollimateStore.setState({ overview: defaultOverviewView() });
    useChannelSettingsStore.setState({ doc: null, loadedFor: null });
});

it('adopts each channel\'s saved shelf height when its settings load, 316 without one (O05)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(200), loadedFor: '11111111-1111-4111-8111-111111111111' }));
    expect(useCollimateStore.getState().overview.shelfHeight).toBe(200);
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: '22222222-2222-4222-8222-222222222222' }));
    expect(useCollimateStore.getState().overview.shelfHeight).toBe(SHELF_HEIGHT_DEFAULT);
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('saves a resize to the channel\'s layout section with the other layout keys kept (O05)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: '11111111-1111-4111-8111-111111111111' }));
    act(() => useCollimateStore.getState().setShelfHeight(240));
    expect(putChannelSettings).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 640, removedShelfHeight: 240 } });
    expect(useChannelSettingsStore.getState().doc?.layout).toEqual({ defaultStackWidth: 640, removedShelfHeight: 240 });
    // A drag that shuts the shelf keeps its height: nothing to save.
    act(() => useCollimateStore.getState().setShelfHeight(80));
    expect(putChannelSettings).toHaveBeenCalledTimes(1);
});

it('adopts each channel\'s tag toggle when its settings load, off without one (tags toggle)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, undefined, true), loadedFor: A }));
    expect(useCollimateStore.getState().overview.showTags).toBe(true);
    act(() => useChannelSettingsStore.setState({ doc: null, loadedFor: null }));
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: B }));
    expect(useCollimateStore.getState().overview.showTags).toBe(false);
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('saves a toggle to the channel\'s layout section with the other layout keys kept (tags toggle)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(200), loadedFor: A }));
    act(() => useCollimateStore.getState().setShowTags(true));
    const layout = { defaultStackWidth: 640, removedShelfHeight: 200, overviewShowTags: true };
    expect(putChannelSettings).toHaveBeenCalledWith(A, { layout });
    expect(useChannelSettingsStore.getState().doc?.layout).toEqual(layout);
    // A height save afterwards carries the toggle.
    act(() => useCollimateStore.getState().setShelfHeight(260));
    expect(putChannelSettings).toHaveBeenLastCalledWith(A, { layout: { ...layout, removedShelfHeight: 260 } });
    act(() => useCollimateStore.getState().setShowTags(false));
    expect(putChannelSettings).toHaveBeenLastCalledWith(A, { layout: { ...layout, removedShelfHeight: 260, overviewShowTags: false } });
    expect(putChannelSettings).toHaveBeenCalledTimes(3);
});

it('a forced reload of the same channel (a settings import) adopts the toggle it brings, saving nothing (tags toggle)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(200), loadedFor: A }));
    expect(useCollimateStore.getState().overview.showTags).toBe(false);
    act(() => useChannelSettingsStore.setState({ doc: doc(200, undefined, true) }));
    expect(useCollimateStore.getState().overview.showTags).toBe(true);
    act(() => useChannelSettingsStore.setState({ doc: doc(200) }));
    expect(useCollimateStore.getState().overview.showTags).toBe(false);
    expect(putChannelSettings).not.toHaveBeenCalled();
    // The next channel's load is adopted on load, not by the last channel's reread.
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, undefined, true), loadedFor: B }));
    expect(useCollimateStore.getState().overview.showTags).toBe(true);
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('adopts each channel\'s remove zones when its settings load, none without them (TG-06)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, TWO), loadedFor: A }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual(TWO);
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: B }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual([]);
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('saves the remove zones 300ms after the last change, to the doc and the server (TG-06)', () => {
    vi.useFakeTimers();
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }));
    act(() => useCollimateStore.getState().setRemoveZones(TWO));
    act(() => useCollimateStore.getState().setRemoveZones([{ ...TWO[0], width: 0.6 }, { ...TWO[1], width: 0.4 }]));
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS - 1); });
    expect(putChannelSettings).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(1); });
    const zones = [{ ...TWO[0], width: 0.6 }, { ...TWO[1], width: 0.4 }];
    expect(putChannelSettings).toHaveBeenCalledExactlyOnceWith(A, { remove_zones: { zones } });
    expect(useChannelSettingsStore.getState().doc?.remove_zones).toEqual({ zones });
    // Reset to one zone saves the empty list.
    act(() => useCollimateStore.getState().setRemoveZones([]));
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(putChannelSettings).toHaveBeenLastCalledWith(A, { remove_zones: { zones: [] } });
});

it('a change pending at a channel switch is saved to its own channel; a reread of the doc brings its zones (TG-06)', () => {
    vi.useFakeTimers();
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }));
    act(() => useCollimateStore.getState().setRemoveZones(TWO));
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: B }));
    expect(putChannelSettings).toHaveBeenCalledExactlyOnceWith(A, { remove_zones: { zones: TWO } });
    expect(useCollimateStore.getState().overview.removeZones).toEqual([]);
    // A forced reload of B (a settings import) keeps loadedFor and brings new zones.
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, TWO) }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual(TWO);
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(putChannelSettings).toHaveBeenCalledTimes(1);
});

it('the overview has no zones while the next channel\'s settings load, and nothing is saved to the last (TG-06)', () => {
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, TWO), loadedFor: A }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual(TWO);
    // The switch to B: its document is still on its way (or never comes).
    act(() => useChannelSettingsStore.setState({ loadedFor: null }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual([]);
    expect(putChannelSettings).not.toHaveBeenCalled();
    act(() => useChannelSettingsStore.setState({ doc: doc(undefined, TWO), loadedFor: B }));
    expect(useCollimateStore.getState().overview.removeZones).toEqual(TWO);
    expect(putChannelSettings).not.toHaveBeenCalled();
});

it('a tag zone with no tag yet is not saved; its tag saves it, and a channel switch saves the zones without it (TG-06)', () => {
    vi.useFakeTimers();
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }));
    const blank: RemoveZone[] = [
        { id: 'z1', width: 0.5, action: { kind: 'add_tag', tag: '' } },
        { id: 'z2', width: 0.5, action: { kind: 'remove' } },
    ];
    act(() => useCollimateStore.getState().setRemoveZones(blank));
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(putChannelSettings).not.toHaveBeenCalled();
    expect(useChannelSettingsStore.getState().doc?.remove_zones).toEqual({ zones: [] });
    // The zone being edited stays on show.
    expect(useCollimateStore.getState().overview.removeZones).toEqual(blank);
    act(() => useCollimateStore.getState().setRemoveZones(TWO));
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(putChannelSettings).toHaveBeenCalledExactlyOnceWith(A, { remove_zones: { zones: TWO } });
    // Left blank again when the channel switches: A keeps what the server would.
    act(() => useCollimateStore.getState().setRemoveZones([...TWO, { id: 'z3', width: 0, action: { kind: 'remove_tag', tag: ' ' } }]));
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: B }));
    expect(putChannelSettings).toHaveBeenCalledTimes(1);
    act(() => useCollimateStore.getState().setRemoveZones(blank));
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }));
    expect(putChannelSettings).toHaveBeenLastCalledWith(B, { remove_zones: { zones: [{ ...blank[1], width: 1 }] } });
});

it('a failed save warns and keeps the edit in the doc, for the height, the toggle and the zones', async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.mocked(putChannelSettings).mockRejectedValue(new Error('offline'));
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(), loadedFor: A }));
    act(() => useCollimateStore.getState().setShelfHeight(240));
    act(() => useCollimateStore.getState().setShowTags(true));
    act(() => useCollimateStore.getState().setRemoveZones(TWO));
    await act(async () => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(warn.mock.calls.map(call => call[0])).toEqual([
        'saving the Removed shelf height failed',
        'saving the overview tag toggle failed',
        'saving the remove zones failed',
    ]);
    expect(useChannelSettingsStore.getState().doc).toMatchObject({
        layout: { removedShelfHeight: 240, overviewShowTags: true },
        remove_zones: { zones: TWO },
    });
});

it('an edit made after the next channel\'s settings land, before the overview adopts them, saves to neither channel', () => {
    vi.useFakeTimers();
    renderHook(() => useOverviewPersistence());
    act(() => useChannelSettingsStore.setState({ doc: doc(200), loadedFor: A }));
    act(() => {
        useChannelSettingsStore.setState({ doc: doc(), loadedFor: B });
        useCollimateStore.getState().setShelfHeight(250);
        useCollimateStore.getState().setShowTags(true);
        useCollimateStore.getState().setRemoveZones(TWO);
    });
    act(() => { vi.advanceTimersByTime(REMOVE_ZONES_SAVE_MS); });
    expect(putChannelSettings).not.toHaveBeenCalled();
    const { overview } = useCollimateStore.getState();
    expect([overview.shelfHeight, overview.showTags, overview.removeZones]).toEqual([SHELF_HEIGHT_DEFAULT, false, []]);
});
