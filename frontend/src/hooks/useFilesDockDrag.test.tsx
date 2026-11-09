// The Files pane's drag-to-dock (FP-04): useDockDrag over the cell's pane row
// with the pane's two sides. A release docks the pane on the nearer of its
// left or top edge and shows it; the store names the Files pane.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, fireEvent, cleanup } from '@testing-library/react';

import { FILES_DOCK_REGION_ATTR, useFilesDockDrag } from './useFilesDockDrag';
import { createDefaultLayout, setFilesPane } from '@/lib/cellLayout';
import { useCollimateStore } from '@/stores/useCollimateStore';
import type { CellLayout } from '@/stores/collimate/types';

const REGION = { left: 100, top: 50, right: 500, bottom: 350, width: 400, height: 300, x: 100, y: 50, toJSON: () => ({}) } as DOMRect;

function Harness({ onLayoutChange }: { onLayoutChange: (fn: (l: CellLayout) => CellLayout) => void }) {
    const handlers = useFilesDockDrag('cell-1', onLayoutChange);
    return (
        <div {...{ [FILES_DOCK_REGION_ATTR]: '' }} data-testid="region">
            <span {...handlers}><button type="button">Hide files</button></span>
        </div>
    );
}

const base = (): CellLayout => setFilesPane(createDefaultLayout({ filePaths: [], pinnedPath: null }), { collapsed: true });
const drag = () => useCollimateStore.getState().dockDrag;

beforeEach(() => { useCollimateStore.setState({ dockDrag: null }); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); useCollimateStore.setState({ dockDrag: null }); });

describe('useFilesDockDrag (FP-04)', () => {
    it('the region is the pane row', () => {
        expect(FILES_DOCK_REGION_ATTR).toBe('data-files-dock-region');
    });

    it.each([
        ['left', 'left', 110, 200],
        ['top', 'top', 300, 60],
        ['right', 'top', 490, 200],
        ['bottom', 'left', 300, 340],
    ] as const)('a release near the %s edge docks the pane on the %s and shows it (FP-04)', (_edge, side, x, y) => {
        const onLayoutChange = vi.fn();
        const view = render(<Harness onLayoutChange={onLayoutChange} />);
        vi.spyOn(view.getByTestId('region'), 'getBoundingClientRect').mockReturnValue(REGION);
        const button = view.getByRole('button');
        fireEvent.pointerDown(button, { button: 0, pointerId: 1, clientX: 120, clientY: 60 });
        fireEvent.pointerMove(document.body, { pointerId: 1, clientX: x, clientY: y });
        expect(drag()).toEqual({ cellId: 'cell-1', pane: 'files', side });
        fireEvent.pointerUp(document.body, { pointerId: 1, clientX: x, clientY: y });
        expect(drag()).toBeNull();
        expect(onLayoutChange).toHaveBeenCalledTimes(1);
        const next = (onLayoutChange.mock.calls[0][0] as (l: CellLayout) => CellLayout)(base());
        expect(next.filesPane).toMatchObject({ side, collapsed: false });
    });
});
