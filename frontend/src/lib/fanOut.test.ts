import { describe, it, expect } from 'vitest';
import type { Cell, Stack } from '@/lib/types';
import type { PendingMoveState } from '@/stores/useSpatialStore';
import {
    FanRange, CellMoveStop, CellSource, DisplayTarget,
    fanColumnId, getFanRange, buildDisplayStacks, createDisplayStacksBuilder, displayToReal, realInsertIndex, realStackCount,
    resolveDisplayId, displaySelectedIdFor, fanAnchorFor, adjustTopIndexAfterRemove, landingOf,
    resolveCellSource, resolveCellLanding, resolveCellTarget, hitTestCellDrop, hitTestStackDrop,
    cellMoveStops, firstCellMoveStop, stepCellMoveStop, pendingMoveTarget,
    fanFrameExtent, fanHeaderSpan, layoutAlias, fanWidthsFor, fanCollapseOf, MOVE_INTO_CAPABILITIES_RUNNING,
} from './fanOut';

const cell = (id: string): Cell => ({ id, name: id, files: [] } as unknown as Cell);
const stack = (id: string, cells: Cell[], stack_type?: Stack['stack_type']): Stack =>
    ({ id, name: `${id}-name`, cells, ...(stack_type ? { stack_type } : {}) });

// ── Fixtures ────────────────────────────────────────────────────────
// [A(a0), S(s0,s1,s2), B(b0,b1), C(c0)] fanned on S:
//   display = [A, fan-s0, fan-s1, fan-s2, B, C], K = 6
const A = stack('A', [cell('a0')]);
const S = stack('S', [cell('s0'), cell('s1'), cell('s2')]);
const B = stack('B', [cell('b0'), cell('b1')]);
const C = stack('C', [cell('c0')]);
const main = [A, S, B, C];
const fan = getFanRange(main, 'S') as FanRange;
const display = buildDisplayStacks(main, fan);
const noVti: Record<string, number> = {};

// [S(2)] alone
const S2 = stack('S', [cell('s0'), cell('s1')]);
const mainSolo = [S2];
const fanSolo = getFanRange(mainSolo, 'S') as FanRange;
const displaySolo = buildDisplayStacks(mainSolo, fanSolo);

// [A(1), S(2)]
const mainAS = [A, S2];
const fanAS = getFanRange(mainAS, 'S') as FanRange;
const displayAS = buildDisplayStacks(mainAS, fanAS);

const notLive = (): boolean => false;
const src = (id: string, cellIdx = 0, d: Stack[] = display, f: FanRange | null = fan, isLive: (id: string) => boolean = notLive): CellSource =>
    resolveCellSource(d, f, id, cellIdx, isLive) as CellSource;
const gapIn = (slot: number): DisplayTarget => ({ kind: 'gap', slot, inFan: true });
const gapOut = (slot: number): DisplayTarget => ({ kind: 'gap', slot, inFan: false });
const onto = (col: number, intra: number): DisplayTarget => ({ kind: 'onto', col, intra });

// Stop-list shorthand: in<g> / out<g> = gap slot, onto<d> = column.
const label = (s: CellMoveStop): string =>
    s.mode === 'stack' ? `onto${(s.targetIndex - 1) / 2}` : `${s.inFan ? 'in' : 'out'}${s.targetIndex / 2}`;
const labels = (stops: CellMoveStop[]) => stops.map(label);

