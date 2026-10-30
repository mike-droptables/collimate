/** True when a pointer target is somewhere the user types or selects text on
 *  purpose — an input, textarea, select or contenteditable region. Such a
 *  target keeps the browser's own behaviour: its caret and selection on a
 *  press (blockNativeSelection), and its own context menu on a right-click
 *  (Cut / Copy / Paste, and spelling suggestions in the desktop shell), so a
 *  page-drawn menu around it returns early without preventDefault. */
export function isEditableTarget(target: EventTarget | null): boolean {
    if (!(target instanceof Element)) return false;
    if (target instanceof HTMLElement && target.isContentEditable) return true;
    return !!target.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]');
}

/**
 * Stop a primary-button press on draggable chrome (the stack head, the fan
 * bracket) from starting the browser's native text selection. Without this, a fast press-and-drag sweeps a
 * selection across every cell the pointer crosses.
 *
 * Cancelling pointerdown suppresses only the default mouse action (selection
 * start, focus-on-press); the trailing click still fires. Editable targets
 * keep their caret and selection, so an inline rename input works normally.
 */
export function blockNativeSelection(e: Pick<PointerEvent, 'button' | 'target' | 'preventDefault'>): void {
    if (e.button !== 0) return;
    if (isEditableTarget(e.target)) return;
    e.preventDefault();
}
