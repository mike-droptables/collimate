import { create } from 'zustand';

// Tiny shared flag — set for ~350ms after exiting zoom-out so that heavy
// renderers (preview iframes, native shadow-DOM mounts) stay deferred
// while the camera animation plays. CellEditor reads this to keep showing
// CompactPinnedPreview during the transition window.
interface ZoomTransitionState {
    isTransitioning: boolean;
    setTransitioning: (v: boolean) => void;
}

export const useZoomTransitionStore = create<ZoomTransitionState>((set) => ({
    isTransitioning: false,
    setTransitioning: (v) => set({ isTransitioning: v }),
}));

export const useIsZoomTransitioning = () =>
    useZoomTransitionStore((s) => s.isTransitioning);
