// The full-window view (CellEditor's maximised content region) as the
// Commands pane meets it: the attribute on the view's root, the pane's
// "to cell" focus move into it, and leaving it before a command that acts
// past its cell.

import { useCollimateStore } from '@/stores/useCollimateStore';
import { focusCellCard } from '@/lib/commandPane';

/** On the full-window view's root (the fixed layer over the whole app). */
export const FULL_WINDOW_ATTR = 'data-full-window';

/** The full-window view's root, while it is open. */
export function fullWindowRoot(): HTMLElement | null {
    return document.querySelector<HTMLElement>(`[${FULL_WINDOW_ATTR}]`);
}

/** Close the full-window view, when one is open. Its CellEditor flushes
 *  the cell's edits and brings the content region back into the cell. */
export function leaveFullWindow(): void {
    const store = useCollimateStore.getState();
    if (store.maximizedOverlayCellId !== null) store.setMaximizedOverlayCellId(null);
}

/** Put the keyboard back on the cell the pane acts on: the full-window
 *  view's content while it is open (the cell's card lies under it), the
 *  cell's card otherwise. */
export function focusPaneTarget(cellId: string | null): void {
    const root = useCollimateStore.getState().maximizedOverlayCellId !== null ? fullWindowRoot() : null;
    if (root) {
        // Not in the tab order: it takes a programmatic tabindex, as the
        // card does (focusCellCard).
        if (!root.hasAttribute('tabindex')) root.tabIndex = -1;
        root.focus({ preventScroll: true, focusVisible: false } as FocusOptions);
        return;
    }
    focusCellCard(cellId);
}
