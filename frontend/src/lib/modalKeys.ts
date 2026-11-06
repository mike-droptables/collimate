import { useEffect, type KeyboardEvent, type RefObject } from 'react';

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]';

/** While `active`, focus the modal's first `initialSelector` match; on close,
 *  hand focus back to whatever held it before, if that is still mounted. */
export function useModalFocus(ref: RefObject<HTMLElement | null>, active: boolean, initialSelector: string) {
    useEffect(() => {
        if (!active) return;
        const prior = document.activeElement;
        ref.current?.querySelector<HTMLElement>(initialSelector)?.focus();
        return () => { if (prior instanceof HTMLElement && prior.isConnected) prior.focus(); };
    }, [ref, active, initialSelector]);
}

/** Tab / Shift+Tab wrap between the first and last control of `root` in
 *  the tab order (a modal, the Commands pane). Other keys pass. */
export function wrapTab(e: Pick<KeyboardEvent, 'key' | 'shiftKey' | 'preventDefault'>, root: HTMLElement | null) {
    if (e.key !== 'Tab') return;
    const elements = Array.from(root?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])
        .filter(el => el.tabIndex >= 0)
        .sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));
    if (e.shiftKey && document.activeElement === elements[0]) { e.preventDefault(); elements.at(-1)?.focus(); }
    if (!e.shiftKey && document.activeElement === elements.at(-1)) { e.preventDefault(); elements[0]?.focus(); }
}

/** A modal's keydown: keys stay inside it, Escape closes it, and Tab /
 *  Shift+Tab wrap between its first and last focusable control. */
export function trapModalKeys(e: KeyboardEvent, root: HTMLElement | null, onClose: () => void) {
    e.stopPropagation();
    if (e.key === 'Escape') { e.preventDefault(); onClose(); }
    wrapTab(e, root);
}