describe('fanOut — ids, building, resolution', () => {
    it('getFanRange is null for a null id, a missing stack, or a 1-cell stack', () => {
        expect(getFanRange(main, null)).toBeNull();
        expect(getFanRange(main, 'nope')).toBeNull();
        expect(getFanRange(main, 'A')).toBeNull();
    });

    it('getFanRange spans S at real 1', () => {
        expect(fan).toEqual({ stackId: 'S', name: 'S-name', realIdx: 1, start: 1, end: 3, count: 3 });
    });

    it('getFanRange spans S at real 0', () => {
        expect(fanSolo).toEqual({ stackId: 'S', name: 'S-name', realIdx: 0, start: 0, end: 1, count: 2 });
    });

    it('getFanRange is null for the Capabilities stack: it never fans out', () => {
        const caps = stack('K', [cell('k0'), cell('k1'), cell('k2')], 'capabilities');
        expect(getFanRange([A, caps], 'K')).toBeNull();
        expect(buildDisplayStacks([A, caps], getFanRange([A, caps], 'K'))).toEqual([A, caps]);
    });

    it('buildDisplayStacks returns the same reference without a fan', () => {
        expect(buildDisplayStacks(main, null)).toBe(main);
    });

    it('buildDisplayStacks replaces S in place by exact single-cell columns', () => {
        expect(display.map(s => s.id)).toEqual(['A', 'fan-s0', 'fan-s1', 'fan-s2', 'B', 'C']);
        expect(display[0]).toBe(A);
        expect(display[4]).toBe(B);
        expect(display[5]).toBe(C);
        expect(display[2]).toEqual({
            id: 'fan-s1', name: 's1', cells: [S.cells[1]], synthetic: true,
            sourceCellId: 's1', sourceStackId: 'S', fanIndex: 1, fanCount: 3,
        });
        expect(display[2].cells[0]).toBe(S.cells[1]);
    });

    it('buildDisplayStacks reuses a previous column wrapping the same cell', () => {
        const again = buildDisplayStacks(main, fan, display);
        expect(again[1]).toBe(display[1]);
        expect(again[2]).toBe(display[2]);
        expect(again[3]).toBe(display[3]);
        // A different cell object under the same id mints a new column.
        const S2b = stack('S', [cell('s0'), cell('s1'), cell('s2')]);
        const main2 = [A, S2b, B, C];
        const d2 = buildDisplayStacks(main2, getFanRange(main2, 'S'), display);
        expect(d2[1]).not.toBe(display[1]);
        expect(d2[1].cells[0]).toBe(S2b.cells[0]);
        // A changed index / count mints a new column too.
        const S3 = stack('S', [cell('x'), ...S.cells]);
        const main3 = [A, S3, B, C];
        const d3 = buildDisplayStacks(main3, getFanRange(main3, 'S'), display);
        expect(d3[2].id).toBe('fan-s0');
        expect(d3[2]).not.toBe(display[1]);
        expect(d3[2].fanIndex).toBe(1);
        expect(d3[2].fanCount).toBe(4);
    });

    it('createDisplayStacksBuilder keeps unchanged columns across rebuilds', () => {
        const build = createDisplayStacksBuilder();
        const d1 = build(main, fan);
        expect(d1.map(s => s.id)).toEqual(['A', 'fan-s0', 'fan-s1', 'fan-s2', 'B', 'C']);
        // Same cells, new stack array: every column (and its cells array) is reused.
        const d2 = build([A, S, B, C], fan);
        expect(d2).not.toBe(d1);
        expect(d2[1]).toBe(d1[1]);
        expect(d2[2]).toBe(d1[2]);
        expect(d2[3]).toBe(d1[3]);
        expect(d2[2].cells).toBe(d1[2].cells);
        // A replaced cell mints only its own column.
        const Sx = stack('S', [S.cells[0], cell('s1'), S.cells[2]]);
        const mx = [A, Sx, B, C];
        const d3 = build(mx, getFanRange(mx, 'S'));
        expect(d3[1]).toBe(d1[1]);
        expect(d3[2]).not.toBe(d1[2]);
        expect(d3[3]).toBe(d1[3]);
        // No fan: the input list itself.
        expect(build(main, null)).toBe(main);
    });

    it('displayToReal maps every display column', () => {
        expect([0, 1, 2, 3, 4, 5].map(i => displayToReal(fan, i))).toEqual([
            { realIdx: 0, fanCellIdx: null },
            { realIdx: 1, fanCellIdx: 0 },
            { realIdx: 1, fanCellIdx: 1 },
            { realIdx: 1, fanCellIdx: 2 },
            { realIdx: 2, fanCellIdx: null },
            { realIdx: 3, fanCellIdx: null },
        ]);
        expect(displayToReal(null, 4)).toEqual({ realIdx: 4, fanCellIdx: null });
    });

    it('realInsertIndex maps the outside gaps', () => {
        expect([0, 1, 4, 5, 6].map(s => realInsertIndex(fan, s))).toEqual([0, 1, 2, 3, 4]);
        expect(realInsertIndex(null, 3)).toBe(3);
        // Inside gaps are never outside; the fanned stack's index is returned defensively.
        expect(realInsertIndex(fan, 2)).toBe(1);
    });

    it('realStackCount removes the extra columns', () => {
        expect(realStackCount(6, fan)).toBe(4);
        expect(realStackCount(4, null)).toBe(4);
    });

    it('resolveDisplayId handles fan columns, real stacks, the fanned stack itself, and unknown ids', () => {
        expect(resolveDisplayId(display, fan, 'fan-s2')).toEqual({ stackId: 'S', fanCellIdx: 2 });
        expect(resolveDisplayId(display, fan, 'B')).toEqual({ stackId: 'B', fanCellIdx: null });
        expect(resolveDisplayId(display, fan, 'S')).toEqual({ stackId: 'S', fanCellIdx: null });
        expect(resolveDisplayId(display, fan, 'nope')).toBeNull();
        expect(resolveDisplayId(main, null, 'S')).toEqual({ stackId: 'S', fanCellIdx: null });
    });

    it('displaySelectedIdFor picks the front column of the fanned stack', () => {
        expect(displaySelectedIdFor(main, fan, 'S', { S: 4 })).toBe('fan-s1');
        expect(displaySelectedIdFor(main, fan, 'S', {})).toBe('fan-s0');
        expect(displaySelectedIdFor(main, fan, 'S', { S: -1 })).toBe('fan-s2');
        expect(displaySelectedIdFor(main, fan, 'B', { S: 4 })).toBe('B');
        expect(displaySelectedIdFor(main, null, 'S', { S: 4 })).toBe('S');
        expect(displaySelectedIdFor(main, fan, null, {})).toBeNull();
    });

    it('fanAnchorFor names the stack and its front column', () => {
        expect(fanAnchorFor(main, null, {})).toBeNull();
        expect(fanAnchorFor(main, 'nope', {})).toBeNull();
        expect(fanAnchorFor(main, 'S', { S: 2 })).toEqual({ realId: 'S', columnId: 'fan-s2' });
        expect(fanAnchorFor(main, 'S', {})).toEqual({ realId: 'S', columnId: 'fan-s0' });
        expect(fanAnchorFor([stack('E', [])], 'E', {})).toBeNull();
    });

    it('fanColumnId prefixes the cell id', () => {
        expect(fanColumnId('abc')).toBe('fan-abc');
    });
});

describe('fanOut — adjustTopIndexAfterRemove / landingOf', () => {
    it('keeps the same cell on top when a cell before it goes', () => {
        expect(adjustTopIndexAfterRemove(2, 0, 3)).toBe(1);
    });
    it('moves to the right neighbour when the top cell itself goes (mid)', () => {
        expect(adjustTopIndexAfterRemove(1, 1, 3)).toBe(1);
    });
    it('moves to the left neighbour when the last cell was on top and goes', () => {
        expect(adjustTopIndexAfterRemove(2, 2, 3)).toBe(1);
    });
    it('is unchanged when a cell after the top goes', () => {
        expect(adjustTopIndexAfterRemove(0, 2, 3)).toBe(0);
    });
    it('normalises a wrapped top index and never goes below 0', () => {
        expect(adjustTopIndexAfterRemove(4, 1, 3)).toBe(1);   // 4 mod 3 = 1, removed itself → right neighbour
        expect(adjustTopIndexAfterRemove(0, 0, 2)).toBe(0);
        expect(adjustTopIndexAfterRemove(0, 0, 1)).toBe(0);
    });

    it('landingOf finds the stack and index holding a cell', () => {
        const ch = { main_stacks: [A, S, B] };
        expect(landingOf(ch, 's2')).toEqual({ stackId: 'S', cellIdx: 2 });
        expect(landingOf(ch, 'b1')).toEqual({ stackId: 'B', cellIdx: 1 });
        expect(landingOf(ch, 'zz')).toBeNull();
        expect(landingOf(ch, undefined)).toBeNull();
        expect(landingOf(null, 's2')).toBeNull();
        expect(landingOf({}, 's2')).toBeNull();
    });
});

describe('fanOut — resolveCellSource', () => {
    it('resolves a fan column to its real stack and cell', () => {
        expect(src('fan-s1')).toEqual({
            displayId: 'fan-s1', displayIdx: 2, stackId: 'S', realIdx: 1, cellIdx: 1, cellId: 's1',
            cellCount: 3, capabilities: false, live: false, isFanColumn: true, builtin: false,
        });
    });
    it('resolves a real stack with a clamped cell index and real index', () => {
        expect(src('B', 5)).toEqual({
            displayId: 'B', displayIdx: 4, stackId: 'B', realIdx: 2, cellIdx: 1, cellId: 'b1',
            cellCount: 2, capabilities: false, live: false, isFanColumn: false, builtin: false,
        });
        expect(src('A', 0).realIdx).toBe(0);
        expect(src('C', 0).realIdx).toBe(3);
    });
    it('is null for an unknown id or an empty stack', () => {
        expect(resolveCellSource(display, fan, 'nope', 0, notLive)).toBeNull();
        expect(resolveCellSource([stack('E', [])], null, 'E', 0, notLive)).toBeNull();
    });
    it('asks the liveness predicate about the moving cell and marks a Capabilities cell', () => {
        const asked: string[] = [];
        const isLive = (id: string) => { asked.push(id); return id === 's1'; };
        expect(src('fan-s1', 0, display, fan, isLive).live).toBe(true);
        expect(src('B', 1, display, fan, isLive).live).toBe(false);
        expect(asked).toEqual(['s1', 'b1']);
        const caps = stack('K', [cell('k0')], 'capabilities');
        expect(src('K', 0, [caps, B], null).capabilities).toBe(true);
    });
});

