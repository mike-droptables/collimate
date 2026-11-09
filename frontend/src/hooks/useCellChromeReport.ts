import { useEffect, type RefObject } from 'react';
import { useSpatialStore } from '@/stores/useSpatialStore';

/** Report a main-view card's header bottom — px from the card's outer top,
 *  the card's top border included — into useSpatialStore.cellChrome: the
 *  band of the scene's cell-diff hover zones and the top of the card-side
 *  resize strips (see lib/cellDiff, lib/edgeResize). Layout offsets
 *  (offsetTop/offsetHeight), never bounding rects: they ignore the card's
 *  transforms (the stack burn's scale, zoom), so a report is the card's
 *  resting geometry. Off while `enabled` is false — the overview card has a
 *  different header. Unmounting reports nothing: across a fan-out
 *  toggle the same cell's next header mounts BEFORE the old one's exit
 *  animation unmounts it, so an unmount report would erase the live one. */
export function useReportHeaderChrome(
    cellId: string, headerRef: RefObject<HTMLElement | null>, enabled: boolean,
) {
    useEffect(() => {
        const header = headerRef.current;
        if (!enabled || !header || typeof ResizeObserver === 'undefined') return;
        const measure = () => {
            const card = header.offsetParent as HTMLElement | null;
            const border = card ? card.clientTop : 0;
            useSpatialStore.getState().reportCellChrome(
                cellId, border + header.offsetTop + header.offsetHeight);
        };
        measure();
        const ro = new ResizeObserver(measure);
        ro.observe(header);
        return () => ro.disconnect();
    }, [cellId, headerRef, enabled]);
}
