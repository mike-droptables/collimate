// Drag-to-dock for the status pane (hooks/useDockDrag): the cell header's
// caret and the Hide icon on the pane's rail both carry it. The hit area is
// the cell's runs dock region (CellEditor: the Files row and the pane); a
// release docks the pane on its right or bottom edge and opens it.

import { RUNS_PANE_SIDES, dockRunsPane } from '@/lib/cellLayout';
import { useDockDrag, type DockDragHandlers } from '@/hooks/useDockDrag';
import type { CellLayout } from '@/stores/collimate/types';

/** The attribute on the cell's runs dock region (CellEditor): the drag's hit area. */
export const RUNS_DOCK_REGION_ATTR = 'data-runs-dock-region';

export function useRunsDockDrag(
    cellId: string | number,
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void,
): DockDragHandlers {
    return useDockDrag({
        cellId, pane: 'runs', regionAttr: RUNS_DOCK_REGION_ATTR, sides: RUNS_PANE_SIDES,
        onDock: side => onLayoutChange(l => dockRunsPane(l, side)),
    });
}
