// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { fullStackWidth, isMaxWidthOn, setChannelDefaultWidth, toggleMaxWidth } from './stackWidth';
import { NO_INSET } from './commandPane';
import { putChannelSettings, type ChannelSettings } from '@/lib/api';
import { DEFAULT_MIN_CELL_SIZE, useSpatialStore } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { COMMAND_PANE_WIDTH, defaultCommandPane } from '@/stores/collimate/commandPaneSlice';
import { maxStackWidthFor } from '@/hooks/useStackLayout';

// No network: the settings load never settles (the tests seed the doc),
// and saves resolve at once.
vi.mock('@/lib/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/lib/api')>()),
    fetchChannelSettings: vi.fn(() => new Promise(() => {})),
    putChannelSettings: vi.fn(() => Promise.resolve({})),
}));

const commandsPaneOpen = (open: boolean) =>
    useCollimateStore.setState({ commandPane: { ...defaultCommandPane(), open } } as never);
const channelDefault = () => useChannelSettingsStore.getState().doc?.layout?.defaultStackWidth;
const seedDefault = (defaultStackWidth: number | null, extra: Record<string, unknown> = {}) =>
    useChannelSettingsStore.setState({
        doc: { version: 1, kind: 'channel-settings', layout: { defaultStackWidth, ...extra } } as unknown as ChannelSettings,
        loadedFor: '11111111-1111-4111-8111-111111111111',
    });

beforeEach(() => {
    useSpatialStore.setState({
        minCellSize: DEFAULT_MIN_CELL_SIZE, stackWidths: {}, maxColumns: {},
        allStacksByChannel: {}, isMaxWidth: false,
    });
    useCollimateStore.setState({ activeChannel: { id: '11111111-1111-4111-8111-111111111111', name: 'chan' } } as never);
    seedDefault(null);
    commandsPaneOpen(false);
    vi.mocked(putChannelSettings).mockClear();
});

