import {useCallback, useLayoutEffect, useRef, useState} from 'react';

/** Front changes reveal the scrubber until real motion/move gates settle,
 * then retain it for one second. Repeated changes supersede the timer. */
export function useStackScrubberReveal(frontId: string | null, moving: boolean) {
    const previousFront = useRef(frontId);
    const pending = useRef(false);
    const active = useRef(new Set<string>());
    const mounted = useRef(true);
    const [revision, setRevision] = useState(0);
    const [visible, setVisible] = useState(false);
    const onTransitionChange = useCallback((id: string, busy: boolean) => {
        const changed = busy ? !active.current.has(id) : active.current.has(id);
        if (!changed) return;
        if (busy) active.current.add(id);
        else active.current.delete(id);
        if (mounted.current) setRevision(value => value + 1);
    }, []);
    useLayoutEffect(() => {
        mounted.current = true;
        return () => { mounted.current = false; };
    }, []);
    useLayoutEffect(() => {
        if (previousFront.current !== frontId) {
            previousFront.current = frontId;
            pending.current = true;
            setVisible(true);
        }
        if (!pending.current || moving || active.current.size > 0) return;
        const timer = setTimeout(() => {
            pending.current = false;
            setVisible(false);
        }, 1000);
        return () => clearTimeout(timer);
    }, [frontId, moving, revision]);
    return {visible, onTransitionChange};
}
