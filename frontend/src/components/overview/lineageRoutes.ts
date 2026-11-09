// The lineage arrows' geometry: pure math over the rects the overview
// measures. Every px is overview-local (from the overview root's top-left
// corner).
//
// An arrow joins the journal's subject and a cell it was duplicated, merged
// or split with. Two board cards close to each other are joined by an arc
// with room for its label: heads of neighbouring columns (side by side) from
// top edge to top edge near the corners that face each other (clear of the
// stack names centred over them), over their heads; two cards of one column (one
// above the other) from right middle to right middle, out to the right (to
// the left where the board ends first). Every arc keeps ARC_MIN_BULGE clear
// of the cards, more for a wide label, and its label sits on its crest.
// Cards farther apart take the mock's routes (endOf, route, corner, draw):
// a board card is met at its top edge when it heads its column, else from
// its side and up the gap beside its column; a shelf card at its bottom
// edge, as far as the shelf shows it. A cell that cannot be seen is met on
// the Removed bar, where its label names it.

import type { LineageOp } from '@/lib/api/cells';
import { OVERVIEW_BAR_HEIGHT } from '@/stores/collimate/overviewSlice';

/** The attributes the overview's parts carry for the arrows to measure. */
export const OVERVIEW_DOM = {
    /** The overview root (`--ov-shelf` holds the shelf's animated height). */
    root: 'data-overview-root',
    /** A board card; its value is the cell id. */
    boardCard: 'data-ov-card',
    /** A board column's card list: its first `boardCard` heads the column. */
    column: 'data-ov-column',
    /** The shelf's scrolling grid. */
    shelfGrid: 'data-rm-grid',
    /** A shelf card; its value is the cell id. */
    shelfCard: 'data-rm-card',
    /** The Removed bar on the board's top edge: cells that cannot be seen are met on it. */
    bar: 'data-rm-bar',
    /** The Removed count on the bar: lines and labels keep right of it. */
    barCount: 'data-rm-n',
    /** The bar's chevron; with the bar's buttons, the controls bar labels stop short of. */
    barChevron: 'data-rm-chev',
} as const;

export type Point = readonly [number, number];

/** A rect, overview-local. */
export interface Box {
    l: number;
    r: number;
    t: number;
    b: number;
}

/** Where a board card can be met. `top`: it heads its column; `colTop`: the
 *  column's first card's top; `under`: it is half under the shelf. */
export interface BoardEnd {
    kind: 'board';
    r: Box;
    top: boolean;
    colTop: number;
    under: boolean;
}

/** Where a shelf card can be met. `below`: another card sits under it in
 *  the shelf's view; `gt` / `gb`: the grid's visible top and bottom. */
export interface ShelfEnd {
    kind: 'shelf';
    r: Box;
    below: boolean;
    gt: number;
    gb: number;
}

export type LineEnd = BoardEnd | ShelfEnd;

/** The overview's frame: its size, the shelf's current height, the board's
 *  left edge (`left`: the Commands pane's width while it is docked on the
 *  left) and what lies right of the board (`paneWidth`: the journal pane and
 *  the Commands pane docked on the right; the board's right edge is
 *  `width - paneWidth`). */
export interface OverviewFrame {
    width: number;
    height: number;
    shelfPx: number;
    left: number;
    paneWidth: number;
}

export const boardTopOf = (frame: OverviewFrame): number => frame.shelfPx + OVERVIEW_BAR_HEIGHT;
export const barMidOf = (frame: OverviewFrame): number => frame.shelfPx + OVERVIEW_BAR_HEIGHT / 2;

/** A board card as an end, or null when it is out of the board's view. */
export function boardEnd(card: Box, columnTop: number, headsColumn: boolean, frame: OverviewFrame): BoardEnd | null {
    const boardTop = boardTopOf(frame);
    if (card.b < boardTop + 12 || card.t > frame.height - 12 || card.r < frame.left || card.l > frame.width - frame.paneWidth) {
        return null;
    }
    return { kind: 'board', r: card, top: headsColumn, colTop: columnTop, under: card.t < boardTop + 4 };
}

/** A shelf card as an end, or null when the shelf does not show it (shut,
 *  too short, or the card is scrolled away). `others` are the shelf's other
 *  cards. */