describe('fanOut — resolveCellTarget (B5)', () => {
    it('1. A → gap2 in (p=1): intoStack at position 1 of S', () => {
        expect(resolveCellTarget(display, fan, src('A'), gapIn(2))).toEqual(
            { kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 1, cellId: 'a0' });
    });
    it('2. fan-s0 → gap3 in (p=2): reorder 0→1', () => {
        expect(resolveCellTarget(display, fan, src('fan-s0'), gapIn(3))).toEqual(
            { kind: 'reorder', stackId: 'S', realIdx: 1, from: 0, to: 1, cellId: 's0' });
    });
    it('3. fan-s2 → gap1 in (p=0): reorder 2→0', () => {
        expect(resolveCellTarget(display, fan, src('fan-s2'), gapIn(1))).toEqual(
            { kind: 'reorder', stackId: 'S', realIdx: 1, from: 2, to: 0, cellId: 's2' });
    });
    it('4. fan-s1 → gap2 in / gap3 in: no-op', () => {
        expect(resolveCellTarget(display, fan, src('fan-s1'), gapIn(2))).toBeNull();
        expect(resolveCellTarget(display, fan, src('fan-s1'), gapIn(3))).toBeNull();
    });
    it('5. fan-s1 → gap1 out: new stack at real 1', () => {
        expect(resolveCellTarget(display, fan, src('fan-s1'), gapOut(1))).toEqual(
            { kind: 'newStack', srcStackId: 'S', srcCellIdx: 1, insertRealIdx: 1, cellId: 's1' });
    });
    it('6. fan-s1 → gap4 out: new stack at real 2', () => {
        expect(resolveCellTarget(display, fan, src('fan-s1'), gapOut(4))).toEqual(
            { kind: 'newStack', srcStackId: 'S', srcCellIdx: 1, insertRealIdx: 2, cellId: 's1' });
    });
    it('7. fan-s0 → onto4 intra 1: into B at 1', () => {
        expect(resolveCellTarget(display, fan, src('fan-s0'), onto(4, 1))).toEqual(
            { kind: 'intoStack', srcStackId: 'S', srcCellIdx: 0, targetRealIdx: 2, tgtCellIdx: 1, cellId: 's0' });
    });
    it('8. B(b1) → gap4 out: new stack at real 2', () => {
        expect(resolveCellTarget(display, fan, src('B', 1), gapOut(4))).toEqual(
            { kind: 'newStack', srcStackId: 'B', srcCellIdx: 1, insertRealIdx: 2, cellId: 'b1' });
    });
    it('9. C → gap5 / gap6 out: no-op (re-created in place)', () => {
        expect(resolveCellTarget(display, fan, src('C'), gapOut(5))).toBeNull();
        expect(resolveCellTarget(display, fan, src('C'), gapOut(6))).toBeNull();
    });
    it('10. C → gap4 in (p=3): appended to S', () => {
        expect(resolveCellTarget(display, fan, src('C'), gapIn(4))).toEqual(
            { kind: 'intoStack', srcStackId: 'C', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 3, cellId: 'c0' });
    });
    it('11. A → onto0: no-op (single cell onto itself)', () => {
        expect(resolveCellTarget(display, fan, src('A'), onto(0, 0))).toBeNull();
    });
    it('12. B(b0) → onto4 intra 1: reorder 0→1', () => {
        expect(resolveCellTarget(display, fan, src('B', 0), onto(4, 1))).toEqual(
            { kind: 'reorder', stackId: 'B', realIdx: 2, from: 0, to: 1, cellId: 'b0' });
        // Landing on its own index is a no-op.
        expect(resolveCellTarget(display, fan, src('B', 0), onto(4, 0))).toBeNull();
    });
    it('13. A → gap4 out (single, ins 2): new stack at real 2', () => {
        expect(resolveCellTarget(display, fan, src('A'), gapOut(4))).toEqual(
            { kind: 'newStack', srcStackId: 'A', srcCellIdx: 0, insertRealIdx: 2, cellId: 'a0' });
    });

    it('a Capabilities cell moves into a fan, onto a column and into a new column', () => {
        const K = stack('K', [cell('k0')], 'capabilities');
        const m = [K, S, B];
        const f = getFanRange(m, 'S');
        const d = buildDisplayStacks(m, f);
        const k = src('K', 0, d, f);
        expect(resolveCellTarget(d, f, k, gapIn(2))).toEqual(
            { kind: 'intoStack', srcStackId: 'K', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 1, cellId: 'k0' });
        expect(resolveCellTarget(d, f, k, onto(4, 0))).toEqual(
            { kind: 'intoStack', srcStackId: 'K', srcCellIdx: 0, targetRealIdx: 2, tgtCellIdx: 0, cellId: 'k0' });
        // Its only user cell beside Capabilities still opens a fresh column:
        // Capabilities keeps its stack.
        expect(resolveCellTarget(d, f, k, gapOut(0))).toEqual(
            { kind: 'newStack', srcStackId: 'K', srcCellIdx: 0, insertRealIdx: 0, cellId: 'k0' });
        expect(resolveCellTarget(d, f, k, gapOut(1))).toEqual(
            { kind: 'newStack', srcStackId: 'K', srcCellIdx: 0, insertRealIdx: 1, cellId: 'k0' });
    });

    it('intoStack from a source AFTER the fan sends the pre-pop target index (no decrement)', () => {
        expect(resolveCellTarget(display, fan, src('B', 0), gapIn(2))).toEqual(
            { kind: 'intoStack', srcStackId: 'B', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 1, cellId: 'b0' });
        expect(resolveCellTarget(display, fan, src('C'), gapIn(1))).toEqual(
            { kind: 'intoStack', srcStackId: 'C', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 0, cellId: 'c0' });
    });

    it('an in-fan gap requires a fan and a slot on the group', () => {
        expect(resolveCellTarget(main, null, src('A', 0, main, null), gapIn(1))).toBeNull();
        expect(resolveCellTarget(display, fan, src('A'), gapIn(0))).toBeNull();
        expect(resolveCellTarget(display, fan, src('A'), gapIn(5))).toBeNull();
    });

    it('onto a fan column or a missing column is null; onto clamps the intra index', () => {
        expect(resolveCellTarget(display, fan, src('A'), onto(2, 0))).toBeNull();
        expect(resolveCellTarget(display, fan, src('A'), onto(9, 0))).toBeNull();
        expect(resolveCellTarget(display, fan, src('A'), onto(4, 7))).toEqual(
            { kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 2, tgtCellIdx: 2, cellId: 'a0' });
    });

    it('without a fan the plain rules hold', () => {
        const a = src('A', 0, main, null);
        const s1 = src('S', 1, main, null);
        expect(resolveCellTarget(main, null, a, gapOut(0))).toBeNull();
        expect(resolveCellTarget(main, null, a, gapOut(1))).toBeNull();
        expect(resolveCellTarget(main, null, a, gapOut(3))).toEqual(
            { kind: 'newStack', srcStackId: 'A', srcCellIdx: 0, insertRealIdx: 3, cellId: 'a0' });
        expect(resolveCellTarget(main, null, a, onto(1, 1))).toEqual(
            { kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 1, cellId: 'a0' });
        expect(resolveCellTarget(main, null, s1, onto(1, 2))).toEqual(
            { kind: 'reorder', stackId: 'S', realIdx: 1, from: 1, to: 2, cellId: 's1' });
    });
});

