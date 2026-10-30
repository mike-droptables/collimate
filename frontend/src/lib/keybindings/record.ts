import { SHORTCUT_IDS } from './ids';
import { comboKeyFromEvent, isMacPlatform } from './match';
import type { KeyCombo, ShortcutId } from './types';

// Recording a binding from a keystroke (the Commands pane's Shift+Enter
// capture) and the rule a recorded chord must pass.

/** Keys that never stand alone as a binding: the modifiers themselves, and
 *  the dead / lock keys a layout sends while composing. */
const BARE_KEYS = new Set(['Meta', 'Control', 'Alt', 'Shift', 'Dead', 'Fn', 'CapsLock']);

/**
 * The combo a keydown records, or null for a bare modifier. On macOS the
 * physical ⌘ canonicalises to `Mod` (and Ctrl elsewhere), so a binding
 * recorded on one platform works on the other; the other primary modifier
 * (Ctrl on macOS, Meta elsewhere) stays literal. macOS Option chords record
 * the physical US key (`comboKeyFromEvent`), not the character Option types.
 */
export function comboFromKeydown(e: Pick<KeyboardEvent, 'key' | 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>): KeyCombo | null {
    const key = comboKeyFromEvent(e);
    if (!key || BARE_KEYS.has(key)) return null;
    const mac = isMacPlatform();
    const combo: KeyCombo = { key };
    if (mac ? e.metaKey : e.ctrlKey) combo.mod = true;
    if (mac && e.ctrlKey) combo.ctrl = true;
    if (!mac && e.metaKey) combo.meta = true;
    if (e.altKey) combo.alt = true;
    if (e.shiftKey) combo.shift = true;
    return combo;
}

/** Whether a registry shortcut may take this combo: the cell-move
 *  shortcuts need a modifier (a bare arrow belongs to lists and editors). */
export function bindingPermitted(id: ShortcutId, combo: KeyCombo): boolean {
    return (id !== SHORTCUT_IDS.NAV_CELL_MOVE_LEFT && id !== SHORTCUT_IDS.NAV_CELL_MOVE_RIGHT)
        || Boolean(combo.mod || combo.meta || combo.ctrl || combo.alt || combo.shift);
}
