import { memo } from 'react';
import type { MotionValue } from 'motion/react';
import type { Stack } from '@/lib/types';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { columnCardRect } from '@/hooks/useStackLayout';
import {
    cellDiffPairs, cellDiffZoneGeometry, DEFAULT_CELL_HEADER_BOTTOM_OFFSET_PX, type CellDiffColumnBand,
} from '@/lib/cellDiff';
import { frontCellOf } from '@/lib/frontSlots';
import { edgeResizeGeometry, type EdgeResizeColumn } from '@/lib/edgeResize';
import { CardEdgeResize } from './CardEdgeResize';
import { CellDiffZone } from './CellDiffZone';

/** A laid-out display column (useStackDrag's items). */
export type OverlayColumn = Stack & { x: number; computedWidth: number; isOffscreen: boolean };

/** The world-space overlays drawn against each card's MEASURED header
 *  (useSpatialStore.cellChrome): the cell-diff hover zones in the gaps
 *  beside the header rows and the card-side resize strips below them. A
 *  component of its own so a header report re-renders these overlays
 *  only, not the whole scene. */
export const CardChromeOverlays = memo(function CardChromeOverlays({
    columns, visibleTopIndices, frame, gap, screenMax, zoom,
    edgeResizeActive, diffZonesActive, onSelectColumn,
}: {
    columns: OverlayColumn[];
    visibleTopIndices: Record<string, number> | undefined;
    /** Where the cards sit (columnCardRect). */
    frame: { stackTopOffset: number; stripHeight: number; cardPixelHeight: number };
    gap: number;
    screenMax: number;
    zoom: MotionValue<number>;
    /** The resize strips stay up through a width drag (one may own it). */
    edgeResizeActive: boolean;
    diffZonesActive: boolean;
    /** A resize strip's drag selects its column (display id). */
    onSelectColumn: (displayId: string) => void;
}) {
    const cellChrome = useSpatialStore(s => s.cellChrome);
    const widthDragId = useSpatialStore(s => s.widthDrag?.id ?? null);
    const headerBottomOffsetOf = (cellId: string | undefined) =>
        (cellId ? cellChrome[cellId] : undefined) ?? DEFAULT_CELL_HEADER_BOTTOM_OFFSET_PX;

    const strips = edgeResizeActive && (() => {
        const shown = columns.filter(c => c.computedWidth > 0);
        const cards: EdgeResizeColumn[] = shown.map(column => {
            const card = columnCardRect(column, frame);
            // The manage card (no front cell) has a cell's 36px head: the
            // default header offset places its strips.
            const cell = frontCellOf(column, visibleTopIndices);
            return {
                id: column.id,
                left: card.left,
                right: card.right,
                cardTop: card.top,
                cardBottom: card.bottom,
                headerBottomWorldY: card.top + headerBottomOffsetOf(cell ? String(cell.id) : undefined),
            };
        });
        return edgeResizeGeometry(cards, gap).map((g, i) => {
            const column = shown[i];
            // An offscreen column keeps the strips of the one it may be
            // mid-drag on (capture).
            if (column.isOffscreen && widthDragId !== column.id) return null;
            return [g.left, g.right].map(side => (
                <CardEdgeResize
                    key={`edge-${column.id}-${side.side}`}
                    columnId={column.id}
                    geometry={side}
                    width={column.computedWidth}
                    screenMax={screenMax}
                    zoom={zoom}
                    onDragStart={() => onSelectColumn(column.id)}
                />
            ));
        });
    })();

    const zones = diffZonesActive && cellDiffPairs(columns, visibleTopIndices).map(({ left, right, leftCell, rightCell }) => {
        if (left.isOffscreen && right.isOffscreen) return null;
        const band = (column: OverlayColumn, cellId: string): CellDiffColumnBand => {
            const card = columnCardRect(column, frame);
            return { left: card.left, right: card.right, top: card.top, headerBottomOffsetPx: headerBottomOffsetOf(cellId) };
        };
        const leftId = String(leftCell.id);
        const rightId = String(rightCell.id);
        const geometry = cellDiffZoneGeometry(band(left, leftId), band(right, rightId));
        if (!geometry) return null;
        return (
            <CellDiffZone
                key={`cell-diff-${left.id}-${right.id}`}
                geometry={geometry}
                leftCellId={leftId}
                rightCellId={rightId}
                leftName={leftCell.name}
                rightName={rightCell.name}
            />
        );
    });

    return (
        <>
            {/* Card-side resize strips (D-C18b) — both sides of every
                visible column, from the 36px header down to the card
                bottom (the diff zones own the header rows). Hover lights
                the side; a drag turns it teal and sets that column's width
                with the opposite edge still. */}
            {strips}
            {/* Cell-diff hover zones (D-C18a) — in the gap between each
                pair of adjacent columns, from the card tops down to the
                headers' bottoms.
                Hover says "Compare"; a click puts the right column's
                visible cell in diff against the left one's (store
                cellDiffs, by CELL id). */}
            {zones}
        </>
    );
});