describe('fanOut — resolveCellLanding', () => {
    const stays = { kind: 'stays' };
    const refused = { kind: 'refused' };

    it('the card\'s own place stays: its own two in-fan gaps, either gap beside its lone column, its lone column, its own index', () => {
        // 4. and 9.: the gaps either side of itself.
        expect(resolveCellLanding(display, fan, src('fan-s1'), gapIn(2))).toEqual(stays);
        expect(resolveCellLanding(display, fan, src('fan-s1'), gapIn(3))).toEqual(stays);
        expect(resolveCellLanding(display, fan, src('C'), gapOut(5))).toEqual(stays);
        expect(resolveCellLanding(display, fan, src('C'), gapOut(6))).toEqual(stays);
        // 11. and 12.: onto its own column.
        expect(resolveCellLanding(display, fan, src('A'), onto(0, 0))).toEqual(stays);
        expect(resolveCellLanding(display, fan, src('B', 0), onto(4, 0))).toEqual(stays);
    });

    it('a target the card may not land on is refused', () => {
        // A fan column, a missing column, an in-fan gap off the fan or without one.
        expect(resolveCellLanding(display, fan, src('A'), onto(2, 0))).toEqual(refused);
        expect(resolveCellLanding(display, fan, src('A'), onto(9, 0))).toEqual(refused);
        expect(resolveCellLanding(display, fan, src('A'), gapIn(5))).toEqual(refused);
        expect(resolveCellLanding(main, null, src('A', 0, main, null), gapIn(1))).toEqual(refused);
    });

    describe('the Capabilities stack', () => {
        const card = (id: string): Cell => ({ ...cell(id), builtin: true } as Cell);
        const K = stack('K', [card('d'), card('l'), card('t'), cell('u0'), cell('u1')], 'capabilities');
        const W = stack('W', [cell('w0'), cell('w1')]);
        const rows = [K, W];
        const liveW0 = (id: string) => id === 'w0';

        it('a card never moves, wherever it is dropped', () => {
            const d = src('K', 1, rows, null);
            expect(d.builtin).toBe(true);
            for (const target of [onto(0, 1), onto(0, 4), gapOut(2), onto(1, 0)]) {
                expect(resolveCellLanding(rows, null, d, target)).toEqual(refused);
            }
        });

        it('no cell lands before the cards: the slots above them are refused, the ones after move', () => {
            const u1 = src('K', 4, rows, null);
            for (const intra of [0, 1, 2]) expect(resolveCellLanding(rows, null, u1, onto(0, intra))).toEqual(refused);
            expect(resolveCellLanding(rows, null, u1, onto(0, 3))).toEqual(
                { kind: 'move', op: { kind: 'reorder', stackId: 'K', realIdx: 0, from: 4, to: 3, cellId: 'u1' } });
            const w0 = src('W', 0, rows, null);
            expect(resolveCellLanding(rows, null, w0, onto(0, 2))).toEqual(refused);
            expect(resolveCellLanding(rows, null, w0, onto(0, 5))).toEqual(
                { kind: 'move', op: { kind: 'intoStack', srcStackId: 'W', srcCellIdx: 0, targetRealIdx: 0, tgtCellIdx: 5, cellId: 'w0' } });
        });

        it('a cell with a live run is refused onto Capabilities, with the running reason', () => {
            const w0 = src('W', 0, rows, null, liveW0);
            expect(w0.live).toBe(true);
            expect(resolveCellLanding(rows, null, w0, onto(0, 3))).toEqual({ kind: 'refused', reason: 'running' });
            expect(MOVE_INTO_CAPABILITIES_RUNNING).toBe('Stop its run before moving it onto Capabilities.');
            // A live cell still moves between standard stacks.
            const X = stack('X', [cell('x0')]);
            const three = [K, W, X];
            expect(resolveCellLanding(three, null, src('W', 0, three, null, liveW0), onto(2, 0))).toEqual(
                { kind: 'move', op: { kind: 'intoStack', srcStackId: 'W', srcCellIdx: 0, targetRealIdx: 2, tgtCellIdx: 0, cellId: 'w0' } });
            expect(resolveCellLanding(three, null, src('W', 0, three, null, liveW0), gapOut(3))).toEqual(
                { kind: 'move', op: { kind: 'newStack', srcStackId: 'W', srcCellIdx: 0, insertRealIdx: 3, cellId: 'w0' } });
        });

        it('a Capabilities cell moves off onto a standard stack or into a new column', () => {
            const u0 = src('K', 3, rows, null);
            expect(resolveCellLanding(rows, null, u0, onto(1, 1))).toEqual(
                { kind: 'move', op: { kind: 'intoStack', srcStackId: 'K', srcCellIdx: 3, targetRealIdx: 1, tgtCellIdx: 1, cellId: 'u0' } });
            expect(resolveCellLanding(rows, null, u0, gapOut(2))).toEqual(
                { kind: 'move', op: { kind: 'newStack', srcStackId: 'K', srcCellIdx: 3, insertRealIdx: 2, cellId: 'u0' } });
        });
    });

    it('a real move carries the op resolveCellTarget returns', () => {
        for (const [id, cellIdx, target] of [
            ['fan-s0', 0, gapIn(3)], ['C', 0, gapIn(4)], ['A', 0, gapOut(4)], ['B', 0, onto(4, 1)], ['fan-s0', 0, onto(4, 1)],
        ] as const) {
            const op = resolveCellTarget(display, fan, src(id, cellIdx), target);
            expect(op).not.toBeNull();
            expect(resolveCellLanding(display, fan, src(id, cellIdx), target)).toEqual({ kind: 'move', op });
        }
    });
});

