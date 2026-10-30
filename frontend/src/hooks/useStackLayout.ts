import { useState, useEffect, useMemo, useRef } from 'react';
import type { Stack } from '@/lib/types';
import { PendingMoveState, WidthDrag } from '@/stores/useSpatialStore';
import { layoutAlias } from '@/lib/fanOut';
import { chooseLayoutAnchor, takeSelectionHandOff } from '@/lib/stackEntry';
import { NO_INSET, type ViewInset } from '@/lib/commandPane';
import {
    OVERVIEW_CARD_HEIGHT, OVERVIEW_COLUMN_WIDTH, OVERVIEW_GAP, OVERVIEW_ZOOM,
} from '@/stores/collimate/overviewSlice';

/** The canvas's top padding above the stacks. The scene region already
 *  starts under the top bar. */
export const CANVAS_TOP_PADDING = 15;

/** Height of the per-stack head above the cells in both views.
 *  useStackLayout, StackGroup, StackItem and StackScene all read it. */
export const STACK_HEAD_HEIGHT = 33;

/** Height of a fanned-out stack's bracket (`.fan-head`: name,
 *  Collapse, menu). It stands in the head band over the fan's columns, which
 *  have no head of their own, so their cards line up with every other
 *  stack's. */
export const FAN_HEADER_HEIGHT = STACK_HEAD_HEIGHT;
/** A drop this far past a fan's outer columns (inside the stack gap) still
 *  lands in the fan. The bracket itself spans exactly the columns' cards. */
export const FAN_FRAME_INSET = 5;

// ── The overview (zoom-out) board ─────────────────────────────────────
// Every stack is a fixed-width column under its head, with fixed-height
// cards and a gap in the stack's own order. The world adds outer padding.

/** A card's step down its column: its height and the gap under it. */
export const OVERVIEW_CARD_PITCH = OVERVIEW_CARD_HEIGHT + OVERVIEW_GAP;
/** The overview world's padding (the mock's `.ov-world` padding). */
export const OVERVIEW_WORLD_PADDING = { top: CANVAS_TOP_PADDING, side: OVERVIEW_GAP, bottom: 53 } as const;

/** World y of the top of the card in display slot `slot` of a column (cards
 *  at rest; a journal-style card above it pushes it further down). */
export const overviewCardTop = (slot: number): number =>
    OVERVIEW_WORLD_PADDING.top + STACK_HEAD_HEIGHT + slot * OVERVIEW_CARD_PITCH;

/** A column's height: its head and its cards at rest. */
export const overviewColumnHeight = (cards: number): number =>
    STACK_HEAD_HEIGHT + Math.max(0, cards * OVERVIEW_CARD_PITCH - OVERVIEW_GAP);

/** The board's world extent: the columns and the world padding. */
export interface OverviewBounds { left: number; right: number; top: number; bottom: number }

/** The camera ({x, y, zoom}, StackScene's centre-origin transform) that fits
 *  the board into a view of `view` px (the mock's fitView): the zoom fits
 *  the width (and the height unless `widthOnly`), never above 100% nor
 *  below the overview's minimum; the board is centred where it is smaller
 *  than the view and starts at the view's top-left where it is not
 *  (`widthOnly` always starts at the top). */
export function overviewFit(
    bounds: OverviewBounds, view: { width: number; height: number }, widthOnly: boolean,
): { x: number; y: number; zoom: number } {
    const w = Math.max(1, bounds.right - bounds.left);
    const h = Math.max(1, bounds.bottom - bounds.top);
    const fit = Math.min(1, view.width / w, widthOnly ? Infinity : view.height / h);
    const zoom = Math.max(OVERVIEW_ZOOM.min, fit);
    const sx = Math.max(0, (view.width - w * zoom) / 2);
    const sy = widthOnly ? 0 : Math.max(0, (view.height - h * zoom) / 2);
    // Screen = view/2 + zoom * (world - camera) horizontally and
    // view/2 + zoom * (world - camera - view/2) vertically: solve for the
    // camera that puts the board's top-left at (sx, sy).
    return {
        x: bounds.left - (sx - view.width / 2) / zoom,
        y: bounds.top - view.height / 2 - (sy - view.height / 2) / zoom,
        zoom,
    };
}

