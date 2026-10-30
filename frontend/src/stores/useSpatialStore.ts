import { create } from 'zustand';
import { fanWidthsFor, type FanCollapse } from '@/lib/fanOut';

export const SETTLE_MOVE_MS = 600;
const settlingMoves = new Set<number>();
const settleCallbacks: Array<() => void> = [];

/** Keep the move above its neighbours through request and settle. A newer
 * move owns the token, so an older timer cannot clear its paint window. */
export async function withSettlingMove<T>(
    sourceId: string,
    move: () => Promise<T>,
    onSettled?: () => void,
): Promise<T> {
    const token = useSpatialStore.getState().beginSettlingMove(sourceId);
    settlingMoves.add(token);
    if (onSettled) settleCallbacks.push(onSettled);
    try {
        return await move();
    } finally {
        setTimeout(() => {
            useSpatialStore.getState().clearSettlingMove(token);
            settlingMoves.delete(token);
            // A prior keyboard move must not release the camera while another
            // keyboard or arrow move still owns a request/settle window.
            if (settlingMoves.size === 0) {
                const callbacks = settleCallbacks.splice(0);
                for (const callback of callbacks) callback();
            }
        }, SETTLE_MOVE_MS);
    }
}

export interface PendingMoveState {
    type: 'cell';
    sourceId: string;
    originalIndex: number;
    targetIndex: number;
    mode: 'insert' | 'stack';
    /** Cell moves only: the gap at display slot targetIndex/2 lies INSIDE the
     *  fanned stack's border (a position in that stack) rather than outside it
     *  (a new stack). Absent/false for every other target. */
    inFan?: boolean;
}

/** An in-flight width drag: the column (display id) and its anchored edge. */
export interface WidthDrag {
    id: string;
    anchor: 'left' | 'right';
}

interface SpatialState {
    pendingMove: PendingMoveState | null;
    cameraHold: boolean;
    centerRequested: number;
    /** Edge-align request for the focused stack (Alt+[ / Alt+]) — the
     *  left/right siblings of ``centerRequested``. Nonce-bumped so a
     *  repeat of the same mode still fires; consumed by useStackCamera.
     *  Not persisted. */
    edgeAlignRequested: { mode: 'left' | 'right'; nonce: number } | null;
    /** Shared/default width for every column in the main scene (fan
     *  columns included; the overview's columns use a fixed native width). A
     *  column with no entry in ``stackWidths`` uses it. */
    minCellSize: number;
    /** Per-column width overrides, keyed by display id: a real stack id,
     *  or ``fan-<cellId>`` for a fan column. Written by the width control
     *  while a channel's "all stacks" toggle is off (flipping it on and
     *  adjusting the width clears this map), by the card-side resize
     *  strips (always a per-column override), and by the fan-out width
     *  rules (lib/fanOut.ts fanWidthsFor). A column with no entry falls
     *  back to ``minCellSize``. */
    stackWidths: Record<string, number>;
    /** The open fan as the width rules see it (lib/fanOut FanCollapse:
     *  the fanned stack and the cell that becomes its front on collapse),
     *  kept current by useSceneActions; null with no fan. localStorage
     *  always holds ``stackWidths`` with this fan collapsed, so a reload —
     *  which never restores a fan — finds the widths a collapse would
     *  leave. Not persisted. */
    fanCollapse: FanCollapse | null;
    /** Per-channel "all stacks" toggle (keyed by channel id). When on
     *  (the default), a width change sets the shared ``minCellSize``
     *  and clears every per-stack override; when off, the change
     *  writes only the target stack's entry in ``stackWidths``. */
    allStacksByChannel: Record<string, boolean>;
    /** Columns maximised on their own (lib/stackWidth toggleMaxWidth with
     *  "all stacks" off), keyed by display id: the per-stack counterpart of
     *  ``isMaxWidth``. The tracker (useMaxWidthTracker) keeps each at the
     *  full width as the viewport and the Commands pane change. Any other
     *  write of that column's width drops its entry, and clearing the
     *  overrides drops them all. Not persisted. */
    maxColumns: Record<string, true>;
    /** Explicit "max width is on" flag. When true, the StackApp resize
     *  watcher keeps ``minCellSize`` glued to ``screenMax`` so the
     *  layout follows the viewport without re-clicking the toggle.
     *  Detection-by-comparison (``minCellSize >= screenMax``) lost
     *  intent: a window enlarge would silently flip the indicator off
     *  even though the user wanted to stay maxed. Only the shared width
     *  (all-stacks max); a column's own max is ``maxColumns``. */
    isMaxWidth: boolean;
    /** Column whose width is mid-drag (null when idle) and the edge that
     *  stays put: `'left'` for the header width handle and the right-edge
     *  resize strip (the right edge follows the pointer), `'right'` for the
     *  left-edge strip. While set, useStackLayout pins that column's
     *  anchored edge on width changes — instead of the selected stack's
     *  center — so the dragged edge tracks the pointer 1:1, and
     *  useStackCamera parks the camera so nothing pans the edge out from
     *  under it. The header handle keeps it set through its release commit
     *  (the all-stacks width write) and clears it a frame later, so the
     *  commit reflow happens around the same locked edge. Keyed by display
     *  id (fan columns included). Not persisted. */
    widthDrag: WidthDrag | null;
    /** Source stack ID of the most recently committed move (keyboard,
     *  Alt-arrow, or arrow-button), kept set during the post-commit settle
     *  animation. Used by StackScene to bump that stack's content and
     *  renderer iframes above the neighbours it passes over. `pendingMove`
     *  is cleared *before* commit (to prevent double-commit on simultaneous
     *  key releases), so it can't carry this signal across the settle. */
    settlingMoveSourceId: string | null;
    /** Measured header bottom of each main-view card (px from the card's
     *  outer top), keyed by CELL id — the band of the scene's cell-diff
     *  hover zones and the top of the card-side resize strips (lib/cellDiff,
     *  lib/edgeResize). Written by StackItem from a ResizeObserver, only when
     *  the value actually changes; StackScene prunes it to the canvas's
     *  cells. Not persisted. */
    cellChrome: Record<string, number>;
    reportCellChrome: (cellId: string, headerBottomOffsetPx: number) => void;
    /** Keep only the entries of `cellIds` (the cells on the canvas). */
    pruneCellChrome: (cellIds: ReadonlySet<string>) => void;
    setPendingMove: (value: PendingMoveState | null | ((prev: PendingMoveState | null) => PendingMoveState | null)) => void;
    setCameraHold: (value: boolean) => void;
    /** Start a settle window for a moved stack. Returns a token to pass to
     *  ``clearSettlingMove`` — the clear is a no-op unless the token is the
     *  most recent one, so an earlier move's delayed clear can't wipe the
     *  bump of a move that started after it. */
    beginSettlingMove: (id: string) => number;
    clearSettlingMove: (token: number) => void;
    setCenterRequested: (value: number | ((prev: number) => number)) => void;
    /** Ask the camera to align the focused stack's left/right edge to the
     *  matching screen edge (same GAP inset the layout rests stacks at). */
    requestEdgeAlign: (mode: 'left' | 'right') => void;
    setMinCellSize: (value: number | ((prev: number) => number)) => void;
    /** Set a single stack's width override (channel "all stacks" off).
     *  A changed width drops the column's ``maxColumns`` flag. */
    setStackWidth: (stackId: string, value: number) => void;
    /** Mark (or unmark) a column as maximised on its own. */
    setColumnMax: (stackId: string, value: boolean) => void;
    /** Drop every per-stack override (channel "all stacks" on + adjust),
     *  and every column's max flag with them. */
    clearStackWidths: () => void;
    /** Replace the whole override map (the fan-out width rules). A no-op
     *  when handed the current map. */
    replaceStackWidths: (next: Record<string, number>) => void;
    setFanCollapse: (value: FanCollapse | null) => void;
    setAllStacks: (channelId: string, value: boolean) => void;
    setIsMaxWidth: (value: boolean) => void;
    setWidthDrag: (value: WidthDrag | null) => void;
}