describe('fanOut — hitTestCellDrop (B3)', () => {
    // widths 500, GAP 14, inset 5; column lefts A 0, s0 514, s1 1028, s2 1542, B 2056, C 2570
    const positions = [250, 764, 1278, 1792, 2306, 2820];
    const widths = [500, 500, 500, 500, 500, 500];
    const hit = (worldX: number, f: FanRange | null = fan, w = widths, isThumb = true) =>
        hitTestCellDrop({ worldX, positions, widths: w, fan: f, isThumb, inset: 5 });

    it('follows the worked geometry table', () => {
        expect(hit(100)).toEqual({ kind: 'gap', slot: 0, inFan: false });
        expect(hit(250)).toEqual({ kind: 'onto', col: 0 });
        expect(hit(450)).toEqual({ kind: 'gap', slot: 1, inFan: false });
        expect(hit(505)).toEqual({ kind: 'gap', slot: 1, inFan: false });
        expect(hit(510)).toEqual({ kind: 'gap', slot: 1, inFan: true });
        expect(hit(700)).toEqual({ kind: 'gap', slot: 1, inFan: true });
        expect(hit(800)).toEqual({ kind: 'gap', slot: 2, inFan: true });
        expect(hit(1020)).toEqual({ kind: 'gap', slot: 2, inFan: true });
        expect(hit(2045)).toEqual({ kind: 'gap', slot: 4, inFan: true });
        expect(hit(2050)).toEqual({ kind: 'gap', slot: 4, inFan: false });
        expect(hit(2100)).toEqual({ kind: 'gap', slot: 4, inFan: false });
        expect(hit(2306)).toEqual({ kind: 'onto', col: 4 });
        expect(hit(3100)).toEqual({ kind: 'gap', slot: 6, inFan: false });
    });

    it('the border line is the boundary', () => {
        expect(hit(509)).toEqual({ kind: 'gap', slot: 1, inFan: true });
        expect(hit(508.9)).toEqual({ kind: 'gap', slot: 1, inFan: false });
        expect(hit(2047)).toEqual({ kind: 'gap', slot: 4, inFan: true });
        expect(hit(2047.1)).toEqual({ kind: 'gap', slot: 4, inFan: false });
    });

    it('without a fan, today\'s rules apply everywhere', () => {
        expect(hit(600, null)).toEqual({ kind: 'gap', slot: 1, inFan: false });   // s0 left fringe (514..639)
        expect(hit(700, null)).toEqual({ kind: 'onto', col: 1 });
        expect(hit(764, null)).toEqual({ kind: 'onto', col: 1 });
        expect(hit(1020, null)).toEqual({ kind: 'gap', slot: 2, inFan: false });  // gap between s0 and s1
        expect(hit(1278, null, widths, false)).toEqual({ kind: 'onto', col: 2 });
        expect(hit(1400, null, widths, false)).toEqual({ kind: 'onto', col: 2 }); // no fringe without isThumb
        expect(hit(-10, null)).toEqual({ kind: 'gap', slot: 0, inFan: false });
    });

    it('N = 0 is gap 0 outside', () => {
        expect(hitTestCellDrop({ worldX: 123, positions: [], widths: [], fan, isThumb: true, inset: 5 }))
            .toEqual({ kind: 'gap', slot: 0, inFan: false });
    });

    it('a zero-width fan column (pending source) is excluded from the extent and the count', () => {
        // s0 collapsed: the frame starts at s1's left edge (1028 - 5). A
        // point left of that line is outside the border, where today's
        // rules see the gap after the zero-width column.
        const w = [500, 0, 500, 500, 500, 500];
        expect(hit(1000, fan, w)).toEqual({ kind: 'gap', slot: 2, inFan: false });
        expect(hit(1023, fan, w)).toEqual({ kind: 'gap', slot: 1, inFan: true });   // p = 0 (s0 doesn't count)
        expect(hit(1300, fan, w)).toEqual({ kind: 'gap', slot: 2, inFan: true });   // right of s1's centre → p = 1
        expect(hit(1800, fan, w)).toEqual({ kind: 'gap', slot: 3, inFan: true });
    });
});

describe('fanOut — hitTestStackDrop (B3)', () => {
    // Columns P, Q, R, K(Capabilities); widths 100, centres 50, 164, 278, 392.
    const positions = [50, 164, 278, 392];
    const widths = [100, 100, 100, 100];
    const P = stack('P', [cell('p0')]);
    const Q = stack('Q', [cell('q0')]);
    const R = stack('R', [cell('r0')]);
    const K = stack('K', [cell('k0')], 'capabilities');
    const stacks = [P, Q, R, K];
    const hit = (worldX: number, srcStackId = 'P', isZoomedOut = true) =>
        hitTestStackDrop({ worldX, positions, widths, stacks, srcStackId, isZoomedOut });

    it('hovering inside another column merges into it', () => {
        expect(hit(164)).toEqual({ kind: 'merge', idx: 1 });
        expect(hit(230)).toEqual({ kind: 'merge', idx: 2 });
    });

    it('the source column itself gives insert, not merge', () => {
        expect(hit(40)).toEqual({ kind: 'insert', idx: 0 });
        expect(hit(60)).toEqual({ kind: 'insert', idx: 1 });
    });

    it('a Capabilities target falls through to insert', () => {
        expect(hit(380)).toEqual({ kind: 'insert', idx: 3 });
        expect(hit(400)).toEqual({ kind: 'insert', idx: 4 });
    });

    it('a Capabilities source never merges', () => {
        expect(hit(164, 'K')).toEqual({ kind: 'insert', idx: 1 });
        expect(hit(170, 'K')).toEqual({ kind: 'insert', idx: 2 });
    });

    it('outside zoom-out it only inserts', () => {
        expect(hit(164, 'P', false)).toEqual({ kind: 'insert', idx: 1 });
        expect(hit(170, 'P', false)).toEqual({ kind: 'insert', idx: 2 });
    });

    it('edge slots are 0 and N; the gap between columns inserts', () => {
        expect(hit(-50)).toEqual({ kind: 'insert', idx: 0 });
        expect(hit(1000)).toEqual({ kind: 'insert', idx: 4 });
        expect(hit(107)).toEqual({ kind: 'insert', idx: 1 });
    });
});

