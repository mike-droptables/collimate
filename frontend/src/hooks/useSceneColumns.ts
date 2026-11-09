import { useEffect, useMemo, useState, type MutableRefObject } from 'react';
import type { MotionValue } from 'motion/react';
import type { Stack } from '@/lib/types';
import type { PendingMoveState } from '@/stores/useSpatialStore';
import type { StackLayoutResult } from './useStackLayout';

export interface SceneColumn extends Stack {
    x: number;
    isOffscreen: boolean;
    computedWidth: number;
}

interface SceneColumnsInputs {
    layout: StackLayoutResult;
    stacks: Stack[];
    cameraXMV: MotionValue<number>;
    zoomMV: MotionValue<number>;
    cameraTargetRef: MutableRefObject<number>;
    selectedId: string | null;
    isZoomedOut: boolean;
    pendingMove: PendingMoveState | null;
    dragType: 'cell' | 'stack' | null;
    dragStackId: string | null;
    insertTargetIdx: number | null;
}

/** Scene column placement and visibility subscribe to coarse camera changes. */
export function useSceneColumns({
    layout, stacks, cameraXMV, zoomMV, cameraTargetRef, selectedId, isZoomedOut,
    pendingMove, dragType, dragStackId, insertTargetIdx,
}: SceneColumnsInputs): SceneColumn[] {
    const { GAP, positions, widths, naturalWidths, viewSize, cellPendingMoveWorldX, arrowWorldX } = layout;
    // Coarse, reactive camera position for the offscreen gate. The gate must
    // react to where the camera actually is — a center-button drag or wheel
    // pan can carry the viewport several screens away from the selected stack
    // without any React state changing, leaving the cells that scrolled into
    // view frozen at display:none. Re-rendering per camera frame would defeat
    // the motion-value fast path, so quantize to viewport-width buckets and
    // only commit state on bucket crossings.
    const [cameraBucket, setCameraBucket] = useState(0);
    useEffect(() => {
        const bucketW = Math.max(1, viewSize.width);
        const update = (x: number) => {
            const bucket = Math.round(x / bucketW);
            setCameraBucket(prev => (prev === bucket ? prev : bucket));
        };
        update(cameraXMV.get());
        return cameraXMV.on('change', update);
    }, [cameraXMV, viewSize.width]);

    // Coarse, reactive zoom for the overview's gate: the visible world extent
    // is viewSize/zoom, so culling distance must widen as the user zooms out
    // and tighten as they wheel-zoom in. Half-octave buckets (≤ ~19% error,
    // absorbed by the 2× cull margin) keep re-renders to zoom-level changes
    // rather than every animation frame.
    const [zoomBucket, setZoomBucket] = useState(0);
    useEffect(() => {
        const update = (z: number) => {
            const bucket = Math.round(Math.log2(Math.max(0.01, z)) * 2);
            setZoomBucket(prev => (prev === bucket ? prev : bucket));
        };
        update(zoomMV.get());
        return zoomMV.on('change', update);
    }, [zoomMV]);

    const items = useMemo((): SceneColumn[] => {
        const isCellInsertDrag = (dragType === 'cell' && insertTargetIdx !== null) ||
            (pendingMove?.type === 'cell' && pendingMove.mode === 'insert');
        const sourceStackIdx = isCellInsertDrag
            ? stacks.findIndex(s => s.id === (dragStackId || pendingMove?.sourceId))
            : -1;
        const ghostWidth = sourceStackIdx >= 0 ? naturalWidths[sourceStackIdx] : 0;
        const currentInsTarget = dragType === 'cell'
            ? insertTargetIdx
            : (pendingMove?.type === 'cell' && pendingMove.mode === 'insert'
                ? pendingMove.targetIndex / 2
                : null);

        // Calculate the actual camera target X based on the currently selected window
        // rather than waiting for the delayed cameraTargetRef to update.
        let targetX = cameraTargetRef.current;
        if (pendingMove) {
            const trackX = pendingMove?.type === 'cell' ? cellPendingMoveWorldX : arrowWorldX;
            if (trackX !== null) {
                targetX = trackX;
            }
        } else {
            const selectedIdx = stacks.findIndex(s => s.id === selectedId);
            targetX = selectedIdx >= 0 ? positions[selectedIdx] : cameraTargetRef.current;
        }
        const cameraX = cameraBucket * Math.max(1, viewSize.width);
        // Cull distance. Main view: two screen-widths of margin. Overview:
        // the visible world extent is viewSize/zoom, so scale accordingly
        // (full width ≈ half-extent × 2 margin) — culled overview cards render
        // a light placeholder body instead of parsing/painting full content.
        const approxZoom = Math.pow(2, zoomBucket / 2);
        const cullDistance = isZoomedOut
            ? viewSize.width / Math.max(0.01, approxZoom)
            : viewSize.width * 2.0;

        return stacks.map((stack, i) => {
            let x = positions[i];
            const w = widths[i];
            if (isCellInsertDrag && ghostWidth > 0 && currentInsTarget !== null && i >= currentInsTarget) {
                x += ghostWidth + GAP;
            }
            // A stack sleeps only when it's far from BOTH the tracked target
            // (selection / pending move — keeps the destination awake while
            // the camera flies there) and the actual camera position (keeps
            // whatever a manual pan scrolled into view awake).
            const dist = Math.min(Math.abs(x - targetX), Math.abs(x - cameraX));
            const isOffscreen = dist > cullDistance;

            return { ...stack, x, isOffscreen, computedWidth: w };
        });
    }, [stacks, positions, widths, naturalWidths, viewSize.width, isZoomedOut,
        dragType, dragStackId, insertTargetIdx, pendingMove, GAP, cameraTargetRef, selectedId,
        cameraBucket, zoomBucket, cellPendingMoveWorldX, arrowWorldX]);

    return items;
}