describe('lib/stackWidth — the one max operator', () => {
    it('the full width is the viewport\'s, less the Commands pane while it is open (polish)', () => {
        expect(fullStackWidth()).toBe(maxStackWidthFor(window.innerWidth));
        commandsPaneOpen(true);
        expect(fullStackWidth()).toBe(maxStackWidthFor(window.innerWidth - COMMAND_PANE_WIDTH));
        expect(fullStackWidth(1600, NO_INSET)).toBe(maxStackWidthFor(1600));
        expect(fullStackWidth(1600, { left: COMMAND_PANE_WIDTH, right: 0 })).toBe(maxStackWidthFor(1600 - COMMAND_PANE_WIDTH));
        expect(fullStackWidth(1600, { left: 0, right: COMMAND_PANE_WIDTH })).toBe(maxStackWidthFor(1600 - COMMAND_PANE_WIDTH));
    });

    it('maximising from 420 stores 420 as the channel default, and the second toggle returns to 420 (polish)', () => {
        seedDefault(640, { commandPane: { open: false } });
        useSpatialStore.setState({ minCellSize: 420 });
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState()).toMatchObject({ minCellSize: maxStackWidthFor(window.innerWidth), isMaxWidth: true });
        expect(channelDefault()).toBe(420);
        // The save carries the layout section's other keys.
        expect(putChannelSettings).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 420, commandPane: { open: false } } });
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 420, isMaxWidth: false });
        // Un-maximising stores nothing.
        expect(putChannelSettings).toHaveBeenCalledTimes(1);
    });

    it('maximising with the pane open fills the canvas the pane leaves, and un-maximising returns to the width before (polish)', () => {
        commandsPaneOpen(true);
        useSpatialStore.setState({ minCellSize: 420 });
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState()).toMatchObject({
            minCellSize: maxStackWidthFor(window.innerWidth - COMMAND_PANE_WIDTH), isMaxWidth: true,
        });
        expect(isMaxWidthOn('s', 'ch')).toBe(true);
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState()).toMatchObject({ minCellSize: 420, isMaxWidth: false });
    });

    it('shared mode stores the width of the column it came from (polish)', () => {
        useSpatialStore.setState({ minCellSize: 700, stackWidths: { s: 540 } });
        toggleMaxWidth('s', 'ch');
        expect(channelDefault()).toBe(540);
        expect(useSpatialStore.getState().stackWidths).toEqual({});
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState().minCellSize).toBe(540);
    });

    it('toggling an already-full stack stores nothing and goes to the channel default (polish)', () => {
        seedDefault(640);
        useSpatialStore.setState({ minCellSize: fullStackWidth() });
        toggleMaxWidth('s', 'ch');
        expect(putChannelSettings).not.toHaveBeenCalled();
        expect(useSpatialStore.getState().minCellSize).toBe(640);
        useSpatialStore.setState({ allStacksByChannel: { ch: false }, stackWidths: { s: fullStackWidth() } });
        toggleMaxWidth('s', 'ch');
        expect(putChannelSettings).not.toHaveBeenCalled();
        expect(channelDefault()).toBe(640);
        expect(useSpatialStore.getState().stackWidths.s).toBe(640);
    });

    it('per stack, a column as wide as the pane leaves reads as maximised (polish)', () => {
        commandsPaneOpen(true);
        const full = maxStackWidthFor(window.innerWidth - COMMAND_PANE_WIDTH);
        useSpatialStore.setState({ allStacksByChannel: { ch: false }, stackWidths: { s: full } });
        expect(isMaxWidthOn('s', 'ch')).toBe(true);
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState().stackWidths.s).toBe(DEFAULT_MIN_CELL_SIZE);
        expect(isMaxWidthOn('s', 'ch')).toBe(false);
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState().stackWidths.s).toBe(full);
        expect(useSpatialStore.getState().maxColumns).toEqual({ s: true });
    });

    it('per column ("all stacks" off): the column\'s width before becomes the channel default and un-maximising sets the column back to it (polish)', () => {
        commandsPaneOpen(true);
        useSpatialStore.setState({ allStacksByChannel: { ch: false }, stackWidths: { s: 420, other: 610 } });
        toggleMaxWidth('s', 'ch');
        expect(channelDefault()).toBe(420);
        expect(useSpatialStore.getState().maxColumns).toEqual({ s: true });
        expect(useSpatialStore.getState().minCellSize).toBe(DEFAULT_MIN_CELL_SIZE);
        commandsPaneOpen(false);
        // Narrower than the full width now (the tracker widens it), still on.
        expect(useSpatialStore.getState().stackWidths.s).toBeLessThan(fullStackWidth());
        expect(isMaxWidthOn('s', 'ch')).toBe(true);
        toggleMaxWidth('s', 'ch');
        expect(useSpatialStore.getState().stackWidths).toEqual({ s: 420, other: 610 });
        expect(useSpatialStore.getState().maxColumns).toEqual({});
        expect(isMaxWidthOn('s', 'ch')).toBe(false);
    });

    it('"all stacks" off with no column toggles nothing and stores nothing (polish)', () => {
        useSpatialStore.setState({ allStacksByChannel: { ch: false } });
        toggleMaxWidth(null, 'ch');
        expect(putChannelSettings).not.toHaveBeenCalled();
        expect(useSpatialStore.getState()).toMatchObject({ minCellSize: DEFAULT_MIN_CELL_SIZE, isMaxWidth: false, stackWidths: {} });
    });
});

describe('lib/stackWidth — the channel default ("Set current")', () => {
    it('stores the width in the layout section, keeping its other keys (polish)', () => {
        seedDefault(null, { commandPane: { open: true } });
        setChannelDefaultWidth(560);
        expect(useChannelSettingsStore.getState().doc!.layout).toEqual({ defaultStackWidth: 560, commandPane: { open: true } });
        expect(putChannelSettings).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { layout: { defaultStackWidth: 560, commandPane: { open: true } } });
    });

    it('stores nothing while the channel\'s settings are not loaded, so no section is overwritten blind (polish)', () => {
        useChannelSettingsStore.setState({ doc: null, loadedFor: null });
        setChannelDefaultWidth(560);
        seedDefault(640);
        setChannelDefaultWidth(560, 'another');
        useCollimateStore.setState({ activeChannel: null } as never);
        setChannelDefaultWidth(560);
        expect(putChannelSettings).not.toHaveBeenCalled();
        expect(channelDefault()).toBe(640);
    });
});