/** Where a front card sits vertically in the main view, in world px: under
 *  the scene's top offset and the head band (a stack's head, or a fan's
 *  bracket). The one source for every overlay drawn against a card (the
 *  pan-align glow, the resize strips, the diff zones, the drop preview). */
export function cardSpan(
    frame: { stackTopOffset: number; stripHeight: number; cardPixelHeight: number },
): { top: number; height: number } {
    return { top: frame.stackTopOffset + frame.stripHeight, height: frame.cardPixelHeight };
}

/** A laid-out display column's front card, in world px (see cardSpan). */
export function columnCardRect(
    column: { x: number; computedWidth: number },
    frame: { stackTopOffset: number; stripHeight: number; cardPixelHeight: number },
): { left: number; right: number; top: number; bottom: number } {
    const { top, height } = cardSpan(frame);
    const half = column.computedWidth / 2;
    return { left: column.x - half, right: column.x + half, top, bottom: top + height };
}

/** Horizontal gap between stacks — also the row's inset from each screen
 *  edge (see startX / screenMax below). */
export const STACK_GAP = 20;

/** Narrowest column width. Every width the layout renders, and every
 *  width a control writes, is clamped to [MIN_STACK_WIDTH, screenMax]. */
export const MIN_STACK_WIDTH = 330;

/** Largest stack width for a given viewport width — the layout's
 *  ``screenMax`` (a STACK_GAP inset on each side). Every "max width"
 *  producer (Alt+F, the isMaxWidth viewport tracker) must derive from
 *  this: a max-width stack centered at this size puts its neighbors
 *  exactly at the screen edges. Any larger margin (the old
 *  ``innerWidth - 40``) leaves margin − STACK_GAP pixels of each
 *  neighbor peeking through. */
export const maxStackWidthFor = (viewWidth: number): number =>
    Math.max(MIN_STACK_WIDTH, viewWidth - 2 * STACK_GAP);

export interface StackLayoutResult {
    viewSize: { width: number; height: number };
    GAP: number;
    FRONT_CARD_OFFSET: number;
    positions: number[];
    widths: number[];
    naturalWidths: number[];
    arrowWorldX: number | null;
    isCancelMove: boolean;
    cellPendingMoveWorldX: number | null;
    cardPixelHeight: number;
    stackContainerHeight: number;
    stackTopOffset: number;
    /** Where the cards sit (columnCardRect / cardSpan): shared by the
     *  scene's overlays and the drag's drop preview. */
    cardFrame: { stackTopOffset: number; stripHeight: number; cardPixelHeight: number };
    /** The overview board's world extent (zoom-out). */
    overviewBounds: OverviewBounds;
    /** Where zoom-out lands: the board fitted to the view's width, from its
     *  top (the mock's initView). */
    overviewTarget: { x: number; y: number; zoom: number };
    /** Largest cell width that fits the viewport the Commands pane leaves
     *  free (the full width) — the clamp on every column and the width
     *  surfaces' dynamic max. */
    screenMax: number;
    /** Bumped whenever the stack set is replaced wholesale (channel
     *  switch). World coordinates are rebased at that
     *  moment (layout offset reset), so useStackCamera hard-lands the camera
     *  on the selection instead of springing from a stale position. */
    sceneSwapNonce: number;
}

