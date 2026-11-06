import { useCallback, useId, useRef, useState } from 'react';
import { motion, useMotionValue, useReducedMotion, useTransform, type MotionValue, type Transition } from 'motion/react';
import { Ellipsis, FoldHorizontal } from 'lucide-react';
import { FAN_HEADER_HEIGHT } from '@/hooks/useStackLayout';
import { fanHeaderSpan } from '@/lib/fanOut';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { blockNativeSelection } from '@/lib/nativeSelection';
import { cellHasLiveRun } from '@/lib/runningCell';
import { useCenterPanGesture } from '@/hooks/useCenterPanGesture';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { cn } from '@/lib/utils';
import { StackMenu } from './StackMenu';
import { StackRenameInput } from './StackRenameInput';
import { buildStackMenuItems } from './stackMenuItems';

interface FanFrameProps {
    /** World x of the bracket's left end (lib/fanOut.ts fanFrameExtent). */
    left: number;
    width: number;
    /** World y of the bracket's top (the scene's stackTopOffset): it stands
     *  in the head band over the fan's columns, which have no head. */
    top: number;
    name: string;
    /** The fanned stack's cell count (the stack menu's policy). */
    count: number;
    /** The fanned stack's cell ids: a live run in any of them greys the
     *  menu's Merge and Remove. */
    cellIds: string[];
    /** The fanned stack holds the selection: name, line and Collapse brass. */
    selected: boolean;
    /** A drop target sits inside the fan: the line lights teal. */
    targeted: boolean;
    /** The same x/width transition the scene's columns use, so the bracket
     *  tracks them through reorders and resizes. */
    transition: Transition;
    onCollapse: () => void;
    onSelectStack: () => void;
    /** The stack menu's actions on the fanned stack (Collapse is onCollapse). */
    onRename?: (name: string) => void;
    onDuplicateStack?: () => void;
    onMergeStack?: () => void;
    /** Remove the fanned stack. */
    onRemove: () => void;
    /** The bracket pans the camera like a stack head: press and drag slides
     *  the view (useStackCamera's centre pan). A plain click centres
     *  nothing: a fan is usually wider than the viewport, so there is no one
     *  place to centre on. Omitted → a press only selects the stack. */
    onPanStart?: (e: React.PointerEvent) => void;
    onPanMove?: (e: React.PointerEvent) => void;
    /** Returns whether the gesture moved (the trailing click is swallowed). */
    onPanEnd?: () => boolean;
    /** The scene camera (useStackCamera): world x at the viewport centre
     *  and the zoom. With the viewport's width they keep the bracket's name
     *  and Collapse inside its visible part. Omitted → the content spans
     *  the whole bracket. */
    cameraX?: MotionValue<number>;
    zoom?: MotionValue<number>;
    /** Width of the scene root (the layout's viewSize.width). */
    viewportWidth?: number;
}

/** The content's narrowest width, used when only a sliver of the bracket is
 *  on screen: the name down to a few characters, Collapse and … whole. */
const HEADER_MIN_CONTENT_WIDTH = 220;
/** The bracket's lead before the name (the mock's `.flead`). */
const LEAD = 16;
/** On the track the line's ends draw in this far, as the rail sits inside its
 *  track; the lead gives up the width it gains in margin, so the name holds. */
const TRACK_INSET = 8;

/** A fanned-out stack's bracket (the mock's `.fan-head`): a lead, the name
 *  "· fanned" (the name reads as it does on the stack head, no count), a
 *  line over the columns, Collapse and the stack menu. No
 *  border and no per-column heads: the bracket stands in the head band of
 *  the fan's columns. Drawn in world space by StackScene at z 115, above
 *  the surface overlay (110) so no pooled surface paints over it, below
 *  moving / dragged / key-request stacks (500+).
 *
 *  The line answers the pointer the way a stack head's rail does: a sheet-2
 *  field comes up behind it and it goes a step lighter. A press on the
 *  bracket selects the fanned stack and drags the camera.
 *
 *  A fan is often wider than the viewport, so the content (the name and
 *  Collapse) clamps to the bracket's on-screen part (fanHeaderSpan): it
 *  slides with the camera and the bracket's own animation through motion
 *  values (the bracket's x/width are motion values its animate prop
 *  drives, so the clamp reads where it is this frame, not where it is
 *  heading), never a React render. */
