import { useEffect, useLayoutEffect, useMemo, useRef, useCallback, useState } from 'react';
import type { Stack } from '@/lib/types';
import { StackGroup, NO_OP_STACK_CALLBACKS, FLOATING_PREVIEW_STACK_ID } from './StackGroup';
import { motion, AnimatePresence, animate, useReducedMotion, useTransform } from 'motion/react';
import { PHYSICS, SNAP } from '@/lib/physics';
import { useGuestOccluder } from '@/lib/guestOcclusion';
import { SceneActions } from '@/hooks/useSceneActions';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { useStackLayout, overviewColumnHeight, overviewFit, type HeldGap } from '@/hooks/useStackLayout';
import { zoomCameraAt } from '@/hooks/usePanZoomCamera';
import { useCollimateStore, type CollimateState } from '@/stores/useCollimateStore';
import {
    armedBoard, boardFollowHint, connectedCells, connectedOn, subjectCellId, JOURNAL_PANE_WIDTH, OVERVIEW_BAR_HEIGHT,
} from '@/stores/collimate/overviewSlice';
import { OverviewBoardContext, type OverviewBoard } from './overview/OverviewCard';
import { OverviewZoom } from './overview/OverviewZoom';
import { RemovedShelf } from './overview/RemovedShelf';
import { RemovedBar } from './overview/RemovedBar';
import { JournalPane } from './overview/JournalPane';
import { OverviewArrows } from './overview/OverviewArrows';
import { useStackCamera } from '@/hooks/useStackCamera';
import { NO_INSET, commandPaneInsetOf, paneInsetTransition } from '@/lib/commandPane';
import { useStackDrag } from '@/hooks/useStackDrag';
import { useStackCallbacks } from '@/hooks/useStackCallbacks';
import { useEntryHold } from '@/hooks/useEntryHold';
import { RendererConcealContext } from '@/contexts/RendererConcealContext';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { CAMERA_ORIGIN_ATTR, OVERLAY_RULER_PX } from '@/lib/guestPoolGeometry';
import { alignToDevicePixels, deviceSnappedTransform, ORIGIN_DX_VAR, ORIGIN_DY_VAR } from '@/lib/deviceOrigin';
import {
    FanRange, displayToReal, realStackCount, fanFrameExtent, FAN_TOGGLE_SETTLE_MS,
} from '@/lib/fanOut';
import { FanFrame } from './FanFrame';
import { pendingMovePeekSlot } from '@/lib/sceneDropGeometry';
import { StackSlot } from './StackSlot';
import { SceneDragOverlays, SceneCursorOverlays } from './SceneDragOverlays';
import { isEditableTarget } from '@/lib/nativeSelection';

// --- Types ---
interface StackSceneProps {
    /** The display list: main stacks with the fanned stack (if any)
     *  replaced in place by its `fan-<cellId>` columns. */
    stacks: Stack[];
    /** Display-level selection (a fan column's id when the fanned stack
     *  is selected). */
    selectedId: string | null;
    actions: SceneActions;
    transitionType: 'nav' | 'cell';
    topology: 'added' | 'removed' | 'stable';
    isZoomedOut: boolean;
    visibleTopIndices?: Record<string, number>;
    mergeNonces?: Record<string, number>;
    onZoomToCell?: (stackId: string, cellIndex: number) => void;
    /** The fanned-out stack's display span, or null. */
    fan: FanRange | null;
    /** The fanned (or last-fanned) real stack and its front column — the
     *  layout lands one exactly where the other was across a toggle. */
    fanAnchor: { realId: string; columnId: string } | null;
    /** Fan a stack out (the stack menu's Fan out item). */
    onFanOut?: (stackId: string) => void;
    /** Collapse the fanned stack (the border header's Collapse button). */
    onCollapseFan: () => void;
    /** Resolve a display id (fan columns included) to the real stack plus
     *  visible top index. Every scene-side selection write goes through
     *  it, so FocusContext only ever holds real stack ids. */
    onSelectStack: (displayId: string, displayCellIdx?: number) => void;
}

/** The board keeps a revealed card this far inside its region (the mock's
 *  revealBoard): 12px from the sides and the bottom, 8px from the top. */
const REVEAL = { side: 12, top: 8, bottom: 12, foot: 80 } as const;

/** A second click this soon after the first is a double click on the card
 *  the first one hit (the mock's lastClick). */
const DOUBLE_CLICK_MS = 800;

/** The overview card of a cell, inside the board. */
const cardElement = (board: HTMLElement | null, cellId: string) =>
    board?.querySelector<HTMLElement>(`[data-ov-card="${CSS.escape(cellId)}"]`) ?? null;

/** The name of a cell the overview knows: on the board, on the shelf, or in
 *  the lineage. */
function overviewCellName(s: CollimateState, cellId: string): string {
    for (const stack of s.activeChannel?.main_stacks ?? []) {
        const cell = stack.cells.find(c => String(c.id) === cellId);
        if (cell) return cell.name;
    }
    return s.overviewData.removed?.find(r => r.cell_id === cellId)?.name
        ?? s.overviewData.lineage?.cells.find(c => c.cell_id === cellId)?.name
        ?? '';
}