export function shelfEnd(card: Box, grid: Box | null, others: readonly Box[]): ShelfEnd | null {
    if (!grid || card.b < grid.t + 24 || card.t > grid.b - 8) return null;
    const below = others.some(q => q.t >= card.b - 1 && q.t < grid.b - 8 && q.l < card.r - 4 && q.r > card.l + 4);
    return { kind: 'shelf', r: { ...card, b: Math.min(card.b, grid.b - 3) }, below, gt: grid.t, gb: grid.b };
}

/** The points an arrow leaves (or meets) an end by, the `i`th line of that
 *  end. `nameR` is the right edge of the bar's Removed count: lines meet
 *  first-column cells to the right of it. */
export function route(end: LineEnd, i: number, nameR: number, frame: OverviewFrame): Point[] {
    const boardTop = boardTopOf(frame);
    // Off to the left, clear of the stack name centred over the card.
    const x = Math.max(end.r.l + 28, nameR + 14) + 30 * i;
    // First-column cells take the gap on their right.
    const right = end.r.l < frame.left + 40;
    const side = right ? end.r.r : end.r.l;
    const gx = right ? end.r.r + 6 + 4 * i : end.r.l - 6 - 4 * i;
    if (end.kind === 'shelf') {
        if (!end.below) return [[x, end.r.b]];
        const sy = Math.max(end.r.t + 18 + 8 * i, end.gt + 10);
        return [[side, sy], [gx, sy], [gx, end.gb - 6]];
    }
    // Half under the shelf: the line stops where the cell comes out.
    if (end.under) return [[x, boardTop + 1]];
    if (end.top) return [[x, end.r.t]];
    const y = end.r.t + 18 + 8 * i;
    const fy = Math.max(end.colTop - 10, boardTop + 6);
    return [[side, y], [gx, y], [gx, fy]];
}

/** Two column heads whose tops are this close are at the same height. */
const LEVEL_PX = 12;

/** Both ends head their board columns at the same height: a line between
 *  their top edges would run flat, with no direction for its head. */
export function levelHeads(a: LineEnd, b: LineEnd): boolean {
    return a.kind === 'board' && b.kind === 'board' && a.top && b.top && !a.under && !b.under
        && Math.abs(a.r.t - b.r.t) < LEVEL_PX;
}

/** The `i`th line of a board card, out of the side that faces `toward` (the
 *  other card's centre) and up the gap there to above its column. */
export function sideRoute(end: BoardEnd, i: number, toward: number, frame: OverviewFrame): Point[] {
    const right = toward > (end.r.l + end.r.r) / 2;
    const side = right ? end.r.r : end.r.l;
    const gx = right ? end.r.r + 6 + 4 * i : end.r.l - 6 - 4 * i;
    const y = end.r.t + 18 + 8 * i;
    const fy = Math.max(end.colTop - 10, boardTopOf(frame) + 6);
    return [[side, y], [gx, y], [gx, fy]];
}

/** The routes of the subject's `i`th line to a farther column head at its
 *  height (side by side heads take an arc, `standing`), the subject's
 *  first: each one's side route, the line running along the gap above the
 *  columns between. */
export function levelRoutes(a: BoardEnd, b: BoardEnd, i: number, frame: OverviewFrame): [Point[], Point[]] {
    return [sideRoute(a, i, (b.r.l + b.r.r) / 2, frame), sideRoute(b, 0, (a.r.l + a.r.r) / 2, frame)];
}

/** How two cards stand. `beside`: board cards heading neighbouring columns
 *  (no column fits between them); `stacked`: board cards of one column;
 *  `far`: anything else (a card on the shelf or half under it included). */
export type Standing = 'beside' | 'stacked' | 'far';

export function standing(a: LineEnd, b: LineEnd): Standing {
    if (a.kind !== 'board' || b.kind !== 'board' || a.under || b.under) return 'far';
    const narrower = Math.min(a.r.r - a.r.l, b.r.r - b.r.l);
    const overlap = Math.min(a.r.r, b.r.r) - Math.max(a.r.l, b.r.l);
    if (overlap > narrower / 2) return 'stacked';
    const gap = -overlap;
    return gap >= 0 && gap < narrower && a.top && b.top ? 'beside' : 'far';
}

const n2 = (v: number): string => String(Math.round(v * 100) / 100);
const pt = (p: Point): string => `${n2(p[0])} ${n2(p[1])}`;

