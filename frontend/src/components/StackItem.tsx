import { currentChannelId } from '@/lib/currentChannel';
import React, { useState, useEffect } from 'react';
import type { Cell } from '@/lib/types';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { ArrowUp, Minus, Copy, Move } from 'lucide-react';
import { cn } from '@/lib/utils';
import { iconBtn } from './ui/formStyles';
import { patchCellMeta } from '@/lib/api';
import { cellHasLiveRun } from '@/lib/runningCell';
import { toggleRunsPane } from '@/lib/cellLayout';
import { RunsPaneToggle } from './cellEditor/RunsPaneToggle';
import { RunLight } from './RunLight';
import { PlayGlyph } from './ui/PlayGlyph';
import CellEditor from './CellEditor';
import { StackWidthControl } from './StackWidthControl';
import { CellTagMenu } from './TagMenu';
import { EditableLabel } from '@/components/ui/editable-label';
import { FocusArea } from '@/contexts/FocusContext';
import { RendererConcealContext, useRendererConcealed } from '@/contexts/RendererConcealContext';
import { CellSelectionContext } from '@/contexts/CellSelectionContext';
import { motion, type Transition } from 'motion/react';
import { PHYSICS, SNAP } from '@/lib/physics';
import { OverviewCard } from './overview/OverviewCard';
import { useRunRequest } from '@/hooks/useRunRequest';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { STACK_HEAD_HEIGHT } from '@/hooks/useStackLayout';
import { useCellDiff } from '@/hooks/useCellDiff';
import { useReportHeaderChrome } from '@/hooks/useCellChromeReport';
import { isFocusWheel } from '@/lib/wheelReplay';
import { blurForeignEditor } from '@/lib/blurForeignEditor';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { GUEST_FOCUS_EVENT } from './guestMount';
import { BuiltinChip } from './BuiltinChip';
import { promoteForAnimation } from '@/lib/motionLayers';
import { deviceSnappedTransform } from '@/lib/deviceOrigin';

/** The cell head's tools (the mock's `.cell-head .tools button`: 5px
 *  padding around a 14px icon): 24px. */
const headTool = 'h-6 w-6';
/** A head tool that removes: the red hover. */
const removeTool = 'hover:bg-red-soft hover:text-red';
/** The tools' quiet look, set on their row while the cell is neither
 *  selected nor hovered: each tool (a button, or the Move grip's
 *  role=button) takes its ink at 55% alpha, the look the mock gets from
 *  opacity 0.55. `:where` keeps the rule at one class's weight, so a tool's
 *  own hover colour still wins. The alpha is a variable so the card's hover
 *  lifts it with a plain group-hover. */
const toolsQuiet = '[--tools-alpha:55%] group-hover/card:[--tools-alpha:100%] [&_:where(button,[role=button])]:text-ink-3/(--tools-alpha)';
/** The tools' fade between quiet and full: each tool's own colour
 *  transition, at the row's 120ms. Never an opacity transition: Chromium
 *  runs one on the compositor and squashes the card's content painted after
 *  it into an Overlap layer, whose text is grayscale while it runs. */
const toolsFade = '[&_:where(button,[role=button])]:duration-[120ms]';
/** The cell's title (the mock's `.cell-head h3`): the UI face at 14px/600. */
const titleText = 'text-[0.875rem] font-semibold tracking-[-0.005em] text-ink';
// The remove-cell burn's y / scale keyframe timing.
const BURN_TRANSITION: Transition = { duration: 0.34, times: [0, 0.1, 1], ease: [0.4, 0, 0.15, 1] };

