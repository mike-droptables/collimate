import { describe, expect, it } from 'vitest';
import {
    ARC_MIN_BULGE, boardEnd, corner, drawArc, drawArrow, layoutArrows, levelHeads, levelRoutes, lineLabel, overArc,
    placeBarLabels, route, shelfEnd, sideArc, standing, stepMidLabels,
    type ArrowLabel, type ArrowLine, type Box, type OverviewFrame,
} from './lineageRoutes';

// An overview 1000 x 800 with the journal open (340px) and the shelf shut:
// the bar spans 0-33, its middle at 16.5, the board starts at 33.
const frame: OverviewFrame = { width: 1000, height: 800, shelfPx: 0, left: 0, paneWidth: 340 };
// The shelf open at 316px: its grid from 36 (under the controls) to 316.
const open: OverviewFrame = { ...frame, shelfPx: 316 };
const grid: Box = { l: 0, r: 660, t: 36, b: 316 };
const nameR = 90;
const box = (l: number, t: number, w = 272, h = 248): Box => ({ l, r: l + w, t, b: t + h });
const measure = (text: string, name: string | null) => (name ? `${text} ${name}` : text).length * 6;
/** A label wider than the arc's minimum bulge allows for (a larger UI font). */
const wideLabel = () => 78;

describe('where a line meets a cell', () => {
    it('meets a board card that heads its column at its top edge, left of the centred stack name (O07)', () => {
        const end = boardEnd(box(300, 50), 50, true, frame)!;
        expect(end).toMatchObject({ kind: 'board', top: true, under: false });
        expect(route(end, 0, nameR, frame)).toEqual([[328, 50]]);
        // The second line of the same end leaves 30px further right.
        expect(route(end, 1, nameR, frame)).toEqual([[358, 50]]);
    });

    it('meets a lower board card from its side, up the gap beside its column (O07)', () => {
        const end = boardEnd(box(300, 400), 50, false, frame)!;
        expect(route(end, 1, nameR, frame)).toEqual([[300, 426], [290, 426], [290, 40]]);
        // A first-column card takes the gap on its right.
        const first = boardEnd(box(18, 400), 50, false, frame)!;
        expect(route(first, 0, nameR, frame)).toEqual([[290, 418], [296, 418], [296, 40]]);
    });

    it('stops a line where a card half under the shelf comes out, and drops a card out of view (O07)', () => {
        const under = boardEnd(box(300, 20), 20, true, frame)!;
        expect(under.under).toBe(true);
        expect(route(under, 0, nameR, frame)).toEqual([[328, 34]]);
        expect(boardEnd(box(300, 790), 50, false, frame)).toBeNull();          // below the board
        expect(boardEnd(box(300, -300), -300, true, frame)).toBeNull();        // above it, under the shelf
        expect(boardEnd(box(700, 50), 50, true, frame)).toBeNull();            // under the journal pane
    });

    it('meets a shelf card at its bottom edge, as far as the shelf shows it (O07)', () => {
        const end = shelfEnd(box(18, 50, 236, 280), grid, [box(272, 50)])!;
        expect(end).toMatchObject({ kind: 'shelf', below: false });
        expect(end.r.b).toBe(313);
        expect(route(end, 0, nameR, open)).toEqual([[104, 313]]);
    });

    it('meets a shelf card with another under it from the side, down the gap to the shelf\'s foot (O07)', () => {
        const end = shelfEnd(box(18, 50, 236, 118), grid, [box(18, 186, 236, 118)])!;
        expect(end.below).toBe(true);
        expect(route(end, 0, nameR, open)).toEqual([[254, 68], [260, 68], [260, 310]]);
    });

    it('cannot meet a shelf card scrolled away or a shut shelf\'s card (O07)', () => {
        expect(shelfEnd(box(18, -250), grid, [])).toBeNull();
        expect(shelfEnd(box(18, 320), grid, [])).toBeNull();
        expect(shelfEnd(box(18, 50), null, [])).toBeNull();
    });
});