/** A rounded corner at `c` between the legs from `a` and to `z`. */
export function corner(a: Point, c: Point, z: Point, rad: number): string {
    const d1 = [c[0] - a[0], c[1] - a[1]];
    const d2 = [z[0] - c[0], z[1] - c[1]];
    const l1 = Math.hypot(d1[0], d1[1]) || 1;
    const l2 = Math.hypot(d2[0], d2[1]) || 1;
    const k = Math.min(rad, l1 / 2, l2 / 2);
    return `L${pt([c[0] - (d1[0] / l1) * k, c[1] - (d1[1] / l1) * k])}`
        + `Q${pt(c)} ${pt([c[0] + (d2[0] / l2) * k, c[1] + (d2[1] / l2) * k])}`;
}

/** An arc's least distance from the cards it joins. */
export const ARC_MIN_BULGE = 40;
/** A side arc's label keeps this far from the cards. */
const ARC_LABEL_CLEAR = 12;
/** Each further arc on one side of the subject runs this much wider. */
const ARC_NEST = 22;
/** Arcs leaving one edge of the subject both ways part by this much. */
const ARC_PART = 6;
/** An arc over two heads leaves and meets their top edges this far in from
 *  the corners that face each other: clear of the stack name centred over
 *  each card (the far routes' `route` keeps the same 28px). */
const ARC_HEAD_IN = 28;

/** A cubic arc from `from` to `to` with both legs square to their edges. */
export interface Arc {
    from: Point;
    to: Point;
    c1: Point;
    c2: Point;
}

/** The arc over two top edges whose crest is at `crestY`: both legs run
 *  straight up. A cubic's middle is an eighth of each end and three eighths
 *  of each control, so the controls sit where the middle lands on the crest. */
export function overArc(from: Point, to: Point, crestY: number): Arc {
    const c = (crestY - 0.125 * (from[1] + to[1])) / 0.75;
    return { from, to, c1: [from[0], c], c2: [to[0], c] };
}

/** The arc beside two side edges whose crest is at `crestX`: both legs run
 *  straight out. */
export function sideArc(from: Point, to: Point, crestX: number): Arc {
    const c = (crestX - 0.125 * (from[0] + to[0])) / 0.75;
    return { from, to, c1: [c, from[1]], c2: [c, to[1]] };
}

export interface Arrow {
    /** The line. */
    d: string;
    /** The arrow head (a filled triangle). */
    head: string;
    dashed: boolean;
    hot: boolean;
}

/** One arrow: out of the start cell to its free point, a curve across open
 *  canvas, then into the end cell. `mid` is the curve's middle, where the
 *  label sits. */
export function drawArrow(sp: readonly Point[], ep: readonly Point[], dashed: boolean, hot: boolean): { arrow: Arrow; mid: Point } {
    const e = [...ep].reverse();
    const f1 = sp[sp.length - 1];
    const f2 = e[0];
    const dy = (f2[1] - f1[1]) / 2;
    const c1: Point = [f1[0], f1[1] + dy];
    const c2: Point = [f2[0], f2[1] - dy];
    const last = e[e.length - 1];
    const prev = e.length > 1 ? e[e.length - 2] : c2;
    const ul = Math.hypot(last[0] - prev[0], last[1] - prev[1]) || 1;
    const ux = (last[0] - prev[0]) / ul;
    const uy = (last[1] - prev[1]) / ul;
    const tip: Point = [last[0] - ux * 7, last[1] - uy * 7];
    let d = `M${pt(sp[0])}`;
    for (let i = 1; i < sp.length; i++) d += i < sp.length - 1 ? corner(sp[i - 1], sp[i], sp[i + 1], 6) : `L${pt(sp[i])}`;
    const tail = [...e];
    tail[tail.length - 1] = tip;
    d += `C${pt(c1)} ${pt(c2)} ${pt(e.length > 1 ? f2 : tip)}`;
    for (let j = 1; j < tail.length; j++) d += j < tail.length - 1 ? corner(tail[j - 1], tail[j], tail[j + 1], 6) : `L${pt(tail[j])}`;
    const head = `M${pt(last)}L${pt([tip[0] - uy * 3.5, tip[1] + ux * 3.5])}L${pt([tip[0] + uy * 3.5, tip[1] - ux * 3.5])}Z`;
    const mid: Point = [
        0.125 * f1[0] + 0.375 * c1[0] + 0.375 * c2[0] + 0.125 * f2[0],
        0.125 * f1[1] + 0.375 * c1[1] + 0.375 * c2[1] + 0.125 * f2[1],
    ];
    return { arrow: { d, head, dashed, hot }, mid };
}

/** One arc: 7px short of its end for the head, which points along the
 *  arc's last leg. `mid` is the whole arc's middle, its crest, where the
 *  label sits (the 7px move it by less than a pixel). */