interface StackItemProps {
    /** The REAL stack id (CellEditor's run snapshot). */
    stackId: string;
    /** The column this cell renders in: the real stack id, or
     *  `fan-<cellId>` for a fan column. Keys the width control and the
     *  generator-runner match. Defaults to stackId. */
    displayStackId?: string;
    /** This cell renders as a fan column of a fanned-out stack. */
    isFanColumn?: boolean;
    /** The cell sits on the Capabilities stack: a teal head, no run
     *  chrome, and Hide in Remove's slot. */
    capabilitiesCell?: boolean;
    cell: Cell;
    index: number;
    /** The cell's index in its REAL stack — the index the parent's
     *  onSelect / onRemoveCell / onZoomToCell / onCellDragStart /
     *  onDuplicateToNewStack closures bind (origIdx for a real stack,
     *  fanIndex for a fan column). Not read in render: the memo compares
     *  it so a shifted index refreshes those closures. */
    cellIndex?: number;
    isFront: boolean;
    isSelected: boolean;
    viewMode: 'stack' | 'overview';
    isOffscreen?: boolean;
    width: number;
    height: number;
    focusArea: FocusArea;
    /** Actual card motion participates in the stack scrubber retention gate. */
    onTransitionChange?: (id: string, busy: boolean) => void;
    onSelect: (idx?: number) => void;
    onUpdate: (cell: Cell) => void;
    onCellDragStart?: (e: React.PointerEvent, sourceRect?: DOMRect) => void;
    onZoomToCell?: () => void;
    isDragHidden?: boolean;
    // This card is the one exposed behind the lifted front cell. It renders
    // as a full front card (content + chrome) under a slight shadow veil.
    isDragRevealed?: boolean;
    /** Resolves to the removal's result: null when it failed (the card
     *  then drops its burn and stays). */
    onRemoveCell?: () => Promise<unknown> | void;
    onDuplicateToNewStack?: () => void;
    /** A Capabilities cell: hide the stack to its top-bar pill (the stack
     *  menu's Hide), the head's last tool, in Remove's slot. */
    onHideStack?: () => void;
    isRecentlyDropped?: boolean;
}

