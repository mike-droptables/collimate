/**
 * Marks a DOM subtree that owns every key it receives — a small transient
 * surface such as the stack menu or the stack-name input, which handles its
 * own arrows / Enter / Space / Esc. The scene's global keyboard handler
 * (hooks/useKeyboardHandler.ts) listens on window in the capture phase, so
 * it would otherwise see those keys first (arrow navigation, the Esc
 * cascade); it ignores any keystroke whose composed path crosses an element
 * carrying this attribute. Spread `KEYBOARD_OWNER_PROPS` onto the root.
 */
export const KEYBOARD_OWNER_ATTR = 'data-owns-keyboard';

export const KEYBOARD_OWNER_PROPS = { [KEYBOARD_OWNER_ATTR]: '' } as const;

/**
 * Marks a section that owns only its navigation keys (the lists of a cell's
 * status pane: its status view, drilldown and All view): unmodified ↑ ↓ ↵ Esc, and
 * Ctrl+← / Ctrl+→, which must not move the selected cell from inside it.
 * Every other key (Alt+V, Alt+R, Mod+K, /, zoom, the width keys) still
 * reaches the scene while focus is in it. Spread `NAV_KEYS_OWNER_PROPS`
 * onto the root.
 */
export const NAV_KEYS_OWNER_ATTR = 'data-owns-nav-keys';

export const NAV_KEYS_OWNER_PROPS = { [NAV_KEYS_OWNER_ATTR]: '' } as const;

/**
 * Marks a card that owns its bare keys (the Capabilities manage card): every
 * key pressed with no Alt, Ctrl or Meta (letters, '/', the plain arrows,
 * Enter, Esc, Backspace, and those with only Shift) stays in it, so '/' in
 * its search never opens Commands. Alt, Ctrl and Meta chords (Alt+↑/↓
 * paging, Ctrl+←/→, Mod+K, Alt+Shift+Backspace) still reach the scene from
 * it. Its text fields keep their own editing rules (the scene's typing
 * guard); a menu or dialog opened from it owns every key as usual. Spread
 * `BARE_KEYS_OWNER_PROPS` onto the root.
 */
export const BARE_KEYS_OWNER_ATTR = 'data-owns-bare-keys';

export const BARE_KEYS_OWNER_PROPS = { [BARE_KEYS_OWNER_ATTR]: '' } as const;

/** The keys a bare-keys owner holds back from the scene: any key with no
 *  Alt, Ctrl or Meta. */
function isBareKey(e: Event): boolean {
    return e instanceof KeyboardEvent && !e.altKey && !e.ctrlKey && !e.metaKey;
}

/** The keys a nav-keys owner holds back from the scene. */
function isNavKey(e: Event): boolean {
    if (!(e instanceof KeyboardEvent)) return false;
    if (e.altKey || e.metaKey || e.shiftKey) return false;
    if (e.ctrlKey) return e.key === 'ArrowLeft' || e.key === 'ArrowRight';
    return e.key === 'ArrowUp' || e.key === 'ArrowDown' || e.key === 'Enter' || e.key === 'Escape';
}

/** True when the event's composed path (which pierces open shadow roots,
 *  so renderer hosts count) crosses an element carrying `attr`. */
export function eventPathHasAttr(e: Event, attr: string): boolean {
    const path = typeof e.composedPath === 'function' ? e.composedPath() : [];
    for (const node of path) {
        if (node instanceof Element && node.hasAttribute(attr)) return true;
    }
    return false;
}


/** Gestures in flight that own every key until they end (a card carried
 *  across the overview: its Esc puts the card back and nothing else). */
let gestureOwners = 0;

/** A gesture takes the keyboard from the scene until the returned release
 *  runs. The scene's capture-phase listener and the gesture's own share
 *  the window, in no settled order, so the scene must stand back itself. */
export function claimKeyboardForGesture(): () => void {
    gestureOwners += 1;
    let released = false;
    return () => {
        if (released) return;
        released = true;
        gestureOwners -= 1;
    };
}

/** Scene shortcuts yield to modal roots, a gesture in flight, event
 * owners, and the focused owner (including an iframe whose key was
 * forwarded onto window); a nav-keys owner holds back only its navigation
 * keys, a bare-keys owner only its unmodified ones. */
export function sceneKeyboardOwned(e: Event): boolean {
    if (gestureOwners > 0) return true;
    if (document.querySelector('[aria-modal="true"]')) return true;
    if (eventPathHasAttr(e, KEYBOARD_OWNER_ATTR)) return true;
    const navKey = isNavKey(e);
    if (navKey && eventPathHasAttr(e, NAV_KEYS_OWNER_ATTR)) return true;
    const bareKey = isBareKey(e);
    if (bareKey && eventPathHasAttr(e, BARE_KEYS_OWNER_ATTR)) return true;
    let active: Element | null = document.activeElement;
    while (active) {
        if (active.closest(`[${KEYBOARD_OWNER_ATTR}]`)) return true;
        if (navKey && active.closest(`[${NAV_KEYS_OWNER_ATTR}]`)) return true;
        if (bareKey && active.closest(`[${BARE_KEYS_OWNER_ATTR}]`)) return true;
        const inner: Element | null = active.shadowRoot?.activeElement ?? null;
        if (!inner) break;
        active = inner;
    }
    return false;
}