// ── localStorage persistence ────────────────────────────────────────
// Cell widths are user preferences that should survive reload. Keep
// each key scoped so a corrupted/missing value falls back to the
// default cleanly.
const LS_MIN_CELL_SIZE_KEY = 'collimate.minCellSize';
const LS_IS_MAX_WIDTH_KEY = 'collimate.isMaxWidth';
const LS_STACK_WIDTHS_KEY = 'collimate.stackWidths';
const LS_ALL_STACKS_KEY = 'collimate.allStacksByChannel';
export const DEFAULT_MIN_CELL_SIZE = 550;

const loadNumber = (key: string, fallback: number): number => {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return fallback;
        const n = Number(raw);
        return Number.isFinite(n) && n > 0 ? n : fallback;
    } catch { return fallback; }
};
const saveNumber = (key: string, value: number): void => {
    try { localStorage.setItem(key, String(value)); } catch { /* quota / SSR */ }
};
const loadBool = (key: string, fallback: boolean): boolean => {
    try {
        const raw = localStorage.getItem(key);
        if (raw === null) return fallback;
        return raw === '1' || raw === 'true';
    } catch { return fallback; }
};
const saveBool = (key: string, value: boolean): void => {
    try { localStorage.setItem(key, value ? '1' : '0'); } catch { /* quota / SSR */ }
};
// Per-stack width map: keep only finite positive numbers so a corrupt
// or partially-written value can't poison the layout.
const loadWidthMap = (key: string): Record<string, number> => {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
        const out: Record<string, number> = {};
        for (const [k, v] of Object.entries(parsed)) {
            const n = Number(v);
            if (Number.isFinite(n) && n > 0) out[k] = n;
        }
        return out;
    } catch { return {}; }
};
const loadBoolMap = (key: string): Record<string, boolean> => {
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return {};
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
        const out: Record<string, boolean> = {};
        for (const [k, v] of Object.entries(parsed)) out[k] = !!v;
        return out;
    } catch { return {}; }
};
const saveJSON = (key: string, value: unknown): void => {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* quota / SSR */ }
};
// The width map as a reload should find it: the open fan collapsed and
// no `fan-` override left over (see ``fanCollapse``).
const saveStackWidths = (widths: Record<string, number>, fan: FanCollapse | null): void => {
    saveJSON(LS_STACK_WIDTHS_KEY, fanWidthsFor(widths, fan, null));
};

