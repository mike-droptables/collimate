import { useCallback, useEffect, useRef, useState, type MouseEvent } from 'react';
import { POST_STOP_LOCK_MS } from '@/lib/runStop';

/** Marks the controls whose click arms the lock: the stop buttons. Put it on
 *  a control or on a container of controls. */
export const STOP_LOCK_ARMS_ATTR = 'data-arms-stop-lock';

const ARMING_SELECTOR = ['button', '[role="button"]']
    .map(control => `[${STOP_LOCK_ARMS_ATTR}] ${control}, ${control}[${STOP_LOCK_ARMS_ATTR}]`)
    .join(', ');

/**
 * The post-stop lock over a set of stop buttons (the cell's status pane).
 * When a run stops its row goes and another run's row and its Stop slide
 * under the pointer; a burst of clicks aimed at the first run must never
 * reach them. So a click on a stop arms a POST_STOP_LOCK_MS lock over the
 * whole surface, and every click the lock swallows re-arms it: the lock runs
 * from the LAST click. Spread `onClickCapture` on the surface's root.
 * Controls under it use `aria-disabled`, never `disabled`, for their own
 * cooldowns: a disabled button dispatches no click, so the lock could not
 * see it.
 */
export function usePostStopLock(): { locked: boolean; onClickCapture: (e: MouseEvent<HTMLElement>) => void } {
    const [locked, setLocked] = useState(false);
    const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const arm = useCallback(() => {
        if (timer.current) clearTimeout(timer.current);
        setLocked(true);
        timer.current = setTimeout(() => {
            timer.current = null;
            setLocked(false);
        }, POST_STOP_LOCK_MS);
    }, []);
    useEffect(() => () => {
        if (timer.current) clearTimeout(timer.current);
    }, []);
    // Capture phase: before the control's own onClick, so a locked surface
    // swallows the click whole, and an unlocked click arms on its way through.
    const onClickCapture = useCallback((e: MouseEvent<HTMLElement>) => {
        if (timer.current) {
            e.preventDefault();
            e.stopPropagation();
            arm();
            return;
        }
        if ((e.target as Element).closest?.(ARMING_SELECTOR)) arm();
    }, [arm]);
    return { locked, onClickCapture };
}
