/**
 * Compositor promotion for the layers the scene moves, held only while it
 * moves.
 *
 * The camera's scale wrapper and pan container, every pooled surface in
 * the world-space overlay and each renderer's host box ride transforms
 * that change every frame of a pan, a zoom spring or a stack spring. A
 * promoted layer (`will-change: transform`) makes those frames
 * compositor-only. But Chromium freezes a promoted layer's raster: the
 * scale and the sub-pixel translation it was rasterized at stay put for
 * as long as the hint does, and everything under it is then resampled
 * from that raster. After a zoom spring (an overview round trip, a
 * ctrl+wheel) a renderer's text was drawn from a stale raster, and it
 * blurred a little more with every round trip; only text a fresh layer
 * happened to cover (the caret's overlap layer) stayed sharp. Dropping the
 * `will-change` property alone did not rebuild the raster.
 *
 * So promotion is earned per motion: `promote()` puts the classes on every
 * tracked element before the first moved frame paints, and `demote()`
 * takes them off again once the scene is at rest, which makes Chromium
 * rebuild those layers and rasterize them at the resting scale and offset.
 * The guest pool owns the one instance: it promotes on camera motion and
 * on any sync pass that moved a surface, and demotes on its sync
 * scheduler's settle edge (lib/guestSyncScheduler `onSettle`).
 *
 * The classes are toggled on the element itself, so a React-rendered
 * element registered here must keep them out of its own `className`.
 *
 * An element's own motion (a stack's slot sliding, a card's spring, a
 * stack's burn) is promoted the same way, for that animation alone:
 * `promoteForAnimation` sets an inline `will-change` from motion's
 * `onAnimationStart` (sent before the first animated frame) and clears it
 * on `onAnimationComplete`. The inline hint and the classes are
 * independent, so either motion promotes the element and it is demoted
 * only when both are over.
 */

/** The promotion: a composited layer, hinted to transform. */
const PROMOTED_CLASSES = ['transform-gpu', 'will-change-transform'] as const;

export interface MotionLayers {
  /** Track an element: it carries the promotion whenever the scene moves,
   *  and takes it at once when the scene is moving now. */
  track: (element: HTMLElement) => void;
  /** Stop tracking an element, leaving it demoted. */
  untrack: (element: HTMLElement) => void;
  /** The scene is moving, or is about to: promote every tracked element.
   *  Idempotent; only the first call of a motion touches the DOM. */
  promote: () => void;
  /** The scene is at rest: demote every tracked element. */
  demote: () => void;
  /** Whether the tracked elements are promoted right now. */
  promoted: () => boolean;
}

function setPromoted(element: HTMLElement, on: boolean): void {
  for (const name of PROMOTED_CLASSES) element.classList.toggle(name, on);
}

export function createMotionLayers(): MotionLayers {
  const layers = new Set<HTMLElement>();
  let promoted = false;

  return {
    track: (element) => {
      layers.add(element);
      setPromoted(element, promoted);
    },
    untrack: (element) => {
      layers.delete(element);
      setPromoted(element, false);
    },
    promote: () => {
      if (promoted) return;
      promoted = true;
      for (const element of layers) setPromoted(element, true);
    },
    demote: () => {
      if (!promoted) return;
      promoted = false;
      for (const element of layers) setPromoted(element, false);
    },
    promoted: () => promoted,
  };
}

/** Promote `element` for its own animation: `animating` from motion's
 *  `onAnimationStart` to its `onAnimationComplete`. At rest it holds no
 *  hint, so it is rasterized where it came to rest. */
export function promoteForAnimation(element: HTMLElement | null, animating: boolean): void {
  if (element) element.style.willChange = animating ? 'transform, opacity' : '';
}
