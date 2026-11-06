// Fan-out translation module — pure functions, no React, no store imports.
//
// A fanned-out stack S is shown as N client-minted single-cell columns
// (`fan-<cellId>`) in place of S in the main row. The scene, layout, camera
// and drag code all work on that DISPLAY list; FocusContext and SceneActions
// only ever hold REAL stack ids and cell indices. Every conversion between
// the two spaces lives here:
//
//   * ids / building / resolution (getFanRange, buildDisplayStacks, …)
//   * sources and targets → CellOp (resolveCellSource, resolveCellLanding,
//     resolveCellTarget)
//   * drop hit-testing against the fan's extent (hitTestCellDrop), and the
//     zoom-out stack drop's merge/insert test (hitTestStackDrop)
//   * the keyboard stop list over display columns (cellMoveStops, …)
//   * frame geometry and the layout alias (fanFrameExtent, fanHeaderSpan, layoutAlias)
//   * the fan's width rules (fanWidthsFor)
//
// Every function is total: bad input yields null (or a harmless value),
// never a throw. A CellOp is always REAL-space and is executed by
// SceneActions.applyCellOp against move_cell_to, the only move endpoint,
// which names the moving cell by id.
//
// The Capabilities stack never fans out, so every fan column is standard.
// Its front slots (the manage card ahead of its cells) live in
// lib/frontSlots.ts.
import { isCapabilitiesStack, type Cell, type Stack } from '@/lib/types';
import { frontCellIndexOf } from '@/lib/frontSlots';
import type { PendingMoveState } from '@/stores/useSpatialStore';

export const mod = (x: number, n: number): number => ((x % n) + n) % n;
const clamp = (x: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, x));

// ── B1. Ids, building, resolution ───────────────────────────────────

/** How long a fan toggle takes to settle — the columns' layout springs
 *  and the outgoing stack's exit fade. Scene work that must not act on
 *  mid-flight geometry (the cell-diff zones, the renderer re-observe
 *  pass) waits this long after the fanned stack changes. */
export const FAN_TOGGLE_SETTLE_MS = 450;

export const FAN_COLUMN_PREFIX = 'fan-';
export const fanColumnId = (cellId: string): string => `${FAN_COLUMN_PREFIX}${cellId}`;

/** The display span of a fanned-out stack. */
export interface FanRange {
    /** Real fanned stack S. */
    stackId: string;
    /** S.name (header). */
    name: string;
    /** Index of S in mainStacks. */
    realIdx: number;
    /** Display index of column 0 (=== realIdx). */
    start: number;
    /** Display index of the LAST column (inclusive): start + count - 1. */
    end: number;
    /** n = S.cells.length, >= 2. */
    count: number;
}

/** null when id is null, the stack is absent, it is the Capabilities stack
 *  (which never fans out), or it has < 2 cells. */
export function getFanRange(mainStacks: Stack[], fannedOutStackId: string | null): FanRange | null {
    if (!fannedOutStackId) return null;
    const realIdx = mainStacks.findIndex(s => s.id === fannedOutStackId);
    if (realIdx < 0) return null;
    const s = mainStacks[realIdx];
    if (isCapabilitiesStack(s)) return null;
    const count = s.cells.length;
    if (count < 2) return null;
    return {
        stackId: s.id,
        name: s.name,
        realIdx,
        start: realIdx,
        end: realIdx + count - 1,
        count,
    };
}

/** fan null → mainStacks (same reference). Else mainStacks[0..r) ++ columns ++ mainStacks(r..].
 *  Column k wraps S.cells[k] as a synthetic single-cell stack. When `prev`
 *  holds a column with the same id wrapping the same cell object with the
 *  same fanIndex / fanCount / sourceStackId / name, that object
 *  is returned unchanged (keeps StackGroup's memo cheap across unrelated
 *  renders). */
