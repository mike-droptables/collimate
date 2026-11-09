import { useState, useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import { useZoomTransitionStore } from '@/hooks/useZoomTransition';
import { useCollimateStore } from '@/stores/useCollimateStore';

// Tuned by eye — long enough to cover the camera/zoom animation, short
// enough that the renderer fade-in is perceptible afterward.
const ZOOM_IN_DEFER_MS = 350;

export const useZoomActions = () => {
    const [isZoomedOut, setIsZoomedOut] = useState(false);

    // Mirrors isZoomedOut so toggleZoom can compute the next value outside
    // a state updater (updaters must stay side-effect free).
    const isZoomedOutRef = useRef(isZoomedOut);
    useLayoutEffect(() => {
        isZoomedOutRef.current = isZoomedOut;
    }, [isZoomedOut]);
    // Pending "transition over" timer; a newer zoom-in supersedes it so a
    // quick in/out/in doesn't end the second transition early.
    const deferTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    // On unmount, drop a pending timer and release the flag it would have
    // cleared, so the shared store isn't left stuck in "transitioning".
    useEffect(() => () => {
        if (deferTimerRef.current === null) return;
        clearTimeout(deferTimerRef.current);
        deferTimerRef.current = null;
        useZoomTransitionStore.getState().setTransitioning(false);
    }, []);

    const wasZoomedOutRef = useRef(isZoomedOut);
    useLayoutEffect(() => {
        const zoomingIn = wasZoomedOutRef.current && !isZoomedOut;
        wasZoomedOutRef.current = isZoomedOut;
        if (deferTimerRef.current !== null) {
            clearTimeout(deferTimerRef.current);
            deferTimerRef.current = null;
        }
        useZoomTransitionStore.getState().setTransitioning(zoomingIn);
        if (zoomingIn) {
            deferTimerRef.current = setTimeout(() => {
                deferTimerRef.current = null;
                useZoomTransitionStore.getState().setTransitioning(false);
            }, ZOOM_IN_DEFER_MS);
        }
    }, [isZoomedOut]);

    // The overview reads the Removed shelf and the lineage while it is open,
    // for the channel it shows; leaving it (or the channel) forgets them.
    const channel = useCollimateStore(s => s.activeChannel?.id ?? null);
    useEffect(() => {
        if (!isZoomedOut || !channel) return;
        const store = useCollimateStore.getState();
        void store.loadOverview(channel);
        return () => useCollimateStore.getState().dropOverviewData();
    }, [isZoomedOut, channel]);

    const toggleZoom = useCallback(() => {
        const next = !isZoomedOutRef.current;
        isZoomedOutRef.current = next;
        setIsZoomedOut(next);
    }, []);

    return { isZoomedOut, setIsZoomedOut, toggleZoom };
};
