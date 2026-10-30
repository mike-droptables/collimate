/** A Files pane's in-cell drop target: an inset dashed teal frame over the
 *  pane with a tag naming where the dropped files land ("Add to <cell>",
 *  plus the folder when it is not the cell root). Distinct from the canvas's
 *  NEW CELL target, a bar between stacks: this one is a frame inside a
 *  cell. It paints no fill, so the tree's lit folder row stays readable,
 *  and takes no pointer events. Lit by useInCellDropFrame. */
export function InCellDropFrame({ cellName, folder = '' }: { cellName?: string; folder?: string }) {
    const label = `Add to ${cellName || 'this cell'}${folder ? ` › ${folder}/` : ''}`;
    return (
        <div
            data-in-cell-drop-frame
            className="pointer-events-none absolute inset-1 z-10 flex items-end justify-center rounded-md border-2 border-dashed border-teal p-2"
        >
            <span className="max-w-full truncate rounded-sm border border-teal bg-sheet px-2 py-0.5 text-[0.71875rem] font-medium text-teal shadow-(--shadow)">
                {label}
            </span>
        </div>
    );
}
