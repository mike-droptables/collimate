import { useState, useMemo, useRef, useCallback, useEffect, useLayoutEffect } from 'react';
import { MotionValue, useMotionValue } from 'motion/react';
import { isCapabilitiesStack, type Stack } from '@/lib/types';
import { SceneActions } from '@/hooks/useSceneActions';
import { PendingMoveState } from '@/stores/useSpatialStore';
import { useFocusActions } from '@/contexts/FocusContext';
import { useCollimateStore, type CollimateState } from '@/stores/useCollimateStore';
import {
    OVERVIEW_CARD_HEIGHT, REMOVE_RUNNING, removableCard, type RestoreTarget, type ShelfZone,
} from '@/stores/collimate/overviewSlice';
import { cellHasLiveRun } from '@/lib/runningCell';
import { claimKeyboardForGesture } from '@/lib/keyboardOwner';
import { useSceneColumns } from './useSceneColumns';
import { shelfZoneAt, zoneStripOf } from './useShelfDrag';
import { effectiveZones, removeZoneAt } from '@/components/overview/removeZones';
import {
    boardSignature, overviewColumnLanding, overviewInsertPreview, resolveSceneDropPreview, type ColumnCard, type SlotPreview,
} from '@/lib/sceneDropGeometry';
import { StackLayoutResult, FAN_FRAME_INSET, slotGapCenterX, type HeldGap } from '@/hooks/useStackLayout';
import {
    FanRange, DisplayTarget, hitTestCellDrop, hitTestStackDrop, resolveCellSource, resolveCellLanding,
    landingOf, builtinHead, MOVE_INTO_CAPABILITIES_RUNNING,
} from '@/lib/fanOut';
import { frontCellIndexOf } from '@/lib/frontSlots';

/** A live run on the cell (the store's), for resolveCellSource. */
const isLiveCell = (cellId: string) => cellHasLiveRun(useCollimateStore.getState(), cellId);

/** Where a cell dropped onto a column's face lands in the main view: before
 *  its front cell, never before its built-in cards (the manage card in
 *  front counts as the head of the stack). */
function ontoFrontIndex(target: Stack, vti: Record<string, number> | undefined): number {
    return Math.max(frontCellIndexOf(target, vti) ?? 0, builtinHead(target.cells));
}

interface UseStackDragParams {
    layout: StackLayoutResult;
    cameraXMV: MotionValue<number>;
    cameraYMV: MotionValue<number>;
    zoomMV: MotionValue<number>;
    cameraTargetRef: React.MutableRefObject<number>;
    /** Commit the camera's current x as useStackCamera's target (state,
     *  not just cameraTargetRef) — run after a drag edge-scrolled. */
    commitCameraTarget: (x: number) => void;
    /** Scene root container. clientX/clientY are window coordinates, but
     *  the camera transforms (and viewSize) live in this container's
     *  space — in the desktop shell a title bar offsets the container from
     *  the window's top edge, so client→world conversions must subtract
     *  the container's origin. */
    rootDivRef: React.RefObject<HTMLDivElement | null>;
    /** The display list (a fanned stack's columns included). */
    stacks: Stack[];
    /** The fanned-out stack's display span, or null. Drops inside its
     *  border land in that stack; lib/fanOut.ts turns every drop into a
     *  real-space CellOp. */
    fan: FanRange | null;
    /** Select a display id (fan columns included) — resolves it to the
     *  real stack + visible top index. Every drag-side selection write
     *  goes through it. */
    onSelectDisplay: (displayId: string, displayCellIdx?: number) => void;
    /** Display-level selection (fan column ids included) — the offscreen
     *  gate keeps the selected column's neighbourhood awake. */
    selectedId: string | null;
    actions: SceneActions;
    isZoomedOut: boolean;
    pendingMove: PendingMoveState | null;
    visibleTopIndices?: Record<string, number>;
    /** Written each frame with the gap the columns open (or null), for
     *  useStackLayout: the frame a drop's result arrives, it anchors the
     *  board on where the columns stood, gap included. */
    heldGapRef?: React.MutableRefObject<HeldGap | null>;
}

interface DragState {
    type: 'stack' | 'cell';
    stackId: string;
    cellIndex?: number;
    cellId?: string;
    ghostX: number;
    ghostY: number;
    grabOffsetX: number;
    grabOffsetY: number;
    /** Screen-pixel size at drag start (already scaled by camera zoom). */
    cellWidth: number;
    cellHeight: number;
    /** Natural (pre-zoom) size. Rendering the floating preview at this size
     *  with a matching scale(zoom) transform keeps the dragged cell's internal
     *  layout identical to its in-canvas appearance — without this, overview
     *  drags render text at full size inside a shrunken box. */
    naturalWidth: number;
    naturalHeight: number;
    /** Zoom at drag start, used for the floating preview's scale transform. */
    dragZoom: number;
    /** 'natural' = float at the source cell's full size (overview drag).
     *  'thumbnail' = render a small fixed-size card following the cursor
     *  (main-view grab handle) so the preview doesn't dwarf the canvas. */
    previewMode: 'natural' | 'thumbnail';
}

/** A pressed-but-not-yet-dragging grab: the drag's state minus the ghost
 *  position, plus where the press started (for the move threshold). */
type PotentialDrag = Omit<DragState, 'ghostX' | 'ghostY'> & { startX: number; startY: number };

/** The cards an overview column shows, top down (Only connected may hide
 *  some), each with its height: read from the drawn column, else every
 *  cell of `cells` at the card height. */
function shownCards(root: HTMLElement | null, stackId: string, cells: readonly string[]): ColumnCard[] {
    const column = root?.querySelector(`[data-stack-id="${stackId}"] [data-ov-column]`);
    if (!column) return cells.map(id => ({ id, height: OVERVIEW_CARD_HEIGHT }));
    return [...column.querySelectorAll<HTMLElement>('[data-ov-card]')]
        .map(el => ({ id: el.dataset.ovCard ?? '', height: el.offsetHeight || OVERVIEW_CARD_HEIGHT }))
        .filter(card => cells.includes(card.id));
}