describe('fanOut — cellMoveStops (B4)', () => {
    const stopsFor = (id: string, cellIdx = 0, d = display, f: FanRange | null = fan, vti = noVti) =>
        cellMoveStops(d, f, src(id, cellIdx, d, f), vti);

    it('A (single, disp 0)', () => {
        const stops = stopsFor('A');
        expect(labels(stops)).toEqual(['in1', 'in2', 'in3', 'in4', 'out4', 'onto4', 'onto5', 'out6']);
        expect(firstCellMoveStop(stops, 0, -1)).toBeNull();
        expect(label(firstCellMoveStop(stops, 0, 1) as CellMoveStop)).toBe('in1');
    });

    it('fan-s1 (j=1)', () => {
        const stops = stopsFor('fan-s1');
        expect(labels(stops)).toEqual(['out0', 'onto0', 'out1', 'in1', 'in4', 'out4', 'onto4', 'onto5', 'out6']);
        expect(label(firstCellMoveStop(stops, 2, -1) as CellMoveStop)).toBe('in1');
        expect(label(firstCellMoveStop(stops, 2, 1) as CellMoveStop)).toBe('in4');
    });

    it('fan-s0 (j=0)', () => {
        const stops = stopsFor('fan-s0');
        expect(labels(stops)).toEqual(['out0', 'onto0', 'out1', 'in3', 'in4', 'out4', 'onto4', 'onto5', 'out6']);
        expect(label(firstCellMoveStop(stops, 1, -1) as CellMoveStop)).toBe('out1');
        expect(label(firstCellMoveStop(stops, 1, 1) as CellMoveStop)).toBe('in3');
    });

    it('B (2 cells, disp 4)', () => {
        const stops = stopsFor('B');
        expect(labels(stops)).toEqual(['out0', 'onto0', 'out1', 'in1', 'in2', 'in3', 'in4', 'out4', 'onto5', 'out6']);
        expect(label(firstCellMoveStop(stops, 4, -1) as CellMoveStop)).toBe('out4');
        expect(label(firstCellMoveStop(stops, 4, 1) as CellMoveStop)).toBe('onto5');
    });

    it('C (single, disp 5)', () => {
        const stops = stopsFor('C');
        expect(labels(stops)).toEqual(['out0', 'onto0', 'out1', 'in1', 'in2', 'in3', 'in4', 'out4', 'onto4']);
        expect(label(firstCellMoveStop(stops, 5, -1) as CellMoveStop)).toBe('onto4');
        expect(firstCellMoveStop(stops, 5, 1)).toBeNull();
    });

    it('stops carry the parity encoding, the inFan flag and a left-to-right order', () => {
        const stops = stopsFor('fan-s1');
        expect(stops.map(s => s.order)).toEqual([...stops.map(s => s.order)].sort((a, b) => a - b));
        expect(stops.find(s => label(s) === 'out1')).toEqual({ targetIndex: 2, mode: 'insert', inFan: false, order: 2 });
        expect(stops.find(s => label(s) === 'in1')).toEqual({ targetIndex: 2, mode: 'insert', inFan: true, order: 2.25 });
        expect(stops.find(s => label(s) === 'in4')).toEqual({ targetIndex: 8, mode: 'insert', inFan: true, order: 7.75 });
        expect(stops.find(s => label(s) === 'out4')).toEqual({ targetIndex: 8, mode: 'insert', inFan: false, order: 8 });
        expect(stops.find(s => label(s) === 'onto4')).toEqual({ targetIndex: 9, mode: 'stack', inFan: false, order: 9 });
    });

    it('stepCellMoveStop clamps at both ends and starts from the first/last stop when cur is absent', () => {
        const stops = stopsFor('fan-s1');
        const first = stops[0];
        const last = stops[stops.length - 1];
        expect(stepCellMoveStop(stops, first, -1)).toBe(first);
        expect(stepCellMoveStop(stops, last, 1)).toBe(last);
        expect(stepCellMoveStop(stops, { targetIndex: 999 }, 1)).toBe(first);
        expect(stepCellMoveStop(stops, { targetIndex: 999 }, -1)).toBe(last);
        expect(stepCellMoveStop([], { targetIndex: 0 }, 1)).toBeNull();
        // The inside/outside twins at a boundary slot are distinct stops.
        const out1 = stops.find(s => label(s) === 'out1') as CellMoveStop;
        const in1 = stops.find(s => label(s) === 'in1') as CellMoveStop;
        expect(stepCellMoveStop(stops, { targetIndex: 2, inFan: false }, 1)).toBe(in1);
        expect(stepCellMoveStop(stops, { targetIndex: 2, inFan: true }, -1)).toBe(out1);
        expect(label(stepCellMoveStop(stops, { targetIndex: 2, inFan: true }, 1) as CellMoveStop)).toBe('in4');
    });

    it('a 2-cell fan with internal source j=0 offers out(start), in(end+1) = swap, out(end+1) plus the other stacks', () => {
        const stops = stopsFor('fan-s0', 0, displayAS, fanAS);
        expect(labels(stops)).toEqual(['out0', 'onto0', 'out1', 'in3', 'out3']);
        const s0 = src('fan-s0', 0, displayAS, fanAS);
        const swap = stops.find(s => label(s) === 'in3') as CellMoveStop;
        expect(resolveCellTarget(displayAS, fanAS, s0, pendingMoveTarget(swap, displayAS, noVti)))
            .toEqual({ kind: 'reorder', stackId: 'S', realIdx: 1, from: 0, to: 1, cellId: 's0' });
        // Alone in the channel, the only moves are the swap and the two push-outs.
        expect(labels(stopsFor('fan-s1', 0, displaySolo, fanSolo))).toEqual(['out0', 'in0', 'out2']);
    });

    it('every inside position p ∈ 0..n is reachable for an external source', () => {
        for (const id of ['A', 'B', 'C']) {
            const stops = stopsFor(id).filter(s => s.inFan);
            expect(stops.map(s => s.targetIndex / 2 - fan.start)).toEqual([0, 1, 2, 3]);
        }
    });

    it('every final index ≠ j is reachable for an internal source', () => {
        for (let j = 0; j < 3; j++) {
            const id = `fan-s${j}`;
            const s = src(id, 0);
            const finals = cellMoveStops(display, fan, s, noVti)
                .filter(st => st.inFan)
                .map(st => resolveCellTarget(display, fan, s, { kind: 'gap', slot: st.targetIndex / 2, inFan: true }))
                .map(op => (op && op.kind === 'reorder' ? op.to : -1));
            expect(finals.sort()).toEqual([0, 1, 2].filter(x => x !== j));
        }
    });

    it('the onto stops use the target stack\'s front cell and drop self-drops', () => {
        // B's front is b1 (vti 1): dropping b0 "onto" B is not offered from B itself,
        // and A landing on B goes before b1.
        const vti = { B: 1 };
        const stopsA = stopsFor('A', 0, display, fan, vti);
        const ontoB = stopsA.find(s => label(s) === 'onto4') as CellMoveStop;
        expect(resolveCellTarget(display, fan, src('A'), pendingMoveTarget(ontoB, display, vti)))
            .toEqual({ kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 2, tgtCellIdx: 1, cellId: 'a0' });
        expect(labels(stopsFor('B', 1, display, fan, vti))).not.toContain('onto4');
    });

    it('a live cell keeps its stop onto Capabilities (committing there explains the refusal); a stop there lands after the cards while Manage is in front', () => {
        const card = (id: string): Cell => ({ ...cell(id), builtin: true } as Cell);
        const K = stack('K', [card('d'), card('l'), cell('k0')], 'capabilities');
        const m = [A, K, B];
        const live = src('A', 0, m, null, () => true);
        const liveStops = cellMoveStops(m, null, live, noVti);
        expect(labels(liveStops)).toEqual(['onto1', 'onto2', 'out3']);
        const liveOntoK = liveStops.find(st => label(st) === 'onto1') as CellMoveStop;
        expect(resolveCellLanding(m, null, live, pendingMoveTarget(liveOntoK, m, noVti))).toEqual({ kind: 'refused', reason: 'running' });
        const stops = cellMoveStops(m, null, src('A', 0, m, null), noVti);
        expect(labels(stops)).toEqual(['onto1', 'onto2', 'out3']);
        // vti K = 0 is the manage slot: the move lands after the two cards.
        const ontoK = stops.find(st => label(st) === 'onto1') as CellMoveStop;
        expect(resolveCellTarget(m, null, src('A', 0, m, null), pendingMoveTarget(ontoK, m, noVti)))
            .toEqual({ kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 2, cellId: 'a0' });
    });

    it('without a fan only the outer gaps and the other columns are offered', () => {
        const stops = cellMoveStops(main, null, src('S', 0, main, null), noVti);
        expect(labels(stops)).toEqual(['out0', 'onto0', 'onto2', 'onto3', 'out4']);
    });
});

