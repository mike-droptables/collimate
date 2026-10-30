import React from 'react';
import type { DockSide } from '@/lib/dockDrag';

/** Where each side's band sits in the pane region, inset 4px like the
 *  in-cell drop frame. */
const PLACE: Record<DockSide, string> = {
    left: 'left-1 top-1 bottom-1',
    right: 'right-1 top-1 bottom-1',
    top: 'top-1 left-1 right-1',
    bottom: 'bottom-1 left-1 right-1',
};

/** A column's width or a band's height, as the docked pane would be. */
export type DockPreviewSize =
    | { width: number; maxWidth: string }
    | { height: number; maxHeight: string };

/** The drop target of a pane's dock drag (hooks/useDockDrag): a band on the
 *  edge the pane would dock on, as wide (a column) or as tall (a band) as
 *  the pane would be there, in the InCellDropFrame look (dashed teal, a teal
 *  tag naming the drop). CellEditor renders it in the pane region while the
 *  drag is over the cell; it takes no pointer events. */
export function DockPreview({ side, size, label }: {
    side: DockSide;
    size: DockPreviewSize;
    /** The tag, e.g. "Dock files on the left". */
    label: string;
}) {
    return (
        <div
            data-dock-preview={side}
            className={`pointer-events-none absolute z-20 flex items-center justify-center rounded-md border-2 border-dashed border-teal p-2 ${PLACE[side]}`}
            style={size}
        >
            <span className="max-w-full truncate rounded-sm border border-teal bg-sheet px-2 py-0.5 text-[0.71875rem] font-medium text-teal shadow-(--shadow)">
                {label}
            </span>
        </div>
    );
}
