import { useEffect, useRef } from 'react';
import { useKeybindingsStore } from './store';
import { comboHasModifier, isTypingTarget, matchCombo } from './match';
import { ShortcutId } from './types';

interface UseShortcutOptions {
    /**
     * When false, the handler won't fire even if the combo
     * matches. Use this to gate shortcuts on modal/panel open
     * state without mounting/unmounting the hook. The event
     * listener stays registered either way — cheaper than
     * toggling add/removeEventListener on every render.
     */
    enabled?: boolean;
    /**
     * DOM target to listen on. Defaults to `document`. Pass
     * `window` for shortcuts that need to catch synthetic
     * KeyboardEvents re-dispatched from the iframe forwarder
     * (see SandboxIframe's FORWARD_KEYDOWN handler).
     */
    target?: 'document' | 'window';
    /**
     * Skip a bare combo (no Mod/Ctrl/Meta/Alt) when the keystroke
     * originated inside a text input. Default true — a bare key typed
     * in a field is text, while a modified chord (Mod+, …) still fires
     * from one, the Commands pane's query included. Per-surface
     * shortcuts (a cell panel's Enter) set this to false and handle
     * the input-focus case themselves.
     */
    skipWhenTyping?: boolean;
}

/**
 * Register a handler for a shortcut by id. The combo is resolved
 * through the store so user overrides take effect immediately —
 * no re-mount needed when the user rebinds it.
 *
 * Multiple handlers can share a combo; they all fire in
 * registration order unless one calls
 * `e.stopImmediatePropagation()`.
 */
export function useShortcut(
    id: ShortcutId,
    handler: (e: KeyboardEvent) => void,
    opts: UseShortcutOptions = {},
): void {
    const { enabled = true, target = 'document', skipWhenTyping = true } = opts;

    // Latest-handler ref so the effect's listener stays identity-
    // stable across renders. Cheap and it removes a whole class of
    // "handler captured stale state" bugs without forcing the
    // caller to use useCallback.
    const handlerRef = useRef(handler);
    useEffect(() => { handlerRef.current = handler; });

    // Store subscription — this re-runs the effect whenever the
    // user remaps this particular shortcut. Subscribing to the
    // whole overrides map would re-run for every shortcut's remap;
    // scoping to one id keeps unrelated remaps cheap.
    const comboString = useKeybindingsStore(
        s => s.resolveString(id),
    );

    useEffect(() => {
        if (!enabled || !comboString) return;
        const combo = useKeybindingsStore.getState().resolve(id);
        if (!combo) return;

        const listener = (e: Event) => {
            // The DOM types add/removeEventListener with a generic
            // Event signature, but for 'keydown' the runtime always
            // delivers a KeyboardEvent. Narrow here.
            const ke = e as KeyboardEvent;
            if (skipWhenTyping && !comboHasModifier(combo) && isTypingTarget(ke)) return;
            if (!matchCombo(ke, combo)) return;
            handlerRef.current(ke);
        };
        const el = target === 'window' ? window : document;
        el.addEventListener('keydown', listener);
        return () => el.removeEventListener('keydown', listener);
    }, [enabled, comboString, id, target, skipWhenTyping]);
}
