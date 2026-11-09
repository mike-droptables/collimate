import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react';
import { animate, type ValueAnimationTransition } from 'motion/react';
import type { Stack } from '@/lib/types';
import { PendingMoveState } from '@/stores/useSpatialStore';
import { PHYSICS } from '@/lib/physics';
import { cameraSettling, ENTRY_VIEW_TOLERANCE } from '@/lib/stackEntry';
import { StackLayoutResult } from '@/hooks/useStackLayout';
import { usePanZoomCamera } from '@/hooks/usePanZoomCamera';
import { useSwipeNavigate } from './useSwipeNavigate';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { NO_INSET, commandPaneInsetOf, type ViewInset } from '@/lib/commandPane';

interface UseStackCameraParams {
    layout: StackLayoutResult;
    stacks: Stack[];
    selectedId: string | null;
    isZoomedOut: boolean;
    topology: 'added' | 'removed' | 'stable';
    pendingMove: PendingMoveState | null;
    centerRequested: number;
    /** Left/right edge-align request (Alt+[ / Alt+]) — see useSpatialStore. */
    edgeAlignRequested: { mode: 'left' | 'right'; nonce: number } | null;
    cameraHold: boolean;
    /** True while a column width drag is in flight (the header handle,
     *  incl. its release commit, or a card-side resize strip). The layout
     *  pins the dragged column's anchored edge, so any camera motion would
     *  slide the dragged edge out from under the pointer — the tracking +
     *  animation effects stand down and in-flight pans are stopped for the
     *  duration. */
    widthDragActive: boolean;
    cameraTargetRef: React.MutableRefObject<number>;
    rootDivRef: React.RefObject<HTMLDivElement | null>;
    /** The overview's whole area, where its wheel listens (usePanZoomCamera). */
    wheelRootRef?: React.RefObject<HTMLElement | null>;
    /** Step the selection one stack left (-1) / right (+1). Fired by the
     *  two-finger swipe gesture (trackpad wheel stream or touchscreen two-
     *  finger drag), which steps selection instead of free-panning. */
    onSwipeNavigate?: (delta: -1 | 1) => void;
}

// ── Pan-align snapping ──────────────────────────────────────────────
// While a center-pan drags the camera, three snap targets exist for the
// panned stack: its left edge at the screen's left inset, its center at
// the centerline, its right edge at the right inset (the same GAP inset
// the layout rests stacks at). Near one, the matching cell edge glows;
// releasing snaps the camera to it. Pure functions so the buffer and
// collapse rules are unit-testable.

export type PanAlignMode = 'left' | 'center' | 'right';

export interface AlignGeometry {
    /** Panned stack's world-x center. */
    stackCenter: number;
    stackWidth: number;
    viewWidth: number;
    gap: number;

}

/** How close (px) the camera must park to a target to arm the snap. */
export const PAN_ALIGN_BUFFER = 40;

/** Camera x that lands the stack at the given alignment. The left/right
 *  formulas are Effect 1's scroll-into-view targets; center is Effect
 *  2b's Alt+c target. */
export function alignCameraX(mode: PanAlignMode, g: AlignGeometry): number {
    const half = g.viewWidth / 2;
    if (mode === 'left') return (g.stackCenter - g.stackWidth / 2) + half - g.gap;
    if (mode === 'right') return (g.stackCenter + g.stackWidth / 2) - half + g.gap;
    return g.stackCenter;
}

/** Camera x that scrolls the stack into the visible band (the viewport
 *  minus `inset` and the GAP each side), or null when it already sits
 *  inside the band give or take `tolerance`. The camera moves only as far
 *  as it takes. A stack wider than the band has one target, its left edge
 *  at the band's left (just past a left pane), so the camera rests there
 *  instead of swapping edges on each call. Effect 1 passes
 *  ENTRY_VIEW_TOLERANCE, so for a stack that fits the band without an
 *  inset its no-pan test is lib/stackEntry slotInView at zoom 1. */