export function StackScene({
    stacks: sceneStacks, selectedId, actions,
    transitionType, topology, isZoomedOut,
    visibleTopIndices, mergeNonces, onZoomToCell,
    fan, fanAnchor, onFanOut, onCollapseFan,
    onSelectStack,
}: StackSceneProps) {
    // Field-level subscriptions — a whole-store subscription re-rendered the
    // entire scene on unrelated spatial writes (max-width flags, per-channel
    // toggles).
    const pendingMove = useSpatialStore(s => s.pendingMove);
    const cameraHold = useSpatialStore(s => s.cameraHold);
    // A re-frame request made while zoomed out (StackApp asks for one with
    // every cell selection, an overview card's included) never reaches the
    // camera: the overview reveals the card itself (revealCard), and the
    // zoom-in scrolls the selection into view only as far as it must (the
    // mock's reveal(selected())), not to its stack's centre.
    const storeCenterRequested = useSpatialStore(s => s.centerRequested);
    const [centerSeen, setCenterSeen] = useState(storeCenterRequested);
    const [centerRequested, setCenterRequested] = useState(storeCenterRequested);
    if (centerSeen !== storeCenterRequested) {
        setCenterSeen(storeCenterRequested);
        if (!isZoomedOut) setCenterRequested(storeCenterRequested);
    }
    const edgeAlignRequested = useSpatialStore(s => s.edgeAlignRequested);
    // Single shared width + per-column overrides (keyed by display id, fan
    // columns included).
    const minCellSize = useSpatialStore(s => s.minCellSize);
    const stackWidths = useSpatialStore(s => s.stackWidths);
    // Active width drag (display id, fan columns included, plus the edge
    // that stays put; null when idle). Layout pins that edge; camera parks —
    // together they keep the dragged edge under the pointer.
    const widthDrag = useSpatialStore(s => s.widthDrag);
    const widthDragActive = widthDrag !== null;
    // In the overview: what the journal and Only connected say about the
    // board. Only connected keeps the subject's connected cells on the board
    // and folds away the columns left empty; their cards do not drag then.
    const journalOpen = useCollimateStore(s => isZoomedOut && s.overview.journalOpen);
    const subject = useCollimateStore(s => (isZoomedOut ? subjectCellId(s) : null));
    const keepKey = useCollimateStore(s => {
        const keep = isZoomedOut ? connectedCells(s) : null;
        return keep ? [...keep].sort().join('\n') : null;
    });
    const keep = useMemo(() => (keepKey === null ? null : new Set(keepKey.split('\n'))), [keepKey]);
    const followCellId = useCollimateStore(s => (isZoomedOut && s.selectedCellId && boardFollowHint(s, s.selectedCellId)
        ? s.selectedCellId : null));
    const subjectName = useCollimateStore(s => (subject ? overviewCellName(s, subject) : ''));
    // A new kept set starts the board at its origin (the mock's `ck`): the
    // subject, or every cell after Clear.
    const keptSetKey = useCollimateStore(s => (isZoomedOut && connectedOn(s.overview)
        ? (s.overview.cleared ? '*' : subjectCellId(s) ?? '*') : null));
    const stacks = useMemo(
        () => (keep ? sceneStacks.filter(st => st.cells.some(c => keep.has(String(c.id)))) : sceneStacks),
        [keep, sceneStacks],
    );
    const journalCellId = journalOpen && subject && stacks.some(st => st.cells.some(c => String(c.id) === subject))
        ? subject : null;
    const overviewBoard = useMemo((): OverviewBoard | null => (isZoomedOut
        ? { keep, journalCellId, followCellId } : null), [isZoomedOut, keep, journalCellId, followCellId]);

    const rootDivRef = useRef<HTMLDivElement>(null);
    // The overview's whole area: the board and the overlays over it.
    const overviewRootRef = useRef<HTMLDivElement>(null);
    const surfaceOverlayRef = useRef<HTMLDivElement>(null);
    const cameraTargetRef = useRef(0);
    const { registerOverlay, bringCellsToFront, invalidate, markMoving, registerMotionLayer } = useGuestPool();
    const overlayRefCb = useCallback((el: HTMLDivElement | null) => {
        surfaceOverlayRef.current = el;
        registerOverlay(el);
    }, [registerOverlay]);
    // The camera's scale wrapper and pan container are promoted to their
    // own compositor layers only while the camera moves (lib/motionLayers).
    const motionLayerRef = useCallback(
        (el: HTMLDivElement | null) => (el ? registerMotionLayer(el) : undefined),
        [registerMotionLayer],
    );


    // The open Commands pane lies over one edge of the canvas. The main
    // view keeps the selected stack clear of it through the camera
    // (useStackCamera) and keeps a full-width stack inside what it leaves
    // free (screenMax). The overview insets its board by it instead (see
    // boardStyle); its zoom stays the user's.
    const paneInset = useCollimateStore(s => commandPaneInsetOf(s.commandPane));
    const boardInset = isZoomedOut ? paneInset : NO_INSET;
    const reduceMotion = useReducedMotion();

    // The drop gap the columns showed last frame (useStackDrag writes it):
    // a drop's result anchors on it, so the card lands where the gap was.
    const heldGapRef = useRef<HeldGap | null>(null);
    const layout = useStackLayout({
        stacks, selectedId, pendingMove, minCellSize, stackWidths, isZoomedOut, cameraTargetRef,
        widthDrag,
        rootRef: rootDivRef,
        fanAnchor,
        paneInset: isZoomedOut ? NO_INSET : paneInset,
        heldGapRef,
    });

    // Two-finger swipe (trackpad/touchscreen) steps the selection across
    // display columns (fan columns included); onSelectStack maps a fan
    // column back to its real stack + top index. The camera follows via
    // the ordinary selection-tracking effect.
    const handleSwipeNavigate = useCallback((delta: -1 | 1) => {
        const idx = stacks.findIndex(s => s.id === selectedId);
        if (idx === -1) return;
        const next = idx + delta;
        if (next < 0 || next >= stacks.length) return;
        onSelectStack(stacks[next].id);
    }, [stacks, selectedId, onSelectStack]);

    const camera = useStackCamera({
        layout, stacks, selectedId, isZoomedOut, topology, pendingMove,
        centerRequested, edgeAlignRequested, cameraHold,
        widthDragActive,
        cameraTargetRef, rootDivRef, wheelRootRef: overviewRootRef,
        onSwipeNavigate: handleSwipeNavigate,
    });

    // In the main view a change of the board's width (a window resize)
    // shifts every raw column position by half of it, which the pan
    // transform cancels in the same frame: the columns take their new x at
    // once. A spring there would trail the pan and wobble the whole board
    // against it. (The overview's columns keep their place through a
    // resize: useStackLayout.)
    const viewWidth = layout.viewSize.width;
    const seenViewWidthRef = useRef(viewWidth);
    // eslint-disable-next-line react-hooks/refs
    const viewWidthMoved = seenViewWidthRef.current !== viewWidth;
    useEffect(() => { seenViewWidthRef.current = viewWidth; }, [viewWidth]);

    const drag = useStackDrag({
        layout, cameraXMV: camera.cameraXMV, cameraYMV: camera.cameraYMV, zoomMV: camera.zoomMV,
        cameraTargetRef, commitCameraTarget: camera.commitCameraTarget, rootDivRef, stacks, fan, onSelectDisplay: onSelectStack,
        selectedId, actions, isZoomedOut,
        pendingMove, visibleTopIndices, heldGapRef,
    });

    // Stacks entering the scene stay invisible while the camera would
    // carry them across the screen (an add path selecting a stack whose
    // slot is out of view pans there), then fade in at their slot once it
    // has settled — see useEntryHold / lib/stackEntry. Their pooled
    // renderers are concealed for the same span.
    const entryHeld = useEntryHold({
        items: drag.items,
        sceneSwapNonce: layout.sceneSwapNonce,
        cameraXMV: camera.cameraXMV,
        zoomMV: camera.zoomMV,
        isCameraSettling: camera.isCameraSettling,
        viewWidth: layout.viewSize.width,

        gap: layout.GAP,
    });

    const handleRootPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        camera.handlePanPointerDown(e);
    }, [camera]);

    const handleRootPointerMove = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        camera.handlePanPointerMove(e);
        drag.handleDragPointerMove(e);
    }, [camera, drag]);

    const handleRootPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
        camera.handlePanPointerUp(e);
        drag.handleDragPointerUp(e);
    }, [camera, drag]);

    // The overview draws its own menus; a field on it (the Removed shelf's
    // search) keeps the browser's own (isEditableTarget).
    const handleContextMenu = useCallback((e: React.MouseEvent) => {
        if (isZoomedOut && !isEditableTarget(e.target)) e.preventDefault();
    }, [isZoomedOut]);

    const { viewSize, stackTopOffset, stackContainerHeight, cardPixelHeight, cardFrame } = layout;
    const { cameraXMV, cameraYMV, panX, panY, zoomMV,
        handleCenterPanStart, handleCenterPanMove, handleCenterPanEnd, panAlignSnap, centerPanning } = camera;
    const { dragGhost, ghostXMV, ghostYMV, dropTargetIdx, intraStackCellIdx, mergeTargetIdx,
        recentlyDroppedId, recentlyDroppedStackId, items, previewCellData, floatingCellData,
        arrowState, insertLineX, handleStackDragStart, handleCellDragStart } = drag;

    const ghostTagX = useTransform(ghostXMV, x => x + 14);
    const ghostTagY = useTransform(ghostYMV, y => y - 10);
    const floatingX = useTransform(ghostXMV, x => x - (floatingCellData?.previewMode === 'thumbnail'
        ? 140 : (dragGhost?.grabOffsetX ?? 0)));
    const floatingY = useTransform(ghostYMV, y => y - (floatingCellData?.previewMode === 'thumbnail'
        ? 140 : (dragGhost?.grabOffsetY ?? 0)));

    // Lift the surfaces hosting any "bumped" stack's cells to the top
    // of the world-space overlay's stacking context. Without this the
    // surfaces all sit at the same z (overlay carries z 110, children
    // at auto) and DOM-insertion order alone decides the painter's-
    // algorithm — the moving stack's renderer can paint *behind* a
    // neighbour's renderer just because the neighbour's surface was
    // allocated later. bringCellsToFront covers both kinds: a z bump for
    // the overlay's own elements, and a raiseGuest for each native guest
    // view, whose stacking order lives in the shell's view list rather
    // than in the DOM at all. The motion.div-level z bump (StackScene's
    // ``stackZ``) only lifts the cell *content*; surfaces live in a
    // separate DOM tree (#collimate-surface-overlay) and need their
    // own z bump.
    // settlingMoveSourceId carries the source-stack id across the post-commit
    // settle window for keyboard / Alt-arrow moves. pendingMove gets cleared
    // *before* commitPendingMove runs (to block double-commit on simultaneous
    // key releases), so it can't keep the bump alive while the motion.div
    // springs from old to new x — that's what this field is for.
    const settlingMoveSourceId = useSpatialStore(s => s.settlingMoveSourceId);
    const bumpedStackIds = useMemo(() => {
        const ids: string[] = [];
        if (dragGhost?.type === 'stack') ids.push(dragGhost.stackId);
        if (recentlyDroppedStackId) ids.push(recentlyDroppedStackId);
        if (pendingMove?.sourceId) ids.push(pendingMove.sourceId);
        if (settlingMoveSourceId) ids.push(settlingMoveSourceId);
        return ids;
    }, [dragGhost, recentlyDroppedStackId, pendingMove, settlingMoveSourceId]);

    useEffect(() => {
        const cellIds = new Set<string>();
        if (bumpedStackIds.length > 0) {
            const ids = new Set(bumpedStackIds);
            for (const stack of stacks) {
                if (!ids.has(stack.id)) continue;
                for (const cell of stack.cells) cellIds.add(cell.id);
            }
        }
        bringCellsToFront(cellIds);
    }, [bumpedStackIds, stacks, bringCellsToFront]);

    // A drag paints DOM (the dragged card, the ghost, the insert line)
    // above other cells' guests — which a native view makes impossible, so
    // every guest freezes to its placeholder for the drag's duration. The
    // camera is busy for most of a drag anyway; this just makes it a rule.
    useGuestOccluder('all', dragGhost !== null || pendingMove !== null);

    // Cell-diff hover zones (one per adjacent pair of display columns, see
    // lib/cellDiff) stand down while anything moves the columns under them:
    // a drag or pending move, a width drag, a center-pan, a resolve, and a
    // fan toggle's settle (the columns spring to their new slots while the
    // zones would already sit at the targets). The band under each card
    // comes from its measured header (CardChromeOverlays).
    const fanStackId = fan?.stackId ?? null;
    const [fanSettling, setFanSettling] = useState(false);
    const prevFanStackIdRef = useRef(fanStackId);
    useEffect(() => {
        if (prevFanStackIdRef.current === fanStackId) return;
        prevFanStackIdRef.current = fanStackId;
        setFanSettling(true);
        const t = window.setTimeout(() => setFanSettling(false), FAN_TOGGLE_SETTLE_MS);
        return () => window.clearTimeout(t);
    }, [fanStackId]);
    const columnsSettled = !isZoomedOut && !dragGhost && !pendingMove
        && !centerPanning && !fanSettling;
    const cellDiffZonesActive = columnsSettled && !widthDragActive;
    // Card-side resize strips stand down for the same motions, but stay up
    // through a width drag — one of them may own it (pointer capture).
    const edgeResizeActive = columnsSettled;
    // Measured card chrome is only kept for the cells on the canvas: drop
    // the rest (removed cells, another channel's) whenever the set changes.
    useEffect(() => {
        const ids = new Set<string>();
        for (const stack of sceneStacks) for (const cell of stack.cells) ids.add(String(cell.id));
        useSpatialStore.getState().pruneCellChrome(ids);
    }, [sceneStacks]);

    const allCallbacks = useStackCallbacks({
        stacks, actions, fan, isZoomedOut, onFanOut, onZoomToCell, onSelectStack,
        handleStackDragStart, handleCellDragStart, handleCenterPanStart, handleCenterPanMove, handleCenterPanEnd,
    });
    // A board Only connected filters is a reading of the lineage: its
    // columns and cards do not drag (a press on a card pans instead).
    const stackCallbacks = useMemo(() => (keep
        ? new Map([...allCallbacks].map(([id, cb]) => [id, { ...cb, onStackDragStart: undefined, onCellDragStart: undefined }]))
        : allCallbacks), [keep, allCallbacks]);

    // ── The overview's camera moves ─────────────────────────────────────
    const { cameraXMV: camX, cameraYMV: camY, zoomMV: camZoom } = camera;
    const { viewSize: boardSize, overviewBounds, overviewTarget } = layout;
    // The entry fit (useStackCamera springs to overviewTarget as the zoom flips)
    // is computed against the board's main-view size; the overview's board
    // region (under the bar and the shelf, left of an open journal pane)
    // is measured a frame later. While the entry spring runs, every new
    // fit re-aims it, so the board lands fitted to its region (the mock's
    // initView).
    useLayoutEffect(() => {
        if (!isZoomedOut || !(camZoom.isAnimating() || camX.isAnimating() || camY.isAnimating())) return;
        animate(camX, overviewTarget.x, { ...PHYSICS.standard });
        animate(camY, overviewTarget.y, { ...PHYSICS.standard });
        animate(camZoom, overviewTarget.zoom, { ...PHYSICS.standard });
    }, [overviewTarget, isZoomedOut, camX, camY, camZoom]);
    const stepZoom = useCallback((factor: number) => zoomCameraAt(
        { cameraXMV: camX, cameraYMV: camY, zoomMV: camZoom }, boardSize,
        boardSize.width / 2, boardSize.height / 2, factor,
    ), [camX, camY, camZoom, boardSize]);
    const fitBoard = useCallback(() => {
        const fit = overviewFit(overviewBounds, boardSize, false);
        camZoom.set(fit.zoom);
        camX.set(fit.x);
        camY.set(fit.y);
    }, [camX, camY, camZoom, overviewBounds, boardSize]);
    /** Keep a card inside the board region, moving the board only as far
     *  as it must (the mock's revealBoard). */
    const revealCard = useCallback((cellId: string): boolean => {
        const board = rootDivRef.current;
        const el = cardElement(board, cellId);
        if (!board || !el) return false;
        const r = el.getBoundingClientRect();
        const b = board.getBoundingClientRect();
        let dx = 0;
        let dy = 0;
        if (r.right > b.right - REVEAL.side) dx = b.right - REVEAL.side - r.right;
        if (r.left + dx < b.left + REVEAL.side) dx = b.left + REVEAL.side - r.left;
        if (r.top < b.top + REVEAL.top) dy = b.top + REVEAL.top - r.top;
        else if (r.top > b.bottom - REVEAL.bottom - REVEAL.foot) dy = b.bottom - REVEAL.bottom - REVEAL.foot - r.top;
        if (dx || dy) {
            const z = camZoom.get();
            camX.set(camX.get() - dx / z);
            camY.set(camY.get() - dy / z);
        }
        return true;
    }, [camX, camY, camZoom]);

    // A selection made elsewhere than on its card (a restore, a name in the
    // journal, the keyboard) reveals the card; a click on a card does not
    // move the board under the pointer. The card may land a render later
    // (a restore), so the target waits for it.
    const pendingSelect = useCollimateStore(s => s.pendingSelectCellId);
    const pressedCardRef = useRef<string | null>(null);
    const [revealTarget, setRevealTarget] = useState<string | null>(null);
    const [prevPendingSelect, setPrevPendingSelect] = useState(pendingSelect);
    if (prevPendingSelect !== pendingSelect) {
        setPrevPendingSelect(pendingSelect);
        if (isZoomedOut && pendingSelect) setRevealTarget(pendingSelect);
    }
    useEffect(() => {
        if (!revealTarget) return;
        if (pressedCardRef.current === revealTarget) {
            pressedCardRef.current = null;
            setRevealTarget(null);
            return;
        }
        const raf = requestAnimationFrame(() => { if (revealCard(revealTarget)) setRevealTarget(null); });
        return () => cancelAnimationFrame(raf);
    }, [revealTarget, stacks, revealCard]);
    // The journal's subject comes into view when it changes (its card grows
    // into journal style first).
    const revealedSubjectRef = useRef<string | null>(null);
    useEffect(() => {
        if (journalCellId === revealedSubjectRef.current) return;
        revealedSubjectRef.current = journalCellId;
        if (!journalCellId) return;
        const raf = requestAnimationFrame(() => revealCard(journalCellId));
        return () => cancelAnimationFrame(raf);
    }, [journalCellId, revealCard]);
    // A double click acts on the card its first click hit, even when that
    // click moved the board (the card grew into journal style, the board
    // revealed it): with Only connected off it opens the cell in the main
    // view; with it on it is the two clicks that follow the cell's lines.
    // The second click is the double click's alone, whatever lies under it.
    const lastClickRef = useRef<{ cellId: string; t: number; followed: boolean } | null>(null);
    const zoomIntoCell = useCallback((cellId: string) => {
        const stack = stacks.find(st => st.cells.some(c => String(c.id) === cellId));
        if (!stack) return;
        stackCallbacks.get(stack.id)?.onZoomToCell?.(stack.cells.findIndex(c => String(c.id) === cellId));
    }, [stacks, stackCallbacks]);
    const handleBoardClickCapture = useCallback((e: React.MouseEvent) => {
        const last = lastClickRef.current;
        lastClickRef.current = null;
        if (e.detail > 1 && last && performance.now() - last.t < DOUBLE_CLICK_MS) {
            e.stopPropagation();
            e.preventDefault();
            const store = useCollimateStore.getState();
            if (!connectedOn(store.overview)) zoomIntoCell(last.cellId);
            else if (!last.followed) store.anchorTo(last.cellId);
            return;
        }
        const target = e.target as Element;
        if (target.closest('button, input, [data-overview-overlay]')) return;
        const cellId = target.closest<HTMLElement>('[data-ov-card]')?.dataset.ovCard;
        if (!cellId) return;
        // Whether this first click follows the card's lines itself (read
        // before the card's own click acts on it).
        const store = useCollimateStore.getState();
        lastClickRef.current = {
            cellId, t: performance.now(), followed: !connectedOn(store.overview) || armedBoard(store, cellId),
        };
    }, [zoomIntoCell]);

    // A new kept set starts at the board's top-left, at the current zoom.
    const keptSetRef = useRef<string | null>(null);
    useLayoutEffect(() => {
        if (keptSetKey === keptSetRef.current) return;
        keptSetRef.current = keptSetKey;
        if (keptSetKey === null) return;
        const z = camZoom.get();
        camX.set(overviewBounds.left + boardSize.width / (2 * z));
        camY.set(overviewBounds.top - boardSize.height / 2 + boardSize.height / (2 * z));
    }, [keptSetKey, overviewBounds, boardSize, camX, camY, camZoom]);

    // --- Guest-pool sync-loop wake sources ---
    //
    // The pool's per-frame position sync parks itself once nothing has
    // moved for a few frames (see lib/guestSyncScheduler), so anything
    // that moves a cell has to say so. Both effects below sit above the
    // viewSize early return because hooks can't be conditional.

    // Camera motion promotes the scene's motion layers and invalidates the
    // pooled guests' measured positions. A spring announces itself as it
    // starts, before its first frame; a direct write (a drag, the wheel)
    // with its change, still ahead of the frame that renders it. Only the
    // three source motion values are subscribed — panX/panY are derived
    // from them and would just fire twice. The values are stable for the
    // camera's life; the camera object identity is not, hence the
    // destructured locals in the dep array.
    useEffect(() => {
        const unsubscribe = [cameraXMV, cameraYMV, zoomMV].flatMap(value => [
            value.on('animationStart', markMoving),
            value.on('change', markMoving),
        ]);
        return () => { for (const off of unsubscribe) off(); };
    }, [cameraXMV, cameraYMV, zoomMV, markMoving]);

    // The pan lands on whole device pixels at the camera's rest zoom
    // (lib/deviceOrigin). Zoomed, the snap would not land on the grid, and
    // the pan that holds the board still against the scale wrapper's moving
    // origin (a pane sliding over the overview) jittered by its rounding.
    const panTransform = useCallback(
        (values: unknown, generated: string) =>
            Math.abs(zoomMV.get() - 1) < 1e-3 ? deviceSnappedTransform(values, generated) : generated,
        [zoomMV],
    );

    // Every commit, deliberately without a dep array. This is the
    // keystone wake source: every declarative spring in the scene — the
    // stack's x/width, StackGroup's layout, StackItem's card motion —
    // begins with a React commit that changes a motion prop, and there
    // is no event, no callback and no store write at the moment the
    // spring starts moving pixels. Waking here catches the first frame,
    // and because the loop stops on quiescence rather than on a timer,
    // one wake covers the spring's entire settle however long it runs.
    // The case that has nothing else: a stack springing in x under
    // cameraHold, where the camera never moves and the pool is never
    // touched.
    useEffect(() => { invalidate(); });

    // The floating drag preview's one-cell stack, kept by reference while
    // the dragged cell is unchanged so StackGroup's memo holds across the
    // per-pointermove re-renders of a cell drag.
    const floatingCell = floatingCellData?.cell;
    const floatingPreviewStack = useMemo((): Stack | null => floatingCell
        ? { id: FLOATING_PREVIEW_STACK_ID, name: '', cells: [floatingCell] }
        : null, [floatingCell]);

    // The board's viewport: the whole canvas in the main view; in the
    // overview, the region under the Removed shelf (its animated height,
    // `--ov-shelf`, written on the overview root) and bar, left of the
    // journal pane, and clear of the Commands pane on its side (its
    // margins, which slide as the pane opens, closes or docks across: the
    // world stays put (useStackLayout) and the camera's resize compensation
    // (usePanZoomCamera) keeps the board's content against the region's
    // left edge at the same zoom, frame by frame, so a pane docked left
    // slides it right by the pane's width and one docked right leaves it
    // where it is, its far part past the region's edge until the user
    // pans). It paints no ground and no edge of its own: the canvas behind
    // it runs on under the pane, whose hairline is the only edge. Nor does
    // it cut its cards off at its sides: they run on under the pane and the
    // journal (both opaque, above it) to the overview's own edge, so the
    // strip a docking-across or closing pane bares mid-slide shows the
    // cards sliding in, not a clipped edge. Every camera and drag
    // measurement is against it.
    //
    // Either way the board's top-left, the camera's layout origin, sits on
    // a whole device pixel: alignToDevicePixels shifts it right and down by
    // under one (ORIGIN_DX_VAR, ORIGIN_DY_VAR) and the far edges by as much,
    // so its size is unchanged. Otherwise text painted while the camera's
    // layers are promoted lands a device pixel off the same text at rest
    // (at 1.25× the 70px top bar is 87.5 device px), and settles by it at
    // the end of every camera move.
    const shiftX = `var(${ORIGIN_DX_VAR}, 0px)`;
    const shiftY = `var(${ORIGIN_DY_VAR}, 0px)`;
    const boardStyle: React.CSSProperties = isZoomedOut
        ? {
            position: 'absolute', left: shiftX, bottom: `calc(-1 * ${shiftY})`, overflowX: 'visible', overflowY: 'clip',
            userSelect: 'none', cursor: 'grab', touchAction: 'none',
            top: `calc(var(--ov-shelf, 0px) + ${OVERVIEW_BAR_HEIGHT}px + ${shiftY})`,
            right: `calc(${journalOpen ? JOURNAL_PANE_WIDTH : 0}px - ${shiftX})`,
            marginLeft: boardInset.left,
            marginRight: boardInset.right,
            transition: paneInsetTransition(['margin-left', 'margin-right'], reduceMotion),
        }
        : {
            position: 'absolute', overflow: 'hidden',
            top: shiftY, left: shiftX, bottom: `calc(-1 * ${shiftY})`, right: `calc(-1 * ${shiftX})`,
        };
    useLayoutEffect(() => {
        const board = rootDivRef.current;
        return board ? alignToDevicePixels(board) : undefined;
    }, []);

    // Pre-measurement frame: useStackLayout sizes the scene against the
    // board div (ResizeObserver), so the div must exist before the first
    // real render. Same element types/positions as the full tree below
    // (the context provider included), so React keeps the DOM nodes — and
    // the measurement — when it fills in.
    if (viewSize.width === 0) {
        return (
            <div ref={overviewRootRef} data-overview-root={isZoomedOut || undefined} style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}>
                <OverviewBoardContext.Provider value={overviewBoard}>
                    <div ref={rootDivRef} style={boardStyle} />
                </OverviewBoardContext.Provider>
            </div>
        );
    }

    return (
        <div ref={overviewRootRef} data-overview-root={isZoomedOut || undefined} style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}>
        <OverviewBoardContext.Provider value={overviewBoard}>
        <div
            ref={rootDivRef}
            data-overview-board={isZoomedOut || undefined}
            {...{ [CAMERA_ORIGIN_ATTR]: '' }}
            style={boardStyle}
            onPointerDownCapture={(e) => {
                pressedCardRef.current = (e.target as Element).closest<HTMLElement>('[data-ov-card]')?.dataset.ovCard ?? null;
            }}
            onClickCapture={isZoomedOut ? handleBoardClickCapture : undefined}
            onPointerDown={handleRootPointerDown}
            onPointerMove={handleRootPointerMove}
            onPointerUp={handleRootPointerUp}
            onContextMenu={handleContextMenu}
        >
            {/* Scale wrapper — handles zoom-out by scaling from viewport
                center: the centre the camera measured (viewSize), the one
                panX is built on, not the box's live one. The frame a resize
                lands before React has re-measured (the Commands pane's
                margins sliding, the shelf opening) then keeps everything
                where it was against the region's top-left, as the camera's
                resize compensation does once it has measured. */}
            <motion.div
                ref={motionLayerRef}
                className="absolute inset-0"
                style={{
                scale: zoomMV,
                transformOrigin: `${viewSize.width / 2}px ${viewSize.height / 2}px`,
            }}>
                {/* Pan container — translateX/Y to pan through world space,
                    on whole device pixels at rest zoom (panTransform) so the
                    board under it stays on the grid its origin is aligned to. */}
                <motion.div
                    ref={motionLayerRef}
                    className="absolute top-0 left-0 h-full w-full"
                    transformTemplate={panTransform}
                    style={{
                    x: panX,
                    y: panY,
                }}>
                    {/* World-space surface overlay — the embedded surfaces
                        live here (renderer iframes, native-guest placeholders)
                        so the compositor handles pan/zoom transforms for free.
                        No height, so it covers nothing; its width is the
                        pool's ruler for the camera's scale (overlayScale).
                        Stable ref via useCallback so registerOverlay doesn't
                        re-fire on every render (inline arrow would change
                        identity and React would call it null→el each render). */}
                    <div
                        ref={overlayRefCb}
                        id="collimate-surface-overlay"
                        style={{
                            position: 'absolute',
                            top: 0,
                            left: 0,
                            width: OVERLAY_RULER_PX,
                            height: 0,
                            overflow: 'visible',
                            pointerEvents: 'none',
                            zIndex: 110,
                        }}
                    />
                    {(() => {
                        // Stack motion.div renderer. Fan columns (stack.synthetic)
                        // render OUTSIDE AnimatePresence so React unmounts them
                        // the moment they leave items — no animation system to
                        // wait on. Inside AnimatePresence with
                        // `transition: { duration: 0 }` was unreliable: framer-motion
                        // didn't always fire onAnimationComplete, leaving synthetic
                        // StackGroups mounted at their fan positions with
                        // viewMode='stack' frozen. That showed up as ghost renderers
                        // floating in the background, click dead-zones in zoom-out
                        // (stuck StackGroup renders a CellEditor instead of the
                        // overview card), and main-view clicks landing on a stuck
                        // column's stale callbacks (which resolve a fan id that no
                        // longer exists once the stack collapses).
                        //
                        // Real (non-synthetic) stacks keep AnimatePresence and the
                        // original spring fade-and-shrink exit unchanged.
                        const renderStackItem = (item: typeof items[number], i: number) => {
                            // Per-stack z-index. The world-space surface
                            // overlay (#collimate-surface-overlay) sits at
                            // z 110 and paints its surfaces above any
                            // cell-local z. A stack must lift its z above
                            // 110 to:
                            //   - render its key-entry modal (a cell-scoped
                            //     overlay) on top of its own surface,
                            //   - pass over neighbouring static stacks
                            //     during a drag, post-drop animation, or
                            //     pendingMove preview.
                            // Each stack's slot is its own stacking
                            // context (StackSlot) so DOM order alone won't
                            // lift it.
                            //   - dragged stack:                  z 1000
                            //   - cell needs key entry:           z  800
                            //   - recently-dropped stack (settle): z  600
                            //   - pending-move / settling source:  z  500
                            //   - selected (no motion):           z  100  (under surface overlay; ok)
                            //   - everything else:                auto
                            // Note: version-history is NOT in this list. It
                            // is a files-pane mode inside the cell body, so it
                            // sits below the surface overlay like any other
                            // cell content — no z bump needed.
                            const isDragging = dragGhost?.type === 'stack' && dragGhost.stackId === item.id;
                            const isMovingSource = (pendingMove?.sourceId === item.id)
                                || settlingMoveSourceId === item.id;
                            const isRecentlyDroppedStack = recentlyDroppedStackId === item.id;
                            const stackZ = isDragging ? 1000
                                : isRecentlyDroppedStack ? 600
                                : isMovingSource ? 500
                                : item.id === selectedId ? 100
                                : undefined;
                            // A held entry mounts (and waits) at opacity 0
                            // on its final slot; releasing it plays the
                            // ordinary fade-in right there.
                            const held = entryHeld.has(item.id);
                            return (
                                <RendererConcealContext.Provider key={item.id} value={held}>
                                <StackSlot
                                    style={{
                                        position: 'absolute',
                                        // The slot hangs stackTopOffset below
                                        // the camera's origin by a transform,
                                        // not by `top`: promoted (a motion
                                        // layer), its layout offset is rounded
                                        // to the device pixel, a transform's
                                        // translation never is. 14px is 17.5
                                        // device px at 1.25×, and as `top` it
                                        // put every card's text half a device
                                        // pixel lower while the camera moved,
                                        // a row lower for some lines, settling
                                        // back at rest.
                                        top: 0,
                                        y: stackTopOffset,
                                        left: 0,
                                        height: isZoomedOut
                                            ? overviewColumnHeight(keep ? item.cells.filter(c => keep.has(String(c.id))).length : item.cells.length)
                                            : stackContainerHeight,
                                        zIndex: stackZ,
                                    }}
                                    initial={{
                                        x: item.x - item.computedWidth / 2,
                                        width: item.computedWidth,
                                        opacity: 0,
                                    }}
                                    animate={{
                                        x: item.x - item.computedWidth / 2,
                                        width: item.computedWidth,
                                        opacity: held || (pendingMove?.type === 'cell' && item.id === pendingMove.sourceId && item.cells.length === 1) ? 0 : 1,
                                        // Explicit so a cancelled exit re-animates back to 1.
                                        // exit sets scale: 0.92; without a matching animate
                                        // target, framer-motion has nothing to interpolate
                                        // toward when the same key returns mid-exit (a
                                        // fan-out collapsed before the real stack's exit
                                        // finished), and the
                                        // motion.div stays stuck at scale(0.92), shrinking
                                        // the cell within its container.
                                        scale: 1,
                                        // Non-animatable — applied instantly. Restores
                                        // hit-testing when the same key returns mid-exit.
                                        pointerEvents: 'auto',
                                    }}
                                    // pointerEvents off for the whole exit: a fading
                                    // (or, if an exit ever wedges again, a stuck-
                                    // invisible) stack must never intercept clicks
                                    // aimed at what renders beneath its footprint —
                                    // e.g. the fan columns that replace it.
                                    exit={{ opacity: 0, scale: 0.92, pointerEvents: 'none' }}
                                    transition={{
                                        x: transitionType === 'cell' ? PHYSICS.movePlace : (widthDragActive || viewWidthMoved ? SNAP : PHYSICS.resize),
                                        // Width must ride the same spring as x: the stack's
                                        // left edge is x - width/2, so mismatched springs
                                        // make both edges wobble during a resize. While a
                                        // width handle is being dragged both snap instead —
                                        // a ~200ms spring settle leaves the stack edge
                                        // trailing the pointer. It applies to every stack,
                                        // not just the dragged one: left-edge anchoring
                                        // shifts everything to its right on each pointermove.
                                        width: transitionType === 'cell' ? PHYSICS.movePlace : (widthDragActive || viewWidthMoved ? SNAP : PHYSICS.resize),
                                        opacity: transitionType === 'cell' ? PHYSICS.movePlace : PHYSICS.navSlide,
                                        scale: PHYSICS.navSlide,
                                    }}
                                >
                                    <StackGroup
                                        stack={item}
                                        width={item.computedWidth}
                                        height={cardPixelHeight}
                                        isSelected={selectedId === item.id}
                                        viewMode={isZoomedOut ? 'overview' : 'stack'}
                                        isOffscreen={item.isOffscreen}
                                        // Real-stack position for the head's move
                                        // arrows (a fanned group counts once);
                                        // fan columns have no head.
                                        stackIndex={item.synthetic ? i : displayToReal(fan, i).realIdx}
                                        stackCount={item.synthetic ? items.length : realStackCount(items.length, fan)}
                                        // The front slot (lib/frontSlots). A pending
                                        // cell move's source (a cell, never the
                                        // manage card) peeks its next cell.
                                        visibleTopIndex={pendingMovePeekSlot(item, pendingMove, visibleTopIndices)}
                                        callbacks={stackCallbacks.get(item.id) ?? NO_OP_STACK_CALLBACKS}
                                        previewInsertCellIdx={isZoomedOut && dropTargetIdx === i ? intraStackCellIdx : null}
                                        hiddenCellId={
                                            (dragGhost?.type === 'cell' && item.id === dragGhost.stackId) ? dragGhost.cellId : null
                                        }
                                        recentlyDroppedId={recentlyDroppedId}
                                        mergeNonce={mergeNonces?.[item.id] ?? 0}
                                    />
                                    {/* Merge-target overlay — lights up the
                                        whole column while a stack is being
                                        dragged onto it, making clear that
                                        letting go will merge-and-delete the
                                        source. Non-interactive, z-index just
                                        above the stack's own content. */}
                                    {mergeTargetIdx === i && (
                                        <div
                                            className="absolute inset-0 pointer-events-none rounded-md border-2 border-teal bg-teal-soft"
                                            style={{ zIndex: 120 }}
                                        />
                                    )}
                                </StackSlot>
                                </RendererConcealContext.Provider>
                            );
                        };
                        return (
                            <>
                                {/* Synthetic stacks: bare render, no AnimatePresence.
                                    React unmounts them on the next render after they
                                    leave the items list. */}
                                {items.map((item, i) => item.synthetic ? renderStackItem(item, i) : null)}
                                {/* Real stacks: animated entry/exit via AnimatePresence. */}
                                <AnimatePresence>
                                    {items.map((item, i) => item.synthetic ? null : renderStackItem(item, i))}
                                </AnimatePresence>
                            </>
                        );
                    })()}

                    {/* The fan's bracket over its columns, in their head band.
                        Outside AnimatePresence: it unmounts with the columns
                        the moment the stack collapses. Follows the items (so
                        the insert-drag shift), spans exactly the columns'
                        cards like a stack head (the mock's .fan-head), and
                        widens over an in-fan drop slot, its line lit while the target sits inside. The
                        camera motion values keep its name and Collapse inside
                        its on-screen part. */}
                    {fan && !isZoomedOut && (() => {
                        const gap = previewCellData?.inFan
                            ? { x: previewCellData.x, width: previewCellData.width, inFan: true }
                            : null;
                        const ext = fanFrameExtent(items, gap, 0);
                        if (!ext) return null;
                        const t = transitionType === 'cell' ? PHYSICS.movePlace : (widthDragActive || viewWidthMoved ? SNAP : PHYSICS.resize);
                        const columns = stacks.filter(st => st.synthetic);
                        return (
                            <FanFrame
                                key={`fan-frame-${fan.stackId}`}
                                left={ext.left}
                                width={ext.right - ext.left}
                                top={stackTopOffset}
                                name={fan.name}
                                count={fan.count}
                                cellIds={columns.map(st => String(st.cells[0]?.id ?? ''))}
                                selected={columns.some(st => st.id === selectedId)}
                                targeted={!!previewCellData?.inFan}
                                transition={t}
                                onCollapse={onCollapseFan}
                                onSelectStack={() => onSelectStack(fan.stackId)}
                                onRename={(name) => actions.renameStack(fan.stackId, name)}
                                onDuplicateStack={() => actions.duplicateStack(fan.stackId)}
                                onMergeStack={() => actions.mergeStack(fan.stackId)}
                                onRemove={() => actions.winClose(fan.stackId)}
                                // The bracket pans like a stack head. No
                                // stack id: a fan has no single column to
                                // align-snap the camera to at the release.
                                onPanStart={(e) => handleCenterPanStart(e)}
                                onPanMove={handleCenterPanMove}
                                onPanEnd={handleCenterPanEnd}
                                cameraX={cameraXMV}
                                zoom={zoomMV}
                                viewportWidth={viewSize.width}
                            />
                        );
                    })()}

                    <SceneDragOverlays previewCellData={previewCellData} insertLineX={insertLineX}
                        dragGhost={dragGhost} items={items} arrowState={arrowState}
                        stackTopOffset={stackTopOffset} panAlignSnap={panAlignSnap} cardFrame={cardFrame}
                        visibleTopIndices={visibleTopIndices} gap={layout.GAP} screenMax={layout.screenMax}
                        zoomMV={zoomMV} edgeResizeActive={edgeResizeActive}
                        cellDiffZonesActive={cellDiffZonesActive} onSelectStack={onSelectStack} />
                </motion.div>
            </motion.div>

            {keep && stacks.length === 0 && (
                <div className="absolute left-[1.125rem] top-4 whitespace-nowrap py-1.5 text-[0.8125rem] text-ink-3">
                    No cell on the board is connected to {subjectName}.
                </div>
            )}

            <SceneCursorOverlays floatingCellData={floatingCellData} dragGhost={dragGhost}
                floatingPreviewStack={floatingPreviewStack} floatingX={floatingX} floatingY={floatingY}
                ghostTagX={ghostTagX} ghostTagY={ghostTagY} />
        </div>
        </OverviewBoardContext.Provider>
        {/* Over the board: the zoom control at its bottom-right, the
            Removed shelf and its bar above it, the journal pane on the
            right and the lineage arrows over all of them. Each keeps its
            content clear of the Commands pane on its own. */}
        {isZoomedOut && (
            <>
                <OverviewZoom zoom={zoomMV} onStep={stepZoom} onFit={fitBoard}
                    right={15 + (journalOpen ? JOURNAL_PANE_WIDTH : 0)} paneRight={paneInset.right} />
                <RemovedShelf />
                <RemovedBar />
                <JournalPane />
                <OverviewArrows />
            </>
        )}
        </div>
    );
}
