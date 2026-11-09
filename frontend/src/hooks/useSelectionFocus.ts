import { useEffect, useRef } from 'react';
import { useGuestPool } from '@/contexts/GuestPoolContext';
import { cardOfCell, editorHoldsKeys, focusCellEditor, focusIsFree, handFocusToSelectedCell, releaseForeignCellFocus } from '@/lib/selectionFocus';
import { focusCellCard } from '@/lib/commandPane';

/** Frames a hand-off waits for the selected cell's editor to mount (a
 *  stack scrolled in, the main view coming back from the overview). */
const EDITOR_WAIT_FRAMES = 60;

/**
 * The keyboard follows the selected cell (lib/selectionFocus): a frame
 * after `selectedCellId` changes, focus left in another cell moves to the
 * selected one, its editor when the keys were in an editor. A hand-off
 * whose cell has no editor mounted yet (picked in the overview, a column
 * still entering) waits for it while nothing else has taken the keyboard,
 * then settles on the card. When the selection moves to no cell (the
 * Capabilities manage card in front), the keys left in a cell or a pooled
 * frame are released a frame later, and the manage card takes them.
 */
export function useSelectionFocus(selectedCellId: string | null, isZoomedOut: boolean): void {
    const { containerOf } = useGuestPool();
    // A native renderer's shadow root forgets its focused element once
    // focus leaves it; remember it per host so the hand-off back restores
    // the caret's element, not the renderer's first field.
    const lastFocused = useRef(new WeakMap<Element, Element>());
    useEffect(() => {
        const onFocusIn = (e: FocusEvent) => {
            const target = e.composedPath()[0];
            if (!(target instanceof Element)) return;
            const root = target.getRootNode();
            if (root instanceof ShadowRoot) lastFocused.current.set(root.host, target);
        };
        document.addEventListener('focusin', onFocusIn, true);
        return () => document.removeEventListener('focusin', onFocusIn, true);
    }, []);

    // The editor hand-off still owed to a cell (it took the keys out of an
    // editor before the cell's card or editor was there).
    const pending = useRef<string | null>(null);
    const previous = useRef(selectedCellId);

    useEffect(() => {
        const changed = previous.current !== selectedCellId;
        previous.current = selectedCellId;
        if (!selectedCellId) {
            pending.current = null;
            if (!changed) return;
            const raf = requestAnimationFrame(() => { releaseForeignCellFocus(containerOf); });
            return () => cancelAnimationFrame(raf);
        }
        // A hand-off still owed moves with the selection (cards picked one
        // after another in the overview) while nothing takes the keys.
        const carried = changed && pending.current !== null;
        const cellId = selectedCellId;
        const remembered = (host: Element) => lastFocused.current.get(host) ?? null;
        let raf = 0;
        let frames = 0;
        // Wait for the selected cell's editor while the keyboard stays
        // free; in the overview, until the main view is back.
        const settle = () => {
            if (pending.current !== cellId || isZoomedOut) return;
            if (!focusIsFree(cellId)) { pending.current = null; return; }
            const card = cardOfCell(cellId);
            if (card && focusCellEditor(card, containerOf, remembered)) { pending.current = null; return; }
            if (++frames < EDITOR_WAIT_FRAMES) { raf = requestAnimationFrame(settle); return; }
            pending.current = null;
            focusCellCard(cellId);
        };
        // A frame first: a press that selected the cell has moved focus
        // itself by then (into its title, onto a tab, to the body). Whether
        // an editor held the keys is read now: a renderer the pool hides by
        // then drops them on its own. The hand-off is owed at once, so a
        // re-run before that frame (the overview closing on the same
        // double click that picked the cell) still finishes it.
        const fromEditor = editorHoldsKeys();
        if (changed) pending.current = carried || fromEditor ? cellId : null;
        // The overview opened over an editor: its keys come back to the
        // selected cell's editor with the main view.
        else if (fromEditor && isZoomedOut) pending.current = cellId;
        raf = requestAnimationFrame(() => {
            if (changed) {
                const handed = handFocusToSelectedCell(cellId, containerOf, remembered);
                if (handed === 'pending') pending.current = cellId;
                else if (handed !== 'none') pending.current = null;
            }
            settle();
        });
        return () => cancelAnimationFrame(raf);
    }, [selectedCellId, isZoomedOut, containerOf]);
}