// Monotonic token for settling-move windows (see beginSettlingMove).
let settlingMoveToken = 0;

export const useSpatialStore = create<SpatialState>()((set) => ({
    pendingMove: null,
    cameraHold: false,
    centerRequested: 0,
    edgeAlignRequested: null,
    minCellSize: loadNumber(LS_MIN_CELL_SIZE_KEY, DEFAULT_MIN_CELL_SIZE),
    stackWidths: fanWidthsFor(loadWidthMap(LS_STACK_WIDTHS_KEY), null, null),
    fanCollapse: null,
    allStacksByChannel: loadBoolMap(LS_ALL_STACKS_KEY),
    maxColumns: {},
    isMaxWidth: loadBool(LS_IS_MAX_WIDTH_KEY, false),
    widthDrag: null,
    settlingMoveSourceId: null,
    cellChrome: {},

    setPendingMove: (value) => set((state) => ({
        pendingMove: typeof value === 'function' ? value(state.pendingMove) : value,
    })),
    setCameraHold: (value) => set({ cameraHold: value }),
    beginSettlingMove: (id) => {
        settlingMoveToken += 1;
        set({ settlingMoveSourceId: id });
        return settlingMoveToken;
    },
    clearSettlingMove: (token) => {
        if (token !== settlingMoveToken) return;
        set({ settlingMoveSourceId: null });
    },
    setCenterRequested: (value) => set((state) => ({
        centerRequested: typeof value === 'function' ? value(state.centerRequested) : value,
    })),
    requestEdgeAlign: (mode) => set((state) => ({
        edgeAlignRequested: { mode, nonce: (state.edgeAlignRequested?.nonce ?? 0) + 1 },
    })),
    setMinCellSize: (value) => set((state) => {
        const next = typeof value === 'function' ? value(state.minCellSize) : value;
        saveNumber(LS_MIN_CELL_SIZE_KEY, next);
        return { minCellSize: next };
    }),
    setStackWidth: (stackId, value) => set((state) => {
        // No-op on an unchanged width: a clamped drag repeats the same value
        // on every pointermove, and a new map would re-run the layout memos.
        if (state.stackWidths[stackId] === value) return {};
        const stackWidths = { ...state.stackWidths, [stackId]: value };
        saveStackWidths(stackWidths, state.fanCollapse);
        if (!state.maxColumns[stackId]) return { stackWidths };
        const { [stackId]: _dropped, ...maxColumns } = state.maxColumns;
        return { stackWidths, maxColumns };
    }),
    setColumnMax: (stackId, value) => set((state) => {
        if (!!state.maxColumns[stackId] === value) return {};
        if (value) return { maxColumns: { ...state.maxColumns, [stackId]: true } };
        const { [stackId]: _dropped, ...maxColumns } = state.maxColumns;
        return { maxColumns };
    }),
    clearStackWidths: () => set((state) => {
        const flags = Object.keys(state.maxColumns).length > 0 ? { maxColumns: {} } : {};
        // No-op when already empty so we don't mint a new reference (and
        // re-run downstream layout memos) on every all-stacks adjustment.
        if (Object.keys(state.stackWidths).length === 0) return flags;
        saveStackWidths({}, null);
        return { stackWidths: {}, ...flags };
    }),
    replaceStackWidths: (next) => set((state) => {
        if (next === state.stackWidths) return {};
        saveStackWidths(next, state.fanCollapse);
        return { stackWidths: next };
    }),
    setFanCollapse: (value) => set((state) => {
        const prev = state.fanCollapse;
        if (prev === value || (prev && value
            && prev.stackId === value.stackId && prev.frontCellId === value.frontCellId)) return {};
        saveStackWidths(state.stackWidths, value);
        return { fanCollapse: value };
    }),
    setAllStacks: (channelId, value) => set((state) => {
        const allStacksByChannel = { ...state.allStacksByChannel, [channelId]: value };
        saveJSON(LS_ALL_STACKS_KEY, allStacksByChannel);
        return { allStacksByChannel };
    }),
    setIsMaxWidth: (value) => {
        saveBool(LS_IS_MAX_WIDTH_KEY, value);
        set({ isMaxWidth: value });
    },
    setWidthDrag: (value) => set({ widthDrag: value }),
    reportCellChrome: (cellId, headerBottomOffsetPx) => set((state) => (
        state.cellChrome[cellId] === headerBottomOffsetPx
            ? {}
            : { cellChrome: { ...state.cellChrome, [cellId]: headerBottomOffsetPx } }
    )),
    pruneCellChrome: (cellIds) => set((state) => {
        const keep = Object.keys(state.cellChrome).filter(id => cellIds.has(id));
        if (keep.length === Object.keys(state.cellChrome).length) return {};
        return { cellChrome: Object.fromEntries(keep.map(id => [id, state.cellChrome[id]])) };
    }),
}));