export function drawArc(arc: Arc, dashed: boolean, hot: boolean): { arrow: Arrow; mid: Point } {
    const { from, to, c1, c2 } = arc;
    const ul = Math.hypot(to[0] - c2[0], to[1] - c2[1]) || 1;
    const ux = (to[0] - c2[0]) / ul;
    const uy = (to[1] - c2[1]) / ul;
    const tip: Point = [to[0] - ux * 7, to[1] - uy * 7];
    const d = `M${pt(from)}C${pt(c1)} ${pt(c2)} ${pt(tip)}`;
    const head = `M${pt(to)}L${pt([tip[0] - uy * 3.5, tip[1] + ux * 3.5])}L${pt([tip[0] + uy * 3.5, tip[1] - ux * 3.5])}Z`;
    const mid: Point = [
        0.125 * from[0] + 0.375 * c1[0] + 0.375 * c2[0] + 0.125 * to[0],
        0.125 * from[1] + 0.375 * c1[1] + 0.375 * c2[1] + 0.125 * to[1],
    ];
    return { arrow: { d, head, dashed, hot }, mid };
}

export type LineOp = Exclude<LineageOp, 'run_cell'>;

/** What a line says: the relation alone ("duplicate", "merge", "split"),
 *  the same from either end. The arrow's direction (source to result)
 *  carries which way it went; only the journal writes it as a sentence. */
export const lineLabel = (op: LineOp): string => op;

/** A label's half height: labels are 18px tall, centred on their point. */
const LABEL_HALF = 9;

/** Mid-line labels that would sit on each other, or on the bar's name, step
 *  aside to the right, keeping their height. Takes each label's centre and
 *  width; returns each one's left, in the input order. */
export function stepMidLabels(labels: readonly { x: number; y: number; w: number }[], barY: number, nameR: number): number[] {
    const placed = labels.map((m, i) => {
        let left = m.x - m.w / 2;
        if (Math.abs(m.y - barY) < 18 && left < nameR + 10) left = nameR + 10;
        return { i, y: m.y, w: m.w, left };
    });
    const order = [...placed].sort((a, z) => a.left - z.left);
    order.forEach((m, i) => {
        for (let k = 0; k < i; k++) {
            const q = order[k];
            if (Math.abs(q.y - m.y) < 18 && m.left < q.left + q.w + 6) m.left = q.left + q.w + 6;
        }
    });
    return placed.map(m => m.left);
}

/** The bar's labels, in a row 14px apart: right of the Removed count, from
 *  the subject's left edge where the row fits before `stop` (the bar's
 *  controls) and clear of the lines leaving a half-hidden subject (`exitR`).
 *  Returns each label's left. */
export function placeBarLabels(widths: readonly number[], at: { from: number; stop: number; anchorLeft: number; exitR: number }): number[] {
    const total = widths.reduce((sum, w) => sum + w + 14, -14);
    let bx = Math.max(at.from, Math.min(at.anchorLeft + 12, at.stop - total), at.exitR + 12);
    return widths.map(w => {
        const left = bx;
        bx += w + 14;
        return left;
    });
}

/** One of the subject's lines, with where its other cell can be met (null:
 *  it cannot be seen, so the line meets the bar). */
export interface ArrowLine {
    cellId: string;
    op: LineOp;
    /** The subject is the move's source. */
    out: boolean;
    /** The other cell is the selected (or picked) one, with Only connected on. */
    hot: boolean;
    /** The other cell's name, written on a bar label. */
    name: string;
    /** The other cell is on the board or the shelf, so its label walks there. */
    walkable: boolean;
    end: LineEnd | null;
}

export interface ArrowLabel {
    cellId: string;
    text: string;
    /** A bar label's cell name, after the label. */
    name: string | null;
    /** The label and the cell's name, for every label (its accessible name). */
    title: string;
    /** A press walks to the cell (it is on the board or the shelf). */
    walkable: boolean;
    left: number;
    top: number;
}

export interface ArrowDrawing {
    arrows: Arrow[];
    labels: ArrowLabel[];
}

export interface ArrowScene {
    frame: OverviewFrame;
    /** The right edge of the bar's Removed count. */
    nameR: number;
    /** The left of the bar's leftmost control. */
    controlsLeft: number;
    anchor: LineEnd;
    lines: readonly ArrowLine[];
    /** A label's width: its label, and the cell name a bar label adds. */
    measure: (text: string, name: string | null) => number;
}