export function scrollIntoViewX(camX: number, g: AlignGeometry, tolerance: number, inset: ViewInset = NO_INSET): number | null {
    const left = g.stackCenter - g.stackWidth / 2;
    const right = g.stackCenter + g.stackWidth / 2;
    const half = g.viewWidth / 2;
    const visibleLeft = camX - half + g.gap + inset.left;
    const visibleRight = camX + half - g.gap - inset.right;
    if (g.stackWidth > visibleRight - visibleLeft) {
        const target = alignCameraX('left', g) - inset.left;
        return Math.abs(camX - target) <= tolerance ? null : target;
    }
    if (left < visibleLeft - tolerance) return alignCameraX('left', g) - inset.left;
    if (right > visibleRight + tolerance) return alignCameraX('right', g) + inset.right;
    return null;
}

/** The camera with a surface docked over the canvas, given the camera
 *  without it (`base`, the unslid camera). The base still tracks the
 *  selection as if nothing covered the canvas; the inset adds only the
 *  slide that clears the selected stack. Taking the inset away (the pane
 *  closes) leaves the base, so the canvas slides back; the inset on the
 *  other side (the pane docks across) slides from the base the other way. */
export function insetCameraX(base: number, g: AlignGeometry, tolerance: number, inset: ViewInset): { base: number; target: number } {
    const tracked = scrollIntoViewX(base, g, tolerance) ?? base;
    return { base: tracked, target: scrollIntoViewX(tracked, g, tolerance, inset) ?? tracked };
}

/** The inset the open Commands pane takes on its side (Effect 1, which
 *  applies it, does not track the selection in zoom-out). */
export function useCommandPaneInset(): ViewInset {
    return useCollimateStore(s => commandPaneInsetOf(s.commandPane));
}

/** The armed snap target for a camera position, or null when none is in
 *  range. When two or more targets are within the buffer at once — the
 *  stack is about as wide as the viewport, so the targets have collapsed
 *  onto each other — only center survives (top-edge glow, center snap). */
export function resolvePanAlignSnap(
    cameraX: number, g: AlignGeometry, buffer: number = PAN_ALIGN_BUFFER,
): PanAlignMode | null {
    const modes: PanAlignMode[] = ['left', 'center', 'right'];
    const hits = modes.filter(m => Math.abs(alignCameraX(m, g) - cameraX) <= buffer);
    if (hits.length >= 2) return 'center';
    return hits[0] ?? null;
}

