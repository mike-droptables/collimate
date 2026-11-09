import { useKeybindingsStore } from './store';
import { ShortcutId } from './types';
import { platformModifiers } from './match';

/**
 * Programmatically fire a shortcut by id.
 *
 * Resolves the combo from the store and dispatches one synthetic,
 * bubbling ``keydown`` at ``document``. Its event path runs window
 * capture listeners, then document listeners, then window bubble
 * listeners, so both event-target options ``useShortcut`` accepts
 * (``window`` and ``document``) see it exactly once. ``matchCombo`` reads
 * the event's ``key`` + modifier fields and does not check
 * ``isTrusted``, so synthetic dispatch is indistinguishable from a
 * real keypress for our handlers.
 *
 * ``Mod+`` is expanded the same way real keypresses resolve it —
 * ``metaKey`` on macOS, ``ctrlKey`` elsewhere — so a single
 * ``fireShortcut(id)`` works on every platform the user might be
 * on.
 *
 * Returns ``true`` if the combo was resolved and dispatched, ``false``
 * when the shortcut has no assigned combo (user cleared the binding).
 * Callers typically ignore the return — a missing binding isn't an
 * error, it just means the button click is a no-op.
 */
export function fireShortcut(id: ShortcutId): boolean {
    const combo = useKeybindingsStore.getState().resolve(id);
    if (!combo || !combo.key) return false;

    const { meta: metaKey, ctrl: ctrlKey } = platformModifiers(combo);

    const init: KeyboardEventInit = {
        key: combo.key,
        metaKey,
        ctrlKey,
        altKey: !!combo.alt,
        shiftKey: !!combo.shift,
        bubbles: true,
        cancelable: true,
    };

    document.dispatchEvent(new KeyboardEvent('keydown', init));
    return true;
}
