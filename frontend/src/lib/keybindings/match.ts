import { KeyCombo } from './types';

// Runtime platform probe. Safe on SSR: defaults to "not mac" so
// tests and server renders pick the Ctrl interpretation of Mod+.
export function isMacPlatform(): boolean {
    if (typeof navigator === 'undefined') return false;
    // navigator.platform is deprecated but still the most reliable
    // on-device signal. userAgentData.platform doesn't exist on
    // Firefox/Safari. Fall back to userAgent substring.
    const p = navigator.platform || '';
    if (/Mac|iPhone|iPad|iPod/i.test(p)) return true;
    const ua = navigator.userAgent || '';
    return /Mac OS|Macintosh/i.test(ua);
}

/** Expand `mod` into the platform's primary modifier (meta on macOS,
 *  ctrl elsewhere), merged with any literal meta/ctrl the combo names. */
export function platformModifiers(
    combo: KeyCombo,
    mac = isMacPlatform(),
): { meta: boolean; ctrl: boolean } {
    return {
        meta: Boolean(combo.meta) || (Boolean(combo.mod) && mac),
        ctrl: Boolean(combo.ctrl) || (Boolean(combo.mod) && !mac),
    };
}

/**
 * True iff `event` exactly matches `combo`. "Exactly" means:
 *
 *   - The key name matches (case-insensitive for letters).
 *   - Every modifier the combo requires is held.
 *   - Every modifier the combo does *not* require is NOT held —
 *     matters so `Enter` (bare) doesn't match `Shift+Enter`.
 *
 * `Mod+` is the platform-adaptive primary modifier. On macOS it
 * maps to `metaKey`; on Windows/Linux it maps to `ctrlKey`. If a
 * user authored `Ctrl+K` explicitly, that's literal `ctrlKey` on
 * every platform. `Mod` keeps defaults portable.
 */
export function matchCombo(e: KeyboardEvent, combo: KeyCombo): boolean {
    const eventKey = comboKeyFromEvent(e);
    if (eventKey !== combo.key) return false;

    // Mod expands to the platform primary modifier; the other primary
    // slot must be clear unless the combo also names it, so `Mod+K`
    // does not fire on `Ctrl+Meta+K`.
    const { meta: wantMeta, ctrl: wantCtrl } = platformModifiers(combo);
    const wantAlt = Boolean(combo.alt);
    const wantShift = Boolean(combo.shift);

    if (Boolean(e.metaKey) !== wantMeta) return false;
    if (Boolean(e.ctrlKey) !== wantCtrl) return false;
    if (Boolean(e.altKey) !== wantAlt) return false;

    // Shift is special: for non-letter keys (Enter, Tab, etc.) we
    // require an exact match. For letters we also require an exact
    // match — `Shift+K` must be explicit, otherwise capslock or an
    // accidental shift would fire `K` bindings inconsistently.
    if (Boolean(e.shiftKey) !== wantShift) {
        // Numbers + symbols: Shift changes the character on some
        // layouts ("!" vs "1"). We don't attempt to normalize that
        // here — combos referring to symbols should be authored as
        // the post-shift character ("!", "?") not "Shift+1".
        return false;
    }

    return true;
}

export function normalizeEventKey(key: string): string {
    if (!key) return '';
    if (key.length === 1) return key.toLowerCase();
    // Pass named keys verbatim — KeyboardEvent.key already gives us
    // "Enter", "Escape", "ArrowUp", etc. in their canonical form.
    return key;
}


const PHYSICAL_SYMBOLS: Record<string, [string, string]> = {
    Backquote: ['`','~'], Minus: ['-','_'], Equal: ['=','+'], BracketLeft: ['[','{'],
    BracketRight: [']','}'], Backslash: ['\\','|'], Semicolon: [';',':'], Quote: ["'",'"'],
    Comma: [',','<'], Period: ['.','>'], Slash: ['/','?'],
};

/** macOS Option changes printable key text; its shortcuts use physical US
 * keys. All other platform/key combinations retain logical characters. */
export function comboKeyFromEvent(e: Pick<KeyboardEvent,'key'|'code'|'altKey'|'shiftKey'>, mac=isMacPlatform()): string {
    if (mac && e.altKey) {
        if (/^Key[A-Z]$/.test(e.code || '')) return e.code.slice(3).toLowerCase();
        if (/^Digit[0-9]$/.test(e.code || '')) {
            const digit=Number(e.code.slice(5));return e.shiftKey ? ')!@#$%^&*('[digit] : String(digit);
        }
        const symbol=PHYSICAL_SYMBOLS[e.code];
        if (symbol) return symbol[e.shiftKey ? 1 : 0];
    }
    return normalizeEventKey(e.key);
}

export function physicalCodeForKey(key: string, shift: boolean): string | undefined {
    if (/^[a-z]$/i.test(key)) return 'Key'+key.toUpperCase();
    for (let digit=0;digit<10;digit++) if ((shift ? ')!@#$%^&*('[digit] : String(digit))===key) return 'Digit'+digit;
    return Object.entries(PHYSICAL_SYMBOLS).find(([,chars])=>chars[shift ? 1 : 0]===key)?.[0];
}

/** Whether a combo holds a modifier that takes it out of typing: Mod, Ctrl,
 *  Meta or Alt. Shift alone does not (Shift+letter is text). */
export function comboHasModifier(combo: KeyCombo): boolean {
    return Boolean(combo.mod || combo.ctrl || combo.meta || combo.alt);
}

/** Whether the keystroke was typed into a text field: an input, a textarea,
 *  a select or a contenteditable. Reads composedPath()[0], not e.target:
 *  NativeRenderer mounts editors in shadow DOM, and an event crossing a
 *  shadow boundary is retargeted to the shadow host (a plain <div>), so
 *  e.target alone misreads "typing in an input inside a renderer" as "not
 *  typing". For events that never cross a shadow boundary composedPath()[0]
 *  is e.target. */
export function isTypingTarget(e: Event): boolean {
    const target = e.composedPath?.()[0] ?? e.target;
    if (!target || !(target instanceof HTMLElement)) return false;
    const tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    return target.isContentEditable === true;
}