export function buildDisplayStacks(mainStacks: Stack[], fan: FanRange | null, prev?: Stack[]): Stack[] {
    if (!fan) return mainStacks;
    const S = mainStacks[fan.realIdx];
    if (!S || S.id !== fan.stackId) return mainStacks;
    const prevById = new Map<string, Stack>();
    if (prev) for (const p of prev) if (p.synthetic) prevById.set(p.id, p);
    const n = S.cells.length;
    const columns: Stack[] = S.cells.map((c, k) => {
        const id = fanColumnId(String(c.id));
        const old = prevById.get(id);
        if (old && old.cells[0] === c && old.fanIndex === k && old.fanCount === n
            && old.sourceStackId === S.id && old.name === c.name) {
            return old;
        }
        return {
            id,
            name: c.name,
            cells: [c],
            synthetic: true,
            sourceCellId: String(c.id),
            sourceStackId: S.id,
            fanIndex: k,
            fanCount: n,
        };
    });
    return [...mainStacks.slice(0, fan.realIdx), ...columns, ...mainStacks.slice(fan.realIdx + 1)];
}

/** A memoising wrapper around buildDisplayStacks: remembers its last result
 *  so a column wrapping an unchanged cell keeps its object identity (and
 *  its cells array) across rebuilds. One instance per scene. */
export function createDisplayStacksBuilder(): (mainStacks: Stack[], fan: FanRange | null) => Stack[] {
    let prev: Stack[] = [];
    return (mainStacks, fan) => {
        const d = buildDisplayStacks(mainStacks, fan, prev);
        prev = d;
        return d;
    };
}

/** Display column → real stack index (+ the cell index for a fan column). */
export function displayToReal(fan: FanRange | null, displayIdx: number): { realIdx: number; fanCellIdx: number | null } {
    if (!fan || displayIdx < fan.start) return { realIdx: displayIdx, fanCellIdx: null };
    if (displayIdx <= fan.end) return { realIdx: fan.realIdx, fanCellIdx: displayIdx - fan.start };
    return { realIdx: displayIdx - fan.count + 1, fanCellIdx: null };
}

/** OUTSIDE gap slot (0..K) → real insert index for move_cell_to 'insert'.
 *  (start < slot <= end is never an outside gap; fan.realIdx is returned
 *  defensively.) */
export function realInsertIndex(fan: FanRange | null, slot: number): number {
    if (!fan || slot <= fan.start) return slot;
    if (slot >= fan.end + 1) return slot - fan.count + 1;
    return fan.realIdx;
}

/** Number of real stacks behind a display list of `displayLength` columns. */
export function realStackCount(displayLength: number, fan: FanRange | null): number {
    return displayLength - (fan ? fan.count - 1 : 0);
}

/** Any id the scene or header may hand back → real stack + fan cell index.
 *  A fan column resolves to its stack and cell; a real stack to itself; an
 *  id that is absent from the display but names the fanned stack (the
 *  frame header) to that stack with no cell. Unknown → null. */
export function resolveDisplayId(
    display: Stack[], fan: FanRange | null, id: string,
): { stackId: string; fanCellIdx: number | null } | null {
    const s = display.find(x => x.id === id);
    if (s) {
        if (s.synthetic) {
            if (s.sourceStackId === undefined) return null;
            return { stackId: s.sourceStackId, fanCellIdx: s.fanIndex ?? 0 };
        }
        return { stackId: s.id, fanCellIdx: null };
    }
    if (fan && id === fan.stackId) return { stackId: fan.stackId, fanCellIdx: null };
    return null;
}

/** The display-level selection: the front cell's column when the fanned
 *  stack is selected, else the real selection unchanged. */
export function displaySelectedIdFor(
    mainStacks: Stack[], fan: FanRange | null, selectedWindowId: string | null, vti: Record<string, number>,
): string | null {
    if (!fan || selectedWindowId !== fan.stackId) return selectedWindowId;
    const S = mainStacks[fan.realIdx];
    if (!S || S.id !== fan.stackId || S.cells.length === 0) return selectedWindowId;
    const front = S.cells[mod(vti[S.id] ?? 0, S.cells.length)];
    return fanColumnId(String(front.id));
}

/** For the real stack `stackId` (fanned or not): its id and the id of its
 *  front cell's fan column. null if the stack is missing/empty or stackId
 *  is null. */
export function fanAnchorFor(
    mainStacks: Stack[], stackId: string | null, vti: Record<string, number>,
): { realId: string; columnId: string } | null {
    if (!stackId) return null;
    const S = mainStacks.find(s => s.id === stackId);
    if (!S || S.cells.length === 0) return null;
    const front = S.cells[mod(vti[S.id] ?? 0, S.cells.length)];
    return { realId: S.id, columnId: fanColumnId(String(front.id)) };
}

