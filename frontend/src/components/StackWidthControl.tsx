import React, { useCallback, useEffect, useRef, useState } from 'react';
import { MoveHorizontal } from 'lucide-react';
import { cn } from '@/lib/utils';
import { btn, formLabel, iconBtn, sectionLabel } from './ui/formStyles';
import { Menu, type MenuCloseReason } from './ui/Menu';
import { Switch } from '@/components/ui/switch';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { useShortcutTooltip } from '@/lib/keybindings/useShortcutDisplay';
import { SHORTCUT_IDS } from '@/lib/keybindings/ids';
import { MIN_STACK_WIDTH as MIN } from '@/hooks/useStackLayout';
import { allStacksOn, channelDefaultWidth, fullStackWidth, setChannelDefaultWidth, toggleMaxWidth } from '@/lib/stackWidth';
import { commandPaneInsetOf } from '@/lib/commandPane';

const POPUP_WIDTH = 256;
/** The popup's small quiet buttons: the steps (mono) and the actions. */
const widthAction = cn(btn, 'h-[1.375rem] px-2 text-[0.71875rem]');
const widthStep = cn(widthAction, 'font-mono');

type Spatial = ReturnType<typeof useSpatialStore.getState>;

/** Commit a width to the shared value every stack uses, dropping per-stack overrides. */
function commitSharedWidth(s: Spatial, w: number): void {
    s.setMinCellSize(w);
    s.clearStackWidths();
    if (s.isMaxWidth) s.setIsMaxWidth(false);
}

/** Apply a stack width through the channel's "all stacks" preference: the
 *  shared width (every stack) or this stack's own override. Stack width
 *  never depends on a cell's files pane (D-C17): the pane opens inside the
 *  cell and its content reflows. */
function applyStackWidthPreference(stackId: string, w: number): void {
    const s = useSpatialStore.getState();
    const clamped = clampWidth(w, fullStackWidth());
    const channelId = useCollimateStore.getState().activeChannel?.id;
    if (allStacksOn(s, channelId)) commitSharedWidth(s, clamped);
    else s.setStackWidth(stackId, clamped);
}

/** Largest stack width that fits the canvas (the viewport less the open
 *  Commands pane) — the slider's dynamic max. */
function useScreenMax(): number {
    const inset = useCollimateStore(s => commandPaneInsetOf(s.commandPane));
    const [viewWidth, setViewWidth] = useState(() => typeof window === 'undefined' ? 0 : window.innerWidth);
    useEffect(() => {
        const update = () => setViewWidth(window.innerWidth);
        update();
        window.addEventListener('resize', update);
        return () => window.removeEventListener('resize', update);
    }, []);
    return viewWidth ? fullStackWidth(viewWidth, inset) : MIN;
}

const clampWidth = (w: number, screenMax: number) => Math.max(MIN, Math.min(w, screenMax));

interface Props {
    /** Column this control resizes (display id: a real stack id, or `fan-<cellId>` for a fan column). */
    stackId: string;
    /** Called when a drag actually starts (threshold crossed, not a click).
     *  StackItem selects the stack here so the post-drag camera tracking
     *  follows the stack under the cursor, not a stale selection. */
    onDragStart?: () => void;
}

/**
 * Per-stack width handle living on the cell header (the mock's Width tool):
 * a click opens the width popup (the shared Menu as a dialog) under the
 * button; click-and-drag resizes the stack live so the button tracks the
 * cursor. A per-channel "all stacks" toggle (default on) in the popup
 * decides whether an adjustment hits the shared width (every stack) or just
 * this one.
 */
