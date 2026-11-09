// What a renderer sees when it asks for the editing preferences. The host
// applies them to <html> (useEditingStore) and every host reads them back
// from there: NativeRenderer's api.getEditing() (a shadow root's editables
// inherit <html spellcheck> already) and the SET_EDITING message an iframe
// renderer's document receives (lib/sandboxBridge). Same record both ways,
// the SDK's RendererEditing.

/** Fired on window whenever the applied preferences change; the detail is
 *  the new EditingSnapshot. */
export const EDITING_CHANGED_EVENT = 'collimate:editing-changed';

export interface EditingSnapshot {
    /** Underline misspelled words in editables that leave `spellcheck`
     *  unset (the HTML attribute inherits from <html>). */
    spellcheck: boolean;
}

/** The applied preferences, read from <html>'s attribute (absent before the
 *  store applies any: the browser default, on). */
export function readEditingSnapshot(): EditingSnapshot {
    return { spellcheck: document.documentElement.getAttribute('spellcheck') !== 'false' };
}

/** Apply the preferences to <html> and tell the renderer hosts. Silent when
 *  <html> already carries them, so a re-adopted doc re-sends nothing. */
export function applyEditingSnapshot(editing: EditingSnapshot): void {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    const value = editing.spellcheck ? 'true' : 'false';
    if (root.getAttribute('spellcheck') === value) return;
    root.setAttribute('spellcheck', value);
    window.dispatchEvent(new CustomEvent<EditingSnapshot>(EDITING_CHANGED_EVENT, { detail: readEditingSnapshot() }));
}
