// Cell-pair diff — the pure half.
//
// A cell in diff shows its files diffed against the files of a BASE cell
// (live: whatever that cell holds now, wherever it sits). The state is a
// map from the diffing cell's id to its base cell's id, kept in the
// collimate store (`cellDiffs`). Every id here is a CELL id — never a
// stack id and never a fan column's display id (`fan-<cellId>`).
//
// The scene enters a diff from a hover zone in the gap between two
// adjacent display columns (StackScene / CellDiffZone): the right
// column's front cell diffs against the left column's front cell. A
// Capabilities stack showing its manage card has no front cell, so it
// takes part in no zone.
// The geometry of those zones lives here so it can be unit-tested.

import type { Cell, Channel, Stack } from '@/lib/types';
import { frontCellOf } from '@/lib/frontSlots';

export type CellDiffs = Record<string, string>;

/** Every cell id of a channel (main and nav stacks). */
function channelCellIds(channel: Channel): Set<string> {
    const ids = new Set<string>();
    for (const stacks of [channel.main_stacks, channel.nav_stacks]) {
        for (const stack of stacks ?? []) {
            for (const cell of stack.cells) ids.add(String(cell.id));
        }
    }
    return ids;
}

/** Drop every diff whose cell or base cell is no longer in `channel`.
 *  Returns `diffs` itself when nothing is dropped, so a store write can
 *  keep the old reference (and skip the re-render). No channel → empty. */
export function pruneCellDiffs(diffs: CellDiffs, channel: Channel | null): CellDiffs {
    const keys = Object.keys(diffs);
    if (keys.length === 0) return diffs;
    if (!channel) return {};
    const ids = channelCellIds(channel);
    let changed = false;
    const next: CellDiffs = {};
    for (const cellId of keys) {
        const baseId = diffs[cellId];
        if (ids.has(cellId) && ids.has(baseId)) next[cellId] = baseId;
        else changed = true;
    }
    return changed ? next : diffs;
}

/** Whether both cells exist in `channel` (the precondition to enter a diff). */
export function channelHasCells(channel: Channel | null, ...cellIds: string[]): boolean {
    if (!channel) return false;
    const ids = channelCellIds(channel);
    return cellIds.every(id => ids.has(id));
}

export interface CellDiffPair<C extends Stack = Stack> {
    left: C;
    right: C;
    leftCell: Cell;
    rightCell: Cell;
}

/** Adjacent display-column pairs that can enter a diff: both columns show
 *  a front cell (lib/frontSlots) and both have a laid-out width (a
 *  single-cell move source renders at width 0). */
export function cellDiffPairs<C extends Stack & { computedWidth: number }>(
    columns: C[],
    visibleTopIndices: Record<string, number> | undefined,
): CellDiffPair<C>[] {
    const pairs: CellDiffPair<C>[] = [];
    for (let i = 1; i < columns.length; i++) {
        const left = columns[i - 1];
        const right = columns[i];
        if (left.computedWidth <= 0 || right.computedWidth <= 0) continue;
        const leftCell = frontCellOf(left, visibleTopIndices);
        const rightCell = frontCellOf(right, visibleTopIndices);
        if (!leftCell || !rightCell) continue;
        pairs.push({ left, right, leftCell, rightCell });
    }
    return pairs;
}

/** How far the zone reaches past the gap into each neighbouring card, in
 *  world px. The closest control to a card's side edge sits 11px in — the
 *  36px head's run light 15px in on the left (its 14px inset plus the 1px
 *  border), its Remove tool 11px in on the right (10px plus the border) —
 *  so 6px never lands on one. Re-measure if a head inset changes. */
export const CELL_DIFF_ZONE_FUZZ = 6;

/** A card's header bottom assumed before it has been measured
 *  (useSpatialStore.cellChrome): the card's 1px top border + the 36px head,
 *  its hairline included (border-box). */
export const DEFAULT_CELL_HEADER_BOTTOM_OFFSET_PX = 37;

/** One column's side of a pair, in world coordinates. */
export interface CellDiffColumnBand {
    /** Card's left / right edge. */
    left: number;
    right: number;
    /** Card's top edge. */
    top: number;
    /** The header's bottom, px below `top` — the zone's band is the
     *  header row. */
    headerBottomOffsetPx: number;
}

export interface ZoneRect { left: number; top: number; width: number; height: number }

export interface CellDiffZoneGeometry {
    /** Hit rects (world px): the gap over the union of both header bands,
     *  plus `fuzz` px into each card over that card's own header. */
    rects: ZoneRect[];
    /** The gap's centre line and the union band — where the hover line
     *  and the "Compare" pill are drawn. */
    centerX: number;
    top: number;
    bottom: number;
    /** The gap's width. */
    gapWidth: number;
    /** The columns' card tops differ (a fan column beside a stack): the row
     *  above the band holds the fan frame's header on one side, whose
     *  controls run to the frame's edge, so the pill goes inside the gap
     *  instead of above the band. */
    pillInGap: boolean;
}

/** The hit geometry of the diff zone between a left and a right column.
 *  Null when the columns overlap or touch (no gap to hover — e.g. while a
 *  layout spring is mid-flight). */
export function cellDiffZoneGeometry(
    left: CellDiffColumnBand,
    right: CellDiffColumnBand,
    fuzz: number = CELL_DIFF_ZONE_FUZZ,
): CellDiffZoneGeometry | null {
    const gapLeft = left.right;
    const gapRight = right.left;
    if (gapRight - gapLeft <= 0) return null;
    const top = Math.min(left.top, right.top);
    const bottom = Math.max(left.top + left.headerBottomOffsetPx, right.top + right.headerBottomOffsetPx);
    const rects: ZoneRect[] = [
        { left: gapLeft, top, width: gapRight - gapLeft, height: bottom - top },
        { left: gapLeft - fuzz, top: left.top, width: fuzz, height: left.headerBottomOffsetPx },
        { left: gapRight, top: right.top, width: fuzz, height: right.headerBottomOffsetPx },
    ];
    return {
        rects, centerX: (gapLeft + gapRight) / 2, top, bottom,
        gapWidth: gapRight - gapLeft, pillInGap: left.top !== right.top,
    };
}
