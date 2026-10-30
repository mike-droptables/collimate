import { useCallback, useRef } from 'react';
import { motion, type HTMLMotionProps } from 'motion/react';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { promoteForAnimation } from '@/lib/motionLayers';
import { deviceSnappedTransform } from '@/lib/deviceOrigin';

type StackSlotProps = Omit<HTMLMotionProps<'div'>, 'ref' | 'className' | 'onAnimationStart' | 'onAnimationComplete' | 'transformTemplate'>;

/**
 * A stack's slot on the board: the box the scene springs in x, width and
 * opacity, holding the stack's group and its merge-target overlay.
 *
 * It is an ancestor of every card, so it holds no permanent compositor
 * promotion: a promoted layer keeps the raster it was drawn at, and the
 * cards' chrome would be resampled from it after a camera zoom. It is
 * promoted while the camera moves (a motion layer) and while its own
 * animation runs, and rests unpromoted (lib/motionLayers). Promoted, its
 * layout offset is rounded to the device pixel, so the scene hangs it by a
 * transform (`y`), never by `top`: a half-pixel top moved its cards' text
 * a row while the camera moved. Its translation lands on whole device
 * pixels (deviceSnappedTransform), every frame of its spring included.
 *
 * `isolate` gives each slot its own stacking context without a layer, so
 * the scene's per-stack z-index lifts the whole stack and the z-indexes
 * inside it stay inside it.
 */
export function StackSlot({ children, ...motion$ }: StackSlotProps) {
    const { registerMotionLayer } = useGuestPool();
    const slot = useRef<HTMLDivElement | null>(null);
    const slotRef = useCallback((el: HTMLDivElement | null) => {
        if (!el) return;
        slot.current = el;
        const untrack = registerMotionLayer(el);
        return () => {
            slot.current = null;
            untrack();
        };
    }, [registerMotionLayer]);
    return (
        <motion.div
            {...motion$}
            ref={slotRef}
            className="isolate"
            transformTemplate={deviceSnappedTransform}
            onAnimationStart={() => promoteForAnimation(slot.current, true)}
            onAnimationComplete={() => promoteForAnimation(slot.current, false)}
        >
            {children}
        </motion.div>
    );
}
