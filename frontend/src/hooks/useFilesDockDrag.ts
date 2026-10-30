// Drag-to-dock for the Files pane (hooks/useDockDrag): the Hide icon on the
// pane's rail and the tab strip's Show files button both carry it. The hit
// area is the cell's pane row; a release docks the pane on its left or top
// edge and shows it.

import { FILES_PANE_SIDES, dockFilesPane } from '@/lib/cellLayout';
import { useDockDrag, type DockDragHandlers } from '@/hooks/useDockDrag';
import type { CellLayout } from '@/stores/collimate/types';

/** The attribute on the cell's pane row (CellEditor): the drag's hit area. */
export const FILES_DOCK_REGION_ATTR = 'data-files-dock-region';

export function useFilesDockDrag(
    cellId: string | number,
    onLayoutChange: (fn: (layout: CellLayout) => CellLayout) => void,
): DockDragHandlers {
    return useDockDrag({
        cellId, pane: 'files', regionAttr: FILES_DOCK_REGION_ATTR, sides: FILES_PANE_SIDES,
        onDock: side => onLayoutChange(l => dockFilesPane(l, side)),
    });
}