describe('fanOut — pendingMoveTarget', () => {
    const move = (over: Partial<PendingMoveState>): PendingMoveState => ({
        type: 'cell', sourceId: 'A', originalIndex: 0, targetIndex: 0, mode: 'insert', ...over,
    });
    it('maps an insert with and without inFan', () => {
        expect(pendingMoveTarget(move({ targetIndex: 4, mode: 'insert', inFan: true }), display, noVti))
            .toEqual({ kind: 'gap', slot: 2, inFan: true });
        expect(pendingMoveTarget(move({ targetIndex: 8, mode: 'insert' }), display, noVti))
            .toEqual({ kind: 'gap', slot: 4, inFan: false });
    });
    it('maps a stack target with the front cell as intra', () => {
        expect(pendingMoveTarget(move({ targetIndex: 9, mode: 'stack' }), display, { B: 3 }))
            .toEqual({ kind: 'onto', col: 4, intra: 1 });
        expect(pendingMoveTarget(move({ targetIndex: 9, mode: 'stack' }), display, { B: -1 }))
            .toEqual({ kind: 'onto', col: 4, intra: 1 });
        expect(pendingMoveTarget(move({ targetIndex: 9, mode: 'stack' }), display, noVti))
            .toEqual({ kind: 'onto', col: 4, intra: 0 });
    });
    it('reads a Capabilities front slot as its real cell, and the manage slot as after the cards', () => {
        const card = (id: string): Cell => ({ ...cell(id), builtin: true } as Cell);
        const K = stack('K', [card('d'), card('l'), cell('k0'), cell('k1')], 'capabilities');
        const m = [A, K];
        const ontoK = move({ targetIndex: 3, mode: 'stack' });
        expect(pendingMoveTarget(ontoK, m, { K: 0 })).toEqual({ kind: 'onto', col: 1, intra: 2 });
        expect(pendingMoveTarget(ontoK, m, { K: 3 })).toEqual({ kind: 'onto', col: 1, intra: 2 });
        expect(pendingMoveTarget(ontoK, m, { K: 4 })).toEqual({ kind: 'onto', col: 1, intra: 3 });
    });
    it('lands a keyboard move onto Capabilities after the cards while a built-in card is in front', () => {
        const card = (id: string): Cell => ({ ...cell(id), builtin: true } as Cell);
        const K = stack('K', [card('d'), card('l'), cell('k0')], 'capabilities');
        const m = [A, K];
        const ontoK = move({ targetIndex: 3, mode: 'stack' });
        // Slots 1 and 2 are the built-in cards d and l.
        for (const slot of [1, 2]) {
            const target = pendingMoveTarget(ontoK, m, { K: slot });
            expect(target).toEqual({ kind: 'onto', col: 1, intra: 2 });
            expect(resolveCellTarget(m, null, src('A', 0, m, null), target))
                .toEqual({ kind: 'intoStack', srcStackId: 'A', srcCellIdx: 0, targetRealIdx: 1, tgtCellIdx: 2, cellId: 'a0' });
        }
    });
});

describe('fanOut — fanFrameExtent / layoutAlias (B7)', () => {
    const items = [
        { x: 250, computedWidth: 500 },
        { x: 764, computedWidth: 500, synthetic: true },
        { x: 1278, computedWidth: 500, synthetic: true },
        { x: 1792, computedWidth: 500, synthetic: true },
        { x: 2306, computedWidth: 500 },
    ];
    it('spans the visible fan columns plus the inset', () => {
        expect(fanFrameExtent(items, null, 5)).toEqual({ left: 509, right: 2047 });
    });
    it('widens to an in-fan gap on the left or right', () => {
        expect(fanFrameExtent(items, { x: 400, width: 100, inFan: true }, 5)).toEqual({ left: 395, right: 2047 });
        expect(fanFrameExtent(items, { x: 2100, width: 100, inFan: true }, 5)).toEqual({ left: 509, right: 2205 });
    });
    it('ignores an outside gap and zero-width columns; null without visible columns', () => {
        expect(fanFrameExtent(items, { x: 2100, width: 100, inFan: false }, 5)).toEqual({ left: 509, right: 2047 });
        const collapsed = items.map(it => (it.x === 764 ? { ...it, computedWidth: 0 } : it));
        expect(fanFrameExtent(collapsed, null, 5)).toEqual({ left: 1023, right: 2047 });
        expect(fanFrameExtent(items.filter(it => !it.synthetic), null, 5)).toBeNull();
        expect(fanFrameExtent([], null, 5)).toBeNull();
    });

    const anchor = { realId: 'S', columnId: 'fan-s1' };
    it('layoutAlias maps a fan-out and a collapse', () => {
        expect(layoutAlias(['A', 'S', 'B'], ['A', 'fan-s0', 'fan-s1', 'B'], anchor)).toEqual({ prevIdx: 1, newIdx: 2 });
        expect(layoutAlias(['A', 'fan-s0', 'fan-s1', 'B'], ['A', 'S', 'B'], anchor)).toEqual({ prevIdx: 2, newIdx: 1 });
        expect(layoutAlias(['S'], ['fan-s0', 'fan-s1'], anchor)).toEqual({ prevIdx: 0, newIdx: 1 });
    });
    it('layoutAlias is null for an unrelated change or no anchor', () => {
        expect(layoutAlias(['A', 'S', 'B'], ['A', 'S', 'B', 'D'], anchor)).toBeNull();
        expect(layoutAlias(['A', 'S', 'B'], ['A', 'B'], anchor)).toBeNull();
        expect(layoutAlias(['A', 'fan-s0', 'fan-s1'], ['A', 'fan-s0', 'fan-s1', 'D'], anchor)).toBeNull();
        expect(layoutAlias(['A', 'S'], ['A', 'fan-s0', 'fan-s1'], null)).toBeNull();
    });
});

