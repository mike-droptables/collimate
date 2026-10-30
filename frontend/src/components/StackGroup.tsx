import { useCallback, useContext, useId, useState, useEffect, useRef, memo } from 'react';
import { motion } from 'motion/react';
import { isCapabilitiesStack, type Stack, type Cell } from '@/lib/types';
import { FocusAreaContext } from '@/contexts/FocusContext';
import { PHYSICS, SNAP } from '@/lib/physics';
import { StackItem } from './StackItem';
import { StackCellScrubber, StackSingleRail } from './StackCellScrubber';
import { stackPipTitles } from '@/lib/stackPips';
import { ArrowLeft, ArrowRight, Ellipsis, GripVertical } from 'lucide-react';
import { StackMenu } from './StackMenu';
import { StackRenameInput } from './StackRenameInput';
import { buildCapabilitiesMenuItems, buildStackMenuItems } from './stackMenuItems';
import { cn } from '@/lib/utils';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { toggleMaxWidth } from '@/lib/stackWidth';
import { RendererConcealContext, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { cellHasLiveRun } from '@/lib/runningCell';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { STACK_HEAD_HEIGHT } from '@/hooks/useStackLayout';
import { shownSlotOf } from '@/lib/sceneDropGeometry';
import { useOverviewBoard } from './overview/OverviewCard';
import { useStackScrubberReveal } from '@/hooks/useStackScrubberReveal';
import { useCenterPanGesture } from '@/hooks/useCenterPanGesture';
import { blockNativeSelection } from '@/lib/nativeSelection';
import { promoteForAnimation } from '@/lib/motionLayers';
import { deviceSnappedTransform } from '@/lib/deviceOrigin';
import { MANAGE_SLOT, frontCellIndexOf, frontSlotOf, leadingSlots, slotCount, slotOfCellIndex } from '@/lib/frontSlots';
import { CapabilitiesManageCard } from './capabilities/CapabilitiesManageCard';

// All the per-stack callbacks StackGroup needs, bundled into one object so
// they can be passed as a single referentially-stable prop. The bundle is
// built by StackScene via a useMemo keyed on stacks/actions/etc. — so across
// drag pointermoves the reference is stable and StackGroup's memo bails.
export interface StackCallbacks {
    onSelect: (idx?: number) => void;
    /** Remove the stack. May resolve false when the removal was refused or
     *  failed (the stack stays), which undoes the burn. */
    onClose: () => void | Promise<boolean>;
    onRemoveCell: (cellIndex: number) => Promise<unknown> | void;
    /** Set the front SLOT (lib/frontSlots: the head's pips and knob). */
    onSetTopSlot?: (slot: number) => void;
    /** Capabilities: bring the cell at this REAL index to the front (the
     *  manage card's click-through). */
    onShowCell?: (cellIndex: number) => void;
    /** Capabilities: front the manage card (the stack menu's Manage). */
    onShowManage?: () => void;
    onUpdate: (cell: Cell) => void;
    onRenameStack?: (name: string) => void;
    /** Fan this stack out (the stack menu's Fan out item). Absent for fan
     *  columns, which have no stack menu, and for Capabilities, which never
     *  fans out. */
    onFanOut?: () => void;
    onStackDragStart?: (e: React.PointerEvent) => void;
    onCellDragStart?: (cellIndex: number, cellId: string, e: React.PointerEvent, sourceRect?: DOMRect) => void;
    onZoomToCell?: (cellIndex: number) => void;
    /** The head's arrows: move the stack one place left or right. */
    onMoveStack?: (dir: -1 | 1) => void;
    /** A click on the head's empty space: recentre the stack. */
    onCenterStack?: () => void;
    // A drag on the head's empty space: pan the viewport horizontally so this
    // stack tracks the cursor. onCenterPanEnd returns whether it moved.
    onCenterPanStart?: (e: React.PointerEvent) => void;
    onCenterPanMove?: (e: React.PointerEvent) => void;
    onCenterPanEnd?: () => boolean;
    onDuplicateToNewStack?: () => void;
    /** Copy the whole stack into a fresh stack to the right. Wired to the
     *  stack menu's Duplicate item (never on Capabilities). */
    onDuplicateStack?: () => void;
    /** Merge this ≥2-cell stack into a single cell with an options declaration (a folder per
     *  input). Wired to the stack menu's Merge item. */
    onMergeStack?: () => void;
}

/** A double-click whose second press travelled further than this from where
 *  it went down was a pan (or a knob drag), not a double-click. */
const DOUBLE_CLICK_SLOP = 4;

// Stack id of the floating drag preview's throwaway StackGroup (StackScene),
// which skips the overview column's head.
export const FLOATING_PREVIEW_STACK_ID = 'floating-preview';

// Static no-op bundle for the floating cell preview, which renders a
// throwaway StackGroup that never receives user interaction.
export const NO_OP_STACK_CALLBACKS: StackCallbacks = {
    onSelect: () => {},
    onClose: () => {},
    onRemoveCell: () => {},
    onUpdate: () => {},
    onMoveStack: () => {},
    onCenterStack: () => {},
    onDuplicateToNewStack: () => {},
};

/** One face of the stack view: the Capabilities manage card (its slot 0)
 *  or a cell at its REAL index. */
type Slot = { kind: 'manage-face' } | { kind: 'cell'; cell: Cell; cellIdx: number };
const MANAGE: Slot = { kind: 'manage-face' };
/** The manage card's key among the faces (and its scrubber-reveal id). */
const MANAGE_FACE = 'capabilities-manage';

/** The manage card in the card position: the same shell as a cell card
 *  (StackItem's) under the stack head, with its motion (y, opacity, its
 *  slot's width and height) promoted only while it runs, its border on
 *  whole device pixels, and its z under the head like any card. The card
 *  draws its own frame. */
function ManageSlot({
    stackId, index, width, height, isSelected, isFront, onTransitionChange, onSelect, onShowCell, onHide,
}: {
    stackId: string; index: number; width: number; height: number;
    isSelected: boolean; isFront: boolean;
    onTransitionChange: (id: string, busy: boolean) => void;
    onSelect: () => void; onShowCell: (cellId: string) => void; onHide: () => void;
}) {
    const shellRef = useRef<HTMLDivElement>(null);
    const widthDragActive = useSpatialStore(s => s.widthDrag !== null);
    useEffect(() => () => { onTransitionChange(MANAGE_FACE, false); }, [onTransitionChange]);
    const setMoving = (moving: boolean) => {
        promoteForAnimation(shellRef.current, moving);
        onTransitionChange(MANAGE_FACE, moving);
    };
    return (
        <motion.div
            ref={shellRef}
            data-manage-slot
            onAnimationStart={() => setMoving(true)}
            onAnimationComplete={() => setMoving(false)}
            transformTemplate={deviceSnappedTransform}
            style={{ position: 'absolute', top: 0, left: 0, zIndex: 100 - index, transformOrigin: 'top center' }}
            initial={false}
            animate={{ y: STACK_HEAD_HEIGHT, opacity: index === 0 ? 1 : 0.7, height, width }}
            transition={{
                y: PHYSICS.industrial,
                height: PHYSICS.industrial,
                width: widthDragActive ? SNAP : PHYSICS.resize,
                opacity: { duration: 0.15 },
            }}
        >
            <CapabilitiesManageCard
                stackId={stackId}
                width={width}
                height={height}
                isSelected={isSelected}
                isFront={isFront}
                onSelect={onSelect}
                onShowCell={onShowCell}
                onHide={onHide}
            />
        </motion.div>
    );
}

interface StackGroupProps {
    stack: Stack;
    width: number;
    height: number;
    isSelected: boolean;
    viewMode: 'stack' | 'overview';
    isOffscreen?: boolean;
    /** The stack's front SLOT, as visibleTopIndices holds it (unwrapped). */
    visibleTopIndex?: number;
    callbacks: StackCallbacks;
    /** The overview: a held card lands in this column at this stack
     *  position, counted without the held card (lib/sceneDropGeometry
     *  overviewColumnLanding). */
    previewInsertCellIdx?: number | null;
    hiddenCellId?: string | null;
    recentlyDroppedId?: string | null;
    mergeNonce?: number;
    /** Position of this stack among main_stacks (real index, a fanned
     *  group counting once): the head's move arrows stop at the row's
     *  ends. Fan columns have no head. */
    stackIndex?: number;
    stackCount?: number;
}

const StackGroupInner = ({
    stack,
    width,
    height,
    isSelected,
    viewMode,
    isOffscreen = false,
    visibleTopIndex = 0,
    callbacks,
    previewInsertCellIdx = null,
    hiddenCellId = null,
    recentlyDroppedId = null,
    mergeNonce = 0,
    stackIndex = 0,
    stackCount = 1,
}: StackGroupProps) => {
    const {
        onSelect, onClose, onRemoveCell, onSetTopSlot, onShowCell, onShowManage,
        onUpdate,
        onRenameStack, onFanOut, onStackDragStart, onCellDragStart, onZoomToCell,
        onMoveStack, onCenterPanStart,
        onDuplicateToNewStack, onDuplicateStack, onMergeStack,
    } = callbacks;
    // The head's own gestures on its empty space and its rail (no bar is
    // drawn, D-C06): a click recentres the stack once the double-click
    // window has passed (useCenterPanGesture), a press-and-drag pans the
    // camera, a double-click toggles full width without the camera moving
    // first (the owner's restored gesture; the max operator Alt+F and the
    // Width tool share, lib/stackWidth toggleMaxWidth).
    const { handlers: centerPanHandlers } = useCenterPanGesture({
        onCenterStack: callbacks.onCenterStack,
        onCenterPanStart: callbacks.onCenterPanStart,
        onCenterPanMove: callbacks.onCenterPanMove,
        onCenterPanEnd: callbacks.onCenterPanEnd,
    });
    // The pointer is on the head: the name fades, the knob rises onto its
    // rail and the arrows and … show.
    const [headHover, setHeadHover] = useState(false);
    // Where the head's latest press went down: a double-click whose second
    // press then travelled was a pan or a knob drag.
    const headPressRef = useRef<{ x: number; y: number } | null>(null);
    // The group's own element, promoted for its own animation only.
    const groupRef = useRef<HTMLDivElement>(null);
    // The knob is held (pointer capture can carry the drag off the head, so
    // hover alone can't tell).
    const [scrubbing, setScrubbing] = useState(false);
    // The stack menu (the … button at the head's right end) and the inline
    // rename its Rename item opens in the name's place.
    const [menuOpen, setMenuOpen] = useState(false);
    const [renaming, setRenaming] = useState(false);
    const menuButtonRef = useRef<HTMLButtonElement>(null);
    const menuId = useId();
    const closeMenu = useCallback(() => setMenuOpen(false), []);
    const cellCount = stack.cells.length;
    // A fan column is a client-minted single-cell wrapper of one cell of a
    // fanned-out stack (lib/fanOut.ts). Its own cells.length is always 1 —
    // the real stack's cell count and this cell's index there ride along.
    const isFanColumn = !!stack.synthetic;
    const fanIndex = stack.fanIndex ?? 0;
    // The Capabilities stack: its name is teal and fixed, it never fans
    // out, and its first face is the manage card (slot 0, lib/frontSlots).
    const isCapabilitiesGroup = isCapabilitiesStack(stack);
    // The stack's front slots and the one in front (the manage card or a
    // cell), as the head's pips and the stack view's face show them.
    const vti = { [stack.id]: visibleTopIndex };
    const nSlots = slotCount(stack);
    const topSlot = frontSlotOf(stack, vti);
    const lead = leadingSlots(stack);
    // The head belongs to every real stack in the main view. A fan column
    // has none: the fan's bracket (FanFrame) stands over its columns in
    // the head's place, and the Move tool reorders.
    const showHead = viewMode === 'stack' && !isFanColumn;
    // Which head the stack shows: the stack view's, the overview column's,
    // or none (a fan column). Switching heads (the overview, the stack
    // fanning out) drops an open menu or rename rather than reviving it in
    // the other head or when this one comes back.
    const headKind = showHead ? 'stack' : viewMode === 'overview' ? 'overview' : null;
    const [prevHeadKind, setPrevHeadKind] = useState(headKind);
    if (prevHeadKind !== headKind) {
        setPrevHeadKind(headKind);
        setMenuOpen(false);
        setRenaming(false);
    }
    // Any column's width being dragged, as a boolean — the id itself is
    // irrelevant here (every stack resizes or shifts during the gesture), and
    // a boolean selector re-renders only on drag start/end, not per pointermove.
    const widthDragActive = useSpatialStore(s => s.widthDrag !== null);
    const movePending = useSpatialStore(state =>
        state.pendingMove?.sourceId === stack.id || state.settlingMoveSourceId === stack.id);
    const frontCellIdx = frontCellIndexOf(stack, vti);
    const frontFace = frontCellIdx !== null ? String(stack.cells[frontCellIdx].id) : nSlots > 0 ? MANAGE_FACE : null;
    const {visible: transitionReveal, onTransitionChange} = useStackScrubberReveal(frontFace, movePending);
    const focusArea = useContext(FocusAreaContext);
    const [isLeaving, setIsLeaving] = useState(false);
    // StackScene conceals an entering stack's renderers while its entry is
    // held (lib/stackEntry); the burn adds its own.
    const parentConceal = useRendererConcealed();
    const [isMerging, setIsMerging] = useState(false);
    const prevMergeNonceRef = useRef(mergeNonce);
    useEffect(() => {
        if (mergeNonce !== prevMergeNonceRef.current) {
            prevMergeNonceRef.current = mergeNonce;
            setIsMerging(true);
            const t = setTimeout(() => setIsMerging(false), 450);
            return () => clearTimeout(t);
        }
    }, [mergeNonce]);

    // The overview's state: the cells Only connected keeps (overview only).
    const overviewBoard = useOverviewBoard();

    // The head's tooltip names its gestures, with the live recentre shortcut.
    // Hoisted above the early return so hook order stays stable even when
    // `focusArea` is null.
    const centerTooltip = useShortcutTooltip(SHORTCUT_IDS.STACK_CENTER, 'Center stack');

    // The menu's Merge and Remove grey out while any run is live in the
    // stack (a run's files are its lock; each would delete or move them).
    // Duplicate stays: the copy leaves the runs' files behind.
    const hasLiveRun = useCollimateStore(s => stack.cells.some(c => cellHasLiveRun(s, String(c.id))));

    if (!focusArea) return null;

    const allCells = stack.cells;

    // The stack view shows one face per front slot: the manage card ahead
    // of the cells on Capabilities, then the cells, rotated so the front
    // slot leads and the pile behind stays in order.
    const slots: Slot[] = [
        ...(lead ? [MANAGE] : []),
        ...allCells.map((cell, cellIdx): Slot => ({ kind: 'cell', cell, cellIdx })),
    ];

    // Where is the hidden cell visually? Computed up-front so the
    // stack-mode branch can peek the next slot forward when the front
    // is being drag-hidden — otherwise the stack would visually empty
    // out while a cell drag is in flight.
    const hiddenCellOriginalIndex = hiddenCellId ? allCells.findIndex(c => c.id === hiddenCellId) : -1;
    const hiddenVisualIndex = hiddenCellOriginalIndex !== -1
        ? (slotOfCellIndex(stack, hiddenCellOriginalIndex) - topSlot + nSlots) % nSlots
        : null;

    let visibleSlots: Slot[] = [];
    if (viewMode === 'stack' && nSlots > 0) {
        const rotated = [...slots.slice(topSlot), ...slots.slice(0, topSlot)];
        const frontIsHidden = hiddenVisualIndex === 0 && rotated.length > 1;
        // During the drop animation the new front shows over the previous
        // one; while the front cell is drag-lifted the next slot shows so
        // the place reads as occupied under the floating preview. Otherwise
        // only the front: no peeking back cards.
        visibleSlots = (isMerging && rotated.length > 1) || frontIsHidden ? rotated.slice(0, 2) : rotated.slice(0, 1);
    }

    // Rechecked at activation, against the channel's current cells, so a
    // newly live run cannot lose its stack (a cell's tool may hold this
    // render's closure: StackItem's memo skips fresh callbacks).
    const liveAtActivation = () => {
        const store = useCollimateStore.getState();
        const cells = store.activeChannel?.main_stacks.find(st => st.id === stack.id)?.cells ?? stack.cells;
        return cells.some(c => cellHasLiveRun(store, String(c.id)));
    };
    // Remove: the stack burns, then onClose. No confirm: the cells go to
    // the Removed shelf, restorable.
    const burnStack = () => {
        if (liveAtActivation()) return;
        setIsLeaving(true);
    };
    // Hide Capabilities (its menu, its cells' and its manage card's ArrowUp):
    // the same leaving animation, then onClose hides it to its top-bar pill.
    // Nothing runs on it, so nothing waits.
    const hideStack = () => setIsLeaving(true);

    // The stack menu's rows (policy in stackMenuItems.ts).
    const stackMenuItems = () => {
        if (isCapabilitiesGroup) {
            return buildCapabilitiesMenuItems({
                onManage: () => { onSelect(); onShowManage?.(); },
                onAddFromCatalog: () => useCollimateStore.getState().requestCapabilitiesManage({ addFromCatalog: true }),
                onCreate: () => useCollimateStore.getState().requestCapabilitiesManage({ create: true }),
                onHide: hideStack,
            });
        }
        return buildStackMenuItems({
            cellCount, hasLiveRun,
            onRename: onRenameStack ? () => setRenaming(true) : undefined,
            onToggleFan: onFanOut, onDuplicateStack, onMergeStack,
            onRemove: burnStack,
        });
    };

    return (
        // The remove-stack burn animates this motion.div's scale/opacity, but
        // pooled renderer iframes live in the world-space overlay and don't
        // inherit any of it — conceal them for the duration instead.
        //
        // `isolate`: the group is a stacking context, so its head (z 1000)
        // and cards stay under the merge-target overlay (z 120) the scene
        // lays over the column. It needs no compositor layer for that: a
        // permanent one froze the cards' chrome at the raster it was drawn
        // at, blurring it after a camera zoom. The group is promoted only
        // while its own animation (the burn, a width spring) runs
        // (lib/motionLayers).
        <RendererConcealContext.Provider value={isLeaving || parentConceal}>
        <motion.div
            ref={groupRef}
            data-stack-id={stack.id}
            className={cn('relative h-full isolate', viewMode === 'overview' && 'group/ovcol')}
            animate={{
                // Stack-level burn: louder than single-cell. Wider scale spread,
                // vertical crush, and a brief brightness flash at ignite.
                scaleX: isLeaving ? [1, 1.03, 0.94] : 1,
                scaleY: isLeaving ? [1, 1.03, 0.78] : 1,
                opacity: isLeaving ? [1, 1, 0] : 1,
                width,
            }}
            transition={{
                scaleX: isLeaving ? { duration: 0.42, times: [0, 0.08, 1], ease: [0.4, 0, 0.1, 1] } : { duration: 0 },
                scaleY: isLeaving ? { duration: 0.42, times: [0, 0.08, 1], ease: [0.4, 0, 0.1, 1] } : { duration: 0 },
                opacity: isLeaving ? { duration: 0.34, times: [0, 0.1, 1], ease: [0.6, 0, 0, 1] } : { duration: 0 },
                // Same spring as the stack container's x/width (StackScene) so
                // both edges of a resizing stack move in lockstep — a faster
                // width spring here makes the card wobble against its slot.
                // A live width-handle drag snaps all three layers instead: the
                // spring's settle time would leave the stack edge behind the
                // pointer. The lockstep requirement is what forces all three to
                // agree in both regimes.
                width: widthDragActive ? SNAP : PHYSICS.resize,
            }}
            onAnimationStart={() => {
                promoteForAnimation(groupRef.current, true);
                onTransitionChange("group", true);
            }}
            onAnimationComplete={() => {
                promoteForAnimation(groupRef.current, false);
                onTransitionChange("group", false);
                if (!isLeaving) return;
                // On success the stack leaves and this group unmounts; a
                // refused or failed removal leaves it here, so undo the burn.
                void Promise.resolve(onClose()).then(ok => { if (ok === false) setIsLeaving(false); });
            }}
        >
            {/* The stack head (the mock's `.stack-head`), one 30px band at
                y=0 over the cells (STACK_HEAD_HEIGHT is shared with
                useStackLayout and StackItem, so the cells sit flush under
                it): the name on its top line with the pips beneath, the move
                arrows and the … menu at its ends. The head's empty space
                and its rail keep the owner's gestures (D-C06): a click
                recentres the stack, a drag pans the camera and a
                double-click toggles full width. The menu portals out. */}
            {showHead && (() => {
                const leftDisabled = stackIndex <= 0;
                const rightDisabled = stackIndex >= stackCount - 1;
                const multi = nSlots > 1 && !!onSetTopSlot;
                // The knob rises while the pointer is on the head, while it
                // is held, and for a moment after the front cell changes.
                const raised = headHover || scrubbing;
                // The name line gives way to the rail over the head (every
                // head, single-cell ones too) and to a front change's pips.
                const labelHidden = raised || (multi && transitionReveal);
                // Shown on hover, while the knob is held or the menu open,
                // and always on the stack that holds the selection.
                const controlsOn = isSelected || headHover || scrubbing || menuOpen;
                // Capabilities' name is teal (its live colour) even while
                // selected; any other selected stack's name is brass.
                const nameTone = isCapabilitiesGroup ? 'text-teal' : isSelected ? 'text-brass' : 'text-ink-2';
                // Hidden and dimmed are colours (transparent, ink at 25%),
                // and the fade a colour fade: an opacity fade runs on the
                // compositor and turns the cards' text grayscale while it
                // runs (the icons are strokes in currentColor).
                const control = (on: boolean, disabled = false) => cn(
                    'absolute top-1 flex h-[1.375rem] w-[1.375rem] items-center justify-center rounded-xs',
                    'transition-colors duration-[120ms] motion-reduce:transition-none',
                    disabled ? 'cursor-default text-ink-3/25' : 'text-ink-3 hover:bg-sheet-2 hover:text-ink',
                    // Focus inside the head shows them too (an inert arrow
                    // stays dimmed).
                    !on && 'not-group-focus-within/head:text-transparent',
                );
                return (
                    <>
                    <div
                        data-stack-head
                        data-selected={isSelected || undefined}
                        title={`${centerTooltip}${onCenterPanStart ? ' · drag to slide the view' : ''} · double-click for full width`}
                        className="group/head absolute inset-x-0 top-0 flex items-start justify-center text-[0.75rem] text-ink-3"
                        style={{
                            height: STACK_HEAD_HEIGHT,
                            zIndex: 1000,
                            userSelect: 'none',
                            touchAction: 'none',
                            pointerEvents: isLeaving ? 'none' : 'auto',
                            opacity: isLeaving ? 0 : 1,
                            transition: 'opacity 0.15s ease-out',
                        }}
                        onPointerEnter={() => setHeadHover(true)}
                        onPointerLeave={() => setHeadHover(false)}
                        // In the capture phase: the knob's own press stops
                        // its event before it bubbles here.
                        onPointerDownCapture={(e) => { headPressRef.current = { x: e.clientX, y: e.clientY }; }}
                        onPointerDown={(e) => {
                            centerPanHandlers.onPointerDown(e);
                            // Never the scene's own pan, and never a native
                            // text selection swept across the cells.
                            e.stopPropagation();
                            blockNativeSelection(e);
                        }}
                        onPointerMove={centerPanHandlers.onPointerMove}
                        onPointerUp={centerPanHandlers.onPointerUp}
                        onPointerCancel={centerPanHandlers.onPointerCancel}
                        onClick={centerPanHandlers.onClick}
                        onDoubleClick={(e) => {
                            e.stopPropagation();
                            // The arrows, the menu button and the rename
                            // field keep their own double-clicks.
                            if ((e.target as Element).closest('button, input')) return;
                            const press = headPressRef.current;
                            if (press && Math.hypot(e.clientX - press.x, e.clientY - press.y) > DOUBLE_CLICK_SLOP) return;
                            toggleMaxWidth(stack.id, useCollimateStore.getState().activeChannel?.id);
                        }}
                    >
                        <button
                            type="button"
                            onPointerDown={(e) => e.stopPropagation()}
                            onClick={(e) => { e.stopPropagation(); onMoveStack?.(-1); }}
                            disabled={leftDisabled}
                            title="Move stack left"
                            aria-label="Move stack left"
                            className={cn(control(controlsOn, leftDisabled), 'left-0.5')}
                        >
                            <ArrowLeft className="w-3.5 h-3.5" strokeWidth={1.6} />
                        </button>

                        {renaming ? (
                            <StackRenameInput
                                value={stack.name || ''}
                                onSave={(name) => {
                                    setRenaming(false);
                                    if (name !== (stack.name || '')) onRenameStack?.(name);
                                }}
                                onCancel={() => setRenaming(false)}
                                className="absolute top-1 text-center"
                                style={{ left: 30, right: 52 }}
                            />
                        ) : (
                            <>
                                {/* The name line: the name alone (the cell
                                    count sits after the last pip, in the
                                    scrubber). Over the head it fades for
                                    the rail. Pointer-transparent, so the
                                    head's gestures pass straight through. */}
                                {stack.name && (
                                    <span
                                        data-stack-name
                                        className={cn(
                                            'pointer-events-none relative z-[2] mt-px h-[1.125rem] min-w-0 truncate px-1.5 font-medium leading-[1.125rem]',
                                            // A colour fade, never opacity
                                            // (see control above).
                                            'transition-colors duration-[140ms] motion-reduce:transition-none',
                                            labelHidden ? 'text-transparent' : nameTone,
                                        )}
                                        style={{ maxWidth: 'calc(100% - 112px)' }}
                                    >
                                        {stack.name}
                                    </span>
                                )}
                                {nSlots === 1 && <StackSingleRail raised={raised} />}
                                {multi && onSetTopSlot && (
                                    <StackCellScrubber
                                        slotCount={nSlots}
                                        titles={stackPipTitles(stack)}
                                        topSlot={topSlot}
                                        selected={isSelected}
                                        raised={raised}
                                        revealed={transitionReveal}
                                        onSetTopSlot={onSetTopSlot}
                                        onDraggingChange={setScrubbing}
                                    />
                                )}
                            </>
                        )}

                        <button
                            type="button"
                            onPointerDown={(e) => e.stopPropagation()}
                            onClick={(e) => { e.stopPropagation(); onMoveStack?.(1); }}
                            disabled={rightDisabled}
                            title="Move stack right"
                            aria-label="Move stack right"
                            className={cn(control(controlsOn, rightDisabled), 'right-6')}
                        >
                            <ArrowRight className="w-3.5 h-3.5" strokeWidth={1.6} />
                        </button>

                        <button
                            ref={menuButtonRef}
                            type="button"
                            onPointerDown={(e) => e.stopPropagation()}
                            onClick={(e) => { e.stopPropagation(); setMenuOpen(o => !o); }}
                            title="Stack menu"
                            aria-label="Stack menu"
                            aria-haspopup="menu"
                            aria-expanded={menuOpen}
                            aria-controls={menuOpen ? menuId : undefined}
                            className={cn(control(controlsOn), 'right-0.5', menuOpen && 'bg-sheet-2 text-ink')}
                        >
                            <Ellipsis className="w-3.5 h-3.5" strokeWidth={1.6} />
                        </button>
                    </div>
                    {/* Outside the head: the menu portals to the body, but its
                        events still bubble through the React tree and must
                        not reach the head's recentre and pan. */}
                    <StackMenu
                        anchorRef={menuButtonRef}
                        open={menuOpen}
                        id={menuId}
                        label={stack.name ? `Stack menu: ${stack.name}` : 'Stack menu'}
                        items={stackMenuItems()}
                        onClose={closeMenu}
                    />
                    </>
                );
            })()}

            {/* The overview's column (the mock's `.ov-col`): a 33px head
                with the grip (on hover; it drags the column, to reorder or
                to merge onto another) and the name, then the cards in the
                stack's own order, 18px apart. A card held over a column
                opens a marked gap at the position the pointer points to,
                where it lands. */}
            {viewMode === 'overview' && (() => {
                const keep = overviewBoard?.keep ?? null;
                const others = allCells.filter(c => c.id !== hiddenCellId);
                const shown = others.filter(c => !keep || keep.has(String(c.id)));
                // The landing slot among the shown cards (Only connected
                // may hide some of the column).
                const dropSlot = previewInsertCellIdx === null ? null
                    : shownSlotOf(previewInsertCellIdx, others.map(c => c.id), shown.map(c => c.id));
                const nameTone = isCapabilitiesGroup ? 'text-teal' : isSelected ? 'text-brass' : 'text-ink-2';
                const gap = (slot: number) => (
                    <div
                        key={`drop-${slot}`}
                        data-drop-slot
                        aria-hidden="true"
                        className="flex-none rounded-lg border-2 border-dashed border-teal bg-teal-soft"
                        style={{ height }}
                    />
                );
                const card = (cell: Cell, slot: number) => {
                    const origIdx = allCells.indexOf(cell);
                    return (
                        <StackItem
                            key={cell.id}
                            stackId={stack.sourceStackId ?? stack.id}
                            displayStackId={stack.id}
                            capabilitiesCell={isCapabilitiesGroup}
                            cell={cell}
                            index={slot}
                            cellIndex={origIdx}
                            isFront={origIdx === frontCellIdx}
                            isSelected={isSelected}
                            viewMode="overview"
                            isOffscreen={isOffscreen}
                            width={width}
                            height={height}
                            focusArea={focusArea}
                            onSelect={() => onSelect(origIdx)}
                            onUpdate={onUpdate}
                            onCellDragStart={onCellDragStart ? (e, rect) => onCellDragStart(origIdx, cell.id, e, rect) : undefined}
                            onZoomToCell={onZoomToCell ? () => onZoomToCell(origIdx) : undefined}
                        />
                    );
                };
                // Capabilities keeps its name (the backend refuses a
                // rename): its name stays static text.
                const canRename = !!onRenameStack && !isCapabilitiesGroup;
                return (
                    <>
                        {stack.id !== FLOATING_PREVIEW_STACK_ID && (
                            <div data-overview-head className="flex items-start justify-center gap-1 text-[0.75rem]" style={{ height: STACK_HEAD_HEIGHT }}>
                                {onStackDragStart && (
                                    <span
                                        data-overview-grip
                                        title="Drag to move the stack, or onto another to merge them"
                                        className="mt-px inline-flex h-[1.125rem] cursor-grab items-center text-transparent transition-colors duration-[120ms] group-hover/ovcol:text-ink-3 motion-reduce:transition-none"
                                        onPointerDown={(e) => { e.stopPropagation(); e.preventDefault(); onStackDragStart(e); }}
                                    >
                                        <GripVertical className="h-3.5 w-3.5" strokeWidth={1.6} />
                                    </span>
                                )}
                                {/* The name: a click-to-rename field (the
                                    stack view's rename input, its keys:
                                    Enter or blur saves, Esc cancels), at
                                    least 96px wide so an unnamed column has
                                    somewhere to click; empty, it shows a
                                    quiet "Name…". The margin balances the
                                    grip so the name stays centred. */}
                                {renaming && canRename ? (
                                    <StackRenameInput
                                        value={stack.name || ''}
                                        onSave={(name) => {
                                            setRenaming(false);
                                            if (name !== (stack.name || '')) onRenameStack?.(name);
                                        }}
                                        onCancel={() => setRenaming(false)}
                                        className={cn(
                                            'mt-px h-[1.125rem] w-[11.25rem] min-w-[6rem] rounded-xs px-1.5 text-center leading-[1rem] placeholder:text-transparent',
                                            onStackDragStart && 'mr-[1.125rem]',
                                        )}
                                    />
                                ) : canRename ? (
                                    <button
                                        type="button"
                                        data-stack-name
                                        title="Click to rename"
                                        onClick={() => setRenaming(true)}
                                        className={cn(
                                            'mt-px h-[1.125rem] min-w-[6rem] cursor-text truncate rounded-xs px-1.5 text-center font-medium leading-[1.125rem]',
                                            'transition-colors duration-[120ms] hover:bg-sheet-2 motion-reduce:transition-none',
                                            onStackDragStart && 'mr-[1.125rem]',
                                            nameTone,
                                        )}
                                    >
                                        {stack.name || <span data-name-placeholder className="text-ink-3">Name…</span>}
                                    </button>
                                ) : (
                                    <span data-stack-name className={cn('mt-px min-w-0 truncate pr-[1.125rem] font-medium leading-[1.125rem]', nameTone)}>
                                        {stack.name}
                                    </span>
                                )}
                            </div>
                        )}
                        <div data-ov-column className="flex flex-col gap-[1.125rem]">
                            {shown.flatMap((cell, slot) => dropSlot === slot ? [gap(slot), card(cell, slot)] : [card(cell, slot)])}
                            {dropSlot !== null && dropSlot >= shown.length && gap(shown.length)}
                        </div>
                    </>
                );
            })()}

            {/* The stack view's faces: the front slot, and behind it the
                next while the front is drag-lifted or a drop lands. Stable
                keys, so switching faces has no mount animation. */}
            {viewMode === 'stack' && visibleSlots.map((slot, i) => {
                // While the front cell is being drag-hidden the next face
                // renders too: index 0, so it lights up at the front's full
                // opacity instead of the dimmed back-card opacity.
                const frontIsDragHidden = hiddenVisualIndex === 0 && nSlots > 1;
                const renderIndex = frontIsDragHidden && i === 1 ? 0 : i;
                if (slot.kind === 'manage-face') {
                    return (
                        <ManageSlot
                            key={MANAGE_FACE}
                            stackId={stack.id}
                            index={renderIndex}
                            width={width}
                            height={height}
                            isSelected={isSelected}
                            isFront={topSlot === MANAGE_SLOT}
                            onTransitionChange={onTransitionChange}
                            onSelect={() => onSelect()}
                            onShowCell={(cellId) => {
                                // Resolved when called, from the store: a cell the
                                // card has just added (Add from catalog) is in the
                                // store before this render's cells catch up.
                                const live = useCollimateStore.getState().activeChannel;
                                const cells = [...(live?.main_stacks ?? []), ...(live?.nav_stacks ?? [])].find(s => s.id === stack.id)?.cells ?? [];
                                const cellIdx = cells.findIndex(c => String(c.id) === cellId);
                                if (cellIdx >= 0) onShowCell?.(cellIdx);
                            }}
                            onHide={hideStack}
                        />
                    );
                }
                const { cell, cellIdx: origIdx } = slot;
                const isFrontCell = origIdx === frontCellIdx;
                return (
                <StackItem
                    key={cell.id}
                    stackId={stack.sourceStackId ?? stack.id}
                    displayStackId={stack.id}
                    isFanColumn={isFanColumn}
                    capabilitiesCell={isCapabilitiesGroup}
                    cell={cell}
                    index={renderIndex}
                    cellIndex={isFanColumn ? fanIndex : origIdx}
                    isFront={isFrontCell}
                    isSelected={isSelected}
                    viewMode={viewMode}
                    isOffscreen={isOffscreen}
                    width={width}
                    height={height}
                    focusArea={focusArea}
                    onTransitionChange={onTransitionChange}
                    onSelect={() => onSelect(origIdx)}
                    onUpdate={onUpdate}
                    onCellDragStart={onCellDragStart ? (e, rect) => onCellDragStart(origIdx, cell.id, e, rect) : undefined}
                    isDragHidden={hiddenCellId === cell.id}
                    isDragRevealed={frontIsDragHidden && i === 1}
                    onRemoveCell={() => onRemoveCell(origIdx)}
                    onDuplicateToNewStack={isCapabilitiesGroup ? undefined : onDuplicateToNewStack}
                    onHideStack={isCapabilitiesGroup ? hideStack : undefined}
                    isRecentlyDropped={recentlyDroppedId === cell.id || (!isFrontCell && isMerging)}
                />
                );
            })}

        </motion.div>
        </RendererConcealContext.Provider>
    );
};

// Memoize with an explicit-prop comparator. StackScene re-renders on every
// drag pointermove (because dragGhost / insertTargetIdx update). Without this
// memo, the function body of every StackGroup in the canvas runs on every
// move event — the "render amplifier" path that makes drag glitchy.
//
// The comparator checks the listed stack/layout props explicitly and
// compares `callbacks` (the StackCallbacks bundle, useMemo'd in StackScene)
// by reference, so the bundle must stay referentially stable across
// pointermoves or every StackGroup re-renders.
export const StackGroup = memo(StackGroupInner, (prev, next) => {
    if (prev.stack.id !== next.stack.id) return false;
    if (prev.stack.cells !== next.stack.cells) return false;
    if (prev.stack.name !== next.stack.name) return false;
    if (prev.stack.stack_type !== next.stack.stack_type) return false;
    if (prev.stack.synthetic !== next.stack.synthetic) return false;
    if (prev.stack.fanIndex !== next.stack.fanIndex) return false;
    if (prev.stack.fanCount !== next.stack.fanCount) return false;
    if (prev.stack.sourceStackId !== next.stack.sourceStackId) return false;
    if (prev.width !== next.width) return false;
    if (prev.height !== next.height) return false;
    if (prev.isSelected !== next.isSelected) return false;
    if (prev.viewMode !== next.viewMode) return false;
    if (prev.isOffscreen !== next.isOffscreen) return false;
    if (prev.visibleTopIndex !== next.visibleTopIndex) return false;
    if (prev.callbacks !== next.callbacks) return false;
    if (prev.previewInsertCellIdx !== next.previewInsertCellIdx) return false;
    if (prev.hiddenCellId !== next.hiddenCellId) return false;
    if (prev.recentlyDroppedId !== next.recentlyDroppedId) return false;
    if (prev.mergeNonce !== next.mergeNonce) return false;
    if (prev.stackIndex !== next.stackIndex) return false;
    if (prev.stackCount !== next.stackCount) return false;
    return true;
});
StackGroup.displayName = 'StackGroup';
