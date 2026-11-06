import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useMaxWidthTracker } from './useMaxWidthTracker';
import { maxStackWidthFor } from './useStackLayout';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { COMMAND_PANE_WIDTH, defaultCommandPane } from '@/stores/collimate/commandPaneSlice';

const spatial = () => useSpatialStore.getState();
const pane = () => useCollimateStore.getState();
const initialSpatial = useSpatialStore.getState();
let innerWidth = 0;

beforeEach(() => {
    innerWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 });
    useSpatialStore.setState(initialSpatial, true);
    useCollimateStore.setState({ commandPane: defaultCommandPane() });
});
afterEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: innerWidth });
    useCollimateStore.setState({ commandPane: defaultCommandPane() });
});

describe('the max-width tracker', () => {
    it('keeps a max-width shared width at the full width the open Commands pane leaves, and gives it back when the pane closes (polish)', () => {
        act(() => {
            spatial().setIsMaxWidth(true);
            spatial().setMinCellSize(500);
        });
        renderHook(() => useMaxWidthTracker());
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1440));
        act(() => pane().openCommandPane());
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1440 - COMMAND_PANE_WIDTH));
        act(() => pane().setCommandPaneSide('right'));
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1440 - COMMAND_PANE_WIDTH));
        // A window resize while it is open tracks the free width too.
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 });
        act(() => { window.dispatchEvent(new Event('resize')); });
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1200 - COMMAND_PANE_WIDTH));
        act(() => pane().closeCommandPane());
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1200));
    });

    it('does not shrink a width that is not max for the pane: the layout clamps it while the pane is open (polish)', () => {
        act(() => {
            spatial().setIsMaxWidth(false);
            spatial().setMinCellSize(1300);
        });
        renderHook(() => useMaxWidthTracker());
        expect(spatial().minCellSize).toBe(1300);
        act(() => pane().openCommandPane());
        expect(spatial().minCellSize).toBe(1300);
        // A narrower window still clamps it down for good.
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1000 });
        act(() => { window.dispatchEvent(new Event('resize')); });
        expect(spatial().minCellSize).toBe(maxStackWidthFor(1000));
    });

    it('keeps a column maximised on its own at the full width as the pane opens and closes, its max flag kept (polish)', () => {
        const full = maxStackWidthFor(1440);
        act(() => {
            spatial().setStackWidth('s1', full);
            spatial().setColumnMax('s1', true);
            spatial().setStackWidth('s2', 600);
        });
        renderHook(() => useMaxWidthTracker());
        act(() => pane().openCommandPane());
        expect(spatial().stackWidths.s1).toBe(maxStackWidthFor(1440 - COMMAND_PANE_WIDTH));
        expect(spatial().maxColumns).toEqual({ s1: true });
        act(() => pane().closeCommandPane());
        expect(spatial().stackWidths.s1).toBe(full);
        expect(spatial().maxColumns).toEqual({ s1: true });
        // A column that is not maximised keeps its stored width.
        expect(spatial().stackWidths.s2).toBe(600);
    });
});
