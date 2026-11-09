import { useEffect } from 'react';
import { useSpatialStore } from '@/stores/useSpatialStore';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { maxStackWidthFor } from '@/hooks/useStackLayout';
import { fullStackWidth } from '@/lib/stackWidth';
import { commandPaneInsetOf } from '@/lib/commandPane';

/** The stored shared width (``minCellSize``) and every maximised column's
 *  width against the viewport and the Commands pane: one pass of the
 *  tracker below. */
export function syncMaxWidth(viewWidth: number = window.innerWidth): void {
    const sp = useSpatialStore.getState();
    const full = fullStackWidth(viewWidth);
    // A column maximised on its own ("all stacks" off) is pinned to the full
    // width the same way; the width write drops its flag, so it is set again.
    for (const columnId of Object.keys(sp.maxColumns)) {
        if (sp.stackWidths[columnId] === full) continue;
        sp.setStackWidth(columnId, full);
        sp.setColumnMax(columnId, true);
    }
    if (sp.isMaxWidth) {
        if (sp.minCellSize !== full) sp.setMinCellSize(full);
        return;
    }
    const max = maxStackWidthFor(viewWidth);
    if (sp.minCellSize > max) sp.setMinCellSize(max);
}

/** Max-width tracker. The stored cell width is *user intent*; the tracker
 *  updates it on two events only:
 *
 *  - ``isMaxWidth`` on: keep it pinned to the full width as the viewport
 *    grows or shrinks and as the Commands pane opens, closes or docks
 *    across. The full width is what the open pane leaves free (the
 *    layout's screenMax), so a full-width stack never lies under the pane
 *    and takes the whole width back when it closes.
 *  - ``isMaxWidth`` off and the viewport narrows past the stored width:
 *    clamp it *down* to the new max. The cell visually shrank to fit;
 *    persisting that shrink means a later viewport grow won't stretch it
 *    back to the (now larger than the screen) old value. "It became the
 *    smaller size — it didn't become max width." The pane takes no part:
 *    it is transient, and the layout clamps at render time while it is
 *    open.
 *
 *  What it deliberately doesn't do: grow a non-max cell when the viewport
 *  widens. Only the shared max flag and a column's own max flag mark a
 *  width as max; other per-stack overrides in ``stackWidths`` are
 *  clamped by useStackLayout at render time, keeping the stored value for
 *  when the viewport widens again. */
export function useMaxWidthTracker(): void {
    useEffect(() => {
        const sync = () => syncMaxWidth();
        // Once on mount, so flags persisted in localStorage resync against
        // the current viewport (resized while the tab was unloaded, or the
        // window moved between displays).
        sync();
        window.addEventListener('resize', sync);
        const unsubscribe = useCollimateStore.subscribe((s, prev) => {
            if (commandPaneInsetOf(s.commandPane) !== commandPaneInsetOf(prev.commandPane)) sync();
        });
        return () => {
            window.removeEventListener('resize', sync);
            unsubscribe();
        };
    }, []);
}