describe('the board clear of the Commands pane (polish)', () => {
    // The pane docked left: the board starts at 320; docked right, it ends
    // 320 further in, the journal against the pane.
    const left: OverviewFrame = { ...frame, left: 320, paneWidth: 340 };
    const right: OverviewFrame = { ...frame, width: 1400, paneWidth: 340 + 320 };

    it('drops a card that lies past the board\'s edge under the pane (polish)', () => {
        expect(boardEnd(box(20, 400), 50, false, left)).toBeNull();
        expect(boardEnd(box(338, 400), 50, false, left)).not.toBeNull();
        expect(boardEnd(box(1400 - 660 + 10, 400), 50, false, right)).toBeNull();
        expect(boardEnd(box(1400 - 660 - 300, 400), 50, false, right)).not.toBeNull();
    });

    it('takes the first column at the board\'s left edge, past a pane docked left (polish)', () => {
        // The first column sits 18px past the pane: its lines take the gap on its right.
        const first = boardEnd(box(338, 400), 50, false, left)!;
        expect(route(first, 0, nameR, left)).toEqual([[610, 418], [616, 418], [616, 40]]);
        // The same card without the pane is a second column: the gap on its left.
        const second = boardEnd(box(338, 400), 50, false, frame)!;
        expect(route(second, 0, nameR, frame)).toEqual([[338, 418], [332, 418], [332, 40]]);
    });
});

describe('drawing a line', () => {
    it('rounds a corner with a 6px quadratic (O07)', () => {
        expect(corner([0, 0], [20, 0], [20, 20], 6)).toBe('L14 0Q20 0 20 6');
        // A short leg rounds by half its length.
        expect(corner([0, 0], [4, 0], [4, 10], 6)).toBe('L2 0Q4 0 4 2');
    });

    it('curves across open canvas, stops 7px short for the head, and labels the curve\'s middle (O07)', () => {
        const { arrow, mid } = drawArrow([[0, 0]], [[100, 100]], true, false);
        expect(arrow.d).toBe('M0 0C0 50 100 50 100 93');
        expect(arrow.head).toBe('M100 100L96.5 93L103.5 93Z');
        expect(arrow).toMatchObject({ dashed: true, hot: false });
        expect(mid).toEqual([50, 50]);
    });

    it('turns rounded corners on a side route at both ends (O07)', () => {
        const { arrow } = drawArrow([[300, 426], [290, 426], [290, 40]], [[328, 50]], false, true);
        expect(arrow.d.startsWith('M300 426L295 426Q290 426 290 421L290 40C')).toBe(true);
        expect(arrow.hot).toBe(true);
    });

    it('labels each line with the relation alone, the arrow carrying its direction (O07, LB-01)', () => {
        expect([lineLabel('duplicate'), lineLabel('merge'), lineLabel('split')]).toEqual(['duplicate', 'merge', 'split']);
    });
});

describe('labels', () => {
    it('steps a label that would sit on another, or on the bar\'s name, aside to the right (O07)', () => {
        expect(stepMidLabels([{ x: 100, y: 300, w: 60 }, { x: 110, y: 305, w: 60 }], 15, nameR)).toEqual([70, 136]);
        // At other heights they stay where the curve puts them.
        expect(stepMidLabels([{ x: 100, y: 300, w: 60 }, { x: 110, y: 400, w: 60 }], 15, nameR)).toEqual([70, 80]);
        // On the bar, clear of "Removed 13".
        expect(stepMidLabels([{ x: 50, y: 20, w: 40 }], 15, nameR)).toEqual([100]);
    });

    it('rows the bar labels from the subject\'s left and stops the row short of the bar\'s controls (O07)', () => {
        expect(placeBarLabels([60, 80], { from: 106, stop: 500, anchorLeft: 300, exitR: -1 })).toEqual([312, 386]);
        // Too long to start there: the row ends at the stop.
        expect(placeBarLabels([60, 80], { from: 106, stop: 400, anchorLeft: 300, exitR: -1 })).toEqual([246, 320]);
        // Never left of the count, never over the lines leaving a half-hidden subject.
        expect(placeBarLabels([60], { from: 106, stop: 140, anchorLeft: 0, exitR: -1 })).toEqual([106]);
        expect(placeBarLabels([60], { from: 106, stop: 900, anchorLeft: 0, exitR: 400 })).toEqual([412]);
    });
});