/** Top index after removing cell `removed` from a stack of nBefore cells
 *  whose top was `top`: the same cell stays on top when it survives; the
 *  right neighbour takes over when the top cell itself went, or the left
 *  neighbour when it was the last cell. Never below 0. */
export function adjustTopIndexAfterRemove(top: number, removed: number, nBefore: number): number {
    if (nBefore <= 1) return 0;
    const t = mod(top, nBefore);
    let next: number;
    if (removed < t) next = t - 1;
    else if (removed === t) next = Math.min(t, nBefore - 2);
    else next = t;
    return Math.max(0, next);
}

/** The stack in channel.main_stacks holding cellId, and the cell's index there. */
export function landingOf(channel: unknown, cellId: string | undefined): { stackId: string; cellIdx: number } | null {
    if (!cellId || !channel || typeof channel !== 'object') return null;
    const stacks = (channel as { main_stacks?: unknown }).main_stacks;
    if (!Array.isArray(stacks)) return null;
    for (const s of stacks as Array<Partial<Stack>>) {
        if (!s || typeof s.id !== 'string' || !Array.isArray(s.cells)) continue;
        const cellIdx = s.cells.findIndex(c => c && String(c.id) === cellId);
        if (cellIdx >= 0) return { stackId: s.id, cellIdx };
    }
    return null;
}

// ── B2. Sources, targets, ops ───────────────────────────────────────

export interface CellSource {
    displayId: string;
    displayIdx: number;
    /** REAL stack id. */
    stackId: string;
    realIdx: number;
    /** REAL cell index. */
    cellIdx: number;
    cellId: string;
    /** REAL cell count. */
    cellCount: number;
    /** The cell sits on the Capabilities stack. */
    capabilities: boolean;
    /** A run is live (or starting) on the cell: it may not move onto
     *  Capabilities, where nothing runs. */
    live: boolean;
    isFanColumn: boolean;
    /** A built-in card: it never moves. */
    builtin: boolean;
}

/** Why a landing is refused when the user can act on it. */
export type LandingRefusal = 'running';

/** The hint a refused 'running' landing shows. */
export const MOVE_INTO_CAPABILITIES_RUNNING = 'Stop its run before moving it onto Capabilities.';

/** How many built-in cards head `cells` (the Capabilities stack's). No cell
 *  lands before them. */
export function builtinHead(cells: readonly Cell[]): number {
    let n = 0;
    while (n < cells.length && cells[n].builtin) n++;
    return n;
}

/** The moving cell behind a display column: a fan column is its wrapped
 *  cell of the real stack; a real stack contributes the cell at the REAL
 *  index cellIdx (clamped). `isLive` reports a live run on a cell id (the
 *  store's cellHasLiveRun), so the rules here stay pure. null if the id
 *  isn't in display or the stack has no cells. */
export function resolveCellSource(
    display: Stack[], fan: FanRange | null, displayId: string, cellIdx: number,
    isLive: (cellId: string) => boolean,
): CellSource | null {
    const displayIdx = display.findIndex(s => s.id === displayId);
    if (displayIdx < 0) return null;
    const d = display[displayIdx];
    if (d.cells.length === 0) return null;
    if (d.synthetic) {
        if (!fan || d.sourceStackId === undefined) return null;
        const cellId = d.sourceCellId ?? String(d.cells[0].id);
        return {
            displayId,
            displayIdx,
            stackId: d.sourceStackId,
            realIdx: fan.realIdx,
            cellIdx: d.fanIndex ?? 0,
            cellId,
            cellCount: d.fanCount ?? fan.count,
            capabilities: false,
            live: isLive(cellId),
            isFanColumn: true,
            builtin: !!d.cells[0].builtin,
        };
    }
    const n = d.cells.length;
    const at = clamp(cellIdx, 0, n - 1);
    const cellId = String(d.cells[at].id);
    return {
        displayId,
        displayIdx,
        stackId: d.id,
        realIdx: displayToReal(fan, displayIdx).realIdx,
        cellIdx: at,
        cellId,
        cellCount: n,
        capabilities: isCapabilitiesStack(d),
        live: isLive(cellId),
        isFanColumn: false,
        builtin: !!d.cells[at].builtin,
    };
}