interface UseStackLayoutParams {
    stacks: Stack[];
    selectedId: string | null;
    pendingMove: PendingMoveState | null;
    minCellSize: number;
    /** Per-stack width overrides (keyed by stack id). A stack with no
     *  entry falls back to ``minCellSize``. */
    stackWidths: Record<string, number>;
    isZoomedOut: boolean;
    cameraTargetRef: React.MutableRefObject<number>;
    /** Column whose width is mid-drag and its anchored edge (see
     *  useSpatialStore). Width changes pin that edge instead of the
     *  selected stack's center, so the dragged edge tracks the pointer 1:1. */
    widthDrag?: WidthDrag | null;
    /** The scene's root container. All viewport math sizes against this
     *  element, NOT the window: in the desktop shell a title bar sits above
     *  the scene (StackApp's flex column), so window.innerHeight
     *  over-measures and would push the cards' bottom edge past the
     *  window edge. In a browser tab the two are identical. */
    rootRef: React.RefObject<HTMLElement | null>;
    /** The fanned (or last-fanned) real stack and its front fan column.
     *  Across a fan toggle the one that appears lands exactly where the
     *  other was (see the layout-shift compensation below). */
    fanAnchor?: { realId: string; columnId: string } | null;
    /** The space a surface docked over the canvas takes (the open Commands
     *  pane, commandPaneInsetOf): the full width is what it leaves free, so
     *  no column is wider than the band the camera can slide it into. The
     *  overview's board is inset by the pane already and passes none. */
    paneInset?: ViewInset;
    /** The drop gap the scene showed last frame (written by useStackDrag),
     *  read when the stack set changes so a card dropped into it lands
     *  there with no column moving. */
    heldGapRef?: React.RefObject<HeldGap | null>;
}

/** World-x centre of the dashed ghost for a cell INSERT at slot ``ins``
 *  (0..N). During an insert every stack at index >= ``ins`` is shifted right
 *  by ``ghostWidth + gap`` (useStackDrag's ``items``), so the ghost occupies
 *  the space right after the nearest VISIBLE column before the slot. A
 *  single-cell move source renders at width 0 without advancing the row, so
 *  it is skipped — its "right edge" is the next column's left edge. With no
 *  visible column before the slot the ghost starts at the row's left edge.
 *  Shared by the keyboard pending-move track (cellPendingMoveWorldX) and the
 *  drag / keyboard preview (useStackDrag.previewCellData). */
export function slotGhostCenterX(
    ins: number, positions: number[], widths: number[], ghostWidth: number, gap: number,
): number {
    const N = positions.length;
    let lv = Math.min(ins, N) - 1;
    while (lv >= 0 && widths[lv] === 0) lv--;
    if (lv >= 0) return positions[lv] + widths[lv] / 2 + gap + ghostWidth / 2;
    // Every column before the slot is collapsed (or there are none): the
    // row's first column — collapsed or not — starts at the row's left edge.
    const rowLeft = N > 0 ? positions[0] - widths[0] / 2 : 0;
    return rowLeft + ghostWidth / 2;
}

/** World-x centre of the gap before slot ``slot`` (0..N) over the unshifted
 *  row: half a gap outside the first/last column's edge, else midway between
 *  the neighbours' facing edges. Shared by the keyboard insert arrow
 *  (arrowWorldX) and the drag's insert line (useStackDrag.insertLineX).
 *  Precondition: positions.length > 0. */
export function slotGapCenterX(slot: number, positions: number[], widths: number[], gap: number): number {
    const N = positions.length;
    if (slot <= 0) return positions[0] - widths[0] / 2 - gap / 2;
    if (slot >= N) return positions[N - 1] + widths[N - 1] / 2 + gap / 2;
    return ((positions[slot - 1] + widths[slot - 1] / 2) + (positions[slot] - widths[slot] / 2)) / 2;
}

/** World-x shift that keeps a width-dragged column's anchored edge where
 *  it was across a width change (the layout offset to add). The raw
 *  layout packs columns left to right, so a column's left edge only moves
 *  when something BEFORE it changes width, while its right edge moves by
 *  its own growth too: anchoring the left edge (right edge dragged) needs
 *  no shift for the column's own growth; anchoring the right edge (left
 *  edge dragged) shifts the whole row back by that growth, which leaves
 *  every column right of it where it was on screen. */