const freeX = (points: readonly Point[]): number => points[points.length - 1][0];

/** Each line's slot on the subject (its `i` in `route`). The lines to seen
 *  cells take the first slots in the order those cells are met along the
 *  subject's slots, so no two of them cross on the way (two merge inputs on
 *  the open shelf: the left input's line takes the left slot, and its
 *  mid-line label sits left of the other's); the bar lines take the next
 *  ones. An arc's or a level head's line (`own`) takes none. */
function subjectSlots(scene: ArrowScene, own: readonly boolean[]): number[] {
    const { frame, nameR, anchor, lines } = scene;
    const met = lines.flatMap((line, i) => (line.end && !own[i] ? [i] : []));
    const slots = lines.map(() => 0);
    // The way the subject's slots run: rightward, or leftward up a left gap.
    const dir = Math.sign(freeX(route(anchor, 1, nameR, frame)) - freeX(route(anchor, 0, nameR, frame))) || 1;
    const atX = (i: number): number => freeX(route(lines[i].end!, 0, nameR, frame));
    [...met].sort((p, q) => dir * (atX(p) - atX(q))).forEach((i, n) => { slots[i] = n; });
    let next = met.length;
    lines.forEach((line, i) => { if (!line.end) slots[i] = next++; });
    return slots;
}

const midX = (r: Box): number => (r.l + r.r) / 2;
const midY = (r: Box): number => (r.t + r.b) / 2;

/** The arcs of the subject's lines to the cards that stand beside it or in
 *  its column (`kinds`), by line. Arcs leaving one edge of the subject the
 *  same way nest: the nearest card's is the innermost, the others run
 *  ARC_NEST wider each, and the arcs part along the edge so their legs do
 *  not overlap. A column's arcs bow out to the right, or to the left where
 *  the board (or the journal pane) leaves the label no room on the right. */
function subjectArcs(scene: ArrowScene, kinds: readonly Standing[]): (Arc | null)[] {
    const { frame, anchor: a, lines, measure } = scene;
    const boardRight = frame.width - frame.paneWidth;
    const crestTop = frame.shelfPx + LABEL_HALF + 3;
    type Plan = { i: number; edge: 'over' | 'right' | 'left'; dir: number; far: number; w: number };
    const plans: Plan[] = [];
    lines.forEach((line, i) => {
        const e = line.end;
        if (!e || kinds[i] === 'far') return;
        const w = measure(lineLabel(line.op), null);
        if (kinds[i] === 'beside') {
            plans.push({ i, edge: 'over', dir: Math.sign(midX(e.r) - midX(a.r)) || 1, far: Math.abs(midX(e.r) - midX(a.r)), w });
            return;
        }
        const bulge = Math.max(ARC_MIN_BULGE, w / 2 + ARC_LABEL_CLEAR);
        const rightRoom = Math.max(a.r.r, e.r.r) + bulge + w / 2 <= boardRight - 4;
        const leftRoom = Math.min(a.r.l, e.r.l) - bulge - w / 2 >= frame.left + 4;
        plans.push({
            i, edge: rightRoom || !leftRoom ? 'right' : 'left',
            dir: Math.sign(midY(e.r) - midY(a.r)) || 1, far: Math.abs(midY(e.r) - midY(a.r)), w,
        });
    });
    const arcs: (Arc | null)[] = lines.map(() => null);
    for (const plan of plans) {
        const same = plans.filter(p => p.edge === plan.edge && p.dir === plan.dir).sort((p, q) => p.far - q.far || p.i - q.i);
        const rank = same.indexOf(plan);
        const both = plans.some(p => p.edge === plan.edge && p.dir !== plan.dir);
        const part = plan.dir * (8 * (same.length - 1 - rank) + (both ? ARC_PART : 0));
        const e = lines[plan.i].end!;
        let from: Point;
        let to: Point;
        let arc: (f: Point, t: Point) => Arc;
        if (plan.edge === 'over') {
            // Facing corners; a further arc the same way leaves farther in.
            const inset = ARC_HEAD_IN + 8 * (same.length - 1 - rank);
            from = [(plan.dir > 0 ? a.r.r : a.r.l) - plan.dir * inset, a.r.t];
            to = [(plan.dir > 0 ? e.r.l : e.r.r) + plan.dir * ARC_HEAD_IN, e.r.t];
            const crest = Math.max(Math.min(a.r.t, e.r.t) - ARC_MIN_BULGE - ARC_NEST * rank, crestTop);
            arc = (f, t) => overArc(f, t, crest);
        } else {
            const right = plan.edge === 'right';
            from = [right ? a.r.r : a.r.l, midY(a.r) + part];
            to = [right ? e.r.r : e.r.l, midY(e.r)];
            const bulge = Math.max(ARC_MIN_BULGE, plan.w / 2 + ARC_LABEL_CLEAR) + ARC_NEST * rank;
            const crest = right ? Math.max(a.r.r, e.r.r) + bulge : Math.min(a.r.l, e.r.l) - bulge;
            arc = (f, t) => sideArc(f, t, crest);
        }
        arcs[plan.i] = lines[plan.i].out ? arc(from, to) : arc(to, from);
    }
    return arcs;
}