export type DisplayTarget =
    /** slot ∈ 0..K; a new column opens at this display slot. inFan: the gap
     *  lies inside the fan (a position in the fanned stack). */
    | { kind: 'gap'; slot: number; inFan: boolean }
    /** col is never a fan column; intra = the cell's final array index. */
    | { kind: 'onto'; col: number; intra: number };

/** A REAL-space move. The backend finds the cell by `cellId`, pops it,
 *  removes an emptied standard source stack (never Capabilities) and
 *  decrements a target stack index that was past it, so every index here
 *  is PRE-pop and needs no frontend adjustment. */
export type CellOp =
    | { kind: 'reorder'; stackId: string; realIdx: number; from: number; to: number; cellId: string }
    | { kind: 'intoStack'; srcStackId: string; srcCellIdx: number; targetRealIdx: number; tgtCellIdx: number; cellId: string }
    | { kind: 'newStack'; srcStackId: string; srcCellIdx: number; insertRealIdx: number; cellId: string };

/** Where a display target leaves `src`: a real move; its own place, where
 *  it stays (a drag previews it there and a drop sends nothing); or a
 *  target it may not land on. */
export type CellLanding =
    | { kind: 'move'; op: CellOp }
    | { kind: 'stays' }
    | { kind: 'refused'; reason?: LandingRefusal };

const STAYS: CellLanding = { kind: 'stays' };
const REFUSED: CellLanding = { kind: 'refused' };
const REFUSED_RUNNING: CellLanding = { kind: 'refused', reason: 'running' };
const moving = (op: CellOp): CellLanding => ({ kind: 'move', op });

/** Resolve a display target for `src` into its landing (CellLanding). A
 *  cell moves between any two stacks: it never lands before built-in cards,
 *  and it lands on Capabilities only while nothing runs on it. */
export function resolveCellLanding(
    display: Stack[], fan: FanRange | null, src: CellSource, target: DisplayTarget,
): CellLanding {
    // 0. A built-in card never moves (the backend refuses too).
    if (src.builtin) return REFUSED;

    if (target.kind === 'gap') {
        if (target.inFan) {
            // 1. A position p ∈ 0..n inside the fanned stack, never before
            //    its built-in cards.
            if (!fan || target.slot < fan.start || target.slot > fan.end + 1) return REFUSED;
            const p = target.slot - fan.start;
            if (p < builtinHead(display.slice(fan.start, fan.end + 1).map(column => column.cells[0]))) return REFUSED;
            if (src.stackId === fan.stackId) {
                const j = src.cellIdx;
                // The cell would stay put on either side of itself.
                if (p === j || p === j + 1) return STAYS;
                // Pop-first arithmetic: after the pop every index > j shifts
                // down by one, so "before the cell now at p" is p - 1 when
                // p > j.
                const to = p > j ? p - 1 : p;
                return moving({ kind: 'reorder', stackId: fan.stackId, realIdx: fan.realIdx, from: j, to, cellId: src.cellId });
            }
            return moving({
                kind: 'intoStack',
                srcStackId: src.stackId,
                srcCellIdx: src.cellIdx,
                targetRealIdx: fan.realIdx,
                tgtCellIdx: p,
                cellId: src.cellId,
            });
        }
        // 2. An outside gap: a new standard stack at the real insert index.
        const ins = realInsertIndex(fan, target.slot);
        // A single-cell standard stack dropped beside itself would be
        // re-created in place (Capabilities keeps its stack and its cards).
        if (!src.isFanColumn && !src.capabilities && src.cellCount === 1
            && (ins === src.realIdx || ins === src.realIdx + 1)) return STAYS;
        return moving({ kind: 'newStack', srcStackId: src.stackId, srcCellIdx: src.cellIdx, insertRealIdx: ins, cellId: src.cellId });
    }

    // 3. Onto a real column, never before its built-in cards, and onto
    //    Capabilities only while nothing runs on the cell.
    const t = display[target.col];
    if (!t || t.synthetic) return REFUSED;
    if (isCapabilitiesStack(t) && !src.capabilities && src.live) return REFUSED_RUNNING;
    if (target.intra < builtinHead(t.cells)) return REFUSED;
    const tr = displayToReal(fan, target.col).realIdx;
    if (t.id === src.stackId) {
        // A single-cell stack dropped on its own column: the backend pops
        // the cell, empties and removes the stack, and the target index
        // then names the right neighbour — never send it.
        if (src.cellCount === 1) return STAYS;
        const to = clamp(target.intra, 0, src.cellCount - 1);
        if (to === src.cellIdx) return STAYS;
        return moving({ kind: 'reorder', stackId: src.stackId, realIdx: tr, from: src.cellIdx, to, cellId: src.cellId });
    }
    return moving({
        kind: 'intoStack',
        srcStackId: src.stackId,
        srcCellIdx: src.cellIdx,
        targetRealIdx: tr,
        tgtCellIdx: clamp(target.intra, 0, t.cells.length),
        cellId: src.cellId,
    });
}

