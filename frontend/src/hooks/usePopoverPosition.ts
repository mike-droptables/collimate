import { useEffect, useLayoutEffect, useState, type RefObject } from 'react';

/** Body-portal coordinates are viewport coordinates, independent of canvas zoom. */
export function useViewportClampedPosition(ref: RefObject<HTMLElement | null>, x: number, y: number) {
    const [position, setPosition] = useState({ x, y });
    useLayoutEffect(() => {
        const element = ref.current;
        if (!element) return;
        const update = () => {
            // Layout dimensions stay accurate during the popover's entry
            // animation. Its transformed bounding box starts smaller.
            const padding = 8;
            const next = {
                x: Math.max(padding, Math.min(x, window.innerWidth - element.offsetWidth - padding)),
                y: Math.max(padding, Math.min(y, window.innerHeight - element.offsetHeight - padding)),
            };
            setPosition(previous => previous.x === next.x && previous.y === next.y ? previous : next);
        };
        update();
        const observer = new ResizeObserver(update);
        observer.observe(element);
        window.addEventListener('resize', update);
        return () => {
            observer.disconnect();
            window.removeEventListener('resize', update);
        };
    }, [ref, x, y]);
    return position;
}

/** Iframe focus swallows document mousedown, so it also closes the popover. */
export function useDismissOnOutside(ref: RefObject<HTMLElement | null>, onDismiss: () => void) {
    useEffect(() => {
        const onDown = (event: MouseEvent) => {
            if (ref.current && !ref.current.contains(event.target as Node)) onDismiss();
        };
        const onBlur = () => {
            if (document.activeElement?.tagName === 'IFRAME') onDismiss();
        };
        document.addEventListener('mousedown', onDown);
        window.addEventListener('blur', onBlur);
        return () => {
            document.removeEventListener('mousedown', onDown);
            window.removeEventListener('blur', onBlur);
        };
    }, [ref, onDismiss]);
}
