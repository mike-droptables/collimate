import { KeyCombo } from './types';

// Canonical string form:
//
//   "Mod+K"                 — platform primary modifier + k
//   "Shift+Enter"
//   "Ctrl+Shift+ArrowLeft"  — literal Ctrl (not Mod; physical Ctrl even on Mac)
//   "Escape"
//   "Alt+A"
//
// Modifier order: Mod, Meta, Ctrl, Alt, Shift. The parser is
// tolerant of any ordering; the serializer emits the canonical
// order so two equal combos have identical string forms.
//
// The key name is preserved verbatim for named keys (`Enter`,
// `Escape`, `Tab`, `ArrowUp`, …) and lowercased for single
// letters. Digits stay as-is. Symbols (`,`, `.`, `/`, `\`, etc.)
// pass through untouched, except the two the string form cannot
// hold verbatim: space is written `Space` and `+` is written `Plus`.

const MOD_ORDER: readonly string[] = ['Mod', 'Meta', 'Ctrl', 'Alt', 'Shift'];

const NAMED_KEYS_CANONICAL: Record<string, string> = {
    esc: 'Escape',
    escape: 'Escape',
    enter: 'Enter',
    return: 'Enter',
    tab: 'Tab',
    space: ' ',
    spacebar: ' ',
    plus: '+',
    up: 'ArrowUp',
    down: 'ArrowDown',
    left: 'ArrowLeft',
    right: 'ArrowRight',
    arrowup: 'ArrowUp',
    arrowdown: 'ArrowDown',
    arrowleft: 'ArrowLeft',
    arrowright: 'ArrowRight',
    backspace: 'Backspace',
    delete: 'Delete',
    del: 'Delete',
    home: 'Home',
    end: 'End',
    pageup: 'PageUp',
    pagedown: 'PageDown',
};

function normalizeKey(raw: string): string {
    const trimmed = raw.trim();
    if (!trimmed) return '';
    const lower = trimmed.toLowerCase();
    if (NAMED_KEYS_CANONICAL[lower]) return NAMED_KEYS_CANONICAL[lower];
    // Single letters canonicalize to lowercase; multi-char unknowns
    // keep their case so the caller can round-trip weird inputs.
    if (trimmed.length === 1) return lower;
    return trimmed;
}

export function parseCombo(str: string): KeyCombo | null {
    const parts = str.split('+').map(p => p.trim()).filter(Boolean);
    if (parts.length === 0) return null;
    const combo: KeyCombo = { key: '' };
    let sawKey = false;
    for (const part of parts) {
        const lp = part.toLowerCase();
        if (lp === 'mod') combo.mod = true;
        else if (lp === 'meta' || lp === 'cmd' || lp === 'command') combo.meta = true;
        else if (lp === 'ctrl' || lp === 'control') combo.ctrl = true;
        else if (lp === 'alt' || lp === 'option' || lp === 'opt') combo.alt = true;
        else if (lp === 'shift') combo.shift = true;
        else {
            if (sawKey) return null; // two non-modifier tokens = malformed
            combo.key = normalizeKey(part);
            sawKey = true;
        }
    }
    if (!sawKey || !combo.key) return null;
    return combo;
}

export function formatCombo(combo: KeyCombo): string {
    const mods: string[] = [];
    if (combo.mod) mods.push('Mod');
    if (combo.meta) mods.push('Meta');
    if (combo.ctrl) mods.push('Ctrl');
    if (combo.alt) mods.push('Alt');
    if (combo.shift) mods.push('Shift');
    // Preserve declaration order above (MOD_ORDER) regardless of how
    // parseCombo received them.
    mods.sort((a, b) => MOD_ORDER.indexOf(a) - MOD_ORDER.indexOf(b));
    const displayKey = combo.key === ' ' ? 'Space' : combo.key === '+' ? 'Plus' : combo.key;
    return [...mods, displayKey].join('+');
}