/** The real move a display target means for `src` (resolveCellLanding's
 *  op), or null when the cell stays where it is or may not land there. */
export function resolveCellTarget(
    display: Stack[], fan: FanRange | null, src: CellSource, target: DisplayTarget,
): CellOp | null {
    const landing = resolveCellLanding(display, fan, src, target);
    return landing.kind === 'move' ? landing.op : null;
}

// ── B3. Drop hit-testing (main view only; zoom-out never has a fan) ─

export type DropHit =
    | { kind: 'gap'; slot: number; inFan: boolean }
    | { kind: 'onto'; col: number };

/** Where a dragged cell at `worldX` would land, over the UNSHIFTED layout
 *  positions/widths (centres and widths per display column). Inside the fan
 *  every position is a gap of the fanned stack: the left half of a column
 *  means before it, the right half after it. The fan's extent (`inset`
 *  outside the outer columns' edges, where its bracket ends) is the
 *  boundary; outside it
 *  the main view's rules apply — `isThumb` adds the outer-25% insert fringe
 *  on each column. */
export function hitTestCellDrop(a: {
    worldX: number;
    positions: number[];
    widths: number[];
    fan: FanRange | null;
    isThumb: boolean;
    inset: number;
}): DropHit {
    const { worldX, positions, widths, fan, isThumb, inset } = a;
    const N = positions.length;
    if (N === 0) return { kind: 'gap', slot: 0, inFan: false };

    if (fan) {
        let fl = Infinity;
        let fr = -Infinity;
        for (let j = 0; j < fan.count; j++) {
            const i = fan.start + j;
            if (i >= N || !(widths[i] > 0)) continue;
            fl = Math.min(fl, positions[i] - widths[i] / 2);
            fr = Math.max(fr, positions[i] + widths[i] / 2);
        }
        if (Number.isFinite(fl) && Number.isFinite(fr)) {
            fl -= inset;
            fr += inset;
            if (fl <= worldX && worldX <= fr) {
                let p = 0;
                for (let j = 0; j < fan.count; j++) {
                    const i = fan.start + j;
                    if (i >= N || !(widths[i] > 0)) continue;
                    if (positions[i] < worldX) p += 1;
                }
                return { kind: 'gap', slot: fan.start + p, inFan: true };
            }
        }
    }

    const firstLeft = positions[0] - widths[0] / 2;
    const lastRight = positions[N - 1] + widths[N - 1] / 2;
    if (worldX < firstLeft) return { kind: 'gap', slot: 0, inFan: false };
    if (worldX > lastRight) return { kind: 'gap', slot: N, inFan: false };
    for (let i = 0; i < N; i++) {
        const left = positions[i] - widths[i] / 2;
        const right = positions[i] + widths[i] / 2;
        if (worldX >= left && worldX <= right) {
            if (isThumb) {
                const fringe = widths[i] * 0.25;
                if (worldX < left + fringe) return { kind: 'gap', slot: i, inFan: false };
                if (worldX > right - fringe) return { kind: 'gap', slot: i + 1, inFan: false };
            }
            return { kind: 'onto', col: i };
        }
        if (i < N - 1) {
            const nextLeft = positions[i + 1] - widths[i + 1] / 2;
            if (worldX > right && worldX < nextLeft) return { kind: 'gap', slot: i + 1, inFan: false };
        }
    }
    // Unreachable for finite input (every point between the outer edges is
    // in a column or strictly between two); land past the last column.
    return { kind: 'gap', slot: N, inFan: false };
}