export function StackWidthControl({ stackId, onDragStart }: Props) {
    const screenMax = useScreenMax();

    // Store reads — narrow selectors so a width change on one stack doesn't
    // re-render every other stack's control.
    const minCellSize = useSpatialStore(s => s.minCellSize);
    const override = useSpatialStore(s => s.stackWidths[stackId]);
    const channelId = useCollimateStore(s => s.activeChannel?.id);
    const allStacks = useSpatialStore(s => allStacksOn(s, channelId));
    const sharedMax = useSpatialStore(s => s.isMaxWidth);
    const columnMax = useSpatialStore(s => !!s.maxColumns[stackId]);

    const setStackWidth = useSpatialStore(s => s.setStackWidth);
    const setAllStacks = useSpatialStore(s => s.setAllStacks);
    const setWidthDrag = useSpatialStore(s => s.setWidthDrag);

    // Pending anchor-clear frame (scheduled on drag release). Tracked so a
    // new drag starting before it fires can cancel it — otherwise the stale
    // clear would drop the new drag's anchor mid-gesture.
    const clearAnchorRafRef = useRef<number | null>(null);

    // If this control unmounts mid-drag (stack removed by a server push),
    // drop the anchor so the layout/camera don't stay frozen on a ghost —
    // and cancel any in-flight clear so it can't fire into a later drag.
    useEffect(() => () => {
        if (clearAnchorRafRef.current !== null) cancelAnimationFrame(clearAnchorRafRef.current);
        const s = useSpatialStore.getState();
        if (s.widthDrag?.id === stackId) s.setWidthDrag(null);
    }, [stackId]);

    const resolved = clampWidth(override ?? minCellSize, screenMax);
    // The max toggle reads on as lib/stackWidth isMaxWidthOn decides it.
    const maxOn = (allStacks ? sharedMax : columnMax) || resolved >= screenMax - 1;

    const decTooltip = useShortcutTooltip(SHORTCUT_IDS.SCENE_WIDTH_DEC, 'Decrease width by 50px');
    const incTooltip = useShortcutTooltip(SHORTCUT_IDS.SCENE_WIDTH_INC, 'Increase width by 50px');
    const maxTooltip = useShortcutTooltip(SHORTCUT_IDS.SCENE_FULLSCREEN, 'Max width');
    // Reset returns to the channel's saved default ("Set current"), or 500.
    const resetWidth = useChannelSettingsStore(s => channelDefaultWidth(s.doc));
    const resetTooltip = useShortcutTooltip(SHORTCUT_IDS.SCENE_WIDTH_RESET, `Reset to default ${resetWidth}px`);

    // "Set current" — store THIS width as the channel's default, so Reset
    // returns to it from now on: the one setter the max toggle uses too
    // (lib/stackWidth setChannelDefaultWidth).
    const setCurrentAsDefault = useCallback(() => {
        if (channelId) setChannelDefaultWidth(resolved, channelId);
    }, [channelId, resolved]);

    // Apply a width per the channel's "all stacks" setting.
    const apply = useCallback((w: number) => applyStackWidthPreference(stackId, w), [stackId]);

    // ── Popup open/close + anchoring ────────────────────────────────────
    // The popup hangs from where the button was when it opened: its own
    // steps move the button (the stack resizes), and the popup must stay put
    // under the pointer rather than close as a menu does when its anchor
    // moves. It closes once the button leaves the DOM.
    const [open, setOpen] = useState(false);
    const buttonRef = useRef<HTMLButtonElement | null>(null);
    const openRectRef = useRef<DOMRect | null>(null);
    // Where focus was before the popup opened: Esc, Tab out and the steps
    // hand it back there, not to the button.
    const previousFocusRef = useRef<Element | null>(null);
    const anchor = useCallback(
        () => (buttonRef.current?.isConnected ? openRectRef.current : null), [],
    );
    const openPopup = useCallback(() => {
        openRectRef.current = buttonRef.current?.getBoundingClientRect() ?? null;
        previousFocusRef.current = document.activeElement;
        setOpen(true);
    }, []);
    // A press that starts on the button while the popup is open: the
    // popup's outside-press close runs before the button sees the press, so
    // the press's click must not open it again. Read in window capture,
    // ahead of that close.
    const pressedWhileOpenRef = useRef(false);
    useEffect(() => {
        if (!open) return;
        const onDown = (e: PointerEvent) => {
            pressedWhileOpenRef.current = e.target instanceof Node && !!buttonRef.current?.contains(e.target);
        };
        window.addEventListener('pointerdown', onDown, true);
        return () => window.removeEventListener('pointerdown', onDown, true);
    }, [open]);
    const closePopup = useCallback((reason: MenuCloseReason) => {
        setOpen(false);
        const previous = previousFocusRef.current;
        if ((reason === 'escape' || reason === 'tab') && previous instanceof HTMLElement && previous.isConnected) {
            previous.focus({ preventScroll: true });
        }
    }, []);

    // ── Click vs drag ───────────────────────────────────────────────────
    // pointerdown captures the start; a move past the threshold becomes a
    // live resize (button tracks the cursor via startWidth + dx); a release
    // with no drag is a click that toggles the popup.
    // `wasOpen`: the press began on an open popup (see pressedWhileOpenRef).
    const dragRef = useRef<{ startX: number; startWidth: number; lastWidth: number; pointerId: number; dragging: boolean; wasOpen: boolean } | null>(null);

    const onPointerDown = (e: React.PointerEvent) => {
        if (e.button !== 0) return;
        e.stopPropagation();
        e.preventDefault();
        dragRef.current = { startX: e.clientX, startWidth: resolved, lastWidth: resolved, pointerId: e.pointerId, dragging: false, wasOpen: pressedWhileOpenRef.current };
        pressedWhileOpenRef.current = false;
        // setPointerCapture keeps move/up on the handle even when the
        // cursor leaves it mid-drag. Guarded — jsdom doesn't implement it.
        try { (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); } catch { /* unsupported */ }
    };
    const onPointerMove = (e: React.PointerEvent) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.pointerId) return;
        const dx = e.clientX - d.startX;
        if (!d.dragging && Math.abs(dx) <= 4) return;
        if (!d.dragging) {
            d.dragging = true;
            // A clear scheduled by a just-released drag must not fire into
            // this one and drop its anchor mid-gesture.
            if (clearAnchorRafRef.current !== null) {
                cancelAnimationFrame(clearAnchorRafRef.current);
                clearAnchorRafRef.current = null;
            }
            // Anchor mode on: useStackLayout pins this stack's LEFT edge on
            // width changes (instead of the selected stack's center, which
            // grows the stack out of both sides and lets the cursor escape
            // the handle) and useStackCamera freezes. Width follows the
            // cursor 1:1, so the right edge — and this handle riding it —
            // stays exactly under the pointer. Selecting the stack
            // (onDragStart) makes the post-drag camera tracking follow the
            // stack under the cursor rather than a stale selection.
            setWidthDrag({ id: stackId, anchor: 'left' });
            onDragStart?.();
        }
        const w = clampWidth(d.startWidth + dx, fullStackWidth());
        d.lastWidth = w;
        // During the drag, resize ONLY this stack — even when "all stacks"
        // is on. The "all stacks" commit is deferred to release; the end
        // state is the same, just without the mid-drag jitter.
        setStackWidth(stackId, w);
    };
    const endDrag = (e: React.PointerEvent) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.pointerId) return;
        dragRef.current = null;
        try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch { /* already released */ }
        if (!d.dragging) {
            // A release that never crossed the threshold is a click —
            // toggle the popup.
            if (d.wasOpen) setOpen(false);
            else openPopup();
            return;
        }
        // Drag finished. In "all stacks" mode, push the final width to the
        // shared value and drop the temporary per-stack override we wrote
        // mid-drag, so every stack lands at the same width. In per-stack
        // mode the override IS the final state — leave it.
        const s = useSpatialStore.getState();
        if (allStacksOn(s, channelId)) commitSharedWidth(s, d.lastWidth);
        // Drop the anchor a frame later — the commit above must render with
        // the left-edge anchor still active, or the all-stacks reflow would
        // re-anchor on the selected stack's center and jump the handle out
        // from under the released pointer. The camera unfreezes then too.
        clearAnchorRafRef.current = requestAnimationFrame(() => {
            clearAnchorRafRef.current = null;
            const now = useSpatialStore.getState();
            if (now.widthDrag?.id === stackId) now.setWidthDrag(null);
        });
    };
    // A cancelled press is not a click: a drag already under way commits
    // like a release, but one that never crossed the threshold is dropped
    // without toggling the popup.
    const cancelDrag = (e: React.PointerEvent) => {
        const d = dragRef.current;
        if (!d || e.pointerId !== d.pointerId) return;
        if (d.dragging) { endDrag(e); return; }
        dragRef.current = null;
        try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch { /* already released */ }
    };

    return (
        <div className="relative flex items-center">
            <button
                ref={buttonRef}
                type="button"
                aria-label="Stack width"
                aria-haspopup="dialog"
                aria-expanded={open}
                title="Click to set stack width · drag to resize"
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerCancel={cancelDrag}
                // The pointer handlers own the click; a keyboard press opens it.
                onClick={(e) => { if (e.detail === 0) { if (open) setOpen(false); else openPopup(); } }}
                className={cn(iconBtn, 'h-6 w-6', open && 'bg-sheet-2 text-ink')}
                style={{ cursor: 'grab', touchAction: 'none' }}
            >
                <MoveHorizontal className="w-3.5 h-3.5" strokeWidth={1.6} />
            </button>

            <Menu
                anchor={anchor}
                open={open}
                onClose={closePopup}
                role="dialog"
                label="Stack width settings"
                align="end"
                minWidth={POPUP_WIDTH}
                maxWidth={POPUP_WIDTH}
                gap={6}
            >
                <div className="p-2">
                    <div className="flex items-center justify-between gap-2 mb-1.5">
                        <div className={sectionLabel}>
                            Stack width: <span className="text-ink font-mono normal-case tracking-normal">{resolved}px</span>
                        </div>
                        <label className="flex items-center gap-1.5 cursor-pointer select-none flex-none">
                            <span className={formLabel}>All stacks</span>
                            <Switch
                                checked={allStacks}
                                disabled={!channelId}
                                onCheckedChange={(v) => { if (channelId) setAllStacks(channelId, v); }}
                            />
                        </label>
                    </div>
                    <input
                        type="range"
                        aria-label="Width in pixels"
                        min={MIN}
                        max={screenMax}
                        step={10}
                        value={resolved}
                        onChange={(e) => apply(Number(e.target.value))}
                        className="w-full h-1.5 accent-teal cursor-pointer"
                    />
                    <div className="flex justify-between text-[0.6875rem] font-mono text-ink-3 mt-0.5">
                        <span>{MIN}</span>
                        <span>{screenMax}</span>
                    </div>
                    <div className="flex items-center justify-between mt-1.5">
                        <div className="flex items-center gap-1">
                            <button type="button" className={widthStep} onClick={() => apply(resolved - 50)} title={decTooltip}>
                                -
                            </button>
                            <button type="button" className={widthStep} onClick={() => apply(resolved + 50)} title={incTooltip}>
                                +
                            </button>
                            {/* The max toggle: the same operator as Alt+F and
                                the head's double-click (lib/stackWidth). */}
                            <button
                                type="button"
                                aria-pressed={maxOn}
                                className={cn(widthStep, 'aria-pressed:bg-sheet-2 aria-pressed:text-ink')}
                                onClick={() => toggleMaxWidth(stackId, channelId)}
                                title={maxTooltip}
                            >
                                Max
                            </button>
                        </div>
                        <div className="flex items-center gap-1">
                            <button
                                type="button"
                                className={widthAction}
                                onClick={() => apply(Math.min(resetWidth, screenMax))}
                                title={resetTooltip}
                            >
                                Reset
                            </button>
                            <button
                                type="button"
                                className={widthAction}
                                onClick={setCurrentAsDefault}
                                disabled={!channelId}
                                title={`Save ${resolved}px as this channel's default width — Reset returns to it`}
                            >
                                Set current
                            </button>
                        </div>
                    </div>
                </div>
            </Menu>
        </div>
    );
}