describe('fanOut — fanHeaderSpan (the header clamp)', () => {
    // A 1400px viewport at zoom 1: world = cameraX + (screen − 700), so the
    // visible world window is [cameraX − 700, cameraX + 700].
    const view = (cameraX: number, over: Partial<{ zoom: number; width: number }> = {}) =>
        ({ cameraX, zoom: 1, width: 1400, ...over });
    const MIN = 220;

    it('a frame that fits on screen keeps the whole band', () => {
        // Band 400..1400 inside the window 0..1400.
        expect(fanHeaderSpan(400, 1000, view(700), MIN)).toEqual({ offset: 0, width: 1000 });
        // Exactly flush with both viewport edges.
        expect(fanHeaderSpan(0, 1400, view(700), MIN)).toEqual({ offset: 0, width: 1400 });
    });

    it('a frame wider than the viewport pins the content to the visible window', () => {
        // Band 0..3000, window 800..2200: content spans exactly the window.
        expect(fanHeaderSpan(0, 3000, view(1500), MIN)).toEqual({ offset: 800, width: 1400 });
    });

    it('a frame partly off-screen left: the content starts at the viewport edge, ends at the border', () => {
        // Band -500..1000, window 0..1400 → visible band 0..1000.
        expect(fanHeaderSpan(-500, 1500, view(700), MIN)).toEqual({ offset: 500, width: 1000 });
    });

    it('a frame partly off-screen right: the content starts at the border, ends at the viewport edge', () => {
        // Band 900..2900, window 0..1400 → visible band 900..1400.
        expect(fanHeaderSpan(900, 2000, view(700), MIN)).toEqual({ offset: 0, width: 500 });
    });

    it('a sliver on screen keeps the minimum width, inside the band, flush with the visible end', () => {
        // Off-screen left, 100px of the band's right end visible → the last
        // MIN px of the band (Collapse stays at the visible right end).
        expect(fanHeaderSpan(-1900, 2000, view(700), MIN)).toEqual({ offset: 1780, width: MIN });
        // Off-screen right, 100px of the band's left end visible → the first MIN px.
        expect(fanHeaderSpan(1300, 2000, view(700), MIN)).toEqual({ offset: 0, width: MIN });
        // Entirely off-screen either way: still inside the band.
        expect(fanHeaderSpan(-5000, 2000, view(700), MIN)).toEqual({ offset: 1780, width: MIN });
        expect(fanHeaderSpan(5000, 2000, view(700), MIN)).toEqual({ offset: 0, width: MIN });
        // A band narrower than the minimum is the whole band.
        expect(fanHeaderSpan(-150, 200, view(700), MIN)).toEqual({ offset: 0, width: 200 });
    });

    it('honours the viewport zoom', () => {
        // Zoom 0.5 doubles the visible world: window -700..2100 around 700.
        expect(fanHeaderSpan(-1000, 4000, view(700, { zoom: 0.5 }), MIN)).toEqual({ offset: 300, width: 2800 });
    });

    it('no view (unmeasured, or no camera) → the whole band', () => {
        expect(fanHeaderSpan(-500, 3000, null, MIN)).toEqual({ offset: 0, width: 3000 });
        expect(fanHeaderSpan(-500, 3000, view(700, { width: 0 }), MIN)).toEqual({ offset: 0, width: 3000 });
    });
});

describe('fanWidthsFor — the fan width rules', () => {
    it('fan-out: every column starts at the stack\'s override; stale fan widths are dropped', () => {
        const widths = { S: 640, B: 420, 'fan-old': 900, 'fan-s1': 333 };
        expect(fanWidthsFor(widths, null, S)).toEqual({
            S: 640, B: 420, 'fan-s0': 640, 'fan-s1': 640, 'fan-s2': 640,
        });
    });

    it('fan-out of a stack on the shared default: columns carry no override either', () => {
        const widths = { B: 420, 'fan-s1': 333 };
        expect(fanWidthsFor(widths, null, S)).toEqual({ B: 420 });
    });

    it('collapse: the stack takes the width of the column that becomes its front', () => {
        const widths = { S: 640, B: 420, 'fan-s0': 640, 'fan-s1': 710, 'fan-s2': 520 };
        expect(fanWidthsFor(widths, fanCollapseOf(S, 1), null)).toEqual({ S: 710, B: 420 });
        expect(fanWidthsFor(widths, fanCollapseOf(S, 2), null)).toEqual({ S: 520, B: 420 });
        // The front index wraps like visibleTopIndices.
        expect(fanWidthsFor(widths, fanCollapseOf(S, 4), null)).toEqual({ S: 710, B: 420 });
    });

    it('collapse onto a front column without an override drops the stack\'s override', () => {
        const widths = { S: 640, 'fan-s0': 640, 'fan-s2': 520 };
        expect(fanWidthsFor(widths, fanCollapseOf(S, 1), null)).toEqual({});
    });

    it('collapse after the stack dropped below 2 cells: the remaining cell\'s column width', () => {
        const shrunk = stack('S', [cell('s2')]);
        const widths = { S: 640, 'fan-s0': 640, 'fan-s1': 700, 'fan-s2': 560 };
        expect(fanWidthsFor(widths, fanCollapseOf(shrunk, 1), null)).toEqual({ S: 560 });
    });

    it('a collapse recorded by cell id applies without the stack at hand (channel switch)', () => {
        const widths = { S: 640, 'fan-s0': 640, 'fan-s1': 710 };
        expect(fanWidthsFor(widths, { stackId: 'S', frontCellId: 's1' }, null)).toEqual({ S: 710 });
    });

    it('fanCollapseOf: the wrapped front cell; null for an empty stack', () => {
        expect(fanCollapseOf(S, 4)).toEqual({ stackId: 'S', frontCellId: 's1' });
        expect(fanCollapseOf(stack('E', []), 0)).toBeNull();
    });

    it('a vanished stack (no collapsing stack): only the fan widths go', () => {
        expect(fanWidthsFor({ B: 420, 'fan-s0': 700 }, null, null)).toEqual({ B: 420 });
    });

    it('another stack fanning out: the old one collapses first, then the new columns start at its width', () => {
        const widths = { S: 640, B: 420, 'fan-s0': 800, 'fan-s1': 640, 'fan-s2': 640 };
        expect(fanWidthsFor(widths, fanCollapseOf(S, 0), B)).toEqual({
            S: 800, B: 420, 'fan-b0': 420, 'fan-b1': 420,
        });
    });

    it('returns the same map when nothing changes', () => {
        const widths = { S: 640, B: 420 };
        expect(fanWidthsFor(widths, null, null)).toBe(widths);
        const fanned = { S: 640, 'fan-s0': 640, 'fan-s1': 640, 'fan-s2': 640 };
        expect(fanWidthsFor(fanned, null, S)).toBe(fanned);
    });
});