export type StackDropHit = { kind: 'merge'; idx: number } | { kind: 'insert'; idx: number };

/** Where a dragged STACK at `worldX` would land. Zoom-out only: hovering
 *  inside another column's X bounds merges into it, unless either side is
 *  the Capabilities stack (it never merges). Everything else is a
 *  slot-between reorder: the count of leading columns whose centre lies left
 *  of the pointer. The source column is skipped so hovering over its own slot
 *  never self-merges. Shared by the drag's per-frame highlight and its drop
 *  so the two can't disagree. */
export function hitTestStackDrop(a: {
    worldX: number;
    positions: number[];
    widths: number[];
    stacks: Stack[];
    srcStackId: string;
    isZoomedOut: boolean;
}): StackDropHit {
    const { worldX, positions, widths, stacks, srcStackId, isZoomedOut } = a;
    if (isZoomedOut && !isCapabilitiesStack(stacks.find(s => s.id === srcStackId))) {
        for (let i = 0; i < positions.length; i++) {
            if (stacks[i]?.id === srcStackId) continue;
            const left = positions[i] - widths[i] / 2;
            const right = positions[i] + widths[i] / 2;
            if (worldX >= left && worldX <= right && !isCapabilitiesStack(stacks[i])) {
                return { kind: 'merge', idx: i };
            }
        }
    }
    let insertIdx = 0;
    for (let i = 0; i < positions.length; i++) {
        if (worldX > positions[i]) insertIdx = i + 1;
        else break;
    }
    return { kind: 'insert', idx: insertIdx };
}

// ── B4. Keyboard stop list (cell moves over DISPLAY columns) ────────

/** One keyboard stop. targetIndex keeps the parity encoding (even = insert
 *  at display slot targetIndex/2, odd = onto display column
 *  (targetIndex-1)/2); inFan distinguishes the inside/outside twins at the
 *  fan's two boundary slots; order sorts the stops left to right. */
export interface CellMoveStop {
    targetIndex: number;
    mode: 'insert' | 'stack';
    inFan: boolean;
    order: number;
}

/** Every keyboard-reachable stop for `src`, left to right, with the no-ops
 *  already removed: each stop resolves to a move, or to Capabilities
 *  refusing a live cell (kept, so committing there shows why: 'Stop its run
 *  before moving it onto Capabilities'). Interior
 *  non-fan gaps stay unreachable, as today; the two outermost gaps, every
 *  position inside the fan, and every real column except the source are
 *  offered. */
export function cellMoveStops(
    display: Stack[], fan: FanRange | null, src: CellSource, vti: Record<string, number>,
): CellMoveStop[] {
    const K = display.length;
    const raw: CellMoveStop[] = [];
    const outside = (g: number): CellMoveStop => ({ targetIndex: 2 * g, mode: 'insert', inFan: false, order: 2 * g });
    const inside = (g: number, order: number): CellMoveStop => ({ targetIndex: 2 * g, mode: 'insert', inFan: true, order });
    for (let g = 0; g <= K; g++) {
        if (fan && g > fan.start && g <= fan.end) {
            raw.push(inside(g, 2 * g));
        } else if (fan && g === fan.start) {
            raw.push(outside(g));
            raw.push(inside(g, 2 * g + 0.25));
        } else if (fan && g === fan.end + 1) {
            raw.push(inside(g, 2 * g - 0.25));
            raw.push(outside(g));
        } else if (g === 0 || g === K) {
            raw.push(outside(g));
        }
        if (g < K) {
            const d = g;
            if (!display[d].synthetic && d !== src.displayIdx) {
                raw.push({ targetIndex: 2 * d + 1, mode: 'stack', inFan: false, order: 2 * d + 1 });
            }
        }
    }
    return raw.filter(stop => {
        const landing = resolveCellLanding(display, fan, src, pendingMoveTarget(stop, display, vti));
        return landing.kind === 'move' || (landing.kind === 'refused' && landing.reason === 'running');
    });
}

