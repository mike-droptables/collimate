import { describe, it, expect } from 'vitest';
import {
    pruneCellDiffs, channelHasCells, cellDiffPairs,
    cellDiffZoneGeometry, CELL_DIFF_ZONE_FUZZ, DEFAULT_CELL_HEADER_BOTTOM_OFFSET_PX,
    type CellDiffColumnBand,
} from '@/lib/cellDiff';
import type { Cell, Channel, Stack } from '@/lib/types';

const cell = (id: string, extra: Partial<Cell> = {}): Cell =>
    ({ id, name: id.toUpperCase(), files: [], ...extra } as Cell);
const stack = (id: string, cells: Cell[], extra: Partial<Stack> = {}): Stack =>
    ({ id, name: '', cells, ...extra });
const channel = (main: Stack[], nav: Stack[] = []): Channel => ({
    id: 'ch', name: 'ch', main_stacks: main, nav_stacks: nav,
    nav_selection_id: null, main_selection_id: null,
});
type Col = Stack & { computedWidth: number };
const col = (s: Stack, computedWidth = 400): Col => ({ ...s, computedWidth });

describe('pruneCellDiffs', () => {
    const ch = channel([stack('s1', [cell('a'), cell('b')]), stack('s2', [cell('c')])], [stack('n1', [cell('d')])]);

    it('keeps the same reference when every pair still exists (nav cells count)', () => {
        const diffs = { b: 'a', c: 'd' };
        expect(pruneCellDiffs(diffs, ch)).toBe(diffs);
    });

    it('drops a pair whose cell or base cell left the channel', () => {
        expect(pruneCellDiffs({ b: 'a', c: 'gone', gone: 'a' }, ch)).toEqual({ b: 'a' });
    });

    it('empties everything without a channel; an empty map passes through', () => {
        expect(pruneCellDiffs({ b: 'a' }, null)).toEqual({});
        const empty = {};
        expect(pruneCellDiffs(empty, null)).toBe(empty);
    });

    it('channelHasCells checks every id', () => {
        expect(channelHasCells(ch, 'a', 'c')).toBe(true);
        expect(channelHasCells(ch, 'a', 'x')).toBe(false);
        expect(channelHasCells(null, 'a')).toBe(false);
    });
});

describe('cellDiffPairs', () => {
    it('pairs every adjacent column by its VISIBLE cell (fan columns included)', () => {
        const cols = [
            col(stack('s1', [cell('a'), cell('b')])),
            col(stack('fan-c', [cell('c')], { synthetic: true, sourceStackId: 'S', fanIndex: 0 })),
            col(stack('fan-d', [cell('d')], { synthetic: true, sourceStackId: 'S', fanIndex: 1 })),
            col(stack('s3', [cell('e'), cell('f')])),
        ];
        const pairs = cellDiffPairs(cols, { s1: 1, s3: 1, S: 0 });
        expect(pairs.map(p => [p.left.id, p.leftCell.id, p.right.id, p.rightCell.id])).toEqual([
            ['s1', 'b', 'fan-c', 'c'],
            ['fan-c', 'c', 'fan-d', 'd'],
            ['fan-d', 'd', 's3', 'f'],
        ]);
    });

    it('skips pairs touching an empty stack or a zero-width column', () => {
        const cols = [
            col(stack('s1', [cell('a')])),
            col(stack('empty0', [])),
            col(stack('s2', [cell('b')])),
            col(stack('empty', [])),
            col(stack('s3', [cell('c')])),
            col(stack('moving', [cell('m')]), 0),
            col(stack('s4', [cell('d')])),
        ];
        expect(cellDiffPairs(cols, {})).toEqual([]);
    });

    it('a Capabilities stack takes part by its front cell, and in no pair while its manage card is in front', () => {
        const caps = stack('k', [cell('d', { builtin: true }), cell('u')], { stack_type: 'capabilities' });
        const cols = [col(stack('s1', [cell('a')])), col(caps), col(stack('s2', [cell('b')]))];
        expect(cellDiffPairs(cols, { k: 0 })).toEqual([]);
        expect(cellDiffPairs(cols, { k: 2 }).map(p => [p.leftCell.id, p.rightCell.id])).toEqual([['a', 'u'], ['u', 'b']]);
    });
});

describe('cellDiffZoneGeometry', () => {
    // Compact header rows: 38px from each card's top.
    const L: CellDiffColumnBand = { left: 0, right: 400, top: 77, headerBottomOffsetPx: 38 };
    const R: CellDiffColumnBand = { left: 414, right: 814, top: 77, headerBottomOffsetPx: 38 };

    it('covers the gap plus FUZZ px into each card, over the header row only', () => {
        const g = cellDiffZoneGeometry(L, R)!;
        expect(g.centerX).toBe(407);
        expect(g.top).toBe(77);
        expect(g.bottom).toBe(115);
        expect([g.gapWidth, g.pillInGap]).toEqual([14, false]);
        expect(g.rects).toEqual([
            { left: 400, top: 77, width: 14, height: 38 },
            { left: 400 - CELL_DIFF_ZONE_FUZZ, top: 77, width: CELL_DIFF_ZONE_FUZZ, height: 38 },
            { left: 414, top: 77, width: CELL_DIFF_ZONE_FUZZ, height: 38 },
        ]);
    });

    it('a fan column one header band lower: the gap spans the union, each fuzz its own header', () => {
        const fanR = { ...R, top: 110 };
        const g = cellDiffZoneGeometry(L, fanR, 6)!;
        expect([g.top, g.bottom]).toEqual([77, 148]);
        // The row above the band is the fan frame's header on the fan side.
        expect(g.pillInGap).toBe(true);
        expect(g.rects[0]).toEqual({ left: 400, top: 77, width: 14, height: 71 });
        expect(g.rects[1]).toEqual({ left: 394, top: 77, width: 6, height: 38 });
        expect(g.rects[2]).toEqual({ left: 414, top: 110, width: 6, height: 38 });
    });

    it('no zone while the columns touch or overlap', () => {
        expect(cellDiffZoneGeometry(L, { ...R, left: 400 })).toBeNull();
        expect(cellDiffZoneGeometry(L, { ...R, left: 380 })).toBeNull();
    });
});

it('uses the header bottom offset from a nonzero card origin (F0654)', () => {
    const left = { left: 0, right: 400, top: 77, headerBottomOffsetPx: 38 };
    const right = { left: 414, right: 814, top: 77, headerBottomOffsetPx: 38 };
    const geometry = cellDiffZoneGeometry(left, right)!;
    expect(geometry.top).toBe(77);
    expect(geometry.bottom).toBe(115);
    expect(geometry.rects).toEqual([
        { left: 400, top: 77, width: 14, height: 38 },
        { left: 394, top: 77, width: 6, height: 38 },
        { left: 414, top: 77, width: 6, height: 38 },
    ]);
});

describe('the header bottom assumed before a card is measured', () => {
    it('is the 1px card border plus the 36px head, its hairline inside it (C11)', () => {
        expect(DEFAULT_CELL_HEADER_BOTTOM_OFFSET_PX).toBe(37);
    });
});