export function FanFrame({
    left, width, top, name, count, cellIds, selected, targeted, transition,
    onCollapse, onSelectStack, onRename, onDuplicateStack, onMergeStack, onRemove,
    onPanStart, onPanMove, onPanEnd,
    cameraX, zoom, viewportWidth = 0,
}: FanFrameProps) {
    const reduceMotion = useReducedMotion();
    const collapseTooltip = useShortcutTooltip(SHORTCUT_IDS.SCENE_FAN_OUT, 'Collapse fan-out');
    // A stack head's press-and-drag pan, without its click-to-centre (no
    // onCenterStack): the gesture captures the pointer, slides the camera
    // and swallows the click that ends a real drag.
    const { handlers: pan } = useCenterPanGesture({
        onCenterPanStart: onPanStart, onCenterPanMove: onPanMove, onCenterPanEnd: onPanEnd,
    });
    const frameX = useMotionValue(left);
    const frameWidth = useMotionValue(width);
    const headerSpan = () => fanHeaderSpan(
        frameX.get(),
        frameWidth.get(),
        cameraX ? { cameraX: cameraX.get(), zoom: zoom?.get() ?? 1, width: viewportWidth } : null,
        HEADER_MIN_CONTENT_WIDTH,
    );
    const contentX = useTransform(() => headerSpan().offset);
    const contentWidth = useTransform(() => headerSpan().width);

    const [onTrack, setOnTrack] = useState(false);
    const [menuOpen, setMenuOpen] = useState(false);
    const [renaming, setRenaming] = useState(false);
    const menuButtonRef = useRef<HTMLButtonElement>(null);
    const menuId = useId();
    const closeMenu = useCallback(() => setMenuOpen(false), []);
    // Merge and Remove grey out while a run is live in the stack (a run's
    // files are its lock).
    const hasLiveRun = useCollimateStore(s => cellIds.some(id => cellHasLiveRun(s, id)));

    const label = name || 'Untitled stack';
    const nameTone = selected ? 'text-brass' : 'text-ink-2';
    const line = targeted ? 'var(--teal)' : selected ? 'var(--brass)' : onTrack ? 'var(--ink-2)' : 'var(--line-2)';
    const ease = reduceMotion ? 'none' : 'background 120ms ease, width 120ms ease, margin 120ms ease';
    const menuItems = buildStackMenuItems({
        cellCount: count, hasLiveRun, fanned: true,
        onRename: onRename ? () => setRenaming(true) : undefined,
        onToggleFan: onCollapse, onDuplicateStack, onMergeStack,
        // Rechecked at activation so a newly live run cannot lose its stack.
        onRemove: () => {
            const store = useCollimateStore.getState();
            if (cellIds.some(id => cellHasLiveRun(store, id))) return;
            onRemove();
        },
    });

    return (
        <motion.div
            data-fan-frame
            style={{
                position: 'absolute',
                left: 0,
                x: frameX,
                width: frameWidth,
                top,
                height: FAN_HEADER_HEIGHT,
                zIndex: 115,
            }}
            // x / width animate the style motion values above.
            initial={{ opacity: 0 }}
            animate={{ x: left, width, opacity: 1 }}
            transition={{ x: transition, width: transition, opacity: { duration: 0.15 } }}
        >
            <div
                role="group"
                aria-label={`Fanned-out stack: ${label}`}
                data-selected={selected || undefined}
                className="group/fan absolute inset-0 text-[0.75rem] text-ink-3"
                style={{ userSelect: 'none', touchAction: 'none' }}
                // The scene root pans on pointerdown; a press on the bracket
                // selects the fanned stack instead, without starting a text
                // selection, and starts the camera pan.
                onPointerDown={(e) => {
                    e.stopPropagation();
                    blockNativeSelection(e);
                    onSelectStack();
                    pan.onPointerDown(e);
                }}
                onPointerMove={pan.onPointerMove}
                onPointerUp={pan.onPointerUp}
                onPointerCancel={pan.onPointerCancel}
                onClick={pan.onClick}
            >
                <motion.div
                    data-fan-header-content
                    style={{
                        position: 'absolute',
                        top: 0,
                        bottom: 0,
                        left: 0,
                        x: contentX,
                        width: contentWidth,
                        boxSizing: 'border-box',
                        overflow: 'hidden',
                        display: 'flex',
                        alignItems: 'center',
                        // The line on the head's top line, as a stack head's name.
                        padding: '0.0625rem 0 0.6875rem',
                    }}
                >
                    <span
                        data-fan-track
                        className="flex min-w-0 flex-1 items-center self-stretch rounded-xs"
                        onPointerEnter={() => setOnTrack(true)}
                        onPointerLeave={() => setOnTrack(false)}
                        style={{
                            background: onTrack ? 'var(--sheet-2)' : 'transparent',
                            cursor: onPanStart ? 'grab' : undefined,
                            transition: reduceMotion ? 'none' : 'background 120ms ease',
                        }}
                    >
                        <span
                            data-fan-lead
                            style={{
                                flex: 'none', height: 1, background: line, transition: ease,
                                width: onTrack ? LEAD - TRACK_INSET : LEAD,
                                marginLeft: onTrack ? TRACK_INSET : 0,
                            }}
                        />
                        {renaming && onRename ? (
                            <StackRenameInput
                                value={name}
                                onSave={(next) => {
                                    setRenaming(false);
                                    if (next !== name) onRename(next);
                                }}
                                onCancel={() => setRenaming(false)}
                                className="mx-1.5 w-[11.25rem]"
                            />
                        ) : (
                            <span data-fan-name className={cn('truncate px-2.5 font-medium leading-[1.125rem]', nameTone)}>
                                {label} · fanned
                            </span>
                        )}
                        <span
                            data-fan-line
                            style={{
                                flex: 1, minWidth: 0, height: 1, background: line, transition: ease,
                                marginRight: onTrack ? TRACK_INSET : 0,
                            }}
                        />
                    </span>
                    <button
                        type="button"
                        aria-label="Collapse fan-out"
                        title={collapseTooltip}
                        className={cn(
                            'ml-1.5 inline-flex h-[1.125rem] flex-none items-center gap-1.5 rounded-sm px-2 transition-colors',
                            selected ? 'text-brass hover:bg-brass-soft' : 'text-ink-3 hover:bg-sheet-2 hover:text-ink',
                        )}
                        onPointerDown={(e) => e.stopPropagation()}
                        onClick={(e) => { e.stopPropagation(); onCollapse(); }}
                    >
                        <FoldHorizontal className="w-3.5 h-3.5" strokeWidth={1.6} />
                        <span>Collapse</span>
                    </button>
                    <button
                        ref={menuButtonRef}
                        type="button"
                        title="Stack menu"
                        aria-label="Stack menu"
                        aria-haspopup="menu"
                        aria-expanded={menuOpen}
                        aria-controls={menuOpen ? menuId : undefined}
                        className={cn(
                            'ml-0.5 inline-flex h-[1.125rem] w-[1.375rem] flex-none items-center justify-center rounded-xs text-ink-3',
                            'transition-colors duration-[120ms] motion-reduce:transition-none',
                            'hover:bg-sheet-2 hover:text-ink',
                            // On hover, while open, and always on the stack
                            // that holds the selection. Hidden is a colour
                            // (transparent), so showing it is a colour fade:
                            // an opacity fade runs on the compositor and
                            // turns nearby text grayscale while it runs.
                            menuOpen
                                ? 'bg-sheet-2 text-ink'
                                : !selected && 'not-focus-visible:not-group-hover/fan:text-transparent',
                        )}
                        onPointerDown={(e) => e.stopPropagation()}
                        onClick={(e) => { e.stopPropagation(); setMenuOpen(o => !o); }}
                    >
                        <Ellipsis className="w-3.5 h-3.5" strokeWidth={1.6} />
                    </button>
                </motion.div>
            </div>
            {/* Outside the bracket's handlers: the menu portals to the body,
                but its events still bubble through the React tree. */}
            <StackMenu
                anchorRef={menuButtonRef}
                open={menuOpen}
                id={menuId}
                label={`Stack menu: ${label}`}
                items={menuItems}
                onClose={closeMenu}
            />
        </motion.div>
    );
}