describe('the subject\'s arrows', () => {
    const anchor = boardEnd(box(300, 50), 50, true, frame)!;
    const line = (over: Partial<ArrowLine>): ArrowLine => ({
        cellId: 'x', op: 'merge', out: true, hot: false, name: 'X', walkable: true, end: null, ...over,
    });

    it('draws a line to a seen cell with its phrase mid-line, dashed for a duplicate (O07)', () => {
        const target = boardEnd(box(18, 400), 50, false, frame)!;
        const out = layoutArrows({
            frame, nameR, controlsLeft: 600, anchor, measure,
            lines: [line({ cellId: 'm', op: 'merge', end: target }), line({ cellId: 'd', op: 'duplicate', out: false, end: target })],
        });
        expect(out.arrows.map(a => a.dashed)).toEqual([false, true]);
        expect(out.labels.map(l => [l.cellId, l.text, l.name])).toEqual([['m', 'merge', null], ['d', 'duplicate', null]]);
        // A line leaving the subject starts at its top edge; one arriving ends
        // there, 30px along (its second line), with the head on the subject.
        expect(out.arrows[0].d.startsWith('M328 50')).toBe(true);
        expect(out.arrows[0].head.startsWith('M290 418')).toBe(true);
        expect(out.arrows[1].d.startsWith('M290 418')).toBe(true);
        expect(out.arrows[1].head.startsWith('M358 50')).toBe(true);
    });

    it('joins farther column heads at the same height by their facing sides, along the gap above the columns between (O07)', () => {
        const wide: OverviewFrame = { ...frame, width: 1600 };
        const a = boardEnd(box(300, 50), 50, true, wide)!;
        const far = boardEnd(box(890, 56), 56, true, wide)!;
        expect(levelRoutes(a, far, 0, wide)).toEqual([[[572, 68], [578, 68], [578, 40]], [[890, 74], [884, 74], [884, 46]]]);
        const out = layoutArrows({ frame: wide, nameR, controlsLeft: 600, anchor: a, measure, lines: [line({ end: far })] });
        expect(out.arrows[0].head).toBe('M890 74L883 77.5L883 70.5Z');
    });

    it('keeps the top-edge route for column heads at different heights or half under the shelf (O07)', () => {
        const lower = boardEnd(box(600, 100), 100, true, { ...frame, paneWidth: 0 })!;
        expect(levelHeads(anchor, lower)).toBe(false);
        expect(levelHeads(boardEnd(box(300, 20), 20, true, frame)!, boardEnd(box(18, 20), 20, true, frame)!)).toBe(false);
        expect(levelHeads(anchor, boardEnd(box(18, 400), 50, false, frame)!)).toBe(false);
    });

    it('gives two merge inputs on the open shelf the subject\'s slots in their order, so the curves do not cross and the labels follow them (O07)', () => {
        // The subject heads its column under the open shelf; its inputs sit
        // on the shelf, the right one listed first (the order that crossed).
        const subject = boardEnd(box(500, 360), 360, true, open)!;
        const ends = { r: shelfEnd(box(300, 50), grid, [])!, l: shelfEnd(box(18, 50), grid, [])! };
        const out = layoutArrows({
            frame: open, nameR, controlsLeft: 600, anchor: subject, measure,
            lines: [line({ cellId: 'r', out: false, end: ends.r }), line({ cellId: 'l', out: false, end: ends.l })],
        });
        const xOf = (path: string): number => Number(/^M(-?[\d.]+) /.exec(path)![1]);
        // Each curve leaves its input's foot and its head lands on the subject's top edge.
        const curves = out.arrows.map((arrow, i) => ({ cellId: out.labels[i].cellId, from: xOf(arrow.d), to: xOf(arrow.head) }));
        expect(curves).toEqual([{ cellId: 'r', from: 328, to: 558 }, { cellId: 'l', from: 104, to: 528 }]);
        // Left to right at both ends: the left input takes the left slot.
        const byFrom = [...curves].sort((p, q) => p.from - q.from).map(c => c.cellId);
        expect([...curves].sort((p, q) => p.to - q.to).map(c => c.cellId)).toEqual(byFrom);
        // The labels, on the bar's line, read in the curves' order.
        expect(out.labels.map(l => l.text)).toEqual(['merge', 'merge']);
        expect([...out.labels].sort((p, q) => p.left - q.left).map(l => l.cellId)).toEqual(byFrom);
    });

    it('meets a cell that cannot be seen on the Removed bar, its label naming it (O07)', () => {
        const out = layoutArrows({
            frame, nameR, controlsLeft: 600, anchor, measure,
            lines: [line({ cellId: 'g', op: 'split', out: true, name: 'Gone', walkable: false, end: null })],
        });
        expect(out.labels).toEqual([{ cellId: 'g', text: 'split', name: 'Gone', title: 'split · Gone', walkable: false, left: 312, top: 7.5 }]);
        // From a board subject the line comes up to the bar's lower half.
        expect(out.arrows[0].head.startsWith('M326 25.5')).toBe(true);
    });

    it('from a shelf subject the bar line meets the bar\'s upper half; from a half-hidden one there is no line (O07)', () => {
        const shelfAnchor = shelfEnd(box(18, 50), grid, [])!;
        const fromShelf = layoutArrows({ frame: open, nameR, controlsLeft: 600, anchor: shelfAnchor, measure, lines: [line({ name: 'Gone' })] });
        expect(fromShelf.arrows[0].head.startsWith(`M${fromShelf.labels[0].left + 14} 323.5`)).toBe(true);
        const under = boardEnd(box(300, 20), 20, true, frame)!;
        const fromUnder = layoutArrows({ frame, nameR, controlsLeft: 600, anchor: under, measure, lines: [line({ name: 'Gone' })] });
        expect(fromUnder.arrows).toEqual([]);
        expect(fromUnder.labels).toHaveLength(1);
    });
});