export function useStackCamera({
    layout, stacks, selectedId, isZoomedOut, topology, pendingMove,
    centerRequested, edgeAlignRequested, cameraHold, widthDragActive, cameraTargetRef, rootDivRef,
    wheelRootRef, onSwipeNavigate,
}: UseStackCameraParams) {
    const { viewSize, GAP, positions, widths, overviewTarget,
        arrowWorldX, cellPendingMoveWorldX, sceneSwapNonce } = layout;

    // Pan/zoom primitive — owns motion values, wheel handler, drag handlers.
    // Wheel + drag are gated on isZoomedOut so the main view's wheel keeps
    // its native scroll behaviour.
    const {
        cameraXMV, cameraYMV, zoomMV, panX, panY,
        handlePanPointerDown, handlePanPointerMove, handlePanPointerUp,
    } = usePanZoomCamera({
        rootDivRef,
        wheelRootRef,
        viewSize,
        enabled: isZoomedOut,
        panBlockSelector: '[data-stack-card]',
        // The shelf's control row and the bar pan; the shelf's cards and the
        // journal pane keep their own wheel.
        wheelPansSelector: '[data-rm-shelf], [data-rm-bar]',
    });

    const [targetCameraX, setTargetCameraXState] = useState(0);
    const prevCenterRequestedRef = useRef(centerRequested);
    // The Commands pane covers one edge of the canvas; the selection stays
    // clear of it. paneBaseRef is the camera without the pane (insetCameraX)
    // from the pane's first slide until the slide is dropped: null while
    // the camera has no slide to undo.
    const paneInset = useCommandPaneInset();
    const paneOpen = paneInset.left > 0 || paneInset.right > 0;
    const paneBaseRef = useRef<number | null>(null);

    // What isCameraSettling knows about the camera's destination.
    // requestedTargetRef mirrors the last targetCameraX written and
    // processedTargetRef the last one Effect 2 acted on with its gates
    // open: they differ only between a write and the Effect 2 run it
    // triggers. (cameraTargetRef is no stand-in — the camera hold and the
    // drag edge-scroll write it without the state.) panGoalRef is where
    // the running pan spring is headed.
    const requestedTargetRef = useRef(0);
    const processedTargetRef = useRef(0);
    const panGoalRef = useRef(0);
    const setTargetCameraX = useCallback((x: number) => {
        requestedTargetRef.current = x;
        setTargetCameraXState(x);
    }, []);
    const animatePan = useCallback((x: number, transition: ValueAnimationTransition<number>) => {
        panGoalRef.current = x;
        animate(cameraXMV, x, transition);
    }, [cameraXMV]);

    const setCameraTarget = useCallback((x: number) => {
        cameraTargetRef.current = x;
        setTargetCameraX(x);
    }, [cameraTargetRef, setTargetCameraX]);

    // A destination someone chose (a pan, a snap, Alt+c, Alt+[ / Alt+], a
    // drag's edge scroll, a pending move, a channel switch, where a hold's
    // release or a width drag parks the camera): it replaces the pane's
    // slide, so closing the pane leaves the camera there.
    const placeCamera = useCallback((x: number) => {
        if (paneBaseRef.current !== null) paneBaseRef.current = x;
        setCameraTarget(x);
    }, [setCameraTarget]);

    // Manual horizontal pan, driven by a drag on a stack head or the top bar.
    // While it's active the tracking + animation effects below stand down
    // (manualPanRef) so the direct cameraXMV.set() calls aren't fought by the
    // spring; on release the parked position is committed as the new target
    // and held against the selected column (heldEdgesRef), so the next state
    // push doesn't spring it back.
    //
    // The drag isn't clamped at the app edge: while the cursor is in-bounds it
    // pans normally with the real (visible) cursor via clientX; the moment it
    // reaches the app's left/right edge — where a clamped drag would stall — it
    // requests Pointer Lock and switches to movementX deltas, which keep
    // flowing no matter how far the (now hidden) pointer "travels", so you can
    // slide the view forever and wrap around. Once it hides at the edge it
    // stays hidden until release. A plain click-to-recenter never reaches the
    // edge, so it never hides the cursor or flashes the lock notice. If the
    // lock is denied (or unsupported) we fall back to clientX — edge-clamped.
    const manualPanRef = useRef(false);
    const centerPanAbortedRef = useRef(false);
    // The selected column the user last placed the camera against (a
    // manual pan, Alt+c, Alt+[ / Alt+], a width drag) and its world edges
    // then. Effect 1 leaves the camera where the user put it while the
    // selection and those edges stay put; see Effect 1.
    const heldEdgesRef = useRef<{ id: string; left: number; right: number } | null>(null);
    const centerPanRef = useRef<{
        startCamX: number;
        accumDX: number;
        moved: boolean;
        requested: boolean;
        lastClientX: number;
        lockEl: Element;
        /** Stack whose handle started the pan — the snap targets' subject. */
        stackId: string | null;
    } | null>(null);
    // The element this hook last asked to lock, until the lock drops. A
    // grant that lands after the pan already ended (release between the
    // request and the async grant) is recognised by it and exited.
    const requestedLockElRef = useRef<Element | null>(null);

    // A center-pan gesture is in flight (grab → release / Esc). State, not
    // just the ref, so the scene can stand its hover affordances down (the
    // cell-diff zones) for the gesture.
    const [centerPanning, setCenterPanning] = useState(false);

    // Armed pan-align snap target (drives the glow via StackScene and the
    // release snap below). State for the glow render; ref-mirrored so the
    // per-pointermove math can diff without re-render churn.
    const [panAlignSnap, setPanAlignSnap] = useState<{ stackId: string; mode: PanAlignMode } | null>(null);
    const panAlignSnapRef = useRef<{ stackId: string; mode: PanAlignMode } | null>(null);
    const updatePanSnap = useCallback((next: { stackId: string; mode: PanAlignMode } | null) => {
        const prev = panAlignSnapRef.current;
        if (prev === next) return;
        if (prev && next && prev.stackId === next.stackId && prev.mode === next.mode) return;
        panAlignSnapRef.current = next;
        setPanAlignSnap(next);
    }, []);

    // Layout snapshot the (stable-identity) pan handlers read through —
    // same ref pattern as useStackDrag's dragInputsRef.
    const alignGeomRef = useRef({ stacks, positions, widths, viewWidth: viewSize.width, gap: GAP, selectedId });
    useLayoutEffect(() => {
        alignGeomRef.current = { stacks, positions, widths, viewWidth: viewSize.width, gap: GAP, selectedId };
    }, [stacks, positions, widths, viewSize.width, GAP, selectedId]);

    const geomForStack = useCallback((stackId: string): AlignGeometry | null => {
        const g = alignGeomRef.current;
        const idx = g.stacks.findIndex(s => s.id === stackId);
        if (idx === -1 || !(g.widths[idx] > 0)) return null;
        return {
            stackCenter: g.positions[idx],
            stackWidth: g.widths[idx],
            viewWidth: g.viewWidth,
            gap: g.gap,

        };
    }, []);

    // Record the selected column's edges as the camera's held placement
    // (heldEdgesRef). Called where the user puts the camera somewhere
    // against the selection: a moved pan's release, Alt+c, Alt+[ / Alt+].
    const holdSelectedColumn = useCallback(() => {
        const g = alignGeomRef.current;
        const idx = g.selectedId === null ? -1 : g.stacks.findIndex(s => s.id === g.selectedId);
        heldEdgesRef.current = idx === -1 ? null : {
            id: g.stacks[idx].id,
            left: g.positions[idx] - g.widths[idx] / 2,
            right: g.positions[idx] + g.widths[idx] / 2,
        };
    }, []);

    const handleCenterPanStart = useCallback((e: React.PointerEvent, stackId?: string) => {
        if (isZoomedOut) return;
        cameraXMV.stop();
        manualPanRef.current = true;
        centerPanAbortedRef.current = false;
        updatePanSnap(null);
        setCenterPanning(true);
        centerPanRef.current = {
            startCamX: cameraXMV.get(),
            accumDX: 0,
            moved: false,
            requested: false,
            lastClientX: e.clientX,
            lockEl: e.currentTarget as HTMLElement,
            stackId: stackId ?? null,
        };
    }, [cameraXMV, isZoomedOut, updatePanSnap]);

    const handleCenterPanMove = useCallback((e: React.PointerEvent) => {
        const p = centerPanRef.current;
        if (!p) return;
        // Once the lock engages, clientX freezes at the edge while movementX
        // keeps reporting real motion — so prefer movementX when locked, and
        // the clientX delta otherwise (the visible-cursor phase and the
        // denied-lock fallback). Both are deltas, so accumDX stays continuous
        // across the hand-off.
        const locked = document.pointerLockElement === p.lockEl;
        const dx = locked ? e.movementX : e.clientX - p.lastClientX;
        p.lastClientX = e.clientX;
        p.accumDX += dx;
        if (!p.moved && Math.abs(p.accumDX) > 4) p.moved = true;

        // Grab the pointer only when the cursor reaches the app's left/right
        // edge while still pushing outward — the moment a clamped drag would
        // stall. Before that the real cursor stays visible and pans normally;
        // from here it hides and movementX carries the pan past the edge, and
        // it stays hidden until release (requested once). Still inside the
        // gesture's transient activation (pointer held), so the lock is allowed.
        // void on old browsers / Promise on new ones — swallow any rejection.
        if (!locked && !p.requested) {
            const w = window.innerWidth;
            const EDGE = 2;
            const atEdge = (e.clientX <= EDGE && dx < 0) || (e.clientX >= w - EDGE && dx > 0);
            if (atEdge) {
                p.requested = true;
                requestedLockElRef.current = p.lockEl;
                try {
                    const req = p.lockEl.requestPointerLock?.() as unknown;
                    if (req && typeof (req as Promise<void>).then === 'function') {
                        (req as Promise<void>).catch(() => { /* clientX fallback */ });
                    }
                } catch { /* pointer lock unavailable — clientX fallback */ }
            }
        }

        const z = zoomMV.get() || 1;
        // Same world-follows-cursor math as the zoom-out pan: a rightward drag
        // lowers cameraX, which raises panX (= w/2 - cameraX), sliding the world
        // — and this stack — right so the grab point stays under the pointer.
        const newX = p.startCamX - p.accumDX / z;
        cameraXMV.set(newX);
        cameraTargetRef.current = newX;

        // Arm/disarm the align snap for the panned stack. Gated on `moved`
        // so a plain click on the handle never flashes a glow.
        if (p.moved && p.stackId) {
            const geom = geomForStack(p.stackId);
            const mode = geom ? resolvePanAlignSnap(newX, geom) : null;
            updatePanSnap(mode ? { stackId: p.stackId, mode } : null);
        }
    }, [cameraXMV, zoomMV, cameraTargetRef, geomForStack, updatePanSnap]);

    // Returns whether the gesture actually moved (so the caller can swallow the
    // trailing click and skip the recenter).
    const handleCenterPanEnd = useCallback((): boolean => {
        if (document.pointerLockElement) document.exitPointerLock();
        const p = centerPanRef.current;
        const snap = panAlignSnapRef.current;
        updatePanSnap(null);
        setCenterPanning(false);
        centerPanRef.current = null;
        manualPanRef.current = false;
        if (centerPanAbortedRef.current) {
            // Esc dropped the lock mid-drag (finalized in the effect below);
            // swallow this trailing click so it doesn't recenter.
            centerPanAbortedRef.current = false;
            return true;
        }
        if (!p) return false;
        if (p.moved) {
            // Released on an armed snap target: spring the camera the last
            // few pixels onto the exact alignment. Otherwise commit where
            // the user parked it: a no-op animate to the current value when
            // Effect 2 next runs, instead of springing back to the pre-pan
            // target.
            const geom = snap ? geomForStack(snap.stackId) : null;
            if (snap && geom) {
                const target = alignCameraX(snap.mode, geom);
                placeCamera(target);
                // Start the spring here rather than leaning on Effect 2:
                // when the pan left an aligned position and re-snaps to the
                // SAME alignment, targetCameraX already holds this value —
                // the state write bails identical, the effect never re-runs,
                // and the camera would stay parked just off the target.
                animatePan(target, PHYSICS.cameraPan);
            } else {
                placeCamera(cameraXMV.get());
            }
            holdSelectedColumn();
        }
        return p.moved;
    }, [cameraXMV, placeCamera, animatePan, geomForStack, updatePanSnap, holdSelectedColumn]);

    // Esc unlocks the pointer without a pointerup. Finalize the pan ourselves
    // so the camera stays where it was parked and the tracking effects stand
    // back up; the abort flag makes the trailing click a no-op. Our own
    // exitPointerLock() on pan-end clears centerPanRef first, so this no-ops
    // for the normal release path.
    useEffect(() => {
        const onLockChange = () => {
            if (document.pointerLockElement) {
                // Lock acquired. Our own grant with no pan in flight arrived
                // after the release: exit it, or the cursor stays hidden.
                if (!centerPanRef.current && document.pointerLockElement === requestedLockElRef.current) {
                    requestedLockElRef.current = null;
                    document.exitPointerLock();
                }
                return;
            }
            requestedLockElRef.current = null;
            const p = centerPanRef.current;
            if (!p) return;
            centerPanRef.current = null;
            manualPanRef.current = false;
            centerPanAbortedRef.current = true;
            setCenterPanning(false);
            // Esc is a cancel — park in place, never take the snap.
            updatePanSnap(null);
            if (p.moved) {
                placeCamera(cameraXMV.get());
                holdSelectedColumn();
            }
        };
        document.addEventListener('pointerlockchange', onLockChange);
        return () => document.removeEventListener('pointerlockchange', onLockChange);
    }, [cameraXMV, placeCamera, updatePanSnap, holdSelectedColumn]);

    useSwipeNavigate(rootDivRef, isZoomedOut, onSwipeNavigate);

    // Hold gates new destinations while an existing spring may finish.
    // Release commits its current position, never rewinds to the entry point.
    const wasCameraHeldRef = useRef(false);
    useLayoutEffect(() => {
        if (wasCameraHeldRef.current && !cameraHold) {
            // stop() samples the spring at the cancellation time; commit that
            // latest sample rather than the preceding animation frame.
            cameraXMV.stop();
            placeCamera(cameraXMV.get());
        }
        wasCameraHeldRef.current = cameraHold;
    }, [cameraHold, cameraXMV, placeCamera]);

    // Width drag: park the camera for the whole gesture. Kill any
    // in-flight pan spring and commit the current position as the target,
    // so nothing moves while the drag runs and the gate release (below)
    // finds camera == target and starts no catch-up animation.
    useEffect(() => {
        if (!widthDragActive) return;
        cameraXMV.stop();
        placeCamera(cameraXMV.get());
    }, [widthDragActive, cameraXMV, placeCamera]);

    // Where the user put the camera against the selected column, it stays:
    // a manual pan, Alt+c and Alt+[ / Alt+] record the column's edges
    // (holdSelectedColumn), and so does a width drag when it ends (below),
    // so a drag that pushed an edge past the viewport doesn't pan that edge
    // out from under the pointer on release. Effect 1 doesn't track the
    // column while the selection and those edges stay put: `stacks` takes a
    // new identity on every state push (a save's echo, the idle checkpoint,
    // a run elsewhere), and re-tracking then would spring a column the user
    // panned partly off screen back into view with no input. Any other
    // change to the column (a move, a resize by other means), a new
    // selection or the Commands pane opening, docking or closing resumes
    // tracking.
    const widthDragEndedRef = useRef(false);
    const trackedInsetRef = useRef(paneInset);

    // Effect 1: Camera tracking (sets targetCameraX)
    useEffect(() => {
        if (widthDragActive) {
            widthDragEndedRef.current = true;
            return;
        }
        if (isZoomedOut) return;
        if (manualPanRef.current) return;

        if (pendingMove) {
            const trackX = pendingMove.type === 'cell' ? cellPendingMoveWorldX : arrowWorldX;
            if (trackX !== null) {
                const newCamX = trackX;
                if (Math.abs(newCamX - cameraTargetRef.current) > 1) placeCamera(newCamX);
                return;
            }
        }

        if (cameraHold) return;

        const trackIdx = stacks.findIndex(s => s.id === selectedId);
        if (trackIdx === -1) return;

        const trackX = positions[trackIdx];
        const trackW = widths[trackIdx];
        const trackLeft = trackX - trackW / 2;
        const trackRight = trackX + trackW / 2;

        if (widthDragEndedRef.current && selectedId) {
            widthDragEndedRef.current = false;
            heldEdgesRef.current = { id: selectedId, left: trackLeft, right: trackRight };
        }
        if (trackedInsetRef.current.left !== paneInset.left || trackedInsetRef.current.right !== paneInset.right) {
            trackedInsetRef.current = paneInset;
            heldEdgesRef.current = null;
        }
        const held = heldEdgesRef.current;
        if (held) {
            if (held.id === selectedId && Math.abs(held.left - trackLeft) < 1
                && Math.abs(held.right - trackRight) < 1) return;
            heldEdgesRef.current = null;
        }

        const geom = { stackCenter: trackX, stackWidth: trackW, viewWidth: viewSize.width, gap: GAP };
        let newCamX: number | null;
        if (paneOpen || paneBaseRef.current !== null) {
            // The pane's slide rides on the camera without the pane: opening
            // starts it from where the camera is headed, docking across
            // re-slides from the same base, and closing drops it (the base
            // tracks the selection with no inset, so the canvas re-centres
            // the selected cell as it would have without the pane).
            const { base, target } = insetCameraX(
                paneBaseRef.current ?? cameraTargetRef.current, geom, ENTRY_VIEW_TOLERANCE,
                paneOpen ? paneInset : NO_INSET);
            paneBaseRef.current = paneOpen ? base : null;
            newCamX = target;
        } else {
            newCamX = scrollIntoViewX(cameraXMV.get(), geom, ENTRY_VIEW_TOLERANCE);
        }
        if (newCamX !== null && Math.abs(newCamX - cameraTargetRef.current) > 1) setCameraTarget(newCamX);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [selectedId, stacks, positions, widths, viewSize.width, isZoomedOut,
        pendingMove, arrowWorldX, cellPendingMoveWorldX, setCameraTarget, placeCamera, cameraHold,
        widthDragActive, paneInset.left, paneInset.right]);

    // Effect 2b: Alt-C centering
    //
    // ``prevCenterRequestedRef`` is updated only after a successful center,
    // not on every fire. The early-return guards (zoomed-out, selectedId
    // missing from stacks) can trigger transiently right after a fan-out
    // collapse or a channel switch — the display selectedId can briefly be
    // absent from stacks (a ``fan-<cellId>`` that just collapsed, or a real
    // id before the new stacks land). If we consumed the bump up-front, the
    // request would silently die there: the dep change that fixes
    // selectedId wouldn't see a fresh ``centerRequested`` to act on, and
    // the camera would stay parked mid-pan. Cells then read as "shrunk"
    // (far from camera) and renderers land in display:none from the
    // offscreen gate. Deferring the ref update lets the next dep change
    // (selectedId resolves, isZoomedOut flips off) re-enter and finish.
    useEffect(() => {
        if (centerRequested === prevCenterRequestedRef.current) return;
        if (isZoomedOut) return;
        const idx = stacks.findIndex(s => s.id === selectedId);
        if (idx === -1) return;
        const stackX = positions[idx];
        placeCamera(stackX);
        holdSelectedColumn();
        prevCenterRequestedRef.current = centerRequested;
    }, [centerRequested, selectedId, stacks, positions, isZoomedOut, placeCamera, holdSelectedColumn]);

    // Effect 2c: Alt+[ / Alt+] edge alignment — the left/right siblings of
    // Alt+c. Same deferred-consume pattern as Effect 2b: the nonce ref only
    // advances after a successful align, so a request that fires during a
    // transient guard (selection mid-flight) completes on the next dep pass.
    const prevEdgeAlignNonceRef = useRef(edgeAlignRequested?.nonce ?? 0);
    useEffect(() => {
        if (!edgeAlignRequested) return;
        if (edgeAlignRequested.nonce === prevEdgeAlignNonceRef.current) return;
        if (isZoomedOut) return;
        const idx = stacks.findIndex(s => s.id === selectedId);
        if (idx === -1 || !(widths[idx] > 0)) return;
        placeCamera(alignCameraX(edgeAlignRequested.mode, {
            stackCenter: positions[idx],
            stackWidth: widths[idx],
            viewWidth: viewSize.width,
            gap: GAP,

        }));
        holdSelectedColumn();
        prevEdgeAlignNonceRef.current = edgeAlignRequested.nonce;
    }, [edgeAlignRequested, selectedId, stacks, positions, widths, viewSize.width, GAP,
        isZoomedOut, placeCamera, holdSelectedColumn]);

    const wasZoomedOutRef = useRef(false);
    const zoomFittedRef = useRef(false);

    // Effect 2: Camera animation
    useEffect(() => {
        if (cameraHold) return;
        if (manualPanRef.current) return;
        if (widthDragActive) return;

        if (isZoomedOut) {
            if (!zoomFittedRef.current) {
                animatePan(overviewTarget.x, { ...PHYSICS.standard });
                animate(cameraYMV, overviewTarget.y, { ...PHYSICS.standard });
                animate(zoomMV, overviewTarget.zoom, { ...PHYSICS.standard });
                zoomFittedRef.current = true;
            }
            wasZoomedOutRef.current = true;
        } else {
            zoomFittedRef.current = false;
            // Guard the no-op re-targets: this effect re-runs on every stacks
            // update (server polls, SSE pushes), and unconditionally spawning
            // fresh animations toward values the camera already sits at is
            // pure churn — and each restart can clip an in-flight spring's
            // settle tail, which reads as micro-stutter during add/remove.
            if (cameraYMV.get() !== 0) animate(cameraYMV, 0, { ...PHYSICS.standard });
            // The zoom-in branch below writes a new target; act on it in
            // this run rather than the closed-over (pre-zoom-out) value.
            // A release layout effect can publish its parked goal before this
            // render's passive effect runs; use that current goal, not a stale
            // state closure that would restart the old spring.
            let target = requestedTargetRef.current;
            if (wasZoomedOutRef.current) {
                const idx = stacks.findIndex(s => s.id === selectedId);
                if (idx !== -1) {
                    const newCamX = scrollIntoViewX(cameraTargetRef.current, {
                        stackCenter: positions[idx],
                        stackWidth: widths[idx],
                        viewWidth: viewSize.width,
                        gap: GAP,

                    }, 0, paneInset) ?? cameraTargetRef.current;
                    cameraTargetRef.current = newCamX;
                    setTargetCameraX(newCamX);
                    target = newCamX;
                }
                wasZoomedOutRef.current = false;
            }
            if (zoomMV.get() !== 1) animate(zoomMV, 1, { ...PHYSICS.standard });
            processedTargetRef.current = target;
            if (cameraXMV.get() !== target
                && !(cameraXMV.isAnimating() && panGoalRef.current === target)) {
                const panConfig = topology !== 'stable' ? PHYSICS.cameraPanTopology : PHYSICS.cameraPan;
                animatePan(target, panConfig);
            }
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isZoomedOut, overviewTarget, targetCameraX, cameraHold, widthDragActive,
        stacks, selectedId, positions, widths, viewSize.width, viewSize.height, topology]);

    // Scene swap (channel switch): useStackLayout just
    // rebased world coordinates (layout offset reset), so both the camera's
    // current position and its target are stale. Hard-land on the selected
    // stack — the scene crossfade covers the cut, and springing across the
    // rebased coordinate space is exactly the "camera flies in from nowhere"
    // glitch. Declared AFTER Effect 2 so the jump cancels any spring Effect 2
    // started toward the stale target in the same commit. The snap is pended
    // until the selection resolves: on a swap frame ``selectedId`` can
    // briefly point at a stack that isn't in the new set yet (the selection
    // invariant lands one effect-tick later).
    const pendingSceneSnapRef = useRef(false);
    const prevSceneSwapNonceRef = useRef(sceneSwapNonce);
    useEffect(() => {
        if (sceneSwapNonce !== prevSceneSwapNonceRef.current) {
            prevSceneSwapNonceRef.current = sceneSwapNonce;
            pendingSceneSnapRef.current = true;
        }
        if (!pendingSceneSnapRef.current) return;
        if (isZoomedOut) {
            // Zoomed-out swap (channel switch from the overview): land
            // straight on the new layout's overview fit.
            pendingSceneSnapRef.current = false;
            cameraXMV.jump(overviewTarget.x);
            cameraYMV.jump(overviewTarget.y);
            zoomMV.jump(overviewTarget.zoom);
            cameraTargetRef.current = overviewTarget.x;
            return;
        }
        const idx = stacks.findIndex(s => s.id === selectedId);
        if (idx === -1) return; // selection settles next tick; re-enter then
        pendingSceneSnapRef.current = false;
        const target = positions[idx];
        cameraXMV.jump(target);
        cameraYMV.jump(0);
        placeCamera(target);
    }, [sceneSwapNonce, stacks, selectedId, positions, overviewTarget, isZoomedOut,
        cameraXMV, cameraYMV, zoomMV, cameraTargetRef, placeCamera]);

    // Whether the camera is on its way somewhere (lib/stackEntry
    // cameraSettling): a zoom spring in flight, a pan spring still more
    // than a pixel from its goal, or a target Effect 2 has not acted on
    // yet (Effect 1 wrote it this commit). A pending target only counts
    // while Effect 2 is free to act on it — zoom-out animates to the overview
    // fit, not the target, and the hold / resolve / width-drag gates and a
    // manual pan keep the camera where it is. Read per frame by the entry
    // hold (useEntryHold): an entering stack stays hidden while this is
    // true, so it never rides a pan across the screen.
    const settleGatesRef = useRef({ isZoomedOut, cameraHold, widthDragActive });
    useEffect(() => {
        settleGatesRef.current = { isZoomedOut, cameraHold, widthDragActive };
    }, [isZoomedOut, cameraHold, widthDragActive]);
    const isCameraSettling = useCallback((): boolean => {
        const g = settleGatesRef.current;
        return cameraSettling({
            zoomAnimating: zoomMV.isAnimating(),
            panAnimating: cameraXMV.isAnimating(),
            panX: cameraXMV.get(),
            panVelocity: cameraXMV.getVelocity(),
            panGoal: panGoalRef.current,
            gated: g.isZoomedOut || g.cameraHold || g.widthDragActive || manualPanRef.current,
            targetPending: requestedTargetRef.current !== processedTargetRef.current,
        });
    }, [cameraXMV, zoomMV]);

    return {
        cameraXMV,
        cameraYMV,
        zoomMV,
        panX,
        panY,
        handlePanPointerDown,
        handlePanPointerMove,
        handlePanPointerUp,
        handleCenterPanStart,
        handleCenterPanMove,
        handleCenterPanEnd,
        panAlignSnap,
        centerPanning,
        isCameraSettling,
        /** Commit a camera position the caller moved to directly (the drag
         *  edge-scroll) as the tracked target, so Effect 2 doesn't spring
         *  back to the old one on the next stacks change. */
        commitCameraTarget: placeCamera,
    };
}
