import { describe, expect, it } from 'vitest';
import {
    boardSignature, overviewColumnLanding, overviewInsertPreview, overviewPointedSlot, pendingMovePeekSlot, resolveSceneDropPreview, shownSlotOf,
} from './sceneDropGeometry';
import { OVERVIEW_CARD_PITCH, overviewCardTop } from '@/hooks/useStackLayout';
import { OVERVIEW_CARD_HEIGHT, OVERVIEW_COLUMN_WIDTH } from '@/stores/collimate/overviewSlice';
import type { Stack } from '@/lib/types';

const cells = (prefix: string, n: number) =>
    Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}`, name: `${prefix}${i}`, files: [] }));
const stacks: Stack[] = [
    { id: 's1', name: 'one', cells: cells('a', 2) },
    { id: 's2', name: 'two', cells: cells('b', 12) },
];
const W = OVERVIEW_COLUMN_WIDTH;
const base = {
    dragType: 'cell' as const, dragStackId: 's1', dragCellIndex: 0, pendingMove: null, stacks,
    positions: [W / 2, W * 1.5 + 18], widths: [W, W], naturalWidths: [W, W],
    insertTargetIdx: null, insertInFan: false, dropTargetIdx: null,
    cardFrame: { stackTopOffset: 0, stripHeight: 0, cardPixelHeight: 0 },
    isZoomedOut: true, visibleTopIndices: undefined, GAP: 18,
};

describe('the overview drop geometry', () => {
    // A column of three cards at rest: card i spans overviewCardTop(i) to
    // overviewCardTop(i) + OVERVIEW_CARD_HEIGHT, its midpoint half way.
    const H = OVERVIEW_CARD_HEIGHT;
    const mid = (slot: number) => overviewCardTop(slot) + H / 2;
    const three = [H, H, H];

    it('the slot follows the pointer: above the first card, between any two, below the last (polish)', () => {
        expect(overviewPointedSlot(overviewCardTop(0) - 40, three, null)).toBe(0);
        expect(overviewPointedSlot(mid(0) - 1, three, null)).toBe(0);
        expect(overviewPointedSlot(mid(0) + 1, three, null)).toBe(1);
        expect(overviewPointedSlot(mid(1) + 1, three, null)).toBe(2);
        expect(overviewPointedSlot(mid(2) + 1, three, null)).toBe(3);
        expect(overviewPointedSlot(overviewCardTop(9), three, null)).toBe(3);
        expect(overviewPointedSlot(overviewCardTop(0), [], null)).toBe(0);
    });

    it('the slot has hysteresis: the open gap pushes the cards below it, so a pointer resting on a boundary does not flicker (polish)', () => {
        // Just past card 1's midpoint: slot 2, and with the gap open there
        // the same pointer stays on slot 2.
        const y = mid(1) + 2;
        expect(overviewPointedSlot(y, three, null)).toBe(2);
        expect(overviewPointedSlot(y, three, 2)).toBe(2);
        // Back up past that midpoint: slot 1. The gap at slot 1 pushes card 1
        // down a pitch, so the pointer back on the first side of the boundary
        // stays on slot 1: it has to pass card 1's shown midpoint, a whole
        // card lower, before the gap moves on.
        expect(overviewPointedSlot(y - 4, three, 2)).toBe(1);
        expect(overviewPointedSlot(y, three, 1)).toBe(1);
        expect(overviewPointedSlot(mid(1) + OVERVIEW_CARD_PITCH - 2, three, 1)).toBe(1);
        expect(overviewPointedSlot(mid(1) + OVERVIEW_CARD_PITCH + 2, three, 1)).toBe(2);
        // A taller card (the journal style) moves the midpoints below it.
        expect(overviewPointedSlot(mid(1) + 2, [H + 200, H, H], null)).toBe(1);
    });

    it('a column lands the card at the stack position the pointer points to, the held card left out (polish)', () => {
        const cells = ['a', 'b', 'c'];
        const shown = cells.map(id => ({ id, height: H }));
        expect(overviewColumnLanding(overviewCardTop(0), cells, shown, null)).toBe(0);
        expect(overviewColumnLanding(mid(0) + 2, cells, shown, null)).toBe(1);
        expect(overviewColumnLanding(mid(2) + 2, cells, shown, null)).toBe(3);
        // The gap shown at position 1 holds the pointer there.
        expect(overviewColumnLanding(mid(1) + 2, cells, shown, 1)).toBe(1);
        // Only connected shows a and c: between them is before c (position 2),
        // below c is after it.
        const kept = [shown[0], shown[2]];
        expect(overviewColumnLanding(mid(0) + 2, cells, kept, null)).toBe(2);
        expect(overviewColumnLanding(mid(1) + 2, cells, kept, null)).toBe(3);
        expect(shownSlotOf(2, cells, ['a', 'c'])).toBe(1);
        // An empty column: its first position.
        expect(overviewColumnLanding(mid(3), [], [], null)).toBe(0);
    });

    it('draws no world preview for a drop onto a column (the column marks its gap); an insert shows a card at the head of the board (O01)', () => {
        expect(resolveSceneDropPreview({ ...base, dropTargetIdx: 1 })).toBeNull();
        const insert = resolveSceneDropPreview({ ...base, insertTargetIdx: 1 });
        expect(insert).toMatchObject({ y: overviewCardTop(0), height: OVERVIEW_CARD_HEIGHT });
    });
});

describe('the main-view drop geometry', () => {
    it('a card held over its own place marks its own column: a slot over it at its width, the card its own (polish)', () => {
        const mainView = { ...base, isZoomedOut: false, cardFrame: { stackTopOffset: 0, stripHeight: 0, cardPixelHeight: 400 } };
        expect(resolveSceneDropPreview({ ...mainView, dropTargetIdx: 0 })).toMatchObject({
            cell: stacks[0].cells[0], x: 0, width: W, inFan: false,
        });
    });
});

describe('the overview drop geometry for a Removed shelf card', () => {
    it('a shelf card\'s gap is the board card\'s: a card-sized marked slot at the head of the board, between the columns (polish)', () => {
        const positions = [W / 2, W * 1.5 + 18];
        const widths = [W, W];
        // Between the two columns: right after the first one's right edge and the gap.
        expect(overviewInsertPreview(1, positions, widths, W, 18)).toEqual({
            x: W + 18, y: overviewCardTop(0), width: W, height: OVERVIEW_CARD_HEIGHT, inFan: false,
        });
        // At the start and at the end of the row.
        expect(overviewInsertPreview(0, positions, widths, W, 18).x).toBe(0);
        expect(overviewInsertPreview(2, positions, widths, W, 18).x).toBe(2 * W + 2 * 18);
        // The very slot a board card's insert shows.
        const board = resolveSceneDropPreview({ ...base, insertTargetIdx: 1 });
        expect({ ...board, cell: undefined }).toEqual({ ...overviewInsertPreview(1, positions, widths, W, 18), cell: undefined });
    });

    it('the board\'s arrangement changes with a moved card or a reordered column, not with a cell\'s other fields (polish)', () => {
        const before = boardSignature(stacks);
        expect(boardSignature(stacks.map(stack => ({ ...stack, name: 'renamed' })))).toBe(before);
        expect(boardSignature([stacks[1], stacks[0]])).not.toBe(before);
        const moved = [
            { ...stacks[0], cells: stacks[0].cells.slice(1) },
            { ...stacks[1], cells: [...stacks[1].cells, stacks[0].cells[0]] },
        ];
        expect(boardSignature(moved)).not.toBe(before);
    });
});

describe('front slots under a pending keyboard cell move', () => {
    const caps: Stack = {
        id: 'caps', name: 'Capabilities', stack_type: 'capabilities',
        cells: [{ id: 'card', name: 'card', files: [], builtin: true }, ...cells('u', 2)],
    };
    const moving = (sourceId: string) => ({ type: 'cell' as const, sourceId, originalIndex: 0, targetIndex: 1, mode: 'insert' as const });

    it('the source column peeks the cell after its front cell, wrapping, in slot space', () => {
        expect(pendingMovePeekSlot(stacks[0], moving('s1'), { s1: 0 })).toBe(1);
        expect(pendingMovePeekSlot(stacks[0], moving('s1'), { s1: 1 })).toBe(0);
        // Capabilities: cell 1 (slot 2) peeks cell 2 (slot 3); its last cell wraps to its first card (slot 1).
        expect(pendingMovePeekSlot(caps, moving('caps'), { caps: 2 })).toBe(3);
        expect(pendingMovePeekSlot(caps, moving('caps'), { caps: 3 })).toBe(1);
    });

    it('any other column, a lone cell or the manage card in front keeps its stored slot', () => {
        expect(pendingMovePeekSlot(stacks[1], moving('s1'), { s2: 5 })).toBe(5);
        expect(pendingMovePeekSlot(stacks[0], null, { s1: 1 })).toBe(1);
        expect(pendingMovePeekSlot({ id: 'one', cells: cells('z', 1) }, moving('one'), {})).toBe(0);
        expect(pendingMovePeekSlot(caps, moving('caps'), { caps: 0 })).toBe(0);
    });

    it('the moving cell is the source\'s front cell by its REAL index', () => {
        const preview = resolveSceneDropPreview({
            ...base, dragType: null, dragStackId: null, dragCellIndex: undefined, isZoomedOut: false,
            stacks: [caps, stacks[1]], pendingMove: { ...moving('caps'), mode: 'stack', targetIndex: 3 },
            visibleTopIndices: { caps: 3 },
        });
        expect(preview?.cell?.id).toBe('u1');
    });
});