/** The pointer's world point: `clientX`/`clientY` are window px, the camera
 *  lives in the scene root's space (StackScene's centre-origin transform). */
function worldPointOf(
    root: HTMLElement | null, clientX: number, clientY: number, view: { width: number; height: number },
    camera: { x: number; y: number; zoom: number },
) {
    const scene = root?.getBoundingClientRect();
    const zoom = camera.zoom || 1;
    return {
        x: (clientX - (scene?.left ?? 0) - view.width / 2) / zoom + camera.x,
        y: (clientY - (scene?.top ?? 0) - view.height / 2) / zoom + view.height / 2 + camera.y,
    };
}

/** A card held over overview column `target` at world `worldY`: the stack
 *  position it lands at (overviewColumnLanding). `heldCellId`: the held
 *  board card (its own column shows the others), or null for a shelf card;
 *  `current`: the position the column's gap shows now, or null. */
function columnLanding(
    root: HTMLElement | null, target: Stack, heldCellId: string | null, worldY: number, current: number | null,
): number {
    const cells = target.cells.map(cell => cell.id).filter(id => id !== heldCellId);
    return overviewColumnLanding(worldY, cells, shownCards(root, target.id, cells), current);
}

/** A card held over the Removed shelf or its bar (`over`), over remove zone
 *  `zone` (null: no zone strip on show). */
interface ShelfHold {
    over: ShelfZone;
    zone: number | null;
}

/** Where an overview card drag at (x, y) is over the Removed shelf or its
 *  bar (useShelfDrag's shelfZoneAt against the board and the overview), and
 *  the remove zone under x, against the zone strip's rect (the bar and the
 *  shelf's drop area share its extent). */
function zoneOf(board: HTMLElement | null, x: number, y: number): ShelfHold | null {
    const overview = board?.closest('[data-overview-root]');
    if (!board || !overview) return null;
    const over = shelfZoneAt(x, y, board.getBoundingClientRect(), overview.getBoundingClientRect());
    if (!over) return null;
    const strip = zoneStripOf(board);
    const zone = strip ? removeZoneAt(x, strip.getBoundingClientRect(), useCollimateStore.getState().overview.removeZones) : null;
    return { over, zone };
}

/** The store's lifted board card over `hold` (null: over the board, where
 *  the drop is written with `over: null` so the bar shows the zones' labels
 *  the whole time a removable card is held). A cell that cannot be removed
 *  here (a Capabilities cell) lights nothing; a running one is refused
 *  over the shelf or the bar. Writes only a change. */
function holdOverShelf(s: CollimateState, cellId: string, hold: ShelfHold | null) {
    const current = s.overview.drop;
    if (!removableCard(s, cellId)) {
        if (current?.kind === 'remove') s.setOverviewDrop(null);
        return;
    }
    const over = hold?.over ?? null;
    const zone = hold?.zone ?? null;
    const refusal = hold && cellHasLiveRun(s, cellId) ? REMOVE_RUNNING : null;
    if (current?.kind === 'remove' && current.cellId === cellId && current.over === over
        && current.zone === zone && current.refusal === refusal) return;
    s.setOverviewDrop({ kind: 'remove', cellId, over, zone, refusal });
}

/** Where a Removed shelf card held at world point `world` over the board
 *  lands: between columns (or past either end), a new column at that slot;
 *  over a column, that stack at the position the pointer points to (a
 *  Removed cell restores to the board: Capabilities takes none). `held`:
 *  the target shown now. */
function restoreTargetAt(
    root: HTMLElement, stacks: Stack[], positions: number[], widths: number[], world: { x: number; y: number },
    held: RestoreTarget | null,
): RestoreTarget | null {
    const hit = hitTestCellDrop({ worldX: world.x, positions, widths, fan: null, isThumb: false, inset: 0 });
    if (hit.kind === 'gap') return { kind: 'column', slot: hit.slot };
    const column = stacks[hit.col];
    if (!column || column.synthetic || isCapabilitiesStack(column)) return null;
    const current = held?.kind === 'stack' && held.stackId === column.id ? held.index : null;
    return { kind: 'stack', stackId: column.id, index: columnLanding(root, column, null, world.y, current) };
}

const sameRestoreTarget = (a: RestoreTarget | null, b: RestoreTarget | null): boolean =>
    a === b || (a !== null && b !== null && (
        a.kind === 'column' ? b.kind === 'column' && a.slot === b.slot
            : b.kind === 'stack' && a.stackId === b.stackId && a.index === b.index));

