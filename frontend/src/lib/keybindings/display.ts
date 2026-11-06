import { parseCombo } from './parser';
import { isMacPlatform } from './match';
import { KeyCombo } from './types';

// Platform-adjusted symbols. On macOS we use the glyphs users
// expect in every native app; on Windows/Linux we spell out the
// modifier names since there's no widely-known symbol tradition.

const MAC_SYMBOLS: Record<string, string> = {
    Meta: '⌘',
    Ctrl: '⌃',
    Alt: '⌥',
    Shift: '⇧',
    Enter: '↵',
    Tab: '⇥',
    Escape: 'Esc',
    ArrowUp: '↑',
    ArrowDown: '↓',
    ArrowLeft: '←',
    ArrowRight: '→',
    Backspace: '⌫',
    Delete: '⌦',
    Space: '␣',
    ' ': '␣',
};

const OTHER_SYMBOLS: Record<string, string> = {
    Enter: 'Enter',
    Tab: 'Tab',
    Escape: 'Esc',
    ArrowUp: '↑',
    ArrowDown: '↓',
    ArrowLeft: '←',
    ArrowRight: '→',
    Backspace: 'Backspace',
    Delete: 'Del',
    Space: 'Space',
    ' ': 'Space',
};

/**
 * Format a combo for display. `Mod` expands to the current
 * platform's primary modifier (⌘ on Mac, Ctrl on others). The
 * caller generally shouldn't build strings directly; use
 * useShortcutDisplay in React contexts.
 */
export function displayCombo(combo: KeyCombo | null): string {
    if (!combo || !combo.key) return '';
    const mac = isMacPlatform();
    const syms = mac ? MAC_SYMBOLS : OTHER_SYMBOLS;

    const pieces: string[] = [];
    const mod = combo.mod;
    const meta = combo.meta || (mod && mac);
    const ctrl = combo.ctrl || (mod && !mac);

    if (meta) pieces.push(mac ? syms.Meta : 'Meta');
    if (ctrl) pieces.push(mac ? syms.Ctrl : 'Ctrl');
    if (combo.alt) pieces.push(mac ? syms.Alt : 'Alt');
    if (combo.shift) pieces.push(mac ? syms.Shift : 'Shift');

    const keyDisplay = syms[combo.key] ?? (
        combo.key.length === 1 ? combo.key.toUpperCase() : combo.key
    );
    pieces.push(keyDisplay);

    // On Mac, glue modifiers and key with no separator (⌘K, ⇧⌘D).
    // On Windows/Linux, "+" reads better (Ctrl+K, Shift+Ctrl+D).
    return mac ? pieces.join('') : pieces.join('+');
}

/** Convenience — parse a canonical-string combo and display it. */
export function displayComboString(canonical: string): string {
    const parsed = parseCombo(canonical);
    if (!parsed) return canonical;
    return displayCombo(parsed);
}