export function widthDragAnchorShift(
    prevCenter: number, prevWidth: number,
    nextCenter: number, nextWidth: number,
    anchor: WidthDrag['anchor'],
): number {
    const side = anchor === 'left' ? -1 : 1;
    return (prevCenter + side * prevWidth / 2) - (nextCenter + side * nextWidth / 2);
}

interface StackLayoutTrack {
    offset: number;
    sceneSwapNonce: number;
    ids: readonly string[];
    selectedId: string | null;
    positions: readonly number[];
    widths: readonly number[];
    viewWidth: number;
}
/** A drop gap open on screen: the columns from `slot` on stand `shift`
 *  world px right of their layout positions (useStackDrag's items). */
export interface HeldGap {
    slot: number;
    shift: number;
}

interface StackLayoutTrackInput {
    ids: readonly string[];
    selectedId: string | null;
    positions: readonly number[];
    widths: readonly number[];
    viewWidth: number;
    /** The overview's board: a resize of its region (the shelf, the
     *  journal, the Commands pane's slide) leaves its world where it is. */
    zoomedOut: boolean;
    cameraTarget: number;
    pendingMove: PendingMoveState | null;
    widthDrag: WidthDrag | null;
    fanAnchor: {realId: string; columnId: string} | null;
    inheritedFrom: string | null;
    /** The drop gap open in the prior frame, if any: an arrival into it
     *  keeps every column where it stood on screen, gap included. */
    heldGap?: HeldGap | null;
}

/** Pure geometry reduction: repeated evaluation never mutates its prior track. */
export function nextStackLayoutTrack(prior: StackLayoutTrack, input: StackLayoutTrackInput): StackLayoutTrack {
    const {ids,selectedId,positions,widths,viewWidth,pendingMove,widthDrag} = input;
    let offset = prior.offset;
    let sceneSwapNonce = prior.sceneSwapNonce;
    const idsChanged = prior.ids.length !== ids.length || !prior.ids.every((id,i)=>id === ids[i]);
    const previousIds = new Set(prior.ids);
    const setChanged = idsChanged && (ids.length !== prior.ids.length || ids.some(id=>!previousIds.has(id)));
    const alias = setChanged ? layoutAlias([...prior.ids], [...ids], input.fanAnchor) : null;
    const sceneSwap = setChanged && !alias && prior.ids.length > 0 && ids.length > 0 && !ids.some(id=>previousIds.has(id));
    if (sceneSwap) {
        offset = 0;
        sceneSwapNonce += 1;
    } else if (setChanged && prior.positions.length > 0 && positions.length > 0) {
        // Where the columns stood on screen: a drop gap pushed those at and
        // after its slot right, and the card it held lands in it.
        const gap = input.heldGap ?? null;
        const shown = gap ? prior.positions.map((p,i)=>(i >= gap.slot ? p + gap.shift : p)) : prior.positions;
        const anchor = chooseLayoutAnchor({
            prevIds:[...prior.ids],currentIds:[...ids],prevPositions:[...shown],widths:[...widths],
            layoutOffset:offset,cameraTarget:input.cameraTarget,selectedId,prevSelectedId:prior.selectedId,
            alias,inheritedFrom:input.inheritedFrom,
        });
        if (anchor) offset += shown[anchor.prevIdx] - positions[anchor.idx];
    } else if (input.zoomedOut && !idsChanged && prior.viewWidth > 0 && viewWidth !== prior.viewWidth) {
        // The raw row starts half the view left of the centre: cancel the
        // shift, so no column moves and the camera alone keeps the board
        // against the region's left edge (usePanZoomCamera) in one frame.
        offset += (viewWidth - prior.viewWidth) / 2;
    } else if (!idsChanged && !pendingMove && viewWidth === prior.viewWidth && prior.positions.length === positions.length) {
        const dragIndex = widthDrag ? ids.indexOf(widthDrag.id) : -1;
        if (widthDrag && dragIndex !== -1 && widths[dragIndex] > 0 && prior.widths[dragIndex] > 0) {
            offset += widthDragAnchorShift(prior.positions[dragIndex],prior.widths[dragIndex],positions[dragIndex],widths[dragIndex],widthDrag.anchor);
        } else {
            const index = selectedId ? ids.indexOf(selectedId) : -1;
            if (index !== -1 && widths[index] > 0) offset += prior.positions[index] - positions[index];
        }
    }
    return {offset,sceneSwapNonce,ids,selectedId,positions,widths,viewWidth};
}

