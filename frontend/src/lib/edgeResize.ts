// Card-side resize strips — the pure half.
//
// Every visible column in stack view (fan columns included) can be resized
// by dragging either side of its card BELOW the header, down to the card's
// bottom. Each side carries a hit strip over the card's border plus half
// the gap outside it, so the gap's midline splits the left column's right
// strip from the right column's left strip. Beside the header rows the gap
// belongs to the cell-diff hover zone (lib/cellDiff), which spans the UNION
// of both neighbours' header bands — so a strip's gap half starts below
// both headers.
//
// The drag is exact: the dragged edge follows the pointer and the opposite
// edge stays put (useStackLayout's width-drag anchor, the camera parked),
// and the write is always that one column's override.

import type { ZoneRect } from '@/lib/cellDiff';

/** How far a strip reaches inside the card edge, in world px: the card's
 *  1 px border and no further. Measured live (compact main view,
 *  1600x900): below the header, content controls start right inside the
 *  border on both sides — the collapsed Files rail (a full-height button,
 *  x 1..25), the Files pane's Import / export link, the
 *  tab bar's "Expand the cell view" button (1 px from the right edge), the
 *  click-to-focus overlay and a renderer iframe (2 px) whose own scrollbar
 *  hugs its right edge. So the grab lives on the border plus the outer
 *  half of the gap (8 px in all). Re-measure before widening it. */
export const EDGE_RESIZE_INSET = 1;

/** Width of the lit side, like the pan-align edge glow. */
export const EDGE_GLOW_WIDTH = 3;

/** How long the pointer rests on a strip before its side lights. A pointer
 *  crossing the strip on its way elsewhere is gone well inside this, so
 *  only a pointer that stays on the edge lights it; a press lights it at
 *  once. */
export const EDGE_HOVER_ARM_MS = 180;

/** The lit side's fade, in and out. */
export const EDGE_GLOW_FADE_MS = 140;

export type EdgeSide = 'left' | 'right';

/** One display column's card, in world coordinates. */
export interface EdgeResizeColumn {
    id: string;
    left: number;
    right: number;
    cardTop: number;
    cardBottom: number;
    /** Bottom of the header, world y. */
    headerBottomWorldY: number;
}

export interface EdgeResizeSideGeometry {
    side: EdgeSide;
    /** Hit rects: the card-inside part (from this card's header down) and
     *  the gap part (from below both neighbours' headers down). */
    rects: ZoneRect[];
    /** The lit 3 px bar inside the card edge, the card's full height. */
    glow: ZoneRect;
}

export interface EdgeResizeColumnGeometry {
    id: string;
    left: EdgeResizeSideGeometry;
    right: EdgeResizeSideGeometry;
}

function sideGeometry(
    col: EdgeResizeColumn,
    side: EdgeSide,
    neighbour: EdgeResizeColumn | null,
    gap: number,
    inset: number,
): EdgeResizeSideGeometry {
    const edge = side === 'left' ? col.left : col.right;
    const bottom = col.cardBottom;
    // Half the gap to the neighbour (the midline splits the two strips);
    // an outer column gets half the standard gap.
    const outer = neighbour
        ? Math.max(0, (side === 'left' ? col.left - neighbour.right : neighbour.left - col.right) / 2)
        : gap / 2;
    const gapTop = neighbour ? Math.max(col.headerBottomWorldY, neighbour.headerBottomWorldY) : col.headerBottomWorldY;
    const rects: ZoneRect[] = [];
    const inner = { top: col.headerBottomWorldY, left: side === 'left' ? edge : edge - inset, width: inset };
    const outerRect = { top: gapTop, left: side === 'left' ? edge - outer : edge, width: outer };
    if (outer > 0 && outerRect.top === inner.top) {
        rects.push({
            left: Math.min(inner.left, outerRect.left), top: inner.top,
            width: inset + outer, height: bottom - inner.top,
        });
    } else {
        if (bottom > inner.top) rects.push({ ...inner, height: bottom - inner.top });
        if (outer > 0 && bottom > outerRect.top) rects.push({ ...outerRect, height: bottom - outerRect.top });
    }
    return {
        side,
        rects,
        glow: {
            left: side === 'left' ? edge : edge - EDGE_GLOW_WIDTH,
            top: col.cardTop,
            width: EDGE_GLOW_WIDTH,
            height: col.cardBottom - col.cardTop,
        },
    };
}

/** Both sides' strips for every column of the row (left to right). A
 *  neighbour counts only when there is a gap between the two cards. */
export function edgeResizeGeometry(
    columns: EdgeResizeColumn[],
    gap: number,
    inset: number = EDGE_RESIZE_INSET,
): EdgeResizeColumnGeometry[] {
    return columns.map((col, i) => {
        const prev = i > 0 && columns[i - 1].right <= col.left ? columns[i - 1] : null;
        const next = i < columns.length - 1 && columns[i + 1].left >= col.right ? columns[i + 1] : null;
        return {
            id: col.id,
            left: sideGeometry(col, 'left', prev, gap, inset),
            right: sideGeometry(col, 'right', next, gap, inset),
        };
    });
}

/** The column width for a drag of `dxScreen` screen px from `startWidth`:
 *  the right side grows with the pointer moving right, the left side with
 *  it moving left; world px = screen px / zoom; clamped to [min, max]. */
export function edgeDragWidth(
    startWidth: number, dxScreen: number, zoom: number, side: EdgeSide, min: number, max: number,
): number {
    const dx = dxScreen / (zoom > 0 ? zoom : 1);
    const w = side === 'right' ? startWidth + dx : startWidth - dx;
    return Math.max(min, Math.min(Math.max(min, max), w));
}

/** The edge that stays put while `side` is dragged. */
export const anchorFor = (side: EdgeSide): EdgeSide => (side === 'right' ? 'left' : 'right');