/** The first stop past the source column in direction `dir`, or null. */
export function firstCellMoveStop(stops: CellMoveStop[], srcDisplayIdx: number, dir: -1 | 1): CellMoveStop | null {
    const anchor = 2 * srcDisplayIdx + 1;
    if (dir > 0) return stops.find(s => s.order > anchor) ?? null;
    let last: CellMoveStop | null = null;
    for (const s of stops) if (s.order < anchor) last = s;
    return last;
}

/** The stop after `cur` in direction `dir`, clamped at both ends. When cur
 *  isn't in the list the walk starts from its first (dir +1) or last (dir
 *  −1) stop. null only for an empty list. */
export function stepCellMoveStop(
    stops: CellMoveStop[], cur: { targetIndex: number; inFan?: boolean }, dir: -1 | 1,
): CellMoveStop | null {
    if (stops.length === 0) return null;
    const i = stops.findIndex(s => s.targetIndex === cur.targetIndex && s.inFan === !!cur.inFan);
    if (i < 0) return dir > 0 ? stops[0] : stops[stops.length - 1];
    return stops[clamp(i + dir, 0, stops.length - 1)];
}

/** The display target a pending keyboard move (or a keyboard stop from
 *  cellMoveStops) points at. An onto target's intra index is the target
 *  stack's front cell, never before Capabilities' built-in cards: while the
 *  manage card or a built-in card is in front, the move lands right after
 *  the cards (the drag path's ontoFrontIndex does the same). */
export function pendingMoveTarget(move: Pick<PendingMoveState, 'targetIndex' | 'mode' | 'inFan'>, display: Stack[], vti: Record<string, number>): DisplayTarget {
    if (move.mode === 'insert') return { kind: 'gap', slot: move.targetIndex / 2, inFan: !!move.inFan };
    const col = (move.targetIndex - 1) / 2;
    const t = display[col];
    return { kind: 'onto', col, intra: Math.max(frontCellIndexOf(t, vti) ?? 0, builtinHead(t?.cells ?? [])) };
}

// ── B7. Frame geometry and layout alias ─────────────────────────────

/** Horizontal extent of the fan frame from the scene's items (they carry
 *  the insert-drag shift). Visible fan items are the synthetic ones with a
 *  positive width; an in-fan gap widens the frame to include the dashed
 *  slot. null when no fan column is visible. */
export function fanFrameExtent(
    items: Array<{ x: number; computedWidth: number; synthetic?: boolean }>,
    gap: { x: number; width: number; inFan: boolean } | null,
    inset: number,
): { left: number; right: number } | null {
    let left = Infinity;
    let right = -Infinity;
    for (const it of items) {
        if (!it.synthetic || !(it.computedWidth > 0)) continue;
        left = Math.min(left, it.x - it.computedWidth / 2);
        right = Math.max(right, it.x + it.computedWidth / 2);
    }
    if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
    if (gap && gap.inFan) {
        left = Math.min(left, gap.x);
        right = Math.max(right, gap.x + gap.width);
    }
    return { left: left - inset, right: right + inset };
}

/** The scene camera as the fan header's clamp needs it: world x at the
 *  viewport centre, zoom, the scene root's width, and the screen x where
 *  the visible scene begins (the layout's sidebar inset). */
export interface FanHeaderView {
    cameraX: number;
    zoom: number;
    width: number;
}

/** Where the fan header's content (name, count … Collapse) sits inside its
 *  band so it stays on screen: `offset` from the band's left edge and
 *  `width`, both world px. The content spans the band's visible part —
 *  its left end at max(band left, viewport left), its right end at
 *  min(band right, viewport right) — so a frame wider than the viewport,
 *  or partly off-screen, keeps its name and Collapse button in view. A
 *  frame that fits on screen gets { 0, bandWidth }: the whole band.
 *
 *  World → screen is the scene's: a panX = width/2 − cameraX container
 *  inside a scale(zoom) wrapper centred on the viewport, so
 *  screen = width/2 + zoom·(world − cameraX) and conversely
 *  world = cameraX + (screen − width/2) / zoom.
 *
 *  The content never overhangs the band: when less than `minWidth` of it
 *  is visible (a sliver of the frame on screen) the content keeps
 *  `minWidth` and stays inside the band, flush with the visible end. No
 *  view (width 0, e.g. before the scene is measured) → the whole band. */
