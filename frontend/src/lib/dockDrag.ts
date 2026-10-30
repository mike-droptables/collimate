// The drag-to-dock geometry shared by the cell's docking panes
// (hooks/useDockDrag): which of a pane's allowed edges of the drag's region
// a pointer is nearest to.

/** An edge of a cell a pane may dock on. */
export type DockSide = 'left' | 'right' | 'top' | 'bottom';

export interface DockRect {
    left: number;
    top: number;
    right: number;
    bottom: number;
}

/** The edge of `rect` nearest to (x, y) among `sides`, measured as a
 *  fraction of the rect's width or height, so with all four sides allowed
 *  the diagonals split the rect into four triangles: a point is on the side
 *  whose triangle holds it, whatever the rect's aspect. An edge not in
 *  `sides` never wins. Null outside the rect (a release there docks
 *  nothing), for an empty rect or for no sides. A tie goes to the side
 *  listed first. */
export function nearestDockSide<S extends DockSide>(rect: DockRect, x: number, y: number, sides: readonly S[]): S | null {
    const width = rect.right - rect.left;
    const height = rect.bottom - rect.top;
    if (width <= 0 || height <= 0) return null;
    if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) return null;
    const distance: Record<DockSide, number> = {
        left: (x - rect.left) / width,
        top: (y - rect.top) / height,
        right: (rect.right - x) / width,
        bottom: (rect.bottom - y) / height,
    };
    let best: S | null = null;
    for (const side of sides) if (best === null || distance[side] < distance[best]) best = side;
    return best;
}