export const StackItem = React.memo(({
    stackId, displayStackId = stackId, isFanColumn = false, capabilitiesCell = false, cell, index, isFront, isSelected, viewMode,
    isOffscreen,
    width, height,
    focusArea, onTransitionChange,
    onSelect, onUpdate, onCellDragStart, onZoomToCell, isDragHidden = false,
    isDragRevealed = false,
    onRemoveCell,
    onDuplicateToNewStack,
    onHideStack,
    isRecentlyDropped = false,
}: StackItemProps) => {
    // Read cell data from normalized entity cache (SSE-fed, avoids stale layout tree)
    const cachedCell = useCollimateStore(s => s.cellCache[cell.id]);
    // Merge — patchCell() can create a cache entry from a partial before the
    // full cell lands, so a bare cachedCell must not shadow the prop's
    // files/etc.
    const cell$ = React.useMemo(() => (cachedCell ? { ...cell, ...cachedCell } : cell), [cell, cachedCell]);
    // A built-in card (the head of the Capabilities stack) is the app's: its
    // title is plain, its tools are Width and Hide, it carries no tags (it
    // takes no metadata) and it never drags.
    const builtin = !!cell$.builtin;
    // Name edits send only the changed field through meta PATCH.
    // The cache is patched first so the header
    // shows the edit at once. patchCell leaves updated_at alone, so a
    // refetch of the unchanged server cell would keep a failed edit
    // (applyServerState skips a cached entry whose stamp matches): a failed
    // request puts the prior value back itself, unless a later edit has
    // replaced this one in the cache meanwhile.
    const saveMeta = React.useCallback((meta: { name: string }) => {
        const { patchCell } = useCollimateStore.getState();
        const edit = { name: meta.name };
        const prior = { name: cell$.name };
        patchCell(cell$.id, edit);
        patchCellMeta(cell$.channel || currentChannelId(), cell$.id, meta).catch((e: unknown) => {
            console.error('Saving the cell name failed', e);
            const cached = useCollimateStore.getState().cellCache[cell$.id];
            if (cached?.name === edit.name) patchCell(cell$.id, prior);
        });
    }, [cell$]);
    // Cell-pair diff (store `cellDiffs`, entered from the scene's hover
    // zone between two columns): this cell renders read-only, its files
    // diffed against the base cell's live files, under a banner with Exit.
    const cellDiff = useCellDiff(cell$);

    // Zoom-out renders the overview card instead (below, after the hooks).
    const isOverview = viewMode === 'overview';
    // Any column's width being dragged, as a boolean — which column owns the
    // gesture doesn't matter here (edge anchoring moves every stack on the
    // dragged side), and a boolean selector re-renders only on drag start/end.
    const widthDragActive = useSpatialStore(s => s.widthDrag !== null);
    const [isLeaving, setIsLeaving] = useState(false);
    const parentConcealed = useRendererConcealed();

    // Move-handle click hint. A quick tap on the grab handle (no drag) flashes
    // a brief "click and hold to drag" nudge.
    const [moveHint, setMoveHint] = useState<string | null>(null);
    const movePointerRef = React.useRef<{ x: number; y: number; t: number } | null>(null);
    const moveHintTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
    const showMoveHint = React.useCallback((msg: string) => {
        if (moveHintTimerRef.current) clearTimeout(moveHintTimerRef.current);
        setMoveHint(msg);
        moveHintTimerRef.current = setTimeout(() => setMoveHint(null), 1600);
    }, []);
    React.useEffect(() => () => {
        if (moveHintTimerRef.current) clearTimeout(moveHintTimerRef.current);
    }, []);

    // Sticky "has ever been onscreen" flag. Once a CellEditor has mounted we
    // keep it mounted across subsequent isOffscreen flips (display:none while
    // offscreen) to preserve renderer state. Gated so that initially-offscreen
    // cells don't eagerly mount on channel load — they mount the first time
    // the camera comes near them, then stay. The overview doesn't count: it
    // renders the overview card instead of a CellEditor, so a trip to the
    // overview never mounts an editor tree for every cell in the channel.
    const [hasEverBeenOnscreen, setHasEverBeenOnscreen] = useState(!isOffscreen && !isOverview);
    React.useEffect(() => {
        if (!isOffscreen && !isOverview) setHasEverBeenOnscreen(true);
    }, [isOffscreen, isOverview]);

    const removeCellTooltip = useShortcutTooltip(SHORTCUT_IDS.CELL_REMOVE, 'Remove cell');
    const duplicateTooltip = useShortcutTooltip(SHORTCUT_IDS.STACK_DUPLICATE, 'Duplicate cell to new stack');
    const fanDuplicateTooltip = useShortcutTooltip(SHORTCUT_IDS.STACK_DUPLICATE, 'Duplicate cell into the next column');
    const moveCellLeftTooltip = useShortcutTooltip(SHORTCUT_IDS.NAV_CELL_MOVE_LEFT, 'Move cell to left stack');
    const moveCellRightTooltip = useShortcutTooltip(SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT, 'Move cell to right stack');
    const runTooltip = useShortcutTooltip(SHORTCUT_IDS.STACK_RUN, 'Run a runner in this cell');

    // Live runs on the cell: the remove tool hides while any is live (a
    // run's files are its lock, and removing the cell would delete them).
    const liveRun = useCollimateStore(s => cellHasLiveRun(s, cell$.id));
    // The cell's status pane (CellEditor's RunsPane) lives in its layout in
    // the store, so the header reaches it from outside the editor: the
    // light and the caret (RunsPaneToggle) select the cell and toggle the
    // pane. A cell the editor has not seeded yet starts from the default
    // layout (updateRunsPane).
    const cellKey = String(cell$.id);
    const runsOpen = useCollimateStore(s => !!s.cellLayouts[cellKey]?.runsPane.open);
    const toggleRuns = () => {
        onSelect(index);
        useCollimateStore.getState().updateRunsPane(cellKey, toggleRunsPane);
    };
    // No runs from version history (the store refuses a panel there too).
    const inHistory = useCollimateStore(s => !!s.cellHistory[cellKey]);
    // ── Runners panel ─────────────────────────────────────────────────────────
    // useRunRequest opens the cell's runners panel when the STACK_RUN
    // keyboard shortcut sets pendingGeneratorRunnerStackId for this column
    // (display id). We call it only for that side-effect — there are no
    // return values to surface.
    useRunRequest({
        // Same read-only rule as the CellEditor mount below.
        displayStackId, cellId: String(cell$.id), isFront, readOnly: !!cellDiff,
    });

    // The per-stack chrome is a single head band (the stack head, or a
    // fan's bracket over its columns). Cells sit below it and run to the
    // card's bottom; STACK_HEAD_HEIGHT is shared with useStackLayout and
    // StackGroup. A back card (the one behind a drag-lifted front) dims.
    const targetY = STACK_HEAD_HEIGHT;
    const targetOpacity = index === 0 ? 1 : Math.max(0.4, 1 - index * 0.3);

    const hasFocus = focusArea === 'main';
    const isFocusedWindow = isFront && isSelected && hasFocus;
    const isCursorWindow = isFront && isSelected && !hasFocus;
    // The selected cell, focused or cursor alike: the one card with the
    // brass edge (focused) or the nav-cursor edge (unfocused). Exactly one while a stack is selected,
    // including right after load (the main area only gains focus on the
    // first click or key).
    const isSelectedCell = isFront && isSelected;

    // The drag-revealed card (exposed behind a lifted front cell) renders as a
    // full front card — header chrome, status row, and live body content — so
    // the slot reads as a real, fully-populated cell rather than an empty shell.
    const isVisualFront = isFront || isDragRevealed;

    const showFocusRing = isFocusedWindow;

    // Run (C14): the selected cell of a standard stack, where it is
    // writable (not a cell diff, not a built-in card) and not in version
    // history. Every standard cell keeps its 24px slot, so the title never
    // moves as the selection does.
    const showRun = isSelectedCell && !capabilitiesCell && !cellDiff && !builtin && !inHistory;

    // Input the card never sees as a pointerdown still selects it. A live
    // desktop guest takes its clicks natively, so its mount reports focus
    // with GUEST_FOCUS_EVENT (composed, out of the renderer's shadow root)
    // and the card selects on that, as on a click; its wheel is relayed as
    // a DOM wheel and meets the card's onWheel like any other.
    const selectFromGuest = React.useRef<() => void>(() => {});
    React.useLayoutEffect(() => {
        selectFromGuest.current = () => { if (!isFocusedWindow) onSelect(index); };
    });
    // A wheel-select maps a focused pooled iframe back to its cell with
    // containerOf. The card rides the camera: its own compositor layer only
    // while the scene moves (lib/motionLayers).
    const { containerOf, registerMotionLayer } = useGuestPool();
    const cardRef = React.useCallback((card: HTMLDivElement | null) => {
        if (!card) return;
        const onGuestFocus = () => selectFromGuest.current();
        card.addEventListener(GUEST_FOCUS_EVENT, onGuestFocus);
        const untrack = registerMotionLayer(card);
        return () => {
            card.removeEventListener(GUEST_FOCUS_EVENT, onGuestFocus);
            untrack();
        };
    }, [registerMotionLayer]);
    // The card's own motion (y and the burn's scale, opacity, its slot's
    // width and height) runs on its shell, which is promoted for exactly
    // that animation: from motion's start notice, sent before the first
    // animated frame, to its completion. At rest the card holds no
    // promotion of its own, so its chrome is rasterized where it rests
    // (lib/motionLayers).
    const shellRef = React.useRef<HTMLDivElement>(null);

    // The selected cell takes the brass edge (the selection accent); every
    // other cell sits on the hairline. A Capabilities cell's marker is its
    // teal-sheet header, in the main view and the overview alike, so
    // selection never competes with it.
    const borderColor = showFocusRing ? 'border-brass' : 'border-line';

    // The front card's header height feeds the scene's cell-diff hover
    // zone (its band is the header row) and the card-side resize strips
    // (they start below it).
    const headerRef = React.useRef<HTMLDivElement>(null);
    useReportHeaderChrome(String(cell.id), headerRef, !isOverview && isFront);


    useEffect(() => () => { onTransitionChange?.(String(cell$.id), false); }, [onTransitionChange, cell$.id]);

    // Zoom-out: the overview card, in its column's flow. A click selects it
    // (through the overview's anchor rule), a double click opens it in the
    // main view, a press drags it to another column.
    if (isOverview) {
        return (
            <OverviewCard
                cell={cell$}
                capabilitiesCell={capabilitiesCell}
                selected={isSelectedCell}
                isOffscreen={!!isOffscreen}
                onDragStart={builtin ? undefined : onCellDragStart}
                onOpen={onZoomToCell}
            />
        );
    }

    const computedZIndex = 100 - index;

    // States where this card's renderers must hide while the component
    // stays mounted: the remove-cell burn and a drag-lifted front card. The
    // card content animates out via motion, but pooled renderer iframes live
    // outside this tree — conceal them too.
    const rendererConcealed = parentConcealed || isLeaving || isDragHidden;

    // y / scale share one transition: the burn while leaving, a snap while
    // drag-hidden or just dropped, else the card spring.
    const snapMove = isDragHidden || isRecentlyDropped;
    const moveTransition = isLeaving ? BURN_TRANSITION : snapMove ? SNAP : PHYSICS.industrial;
    const setMoving = (moving: boolean) => {
        promoteForAnimation(shellRef.current, moving);
        onTransitionChange?.(String(cell$.id), moving);
    };

    return (
        <motion.div
            ref={shellRef}
            data-cell-id={cell$.id}
            onAnimationStart={() => setMoving(true)}
            onAnimationComplete={() => setMoving(false)}
            // The card's border on whole device pixels as it moves
            // (lib/deviceOrigin).
            transformTemplate={deviceSnappedTransform}
            style={{
                position: 'absolute',
                top: 0,
                left: 0,
                zIndex: computedZIndex,
                transformOrigin: 'top center',
                pointerEvents: 'none',
            }}
            // initial={false}: mount snaps to the animate target instead
            // of interpolating from motion's CSS-default inferred initial
            // (y=0, scale=1, opacity=1). Without this, a card revealed
            // mid-session — notably a behind card exposed during the
            // isMerging window, or a brand-new cell inserted via spark —
            // slides from y=0 → targetY=44. That's the "behind card
            // pops into the front slot" bug. First-ever mount of every
            // cell (page load, fan-out toggle) also skips the slide,
            // which is correct — we want cells present at their final
            // place, not swooping in.
            initial={false}
            animate={{
                // Burn animation: brief ignite pop, then collapse in place.
                // Opacity does the heavy lifting — scale adds texture, not distance.
                // scaleY compresses more than scaleX = "crumbling" not "zooming away."
                y: isLeaving ? [targetY, targetY - 3, targetY + 4] : targetY,
                scaleX: isLeaving ? [1, 1.05, 0.97] : 1,
                scaleY: isLeaving ? [1, 1.05, 0.88] : 1,
                opacity: isLeaving
                    ? [targetOpacity, 1, 0]
                    : (isDragHidden ? 0 : targetOpacity),
                height,
                width,
            }}
            transition={{
                // isRecentlyDropped also snaps scale + opacity so a card
                // revealed mid-animation doesn't briefly show the spring
                // interpolation before settling. Previously only y /
                // height snapped, leaving scale and opacity to spring
                // from their previous motion values — visible pop.
                y: moveTransition,
                scaleX: moveTransition,
                scaleY: moveTransition,
                height: snapMove ? SNAP : PHYSICS.industrial,
                // Match the stack container's width spring (StackScene) so the
                // card resizes in lockstep with its slot instead of wobbling.
                // During a width-handle drag every layer snaps instead — the
                // spring settles over ~200ms, which reads as the stack edge
                // sliding after the pointer; lockstep still applies, all three
                // just switch regimes together.
                width: widthDragActive ? SNAP : PHYSICS.resize,
                opacity: isLeaving
                    ? { duration: 0.28, times: [0, 0.12, 1], ease: [0.5, 0, 0, 1] }
                    : isRecentlyDropped
                        ? { duration: 0 }
                        : { duration: 0.15 },
            }}
        >
            <RendererConcealContext.Provider value={rendererConcealed}>
                {/* Main card */}
                <div
                    ref={cardRef}
                    data-stack-card
                    onPointerDown={(e: React.PointerEvent) => {
                        e.stopPropagation();
                        // Fire selection instantly so the app knows the active cell before dragging
                        if (e.button !== 2) onSelect(index);
                    }}
                    // A vertical wheel over an unfocused cell focuses it (the
                    // renderer scrims replay its travel, lib/wheelReplay);
                    // horizontal stays the canvas swipe's, ctrl the pinch's.
                    // Unfocused, not unselected: a selected cell whose stack
                    // has only the nav cursor takes the main focus too. No
                    // pointerdown moves DOM focus, so an editor focused in
                    // another cell is blurred (lib/blurForeignEditor); nothing
                    // in this cell is focused.
                    onWheel={(e: React.WheelEvent) => {
                        if (isFocusedWindow || !isFocusWheel(e)) return;
                        blurForeignEditor(e.currentTarget, containerOf);
                        onSelect(index);
                    }}
                    style={{
                        width: '100%',
                        height: '100%',
                        pointerEvents: 'auto',
                        // A back card recedes onto the canvas.
                        backgroundColor: isVisualFront ? 'var(--sheet)' : 'var(--canvas)',
                    }}
                    className={cn(
                        'group/card flex flex-col rounded-lg overflow-hidden select-none relative',
                        borderColor, 'border',
                        isVisualFront && 'shadow-(--shadow)',
                        isCursorWindow && !showFocusRing && 'nav-cursor'
                    )}
                >
                    {/* Drag-reveal veil. While the front cell is lifted off this
                        stack, the card now exposed behind it is painted in full
                        but under a faint lift wash + inset shadow, so it reads as
                        "underneath" the floating card rather than as the new front.
                        pointer-events:none so it never swallows the drop. */}
                    <div
                        aria-hidden="true"
                        style={{
                            position: 'absolute',
                            inset: 0,
                            borderRadius: 'inherit',
                            pointerEvents: 'none',
                            opacity: isDragRevealed ? 0.25 : 0,
                            transition: 'opacity 120ms ease-out',
                            background: 'var(--lift)',
                            boxShadow: 'inset 0 2px 14px var(--lift)',
                            zIndex: 40,
                        }}
                    />
                    {/* HEADER (the mock's `.cell-head`): 36px — the light and
                        Run's slot (standard cells), the title, the tools.
                        Its measured bottom places the diff zones and the
                        edge strips. */}
                    <div ref={headerRef} className={cn(
                        'flex-shrink-0 relative border-b border-line flex items-center gap-[0.625rem] h-9 pl-[0.875rem] pr-[0.625rem]',
                        isVisualFront && capabilitiesCell && 'bg-teal-sheet',
                    )}>
                        <div className="contents">
                            {!capabilitiesCell && (
                                <RunLight
                                    cellId={cell$.id}
                                    open={runsOpen}
                                    onToggle={isVisualFront ? toggleRuns : undefined}
                                />
                            )}
                            {showRun ? (
                                <button
                                    type="button"
                                    data-testid="cell-run"
                                    aria-label="Run"
                                    title={runTooltip}
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        useCollimateStore.getState().openPickSession(cellKey, 'runners');
                                    }}
                                    className={cn(iconBtn, headTool, '-mx-1')}
                                >
                                    <PlayGlyph className="w-3.5 h-3.5 text-brass" />
                                </button>
                            ) : !capabilitiesCell && (
                                <span aria-hidden data-run-slot className="w-6 h-6 flex-none -mx-1" />
                            )}
                            {builtin ? (
                                <span className={cn(titleText, 'truncate min-w-0', !isVisualFront && 'text-ink-2')} title={cell$.name}>
                                    {cell$.name}
                                </span>
                            ) : isVisualFront ? (
                                // The title edits the NAME only (Alt+T). flex-1 +
                                // min-w-0 let the label ellipsize; the -ml-1 sets
                                // its text where the mock's h3 sits, past the
                                // label's own hover padding.
                                <div className="flex-1 min-w-0">
                                    <EditableLabel
                                        value={cell$.name}
                                        onSave={(name) => saveMeta({ name })}
                                        isActive={isFocusedWindow}
                                        className={cn(titleText, 'block w-full -ml-1')}
                                        inputClassName={cn('w-full min-w-0 h-6 px-1 py-0 -ml-1 border-line-2 bg-sheet', titleText)}
                                    />
                                </div>
                            ) : (
                                <span className={cn(titleText, 'text-ink-2 truncate flex-1 min-w-0')} title={cell$.name}>
                                    {cell$.name}
                                </span>
                            )}
                            {builtin && <BuiltinChip cell={cell$} />}
                        </div>

                        {isVisualFront && (
                            // The tools (the mock's `.tools`): quiet at 0.55
                            // until the cell is hovered or selected. A
                            // Capabilities cell has Tags, Move, Width and Hide
                            // (a built-in card Width and Hide). Quiet is each tool's
                            // ink at 55% alpha (toolsQuiet), a colour fade:
                            // an opacity fade runs on the compositor and
                            // turns the card's text grayscale while it runs.
                            <div className={cn(
                                'ml-auto flex flex-none items-center gap-0.5 text-ink-3',
                                toolsFade,
                                !isSelectedCell && toolsQuiet,
                            )}>
                                {/* Tags — every cell but a built-in card, right
                                    before the Move grip; the tools row
                                    right-anchors it, so its count grows it
                                    leftwards. */}
                                {!builtin && (
                                    <CellTagMenu channel={cell$.channel || currentChannelId()} cell={cell$} />
                                )}
                                {onCellDragStart && !builtin && (
                                    <div className="relative flex items-center">
                                        <div
                                            role="button"
                                            aria-label="Move cell"
                                            title={`Click and hold to drag · ${moveCellLeftTooltip} / ${moveCellRightTooltip}`}
                                            onPointerDown={(e: React.PointerEvent) => {
                                                if (e.button !== 0) return;
                                                e.stopPropagation();
                                                e.preventDefault();
                                                movePointerRef.current = { x: e.clientX, y: e.clientY, t: Date.now() };
                                                onSelect(index);
                                                const cardEl = (e.currentTarget as HTMLElement).closest('[data-stack-card]') as HTMLElement | null;
                                                onCellDragStart(e, cardEl?.getBoundingClientRect());
                                            }}
                                            onPointerUp={(e: React.PointerEvent) => {
                                                if (e.button !== 0) return;
                                                const down = movePointerRef.current;
                                                movePointerRef.current = null;
                                                if (!down) return;
                                                // A quick tap that never crossed the drag threshold:
                                                // the user clicked instead of holding. If a drag had
                                                // started, pointerup fires off the handle and this
                                                // never runs.
                                                const dx = e.clientX - down.x;
                                                const dy = e.clientY - down.y;
                                                if (dx * dx + dy * dy <= 25 && Date.now() - down.t < 400) {
                                                    showMoveHint('Click and hold to drag');
                                                }
                                            }}
                                            className={cn(iconBtn, headTool)}
                                            style={{ cursor: 'grab', touchAction: 'none' }}
                                        >
                                            <Move className="w-3.5 h-3.5" strokeWidth={1.6} />
                                        </div>
                                        {moveHint && (
                                            <div
                                                className="absolute right-full top-1/2 -translate-y-1/2 mr-1.5 z-50 whitespace-nowrap rounded-md border border-line-2 bg-sheet px-2 py-1 text-[0.75rem] text-ink shadow-(--shadow) pointer-events-none"
                                            >
                                                {moveHint}
                                            </div>
                                        )}
                                    </div>
                                )}
                                <StackWidthControl stackId={displayStackId} onDragStart={() => onSelect(index)} />
                                {/* Duplicate — main view: this cell into a fresh
                                    stack beside it. Fan column: in place, the copy
                                    becomes the next column. Always offered, live
                                    runs included: the copy leaves the runs' files
                                    behind. */}
                                {!capabilitiesCell && onDuplicateToNewStack && (
                                    <button
                                        type="button"
                                        aria-label="Duplicate cell"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            onDuplicateToNewStack();
                                        }}
                                        className={cn(iconBtn, headTool)}
                                        title={isFanColumn ? fanDuplicateTooltip : duplicateTooltip}
                                    >
                                        <Copy className="w-3.5 h-3.5" strokeWidth={1.6} />
                                    </button>
                                )}
                                {/* The status caret (RunsPaneToggle): every
                                    standard front card (nothing runs on
                                    Capabilities). Remove's neighbour, so the
                                    rightmost tool while a run is live. */}
                                {!capabilitiesCell && (
                                    <RunsPaneToggle
                                        cellId={cellKey}
                                        onBeforeToggle={() => onSelect(index)}
                                        className={cn(iconBtn, 'h-6 min-w-6')}
                                    />
                                )}
                                {/* Remove — hidden while a run is live on the cell:
                                    its files are the run's until it stops. The main
                                    view plays the burn, and a failed removal (it
                                    resolves null) undoes it so the card stays; the
                                    channel push unmounts the card (the server drops
                                    a stack it empties). A fan column just unmounts
                                    when the removal lands. */}
                                {/* Hide — a Capabilities cell's last tool, in
                                    Remove's slot: its cells are never removed,
                                    the stack hides to its top-bar pill. */}
                                {capabilitiesCell && onHideStack && (
                                    <button
                                        type="button"
                                        aria-label="Hide Capabilities"
                                        data-testid="cell-hide-stack"
                                        title="Hide Capabilities to the top bar"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            onHideStack();
                                        }}
                                        className={cn(iconBtn, headTool)}
                                    >
                                        <ArrowUp className="w-3.5 h-3.5" strokeWidth={1.6} />
                                    </button>
                                )}
                                {!capabilitiesCell && !liveRun && (
                                    <button
                                        type="button"
                                        aria-label="Remove cell"
                                        data-testid="cell-remove"
                                        onClick={(e) => {
                                            e.stopPropagation();
                                            if (isFanColumn) {
                                                onRemoveCell?.();
                                                return;
                                            }
                                            setIsLeaving(true);
                                            Promise.resolve(onRemoveCell?.()).then(res => { if (!res) setIsLeaving(false); });
                                        }}
                                        className={cn(iconBtn, headTool, removeTool)}
                                        title={removeCellTooltip}
                                    >
                                        <Minus className="w-3.5 h-3.5" strokeWidth={1.6} />
                                    </button>
                                )}
                            </div>
                        )}

                    </div>

                    {/* BODY */}
                    <div
                        className="flex-1 overflow-hidden relative"
                        // The card's rounded overflow clip is applied on the
                        // normal paint path only — composited descendants (the
                        // renderer shadow host, editors with their own layers)
                        // can skip it and paint square corners over the card's
                        // bottom border arc. clip-path applies on the
                        // compositor to every descendant layer; rounding the
                        // body's bottom corners to the card's inner radius
                        // keeps the border the same thickness all the way
                        // around. (The clip lives here, not on the card: on
                        // the card it would also clip its own selection glow
                        // box-shadow, which paints outside the border box.)
                        // The radius reads the live --radius token: the card is
                        // rounded-lg (radius), minus its 1px border.
                        style={{ clipPath: 'inset(0 round 0 0 calc(var(--radius) - 1px) calc(var(--radius) - 1px))' }}
                    >
                        {isVisualFront ? (
                            <div className="flex flex-col h-full">
                                <div className="flex-1 overflow-hidden relative">
                                    {/*
                                      Once a CellEditor has mounted we keep it mounted even when
                                      the stack drifts far from the camera. Unmounting on every
                                      isOffscreen flip wiped NativeRenderer state (no pool) and
                                      reset SandboxIframe's ready flag, producing a full renderer
                                      reload on scroll-back — the "renderers unloading when I scroll
                                      away" bug. While offscreen we flip to `display:none`, which
                                      halts painting (iframes stop rendering, shadow DOM stops
                                      layout) but preserves in-memory state.
                                      `hasEverBeenOnscreen` defers the first mount until the cell
                                      is near the camera, so channel-load doesn't eagerly transpile
                                      renderers for every far-away stack.
                                    */}
                                    {hasEverBeenOnscreen ? (
                                        <div className="w-full h-full" style={{ display: isOffscreen ? 'none' : undefined }}>
                                            <CellSelectionContext.Provider value={isSelectedCell}>
                                                <CellEditor
                                                    cell={cell$}
                                                    capabilitiesCell={capabilitiesCell}
                                                    onUpdate={onUpdate}
                                                    isActive={isFocusedWindow}
                                                    readOnly={!!cellDiff}
                                                    visible={!isOffscreen}
                                                    cellDiff={cellDiff}
                                                />
                                            </CellSelectionContext.Provider>
                                        </div>
                                    ) : null}
                                    {isOffscreen && (
                                        <div className="absolute inset-0 w-full h-full bg-sheet-2 animate-pulse flex items-center justify-center">
                                            <span className="text-ink-3 text-[0.75rem] font-mono">Reserving Space...</span>
                                        </div>
                                    )}
                                    {/* Burn-animation cover: the parent motion.div runs a
                                        scaleX/scaleY crumble when the cell leaves, which
                                        distorts any live renderer underneath. Overlay an
                                        opaque panel so what scales is a solid shape, not a
                                        stretched editor. pointer-events:none so pending
                                        clicks still route to CellEditor during its last
                                        render. */}
                                    {isLeaving && (
                                        <div className="absolute inset-0 bg-sheet pointer-events-none" />
                                    )}
                                </div>
                            </div>
                        ) : (
                            <div className="w-full h-full" />
                        )}
                    </div>
                </div>
            </RendererConcealContext.Provider>
        </motion.div>
    );
}, (prev, next) => {
    if (prev.stackId !== next.stackId) return false;
    if (prev.displayStackId !== next.displayStackId) return false;
    if (prev.isFanColumn !== next.isFanColumn) return false;
    if (prev.capabilitiesCell !== next.capabilitiesCell) return false;
    if (prev.cell.id !== next.cell.id) return false;
    if (prev.cell.updated_at !== next.cell.updated_at) return false;
    if (prev.index !== next.index) return false;
    if (prev.isFront !== next.isFront) return false;
    if (prev.isSelected !== next.isSelected) return false;
    if (prev.viewMode !== next.viewMode) return false;
    if (prev.isOffscreen !== next.isOffscreen) return false;
    if (prev.width !== next.width) return false;
    // height must be compared even though width usually changes with it:
    // hiding/showing the desktop title bar is a purely-vertical container
    // resize, and without this check the memo skips the re-render — the
    // card keeps its stale height spring target while the stack container
    // and strips (plain styles) reflow around it.
    if (prev.height !== next.height) return false;
    if (prev.isRecentlyDropped !== next.isRecentlyDropped) return false;
    if (prev.isDragRevealed !== next.isDragRevealed) return false;
    if (!prev.onHideStack !== !next.onHideStack) return false;
    // The callbacks are not compared by identity (fresh closures every
    // parent render); the real-stack index they bind is.
    if (prev.cellIndex !== next.cellIndex) return false;
    // focusArea feeds only hasFocus, which matters only while isSelected
    // (compared above; it already encodes selectedWindowId).
    if (next.isSelected && prev.focusArea !== next.focusArea) return false;
    // `onCellDragStart` is intentionally NOT in the comparator even though
    // it's called inside render. StackGroup binds it as an inline arrow —
    // `(e) => onCellDragStart(origIdx, cell.id, e)` — so a fresh closure
    // identity per render would force every cell to re-render on every
    // drag pointermove. The closure's actual captures (stackId, cell.id,
    // cellIndex — the real-stack origIdx, not the render slot `index`) are
    // already compared above, so dropping the reference check is safe: if
    // any of those changes, we re-render anyway.
    // Whether it drags or opens at all does change: the overview drops the
    // drag while Only connected filters the board.
    if (!prev.onCellDragStart !== !next.onCellDragStart) return false;
    if (!prev.onZoomToCell !== !next.onZoomToCell) return false;
    if (prev.isDragHidden !== next.isDragHidden) return false;
    return true;
});
StackItem.displayName = 'StackItem';