/** Every arrow and label for the subject's lines. */
export function layoutArrows(scene: ArrowScene): ArrowDrawing {
    const { frame, nameR, anchor: a, lines, measure } = scene;
    const barY = barMidOf(frame);
    const under = a.kind === 'board' && a.under;
    const arrows: Arrow[] = [];
    const mids: { line: ArrowLine; text: string; x: number; y: number; w: number }[] = [];
    const bars: { line: ArrowLine; text: string; sp: Point[] | null }[] = [];
    let exitR = -1;
    // Close cards are joined by arcs; farther level column heads are met on
    // the sides that face each other.
    const kinds = lines.map(line => (line.end ? standing(a, line.end) : 'far'));
    const arcs = subjectArcs(scene, kinds);
    const level = lines.map((line, i) => {
        const end = line.end;
        return end && kinds[i] === 'far' && a.kind === 'board' && end.kind === 'board' && levelHeads(a, end)
            ? levelRoutes(a, end, i, frame) : null;
    });
    const slots = subjectSlots(scene, lines.map((_, i) => Boolean(level[i] || arcs[i])));
    // Half under the shelf, the subject's lines leave from the bar's edge;
    // links to cells out of view are only named there.
    lines.forEach((line, i) => {
        const text = lineLabel(line.op);
        const end = line.end;
        const arc = arcs[i];
        if (arc) {
            const { arrow, mid } = drawArc(arc, line.op === 'duplicate', line.hot);
            arrows.push(arrow);
            mids.push({ line, text, x: mid[0], y: mid[1], w: measure(text, null) });
            return;
        }
        const lv = level[i];
        const sp = lv ? lv[0] : route(a, slots[i], nameR, frame);
        if (!end) {
            bars.push({ line, text, sp: under ? null : sp });
            return;
        }
        if (under) exitR = Math.max(exitR, sp[0][0]);
        const bp = lv ? lv[1] : route(end, 0, nameR, frame);
        const dashed = line.op === 'duplicate';
        const { arrow, mid } = line.out ? drawArrow(sp, bp, dashed, line.hot) : drawArrow(bp, sp, dashed, line.hot);
        arrows.push(arrow);
        mids.push({ line, text, x: mid[0], y: mid[1], w: measure(text, null) });
    });
    const midLefts = stepMidLabels(mids, barY, nameR);
    const labels: ArrowLabel[] = mids.map((m, i) => ({
        cellId: m.line.cellId, text: m.text, name: null, title: `${m.text} · ${m.line.name}`,
        walkable: m.line.walkable, left: midLefts[i], top: m.y - LABEL_HALF,
    }));
    // Cells that cannot be seen are met on the Removed bar, their labels in a
    // row that stops short of the bar's controls.
    const widths = bars.map(bar => measure(bar.text, bar.line.name));
    const lefts = placeBarLabels(widths, { from: nameR + 16, stop: scene.controlsLeft - 18, anchorLeft: a.r.l, exitR });
    bars.forEach((bar, i) => {
        labels.push({
            cellId: bar.line.cellId, text: bar.text, name: bar.line.name, title: `${bar.text} · ${bar.line.name}`,
            walkable: bar.line.walkable, left: lefts[i], top: barY - LABEL_HALF,
        });
        if (!bar.sp) return;
        const bp: Point[] = [[lefts[i] + 14, a.kind === 'shelf' ? barY - 9 : barY + 9]];
        const dashed = bar.line.op === 'duplicate';
        arrows.push((bar.line.out ? drawArrow(bar.sp, bp, dashed, false) : drawArrow(bp, bar.sp, dashed, false)).arrow);
    });
    return { arrows, labels };
}