export function useStackDrag({
    layout, cameraXMV, cameraYMV, zoomMV, cameraTargetRef, commitCameraTarget, rootDivRef,
    stacks, fan, onSelectDisplay, selectedId, actions, isZoomedOut,
    pendingMove, visibleTopIndices, heldGapRef,
}: UseStackDragParams) {
    // cardFrame: where the cards sit (cardSpan) — the drop preview lines up with them.
    const {
        GAP, positions, widths, naturalWidths, viewSize, cardFrame,
        arrowWorldX, isCancelMove,
    } = layout;
    // Setters come from the actions context (stable for the provider lifetime,
    // never re-runs the move handler).
    const focusActions = useFocusActions();

    const dragStateRef = useRef<DragState | null>(null);
    const potentialDragRef = useRef<PotentialDrag | null>(null);
    // The overview's drag holds the pointer on the board, so the card can
    // be carried over the Removed bar and shelf above it.
    const captureRef = useRef<{ el: Element; pointerId: number } | null>(null);
    const releaseCapture = useCallback(() => {
        const held = captureRef.current;
        captureRef.current = null;
        try { held?.el.releasePointerCapture(held.pointerId); } catch { /* not held */ }
    }, []);

    const ghostXMV = useMotionValue(0);
    const ghostYMV = useMotionValue(0);
    const [dragGhost, setDragGhost] = useState<DragState | null>(null);
    const [dropTargetIdx, setDropTargetIdx] = useState<number | null>(null);
    const [insertTargetIdx, setInsertTargetIdx] = useState<number | null>(null);
    // The insert gap lies INSIDE the fanned stack's border (a position in
    // that stack) rather than outside it (a new stack). Only meaningful
    // while insertTargetIdx is set by a cell drag.
    const [insertInFan, setInsertInFan] = useState(false);
    const [intraStackCellIdx, setIntraStackCellIdx] = useState<number | null>(null);
    const [recentlyDroppedId, setRecentlyDroppedId] = useState<string | null>(null);
    // Stack equivalent of recentlyDroppedId — set after a stack drop
    // (move-stack-to-idx or merge) so StackScene can keep the moved
    // stack's z above its neighbours through the post-drop spring
    // animation. Without it, all affected stacks animate x with the
    // same default z and DOM order alone decides which renderer iframe
    // paints on top during the slide.
    const [recentlyDroppedStackId, setRecentlyDroppedStackId] = useState<string | null>(null);
    // Overview only: index of the stack the user is currently hovering
    // *over* during a STACK drag. When set, the drop will merge the
    // dragged stack's cells into the target and remove the source.
    // Mutually exclusive with insertTargetIdx (which means drop-between).
    const [mergeTargetIdx, setMergeTargetIdx] = useState<number | null>(null);
    // The board arrangement (boardSignature) the targets above were worked
    // out against. They show only while the board still has it: a drop's
    // result replaces the arrangement, and its preview goes in the same
    // frame (the result already holds the moved card where the gap was).
    const boardSig = useMemo(() => boardSignature(stacks), [stacks]);
    const [targetsSig, setTargetsSig] = useState<string | null>(null);
    // The overview: the column and stack position the held card's gap
    // shows now. The next pointer frame measures against the column as it
    // shows that gap (overviewColumnLanding's hysteresis).
    const landingRef = useRef<{ stackId: string; index: number } | null>(null);

    // Token-guarded settle flashes: two drops inside the 500ms window must
    // not have the first drop's delayed clear wipe the second's highlight
    // (and its z-bump) mid-animation.
    const droppedCellTokenRef = useRef(0);
    const droppedStackTokenRef = useRef(0);
    const flashRecentlyDroppedCell = useCallback((id: string | null) => {
        const token = ++droppedCellTokenRef.current;
        setRecentlyDroppedId(id);
        setTimeout(() => {
            if (droppedCellTokenRef.current === token) setRecentlyDroppedId(null);
        }, 500);
    }, []);
    const flashRecentlyDroppedStack = useCallback((id: string) => {
        const token = ++droppedStackTokenRef.current;
        setRecentlyDroppedStackId(id);
        setTimeout(() => {
            if (droppedStackTokenRef.current === token) setRecentlyDroppedStackId(null);
        }, 500);
    }, []);

    // RAF batching for the per-frame drag math. Browsers fire pointermove
    // at the input device rate (240 Hz on a high-end mouse) which is far
    // higher than the paint rate. Without batching we'd run the math and
    // call setState 4× per move event. We coalesce by stashing the latest
    // event in latestMoveRef and scheduling a single RAF; the flush
    // function reads the latest layout values from a ref so it can stay
    // stable across renders.
    const dragRafRef = useRef<number | null>(null);
    const latestMoveRef = useRef<{ clientX: number; clientY: number } | null>(null);
    // Edge auto-scroll state. While a thumbnail drag is active and the
    // cursor sits near a horizontal viewport edge, we run a RAF that
    // pans the camera and re-runs the drop math so the drag can reach
    // off-screen stacks. `lastPointerRef` is persistent (latestMoveRef
    // is cleared each frame) so edge-scroll can re-feed the pointer
    // into flushDragMove when the user is holding still at the edge.
    const edgeScrollRafRef = useRef<number | null>(null);
    const edgeScrollVelocityRef = useRef<number>(0);
    const edgeScrollLastTimeRef = useRef<number>(0);
    const lastPointerRef = useRef<{ clientX: number; clientY: number } | null>(null);
    // The drag has panned the camera by edge-scroll: those set() calls only
    // write cameraTargetRef, so the drop commits the position as the target.
    const edgeScrolledRef = useRef(false);
    // Forward-declared so flushDragMove can request a tick without
    // closing over edgeScrollTick (which itself references flushDragMove).
    const edgeScrollTickRef = useRef<(() => void) | null>(null);
    const dragInputsRef = useRef({
        positions, widths, viewSize, stacks, fan,
        cameraXMV, cameraYMV, zoomMV, isZoomedOut, visibleTopIndices, boardSig,
    });
    useLayoutEffect(() => {
        dragInputsRef.current = { positions, widths, viewSize, stacks, fan,
            cameraXMV, cameraYMV, zoomMV, isZoomedOut, visibleTopIndices, boardSig };
    }, [positions, widths, viewSize, stacks, fan, cameraXMV, cameraYMV, zoomMV, isZoomedOut, visibleTopIndices, boardSig]);

    const stopEdgeScroll = useCallback(() => {
        if (edgeScrollRafRef.current !== null) {
            cancelAnimationFrame(edgeScrollRafRef.current);
            edgeScrollRafRef.current = null;
        }
        edgeScrollVelocityRef.current = 0;
        edgeScrollLastTimeRef.current = 0;
    }, []);

    const flushDragMove = useCallback(() => {
        dragRafRef.current = null;
        const pt = latestMoveRef.current;
        latestMoveRef.current = null;
        if (!pt || !dragStateRef.current) return;
        lastPointerRef.current = pt;

        const {
            positions, widths, viewSize, stacks, fan,
            cameraXMV, cameraYMV, zoomMV, isZoomedOut, visibleTopIndices, boardSig,
        } = dragInputsRef.current;
        setTargetsSig(boardSig);

        // An edge-scroll re-feed of a stationary cursor leaves the ghost
        // where it is: skip the per-frame re-render for it.
        const moved = dragStateRef.current.ghostX !== pt.clientX || dragStateRef.current.ghostY !== pt.clientY;
        dragStateRef.current.ghostX = pt.clientX;
        dragStateRef.current.ghostY = pt.clientY;
        if (moved) {
            ghostXMV.set(pt.clientX);
            ghostYMV.set(pt.clientY);
        }

        // Ghost/floating previews are position:fixed and keep raw client
        // coordinates; world math needs container-local ones (the desktop
        // title bar offsets the scene from the window's top edge).
        const localX = pt.clientX - (rootDivRef.current?.getBoundingClientRect().left ?? 0);
        const { x: worldX, y: worldY } = worldPointOf(rootDivRef.current, pt.clientX, pt.clientY, viewSize, {
            x: cameraXMV.get(), y: cameraYMV.get(), zoom: zoomMV.get(),
        });
        if (dragStateRef.current.type === 'stack') {
            // The overview: allow dropping a column onto another column to
            // merge. Detect by hovering inside the target's X bounds.
            // Outside any column → slot-between-columns insert, same
            // as the main view's stack reorder. The dragged source
            // itself is excluded so you can hover over your own slot
            // without triggering a self-merge (lib/fanOut.ts hitTestStackDrop).
            const stackHit = hitTestStackDrop({
                worldX, positions, widths, stacks, srcStackId: dragStateRef.current.stackId, isZoomedOut,
            });
            if (stackHit.kind === 'merge') {
                setMergeTargetIdx(stackHit.idx);
                setInsertTargetIdx(null);
                setInsertInFan(false);
                setDropTargetIdx(null);
                return;
            }
            setInsertTargetIdx(stackHit.idx);
            setInsertInFan(false);
            setMergeTargetIdx(null);
            setDropTargetIdx(null);
            return;
        }

        // Cell drag math. Main-view grab drags use a forgiving insert zone:
        // the outer ~25% of each stack reads as "drop between," because the
        // gap is too narrow to hit in the main view without this fringe.
        // The overview keeps strict gap-only insertion. Inside a fanned
        // stack's border every position is a gap of that stack
        // (lib/fanOut.ts hitTestCellDrop).
        // The overview: a card held over the Removed shelf or its bar is
        // the shelf's to take, not the board's.
        const heldLanding = landingRef.current;
        landingRef.current = null;
        if (isZoomedOut && dragStateRef.current.cellId) {
            const hold = zoneOf(rootDivRef.current, pt.clientX, pt.clientY);
            holdOverShelf(useCollimateStore.getState(), dragStateRef.current.cellId, hold);
            if (hold) {
                setInsertTargetIdx(null);
                setInsertInFan(false);
                setDropTargetIdx(null);
                setIntraStackCellIdx(null);
                return;
            }
        }
        const isThumb = dragStateRef.current.previewMode === 'thumbnail';
        const hit = hitTestCellDrop({ worldX, positions, widths, fan, isThumb, inset: FAN_FRAME_INSET });

        if (hit.kind === 'gap') {
            // A gap the card would not move from (either side of its own
            // lone column, or its own two in-fan gaps) previews the card's
            // own place: no column moves, and letting go there leaves it
            // where it is (resolveCellLanding's 'stays').
            const ds = dragStateRef.current;
            const src = resolveCellSource(stacks, fan, ds.stackId, ds.cellIndex ?? 0, isLiveCell);
            const landing = src ? resolveCellLanding(stacks, fan, src, { kind: 'gap', slot: hit.slot, inFan: hit.inFan }).kind : 'refused';
            const moves = landing === 'move';
            const stays = src !== null && landing === 'stays';
            setInsertTargetIdx(moves ? hit.slot : null);
            setInsertInFan(moves && hit.inFan);
            setDropTargetIdx(stays ? src.displayIdx : null);
            setIntraStackCellIdx(stays ? src.cellIdx : null);
            if (stays && isZoomedOut) landingRef.current = { stackId: src.displayId, index: src.cellIdx };
        } else {
            const calcDropIdx = hit.col;
            const target = stacks[calcDropIdx];
            setInsertTargetIdx(null);
            setInsertInFan(false);
            if (!isZoomedOut) {
                // Onto the column's face: a column it may not land on (a
                // running cell over Capabilities) shows nothing.
                const ds = dragStateRef.current;
                const index = target.id === ds.stackId ? (ds.cellIndex ?? 0) : ontoFrontIndex(target, visibleTopIndices);
                const src = resolveCellSource(stacks, fan, ds.stackId, ds.cellIndex ?? 0, isLiveCell);
                const shows = src !== null
                    && resolveCellLanding(stacks, fan, src, { kind: 'onto', col: calcDropIdx, intra: index }).kind !== 'refused';
                setDropTargetIdx(shows ? calcDropIdx : null);
                setIntraStackCellIdx(shows ? index : null);
            } else {
                // The overview: the stack position the pointer points to in
                // the column. Its own place (in its own column) shows the
                // column's own gap there, and letting go leaves it where it
                // is; a column it may not land on shows nothing.
                const ds = dragStateRef.current;
                const index = columnLanding(
                    rootDivRef.current, target, ds.cellId ?? null, worldY,
                    heldLanding?.stackId === target.id ? heldLanding.index : null,
                );
                const src = resolveCellSource(stacks, fan, ds.stackId, ds.cellIndex ?? 0, isLiveCell);
                const shows = src !== null
                    && resolveCellLanding(stacks, fan, src, { kind: 'onto', col: calcDropIdx, intra: index }).kind !== 'refused';
                landingRef.current = shows ? { stackId: target.id, index } : null;
                setDropTargetIdx(shows ? calcDropIdx : null);
                setIntraStackCellIdx(shows ? index : null);
            }
        }

        // Edge-scroll velocity update. Only thumbnail (main-view) drags
        // scroll the canvas; a natural-size drag on the overview does not.
        // Velocity ramps up smoothly as the cursor moves into the edge
        // zone, so the pan doesn't lurch.
        if (dragStateRef.current.previewMode === 'thumbnail') {
            const EDGE_PX = 100;
            const MAX_VEL = 22;
            const leftDist = localX;
            const rightDist = viewSize.width - localX;
            let v = 0;
            if (leftDist < EDGE_PX) v = -MAX_VEL * (1 - Math.max(0, leftDist) / EDGE_PX);
            else if (rightDist < EDGE_PX) v = MAX_VEL * (1 - Math.max(0, rightDist) / EDGE_PX);
            edgeScrollVelocityRef.current = v;
            if (v !== 0 && edgeScrollRafRef.current === null && edgeScrollTickRef.current) {
                edgeScrollLastTimeRef.current = performance.now();
                edgeScrollRafRef.current = requestAnimationFrame(edgeScrollTickRef.current);
            }
        }
    }, [ghostXMV, ghostYMV, rootDivRef]);

    // Edge auto-scroll RAF. Pans the camera at velocity, then re-feeds
    // the last cursor into flushDragMove in the same frame so the drop
    // targets refresh against this frame's camera even when the user is
    // holding still at the edge.
    const edgeScrollTick = useCallback(() => {
        edgeScrollRafRef.current = null;
        const ds = dragStateRef.current;
        const v = edgeScrollVelocityRef.current;
        if (!ds || v === 0) {
            edgeScrollLastTimeRef.current = 0;
            return;
        }
        const { cameraXMV } = dragInputsRef.current;
        const now = performance.now();
        const dt = edgeScrollLastTimeRef.current
            ? Math.min(50, now - edgeScrollLastTimeRef.current) / 16.67
            : 1;
        edgeScrollLastTimeRef.current = now;
        const newX = cameraXMV.get() + v * dt;
        cameraXMV.set(newX);
        cameraTargetRef.current = newX;
        edgeScrolledRef.current = true;
        // Re-arm first, so the flush below sees a tick scheduled and
        // doesn't start a second loop; if it drops the velocity to 0, the
        // scheduled tick ends the loop.
        edgeScrollRafRef.current = requestAnimationFrame(edgeScrollTick);
        if (lastPointerRef.current && dragRafRef.current === null) {
            latestMoveRef.current = { ...lastPointerRef.current };
            flushDragMove();
        }
    }, [cameraTargetRef, flushDragMove]);
    useLayoutEffect(() => { edgeScrollTickRef.current = edgeScrollTick; }, [edgeScrollTick]);

    // Cancel any pending RAF on unmount so a stale frame can't fire after
    // the hook is gone.
    useEffect(() => {
        return () => {
            if (dragRafRef.current !== null) {
                cancelAnimationFrame(dragRafRef.current);
                dragRafRef.current = null;
            }
            if (edgeScrollRafRef.current !== null) {
                cancelAnimationFrame(edgeScrollRafRef.current);
                edgeScrollRafRef.current = null;
            }
        };
    }, []);

    const handleStackDragStart = useCallback((stackId: string, e: React.PointerEvent) => {
        // Primary button only: a right/middle press is the canvas pan, and
        // must bubble to it as if this handler weren't there.
        if (e.button !== 0) return;
        e.stopPropagation();
        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
        const z = zoomMV.get() || 1;
        potentialDragRef.current = {
            type: 'stack', stackId, startX: e.clientX, startY: e.clientY,
            grabOffsetX: e.clientX - rect.left, grabOffsetY: e.clientY - rect.top,
            cellWidth: rect.width, cellHeight: rect.height,
            naturalWidth: rect.width / z, naturalHeight: rect.height / z,
            dragZoom: z,
            previewMode: 'natural',
        };
    }, [zoomMV]);

    const handleCellDragStart = useCallback((stackId: string, cellIndex: number, cellId: string, e: React.PointerEvent, sourceRect?: DOMRect) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        // Main-view grab handle: caller passes the cell card's rect because
        // e.currentTarget is the grab icon, not the card. We also use that
        // signal to switch the floating preview into thumbnail mode — at
        // main-view scale the natural-size card dwarfs the canvas.
        const isGrabHandle = sourceRect !== undefined;
        const rect = sourceRect ?? (e.currentTarget as HTMLElement).getBoundingClientRect();
        const z = zoomMV.get() || 1;
        potentialDragRef.current = {
            type: 'cell', stackId, cellIndex, cellId,
            startX: e.clientX, startY: e.clientY,
            grabOffsetX: e.clientX - rect.left, grabOffsetY: e.clientY - rect.top,
            cellWidth: rect.width, cellHeight: rect.height,
            naturalWidth: rect.width / z, naturalHeight: rect.height / z,
            dragZoom: z,
            previewMode: isGrabHandle ? 'thumbnail' : 'natural',
        };
    }, [zoomMV]);

    const handleDragPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        // Drag-start: cross the threshold, mutate focus context once.
        // This branch only runs once per drag, not per frame.
        if (potentialDragRef.current && !dragStateRef.current) {
            const dx = e.clientX - potentialDragRef.current.startX;
            const dy = e.clientY - potentialDragRef.current.startY;
            if (dx * dx + dy * dy > 25) {
                const { type, stackId, cellIndex } = potentialDragRef.current;
                if (type === 'cell') {
                    onSelectDisplay(stackId, cellIndex);
                    if (cellIndex !== undefined && isZoomedOut) {
                        actions.winSetTopCell(stackId, cellIndex);
                    }
                } else if (type === 'stack') {
                    onSelectDisplay(stackId);
                }
                const { startX: _sx, startY: _sy, ...rest } = potentialDragRef.current;
                dragStateRef.current = { ...rest, ghostX: e.clientX, ghostY: e.clientY };
                if (isZoomedOut && e.currentTarget) {
                    try {
                        e.currentTarget.setPointerCapture(e.pointerId);
                        captureRef.current = { el: e.currentTarget, pointerId: e.pointerId };
                    } catch { /* the pointer is gone */ }
                }
                ghostXMV.set(e.clientX);
                ghostYMV.set(e.clientY);
                setDragGhost({ ...dragStateRef.current });
                potentialDragRef.current = null;
                window.dispatchEvent(new Event('canvas-pan-start'));
            }
        }

        // Per-frame: stash the latest pointer and schedule one RAF. The
        // input device may fire pointermove faster than the display can
        // paint; coalescing into a single RAF turns N events per frame
        // into 1 setState burst per frame.
        if (dragStateRef.current) {
            latestMoveRef.current = { clientX: e.clientX, clientY: e.clientY };
            if (dragRafRef.current === null) {
                dragRafRef.current = requestAnimationFrame(flushDragMove);
            }
        }
        // onSelectDisplay and the focus setters are stable; `actions` changes
        // identity when mainStacks changes (server pushes), never per
        // pointermove. Don't add `focus` (state) here; only setters.
    }, [actions, onSelectDisplay, isZoomedOut, flushDragMove, ghostXMV, ghostYMV]);

    const handleDragPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        // Cancel any pending drag-move RAF before settling the drop, so a
        // stale frame can't fire after the drop and resurrect cleared
        // visual state.
        if (dragRafRef.current !== null) {
            cancelAnimationFrame(dragRafRef.current);
            dragRafRef.current = null;
        }
        latestMoveRef.current = null;
        lastPointerRef.current = null;
        stopEdgeScroll();
        // Before the drop's optimistic stacks change, so useStackCamera
        // sees target == camera and doesn't spring back to the pre-drag spot.
        if (edgeScrolledRef.current) {
            edgeScrolledRef.current = false;
            commitCameraTarget(dragInputsRef.current.cameraXMV.get());
        }

        if (potentialDragRef.current) {
            const { type, stackId, cellIndex } = potentialDragRef.current;
            if (type === 'cell') {
                onSelectDisplay(stackId, cellIndex);
                if (cellIndex !== undefined && isZoomedOut) {
                    actions.winSetTopCell(stackId, cellIndex);
                }
            } else if (type === 'stack') {
                onSelectDisplay(stackId);
            }
            potentialDragRef.current = null;
        }

        if (dragStateRef.current) {
            const ds = dragStateRef.current;
            dragStateRef.current = null;

            // Container-local coordinates for world math — same offset
            // correction as flushDragMove (desktop title bar above scene).
            const { x: worldX, y: worldY } = worldPointOf(rootDivRef.current, e.clientX, e.clientY, viewSize, {
                x: cameraXMV.get(), y: cameraYMV.get(), zoom: zoomMV.get(),
            });
            const heldLanding = landingRef.current;
            landingRef.current = null;
            const shelfHold = ds.type === 'cell' && isZoomedOut ? zoneOf(rootDivRef.current, e.clientX, e.clientY) : null;
            releaseCapture();
            let finalInsertIdx: number | null = null;
            let finalInFan = false;
            let finalDropIdx: number | null = null;
            let finalIntraIdx: number | null = null;
            let finalMergeIdx: number | null = null;

            if (ds.type === 'stack') {
                // The overview: same hit-test as flushDragMove. If the pointer
                // is inside a (non-source, non-Capabilities) column's X bounds,
                // the drop is a merge; otherwise it's a slot-between
                // reorder.
                const stackHit = hitTestStackDrop({
                    worldX, positions, widths, stacks, srcStackId: ds.stackId, isZoomedOut,
                });
                if (stackHit.kind === 'merge') finalMergeIdx = stackHit.idx;
                else finalInsertIdx = stackHit.idx;
            } else if (shelfHold) {
                // Let go over the Removed shelf or bar: the board keeps no target.
            } else {
                const hit = hitTestCellDrop({
                    worldX, positions, widths, fan, isThumb: ds.previewMode === 'thumbnail', inset: FAN_FRAME_INSET,
                });
                if (hit.kind === 'gap') {
                    finalInsertIdx = hit.slot;
                    finalInFan = hit.inFan;
                } else {
                    const target = stacks[hit.col];
                    finalDropIdx = hit.col;
                    finalIntraIdx = isZoomedOut
                        // The stack position the pointer points to, measured
                        // against the column as it shows the held gap.
                        ? columnLanding(
                            rootDivRef.current, target, ds.cellId ?? null, worldY,
                            heldLanding?.stackId === target.id ? heldLanding.index : null,
                        )
                        : target.id === ds.stackId
                            ? (ds.cellIndex ?? 0)
                            : ontoFrontIndex(target, visibleTopIndices);
                }
            }

            setTargetsSig(boardSig);
            setDropTargetIdx(finalDropIdx);
            setInsertTargetIdx(finalInsertIdx);
            setInsertInFan(finalInFan);
            setIntraStackCellIdx(finalIntraIdx);
            setMergeTargetIdx(null);
            // A drop that moves nothing keeps no gap open while it clears.
            const nothingMoves = () => {
                setDropTargetIdx(null);
                setInsertTargetIdx(null);
                setInsertInFan(false);
                setIntraStackCellIdx(null);
            };

            let actionPromise: Promise<unknown> = Promise.resolve();

            if (ds.type === 'stack' && finalMergeIdx !== null) {
                const tgt = stacks[finalMergeIdx];
                if (tgt && tgt.id !== ds.stackId) {
                    actionPromise = actions.mergeStackIntoStack(ds.stackId, tgt.id);
                    flashRecentlyDroppedStack(ds.stackId);
                }
            } else if (ds.type === 'stack' && finalInsertIdx !== null) {
                const sourceIdx = stacks.findIndex(s => s.id === ds.stackId);
                if (sourceIdx !== finalInsertIdx && sourceIdx + 1 !== finalInsertIdx) {
                    actionPromise = actions.moveStackToIdx(ds.stackId, finalInsertIdx);
                    flashRecentlyDroppedStack(ds.stackId);
                } else {
                    nothingMoves();
                }
            } else if (ds.type === 'cell' && shelfHold) {
                // Onto the shelf: removed, as its Remove button does, with
                // the action of the remove zone it was let go over; a
                // running cell is refused where it was let go. A cell with no
                // Remove (a Capabilities cell) stays where it was.
                const store = useCollimateStore.getState();
                const cellId = ds.cellId ?? '';
                if (removableCard(store, cellId)) {
                    if (cellHasLiveRun(store, cellId)) store.refuseDrop(REMOVE_RUNNING, shelfHold.over);
                    else if (shelfHold.zone === null) actionPromise = store.removeFromBoard(cellId);
                    else actionPromise = store.removeFromBoard(cellId, effectiveZones(store.overview.removeZones)[shelfHold.zone]?.action);
                }
            } else if (ds.type === 'cell') {
                // Display-space drop → real-space CellOp (lib/fanOut.ts).
                // A drop that would leave the cell where it is resolves to
                // null: nothing is sent and the ghost just clears.
                const src = resolveCellSource(stacks, fan, ds.stackId, ds.cellIndex ?? 0, isLiveCell);
                const target: DisplayTarget | null = finalInsertIdx !== null
                    ? { kind: 'gap', slot: finalInsertIdx, inFan: finalInFan }
                    : (finalDropIdx !== null && finalIntraIdx !== null
                        ? { kind: 'onto', col: finalDropIdx, intra: finalIntraIdx }
                        : null);
                const landing = src && target ? resolveCellLanding(stacks, fan, src, target) : null;
                if (landing?.kind === 'move') {
                    flashRecentlyDroppedCell(ds.cellId ?? null);
                    actionPromise = actions.applyCellOp(landing.op);
                } else {
                    nothingMoves();
                    // A running cell let go over Capabilities: say why it stays.
                    if (landing?.kind === 'refused' && landing.reason === 'running') {
                        void useCollimateStore.getState().openAlert({ title: 'Move refused', body: MOVE_INTO_CAPABILITIES_RUNNING });
                    }
                }
            }

            const held = useCollimateStore.getState();
            if (held.overview.drop?.kind === 'remove') held.setOverviewDrop(null);
            window.dispatchEvent(new Event('canvas-pan-end'));

            actionPromise.then((result) => {
                setDragGhost(null);
                setDropTargetIdx(null);
                setInsertTargetIdx(null);
                setInsertInFan(false);
                setIntraStackCellIdx(null);

                if (ds.type === 'cell') {
                    // Select wherever the cell landed, with it on top — a
                    // landing in the fanned stack selects the moved column.
                    const land = landingOf(result, ds.cellId);
                    if (land) {
                        focusActions.setSelectedWindowId(land.stackId);
                        focusActions.setFocusArea('main');

                        actions.winSetTopCell(land.stackId, land.cellIdx);
                    }
                }
            });
        }
    }, [stacks, fan, actions, onSelectDisplay, positions, widths, viewSize, zoomMV, cameraXMV, cameraYMV, focusActions, isZoomedOut, flashRecentlyDroppedCell, flashRecentlyDroppedStack, rootDivRef, stopEdgeScroll, commitCameraTarget, visibleTopIndices, releaseCapture, boardSig]);

    /** Escape: the card or column goes back where it was, nothing moves. */
    const cancelDrag = useCallback(() => {
        if (dragRafRef.current !== null) {
            cancelAnimationFrame(dragRafRef.current);
            dragRafRef.current = null;
        }
        latestMoveRef.current = null;
        lastPointerRef.current = null;
        stopEdgeScroll();
        if (edgeScrolledRef.current) {
            edgeScrolledRef.current = false;
            commitCameraTarget(dragInputsRef.current.cameraXMV.get());
        }
        potentialDragRef.current = null;
        dragStateRef.current = null;
        landingRef.current = null;
        releaseCapture();
        setDragGhost(null);
        setDropTargetIdx(null);
        setInsertTargetIdx(null);
        setInsertInFan(false);
        setIntraStackCellIdx(null);
        setMergeTargetIdx(null);
        const held = useCollimateStore.getState();
        if (held.overview.drop?.kind === 'remove') held.setOverviewDrop(null);
        window.dispatchEvent(new Event('canvas-pan-end'));
    }, [stopEdgeScroll, commitCameraTarget, releaseCapture]);

    // The parts of dragGhost the memos below read. dragGhost itself is a
    // fresh object every pointer frame (the floating card's position); these
    // change only when a drag starts or ends.
    const dragType = dragGhost?.type ?? null;
    const dragStackId = dragGhost?.stackId ?? null;
    const dragCellIndex = dragGhost?.cellIndex;
    const dragActive = dragGhost !== null;

    useEffect(() => {
        if (!dragActive) return undefined;
        const release = claimKeyboardForGesture();
        const onKey = (e: KeyboardEvent) => {
            if (e.key !== 'Escape') return;
            e.preventDefault();
            e.stopImmediatePropagation();
            cancelDrag();
        };
        window.addEventListener('keydown', onKey, true);
        return () => {
            window.removeEventListener('keydown', onKey, true);
            release();
        };
    }, [dragActive, cancelDrag]);

    // A shelf card held over the board (useShelfDrag): where it lands goes
    // back to the store, and the board shows it there, as for a board card:
    // over a column, that stack at the position the pointer points to (the
    // column opens its gap there); between columns, a new column (the board
    // opens a gap there). Only the pointer moves the target: once the card is
    // let go the target (and the arrangement it was found on) stays put
    // until the cell lands.
    const [restoreSig, setRestoreSig] = useState<string | null>(null);
    const followedPointRef = useRef<{ x: number; y: number } | null | undefined>(undefined);
    useEffect(() => {
        if (!isZoomedOut) return undefined;
        const follow = (state: CollimateState) => {
            const drop = state.overview.drop;
            if (drop?.kind !== 'restore') {
                followedPointRef.current = undefined;
                return;
            }
            if (drop.point === followedPointRef.current) return;
            followedPointRef.current = drop.point;
            const root = rootDivRef.current;
            const { positions, widths, stacks, viewSize, cameraXMV, cameraYMV, zoomMV, boardSig } = dragInputsRef.current;
            const target = drop.point && root
                ? restoreTargetAt(root, stacks, positions, widths, worldPointOf(root, drop.point.x, drop.point.y, viewSize, {
                    x: cameraXMV.get(), y: cameraYMV.get(), zoom: zoomMV.get(),
                }), drop.target)
                : null;
            setRestoreSig(boardSig);
            if (!sameRestoreTarget(target, drop.target)) state.setOverviewDrop({ ...drop, target });
        };
        follow(useCollimateStore.getState());
        return useCollimateStore.subscribe(follow);
    }, [isZoomedOut, rootDivRef]);
    const restoreTarget = useCollimateStore(s => (isZoomedOut && s.overview.drop?.kind === 'restore' ? s.overview.drop.target : null));
    const shelfLive = restoreTarget !== null && restoreSig === boardSig && stacks.length > 0 && !dragActive;
    // The gap a shelf card opens between columns is a board card's: the
    // same new-column slot (every overview column is one width, so any
    // column's stands in for the shelf card's), the same reflow and the same
    // marked preview.
    const restoreSlot = shelfLive && restoreTarget.kind === 'column' ? restoreTarget.slot : null;
    const shelfGap = restoreSlot !== null;
    // Over a column, the column's own gap at the pointed position.
    const join = shelfLive && restoreTarget.kind === 'stack' ? restoreTarget : null;
    const joinCol = join ? stacks.findIndex(s => s.id === join.stackId) : -1;
    const shelfDrop = join && joinCol >= 0 ? { col: joinCol, index: join.index } : null;

    // The targets as shown: only on the arrangement they were found on.
    const live = targetsSig === boardSig;
    const shownInsert = live ? insertTargetIdx : null;
    const boardDrop = live ? dropTargetIdx : null;
    const shownDrop = boardDrop ?? shelfDrop?.col ?? null;
    const shownIntra = boardDrop !== null ? intraStackCellIdx : shelfDrop?.index ?? null;
    const shownMerge = live ? mergeTargetIdx : null;

    const items = useSceneColumns({
        layout, stacks, cameraXMV, zoomMV, cameraTargetRef, selectedId, isZoomedOut,
        pendingMove,
        dragType: shelfGap ? 'cell' : dragType,
        dragStackId: shelfGap ? stacks[0].id : dragStackId,
        insertTargetIdx: shelfGap ? restoreSlot : shownInsert,
    });

    // The gap as the columns show it: the first column pushed off its
    // layout position, and by how much.
    useLayoutEffect(() => {
        if (!heldGapRef) return;
        const slot = items.findIndex((item, i) => item.x !== positions[i]);
        heldGapRef.current = slot < 0 ? null : { slot, shift: items[slot].x - positions[slot] };
    }, [items, positions, heldGapRef]);

    const boardPreview = useMemo(() => resolveSceneDropPreview({
        dragType, dragStackId, dragCellIndex, pendingMove, stacks, positions, widths, naturalWidths,
        insertTargetIdx: shownInsert, insertInFan, dropTargetIdx: boardDrop,
        cardFrame, isZoomedOut, visibleTopIndices, GAP,
    }), [dragType, dragStackId, dragCellIndex, pendingMove, stacks, positions, widths, naturalWidths, shownInsert, insertInFan, boardDrop,
        cardFrame, isZoomedOut, visibleTopIndices, GAP]);
    const previewCellData = useMemo((): SlotPreview | null => boardPreview
        ?? (shelfGap ? overviewInsertPreview(restoreSlot, positions, widths, naturalWidths[0], GAP) : null),
    [boardPreview, shelfGap, restoreSlot, positions, widths, naturalWidths, GAP]);

    // Floating cell rendered in screen-space, anchored at the cursor grab point.
    // Unlike previewCellData (which shows the target slot), this follows the
    // mouse 1:1 so the cursor stays in the exact spot on the cell where it
    // started the drag. naturalWidth/Height + dragZoom let the renderer scale
    // the preview to match the in-canvas appearance (otherwise overview
    // drags show full-size text inside a shrunken box).
    const floatingCellData = useMemo(() => {
        if (!dragGhost || dragGhost.type !== 'cell') return null;
        const sourceStack = stacks.find(s => s.id === dragGhost.stackId);
        if (!sourceStack) return null;
        const cell = sourceStack.cells.find(c => c.id === dragGhost.cellId);
        if (!cell) return null;
        return {
            cell,
            x: dragGhost.ghostX - dragGhost.grabOffsetX,
            y: dragGhost.ghostY - dragGhost.grabOffsetY,
            // Raw cursor position — the thumbnail preview centers on this so it
            // sits directly under the pointer instead of the card's midpoint.
            cursorX: dragGhost.ghostX,
            cursorY: dragGhost.ghostY,
            width: dragGhost.cellWidth,
            height: dragGhost.cellHeight,
            naturalWidth: dragGhost.naturalWidth,
            naturalHeight: dragGhost.naturalHeight,
            dragZoom: dragGhost.dragZoom,
            previewMode: dragGhost.previewMode,
        };
    }, [dragGhost, stacks]);

    const arrowState = useMemo(() => {
        if (arrowWorldX === null || !pendingMove) return null;
        if (pendingMove?.type === 'cell') return null;
        return { x: arrowWorldX, mode: pendingMove.mode, type: pendingMove.type, isCancel: isCancelMove };
    }, [arrowWorldX, pendingMove, isCancelMove]);

    const insertLineX = useMemo(() => {
        if (shownInsert === null || !dragActive || positions.length === 0) return null;
        return slotGapCenterX(shownInsert, positions, widths, GAP);
    }, [shownInsert, dragActive, positions, widths, GAP]);

    return {
        dragGhost, ghostXMV, ghostYMV,
        dropTargetIdx: shownDrop,
        intraStackCellIdx: shownIntra,
        mergeTargetIdx: shownMerge,
        recentlyDroppedId,
        recentlyDroppedStackId,
        items,
        previewCellData,
        floatingCellData,
        arrowState,
        insertLineX,
        handleStackDragStart,
        handleCellDragStart,
        handleDragPointerMove,
        handleDragPointerUp,
    };
}