export function useStackLayout({
    stacks, selectedId, pendingMove, minCellSize, stackWidths, isZoomedOut, cameraTargetRef,
    widthDrag = null,
    rootRef,
    fanAnchor = null,
    paneInset = NO_INSET,
    heldGapRef,
}: UseStackLayoutParams): StackLayoutResult {
    const [viewSize, setViewSize] = useState({ width: 0, height: 0 });
    useEffect(() => {
        const el = rootRef.current;
        if (!el) return;
        const update = () => {
            const width = el.clientWidth;
            const height = el.clientHeight;
            setViewSize(prev => (prev.width === width && prev.height === height ? prev : { width, height }));
        };
        update();
        // The container is sized by the app's flex layout, so observing it
        // covers window resizes too — no window listener needed.
        const observer = new ResizeObserver(update);
        observer.observe(el);
        return () => observer.disconnect();
    }, [rootRef]);

    const GAP = STACK_GAP;


    const freeWidth = Math.max(0, viewSize.width - paneInset.left - paneInset.right);
    const screenMax = maxStackWidthFor(freeWidth);

    const rawLayout = useMemo(() => {
        if (viewSize.width === 0) return { positions: [] as number[], widths: [] as number[], naturalWidths: [] as number[] };
        const positions: number[] = [];
        const widths: number[] = [];
        const naturalWidths: number[] = [];
        const visibleLeftEdge = -viewSize.width / 2;
        const startX = visibleLeftEdge + GAP;
        let currentX = startX;
        // Each stack renders at its per-stack width override if one exists,
        // else the shared ``minCellSize`` — clamped to [MIN_STACK_WIDTH, screenMax].
        // The overview's columns all share one fixed width,
        // whatever the main view's widths.
        stacks.forEach(stack => {
            const pxWidth = isZoomedOut
                ? OVERVIEW_COLUMN_WIDTH
                : Math.max(MIN_STACK_WIDTH, Math.min(stackWidths[stack.id] ?? minCellSize, screenMax));
            naturalWidths.push(pxWidth);

            const isSingleCellPendingSource = pendingMove?.type === 'cell' &&
                stack.id === pendingMove.sourceId && stack.cells.length === 1;
            if (isSingleCellPendingSource) {
                positions.push(currentX);
                widths.push(0);
                return;
            }
            const halfWidth = pxWidth / 2;
            const centerX = currentX + halfWidth;
            positions.push(centerX);
            widths.push(pxWidth);
            currentX += pxWidth + GAP;
        });

        return { positions, widths, naturalWidths };
    }, [stacks, viewSize.width, minCellSize, stackWidths, pendingMove, isZoomedOut, screenMax, GAP]);

    // Layout shift compensation (runs during render to keep the camera stable
    // when the layout reflows). Five distinct cases:
    //
    //  * Scene swap (channel switch): no ID survives, so
    //    the accumulated offset is meaningless — reset it and bump
    //    ``sceneSwapNonce`` so useStackCamera hard-lands on the selection
    //    instead of springing across stale world coordinates.
    //  * Fan toggle alias (fanAnchor): the selected real stack and its
    //    front fan column are the same thing on screen, so the one that
    //    appears lands exactly where the other was.
    //  * Stacks added/removed (ID *set* changed, some overlap): one
    //    surviving stack keeps its world x — the selected one when a stack
    //    came in or a removal left the selection untouched, else the
    //    survivor nearest the camera (chooseLayoutAnchor, lib/stackEntry;
    //    a user remove's heir is told apart by takeSelectionHandOff). A
    //    pure reorder is deliberately NOT compensated — the untouched
    //    stacks already keep their raw positions, and anchoring a stack
    //    that moved used to lurch the whole row.
    //  * The overview's region resized (the shelf, the journal, the
    //    Commands pane's slide): the world stays put, so no column has to
    //    move and the camera keeps the board on screen in the same frame.
    //  * Widths changed (same IDs, resize slider / zoom-mode width): anchor
    //    the selected stack so it resizes in place instead of shoving the
    //    viewport. Gated on stable viewport metrics — in the main view a
    //    resize shifts every raw position by design (the pan transform
    //    cancels it out) and must not be compensated. Exception: while a width drag is active
    //    (``widthDrag``), pin THAT column's anchored edge instead —
    //    center-anchoring grows the column out of both sides and the
    //    dragged edge escapes the pointer at half speed. Edge anchoring
    //    (with the camera parked by useStackCamera) keeps the dragged edge
    //    — and the header handle riding the right one — exactly under the
    //    pointer, through the drag and the all-stacks release commit.
    const trackRef = useRef<StackLayoutTrack>({
        offset:0,sceneSwapNonce:0,ids:[],selectedId,positions:[],widths:[],viewWidth:viewSize.width,
    });
    const currentIds = stacks.map(stack=>stack.id);
    // This app deliberately does not use StrictMode or speculative scene renders.
    // Its synchronous layout reducer must publish before camera/drag consume this
    // frame's geometry, without a second per-frame React update. One assignment
    // replaces the previous independent render-time history mutations. The pure
    // reducer above remains replayable and is tested with immutable inputs.
    // eslint-disable-next-line react-hooks/refs
    const previousTrack = trackRef.current;
    const changedIds = previousTrack.ids.length !== currentIds.length || !previousTrack.ids.every((id,i)=>id===currentIds[i]);
    const inheritedFrom = changedIds ? takeSelectionHandOff(
        stacks.find(stack=>stack.id===selectedId)?.sourceStackId ?? selectedId,
        stacks.map(stack=>stack.sourceStackId ?? stack.id),
    ) : null;
    const track = nextStackLayoutTrack(previousTrack,{
        ids:currentIds,selectedId,positions:rawLayout.positions,widths:rawLayout.widths,viewWidth:viewSize.width,zoomedOut:isZoomedOut,
        cameraTarget:cameraTargetRef.current,pendingMove,widthDrag,fanAnchor,inheritedFrom,
        // eslint-disable-next-line react-hooks/refs
        heldGap:heldGapRef?.current ?? null,
    });
    // eslint-disable-next-line react-hooks/refs
    trackRef.current = track;
    const positions = useMemo(()=>rawLayout.positions.map(position=>position+track.offset),[rawLayout,track.offset]);
    const { widths, naturalWidths } = rawLayout;

    const arrowWorldX = useMemo(() => {
        if (!pendingMove) return null;
        const position = pendingMove.targetIndex;
        const N = positions.length;
        const isInsert = position % 2 === 0;

        if (isInsert) {
            const insertIndex = position / 2;
            if (N === 0) return 0;
            return slotGapCenterX(insertIndex, positions, widths, GAP);
        } else {
            const stackIndex = (position - 1) / 2;
            if (stackIndex < N) return positions[stackIndex];
            return null;
        }
    }, [pendingMove, positions, widths]);

    const isCancelMove = useMemo(() => {
        if (!pendingMove) return false;
        const position = pendingMove.targetIndex;
        const isInsert = position % 2 === 0;
        if (isInsert) {
            return (position / 2) === pendingMove.originalIndex;
        } else {
            return ((position - 1) / 2) === pendingMove.originalIndex;
        }
    }, [pendingMove]);

    const cellPendingMoveWorldX = useMemo(() => {
        if (!pendingMove || pendingMove.type !== 'cell') return null;
        const N = positions.length;
        const sourceIdx = stacks.findIndex(s => s.id === pendingMove.sourceId);
        const cellWidth = sourceIdx >= 0 ? naturalWidths[sourceIdx] : minCellSize;
        if (pendingMove.mode === 'insert') {
            return slotGhostCenterX(pendingMove.targetIndex / 2, positions, widths, cellWidth, GAP);
        } else {
            const drop = (pendingMove.targetIndex - 1) / 2;
            return drop < N ? positions[drop] : null;
        }
    }, [pendingMove, positions, widths, naturalWidths, stacks, minCellSize]);

    // Per-stack chrome is a single head band (a stack's head, or a fan's
    // bracket over its columns); the cards run from under it to the bottom
    // padding. In the overview the cards use a fixed native height.
    //
    // `FRONT_CARD_OFFSET` is the distance from the top of the stack
    // container where the front card starts, which equals the head's height.
    const FRONT_CARD_OFFSET = STACK_HEAD_HEIGHT;
    // The scene region starts under the top bar (StackApp lays the bar out
    // in flow), so the stacks sit only the canvas's own top padding below
    // it (the canvas and overview world's top padding).
    const stackTopOffset = CANVAS_TOP_PADDING;
    // Breathing room between the cards and the viewport edge in the main
    // view (the mock's `.canvas` padding-bottom).
    const BOTTOM_PADDING = 10;
    const MIN_CARD_HEIGHT = 330;
    const tallestColumn = Math.max(0, ...stacks.map(s => s.cells.length));
    const cardPixelHeight = isZoomedOut
        ? OVERVIEW_CARD_HEIGHT
        : Math.max(MIN_CARD_HEIGHT, viewSize.height - stackTopOffset - STACK_HEAD_HEIGHT - BOTTOM_PADDING);
    const stackContainerHeight = isZoomedOut
        ? overviewColumnHeight(tallestColumn)
        : cardPixelHeight + STACK_HEAD_HEIGHT + 120;
    const cardFrame = useMemo(() => ({
        stackTopOffset, stripHeight: FRONT_CARD_OFFSET, cardPixelHeight,
    }), [stackTopOffset, FRONT_CARD_OFFSET, cardPixelHeight]);

    const overviewBounds = useMemo((): OverviewBounds => {
        const pad = OVERVIEW_WORLD_PADDING;
        if (positions.length === 0) return { left: -pad.side, right: pad.side, top: 0, bottom: pad.top + pad.bottom };
        const left = Math.min(...positions.map((p, i) => p - widths[i] / 2));
        const right = Math.max(...positions.map((p, i) => p + widths[i] / 2));
        return {
            left: left - pad.side, right: right + pad.side, top: 0,
            bottom: pad.top + overviewColumnHeight(tallestColumn) + pad.bottom,
        };
    }, [positions, widths, tallestColumn]);

    const overviewTarget = useMemo(
        () => overviewFit(overviewBounds, viewSize, true),
        [overviewBounds, viewSize],
    );

    return {
        viewSize,
        GAP,
        FRONT_CARD_OFFSET,

        positions,
        widths,
        naturalWidths,
        arrowWorldX,
        isCancelMove,
        cellPendingMoveWorldX,
        cardPixelHeight,
        stackContainerHeight,
        stackTopOffset,
        cardFrame,
        overviewBounds,
        overviewTarget,
        screenMax,
        sceneSwapNonce: track.sceneSwapNonce,
    };
}