describe('arcs between close cards', () => {
    const line = (over: Partial<ArrowLine>): ArrowLine => ({
        cellId: 'x', op: 'duplicate', out: true, hot: false, name: 'X', walkable: true, end: null, ...over,
    });
    /** A label's box: 18px tall, as wide as `measure` says. */
    const labelBox = (label: ArrowLabel, width: typeof measure = measure): Box => {
        const w = width(label.text, label.name);
        return { l: label.left, r: label.left + w, t: label.top, b: label.top + 18 };
    };
    const clear = (p: Box, q: Box): boolean => p.r <= q.l || q.r <= p.l || p.b <= q.t || q.b <= p.t;
    /** The numbers of an SVG path, in order. */
    const nums = (d: string): number[] => (d.match(/-?\d+(\.\d+)?/g) ?? []).map(Number);
    /** The cubic's points sampled along it (`M x y C x y x y x y`). */
    const samples = (d: string): [number, number][] => {
        const [x0, y0, x1, y1, x2, y2, x3, y3] = nums(d);
        return Array.from({ length: 41 }, (_, k) => {
            const t = k / 40;
            const u = 1 - t;
            return [
                u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3,
                u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3,
            ];
        });
    };

    it('tells side by side heads, one card above another and cards far apart (polish)', () => {
        const wide: OverviewFrame = { ...frame, width: 1600 };
        const head = boardEnd(box(300, 150), 150, true, wide)!;
        expect(standing(head, boardEnd(box(18, 150), 150, true, wide)!)).toBe('beside');
        expect(standing(head, boardEnd(box(582, 150), 150, true, wide)!)).toBe('beside');
        expect(standing(head, boardEnd(box(300, 416), 150, false, wide)!)).toBe('stacked');
        // A column between, a neighbour's lower card, a shelf card, a card half under the shelf: far.
        expect(standing(head, boardEnd(box(864, 150), 150, true, wide)!)).toBe('far');
        expect(standing(head, boardEnd(box(18, 416), 150, false, wide)!)).toBe('far');
        expect(standing(head, shelfEnd(box(300, 50), grid, [])!)).toBe('far');
        expect(standing(boardEnd(box(300, 20), 20, true, frame)!, boardEnd(box(18, 20), 20, true, frame)!)).toBe('far');
    });

    it('builds square-legged arcs whose middle is the crest (polish)', () => {
        expect(overArc([0, 100], [200, 100], 60)).toEqual({ from: [0, 100], to: [200, 100], c1: [0, 140 / 3], c2: [200, 140 / 3] });
        expect(sideArc([100, 0], [100, 200], 160)).toEqual({ from: [100, 0], to: [100, 200], c1: [180, 0], c2: [180, 200] });
        const { arrow, mid } = drawArc(sideArc([100, 0], [100, 200], 160), true, true);
        // 7px short of the end along the last leg; the head points back into the card.
        expect(arrow.d).toBe('M100 0C180 0 180 200 107 200');
        expect(arrow.head).toBe('M100 200L107 196.5L107 203.5Z');
        expect(arrow).toMatchObject({ dashed: true, hot: true });
        expect(mid).toEqual([160, 100]);
    });

    it('joins side by side heads near their facing corners over their heads, clear of the stack names, the label on the crest clear of both cards (O07, polish)', () => {
        // The owner's first shot: "duplicate of" squashed in the gap between neighbours.
        const subject = boardEnd(box(300, 150), 150, true, frame)!;
        const source = boardEnd(box(18, 150), 150, true, frame)!;
        const out = layoutArrows({
            frame, nameR, controlsLeft: 600, anchor: subject, measure,
            lines: [line({ cellId: 's', out: false, end: source })],
        });
        const [arrow] = out.arrows;
        expect(arrow.dashed).toBe(true);
        // Out of the source's top edge 28px in from its right corner, straight
        // up; the head lands 28px in from the subject's left corner, pointing
        // down. The middles, where the stack names sit, stay clear.
        expect(arrow.d.startsWith('M262 150C262 ')).toBe(true);
        expect(arrow.head).toBe('M328 150L324.5 143L331.5 143Z');
        const xs = samples(arrow.d).map(p => p[0]);
        expect(Math.min(...xs)).toBeGreaterThan(154 + 48);
        expect(Math.max(...xs)).toBeLessThan(436 - 48);
        // The arc keeps at least ARC_MIN_BULGE above the heads.
        const top = Math.min(...samples(arrow.d).map(p => p[1]));
        expect(150 - top).toBeGreaterThanOrEqual(ARC_MIN_BULGE - 1);
        // Its label sits on the crest, between the two cards, with canvas around it.
        const [label] = out.labels;
        expect(label).toMatchObject({ cellId: 's', text: 'duplicate', walkable: true });
        const lb = labelBox(label);
        expect((lb.l + lb.r) / 2).toBeCloseTo(295, 0);
        expect(Math.abs(lb.t + 9 - top)).toBeLessThan(1);
        expect(clear(lb, subject.r) && clear(lb, source.r)).toBe(true);
        expect(subject.r.t - lb.b).toBeGreaterThanOrEqual(30);
    });

    it('keeps an arc over the heads below the top of the Removed bar when the board has little room above them (polish)', () => {
        const subject = boardEnd(box(300, 50), 50, true, frame)!;
        const out = layoutArrows({
            frame, nameR, controlsLeft: 600, anchor: subject, measure,
            lines: [line({ out: false, end: boardEnd(box(18, 50), 50, true, frame)! })],
        });
        // The crest stops where the label still fits on the bar (its middle 12px down).
        expect(out.labels[0].top).toBe(3);
    });

    it('joins two cards of a column from right middle to right middle, out to the right by more than half the label (polish)', () => {
        // The owner's second shot: a label hugging the column's right edge.
        const roomy: OverviewFrame = { ...frame, paneWidth: 0 };
        const subject = boardEnd(box(300, 50), 50, true, roomy)!;
        const copy = boardEnd(box(300, 316), 50, false, roomy)!;
        const out = layoutArrows({
            frame: roomy, nameR, controlsLeft: 600, anchor: subject, measure: wideLabel,
            lines: [line({ cellId: 'c', out: true, end: copy })],
        });
        const [arrow] = out.arrows;
        // Out of the subject's right middle, straight out; the head lands on the copy's right middle, pointing left.
        expect(arrow.d.startsWith('M572 174C')).toBe(true);
        expect(arrow.head).toBe('M572 440L579 436.5L579 443.5Z');
        // The label is 78px wide here: the arc bows 39 + 12px out, more than the 40px minimum.
        const right = Math.max(...samples(arrow.d).map(p => p[0]));
        expect(right - 572).toBeGreaterThanOrEqual(51 - 1);
        const [label] = out.labels;
        expect(label.text).toBe('duplicate');
        const lb = labelBox(label, wideLabel);
        expect(Math.abs((lb.l + lb.r) / 2 - right)).toBeLessThan(1);
        expect((lb.t + lb.b) / 2).toBe(307);
        expect(lb.l - 572).toBeGreaterThanOrEqual(12);
        expect(clear(lb, subject.r) && clear(lb, copy.r)).toBe(true);
    });

    it('bows a column\'s arc out to the left where the journal pane leaves its label no room on the right (polish)', () => {
        const subject = boardEnd(box(300, 50), 50, true, frame)!;
        const copy = boardEnd(box(300, 316), 50, false, frame)!;
        const out = layoutArrows({ frame, nameR, controlsLeft: 600, anchor: subject, measure: wideLabel, lines: [line({ end: copy })] });
        expect(out.arrows[0].d.startsWith('M300 174C')).toBe(true);
        expect(out.arrows[0].head).toBe('M300 440L293 443.5L293 436.5Z');
        const lb = labelBox(out.labels[0], wideLabel);
        expect(300 - lb.r).toBeGreaterThanOrEqual(12);
    });

    it('nests two arcs leaving one edge the same way, the nearer card\'s inside, their labels apart (polish)', () => {
        const roomy: OverviewFrame = { ...frame, paneWidth: 0 };
        const subject = boardEnd(box(300, 50), 50, true, roomy)!;
        const near = boardEnd(box(300, 316), 50, false, roomy)!;
        const far = boardEnd(box(300, 582), 50, false, roomy)!;
        const out = layoutArrows({
            frame: roomy, nameR, controlsLeft: 600, anchor: subject, measure,
            lines: [line({ cellId: 'far', end: far }), line({ cellId: 'near', end: near })],
        });
        const reach = out.arrows.map(arrow => Math.max(...samples(arrow.d).map(p => p[0])));
        expect(reach[0] - reach[1]).toBeCloseTo(22, 0);
        // The legs part along the subject's edge.
        expect(nums(out.arrows[0].d).slice(0, 2)).toEqual([572, 174]);
        expect(nums(out.arrows[1].d).slice(0, 2)).toEqual([572, 182]);
        const [a, b] = out.labels.map(l => labelBox(l));
        expect(clear(a, b)).toBe(true);
    });

    it('keeps the mock\'s routes for cards far apart (polish)', () => {
        const subject = boardEnd(box(300, 50), 50, true, frame)!;
        const lower = boardEnd(box(18, 400), 50, false, frame)!;
        const out = layoutArrows({ frame, nameR, controlsLeft: 600, anchor: subject, measure, lines: [line({ end: lower })] });
        // Out of the subject's top edge, left of its centred name; into the lower card's side up the gap.
        expect(out.arrows[0].d.startsWith('M328 50')).toBe(true);
        expect(out.arrows[0].head.startsWith('M290 418')).toBe(true);
    });
});