export function fanHeaderSpan(
    bandLeft: number, bandWidth: number, view: FanHeaderView | null, minWidth: number,
): { offset: number; width: number } {
    const w = Math.max(0, bandWidth);
    if (!view || !(view.width > 0)) return { offset: 0, width: w };
    const z = view.zoom > 0 ? view.zoom : 1;
    const visLeft = clamp(view.cameraX + (-view.width / 2) / z - bandLeft, 0, w);
    const visRight = clamp(view.cameraX + (view.width / 2) / z - bandLeft, 0, w);
    const width = Math.min(w, Math.max(visRight - visLeft, minWidth));
    return { offset: clamp(visLeft, 0, w - width), width };
}

/** Identity carry-over between a real stack and its front fan column across
 *  a toggle: the one that appears should land where the other was. */
export function layoutAlias(
    prevIds: string[], currentIds: string[], anchor: { realId: string; columnId: string } | null,
): { prevIdx: number; newIdx: number } | null {
    if (!anchor) return null;
    const prevReal = prevIds.indexOf(anchor.realId);
    const prevCol = prevIds.indexOf(anchor.columnId);
    const curReal = currentIds.indexOf(anchor.realId);
    const curCol = currentIds.indexOf(anchor.columnId);
    // Fan-out: the real stack was there, its front column is now.
    if (prevReal >= 0 && curCol >= 0 && prevCol < 0) return { prevIdx: prevReal, newIdx: curCol };
    // Collapse: the front column was there, the real stack is now.
    if (prevCol >= 0 && curReal >= 0 && prevReal < 0) return { prevIdx: prevCol, newIdx: curReal };
    return null;
}

// ── Width rules ─────────────────────────────────────────────────────

/** A fanned stack as the width rules see it: its id and the cell that
 *  becomes its front when the fan closes. */
export interface FanCollapse {
    stackId: string;
    frontCellId: string;
}

/** The FanCollapse of `stack` with `frontIdx` as its front (wrapped like
 *  visibleTopIndices). Null for an empty stack. */
export function fanCollapseOf(stack: Stack, frontIdx: number): FanCollapse | null {
    if (stack.cells.length === 0) return null;
    return { stackId: stack.id, frontCellId: String(stack.cells[mod(frontIdx, stack.cells.length)].id) };
}

/** The per-column width overrides (useSpatialStore.stackWidths, keyed by
 *  display id) after the fanned stack changes. `collapsing` is the fan
 *  that just closed (any path: the frame's Collapse, Esc, Alt+V, zoom-out,
 *  the stack dropping below 2 cells, another stack fanning out, a channel
 *  switch) — null when no fan closed. `fanning` is the stack whose fan
 *  just opened, or null. The persisted map is this with the open fan
 *  collapsed, so a reload finds the widths a collapse would leave.
 *
 *  - Every `fan-` override is dropped: fan columns only ever carry widths
 *    set during their own fan, so a stale one from an earlier fan never
 *    leaks into the next.
 *  - Collapse: the stack takes its front column's width — that column's
 *    override, or none (the shared default) when it had none.
 *  - Fan-out: every column starts at the stack's width — its override
 *    copied onto each `fan-<cellId>`, or nothing when the stack itself
 *    follows the shared default.
 *
 *  Returns `widths` itself when nothing changes. */
export function fanWidthsFor(
    widths: Record<string, number>,
    collapsing: FanCollapse | null,
    fanning: Stack | null,
): Record<string, number> {
    const next: Record<string, number> = {};
    for (const [id, w] of Object.entries(widths)) {
        if (!id.startsWith(FAN_COLUMN_PREFIX)) next[id] = w;
    }
    if (collapsing) {
        const w = widths[fanColumnId(collapsing.frontCellId)];
        if (w === undefined) delete next[collapsing.stackId];
        else next[collapsing.stackId] = w;
    }
    if (fanning) {
        const w = next[fanning.id];
        if (w !== undefined) {
            for (const cell of fanning.cells) next[fanColumnId(String(cell.id))] = w;
        }
    }
    const keys = Object.keys(next);
    const same = keys.length === Object.keys(widths).length && keys.every(k => widths[k] === next[k]);
    return same ? widths : next;
}
