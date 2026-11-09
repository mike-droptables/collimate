import { shallow } from 'zustand/shallow';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import { SHORTCUT_REGISTRY } from '@/lib/keybindings/registry';
import { parseCombo } from '@/lib/keybindings/parser';
import { comboHasModifier, isMacPlatform, platformModifiers, physicalCodeForKey } from '@/lib/keybindings/match';
import type { KeyCombo } from '@/lib/keybindings/types';

/** A combo as the sandbox iframe compares it against a raw KeyboardEvent. */
export interface ForwardedCombo {
    key: string;
    code?: string;
    metaKey: boolean;
    ctrlKey: boolean;
    altKey: boolean;
    shiftKey: boolean;
}

/**
 * Build the list of combos the renderer frames and desktop guests hand to
 * the host: every registry shortcut and every command binding (the
 * Commands pane's `keyboard.commands`) whose combo carries a modifier
 * (Mod, Ctrl, Meta or Alt; `comboHasModifier`). A bare key is the
 * renderer's own: never forwarded, whatever a remap or a binding made of
 * it. A forwarded combo is the host's alone: the frame consumes it.
 * Resolves on the host's platform (Mod → metaKey on Mac, ctrlKey on
 * Windows/Linux) so the receiver just does a boolean compare on the raw
 * KeyboardEvent fields — no shared parser, no Mod translation inside the
 * frame. Regenerated on every remap and pushed again
 * (onForwardedCombosChange).
 *
 * Shared by the pooled SandboxIframe, the StandaloneSandboxIframe
 * (fullscreen) and the desktop guests, so all forward the same combos.
 */
export function buildForwardedCombos(): ForwardedCombo[] {
    const mac = isMacPlatform();
    const keys = useKeybindingsStore.getState();
    const combos: KeyCombo[] = [
        ...Object.values(SHORTCUT_REGISTRY).flatMap(entry => {
            const combo = keys.resolve(entry.id);
            return combo ? [combo] : [];
        }),
        ...Object.values(keys.commands).flatMap(comboString => {
            const combo = parseCombo(comboString);
            return combo ? [combo] : [];
        }),
    ];
    const out: ForwardedCombo[] = [];
    for (const combo of combos) {
        if (!comboHasModifier(combo)) continue;
        const { meta, ctrl } = platformModifiers(combo, mac);
        out.push({
            key: combo.key,
            ...(mac && combo.alt ? { code: physicalCodeForKey(combo.key, Boolean(combo.shift)) } : {}),
            metaKey: meta,
            ctrlKey: ctrl,
            altKey: Boolean(combo.alt),
            shiftKey: Boolean(combo.shift),
        });
    }
    return out;
}

/** Calls `listener` whenever the forwarded combos may change: a shortcut
 *  remapped, a command bound or unbound. Returns the unsubscribe. */
export function onForwardedCombosChange(listener: () => void): () => void {
    return useKeybindingsStore.subscribe(
        s => [s.overrides, s.commands] as const,
        () => listener(),
        { equalityFn: shallow },
    );
}

/** A key a renderer frame or a desktop guest forwarded (a host chord, or
 *  the release of a modifier one held), as the host hears it: dispatched
 *  on window, where the scene, the command bindings and every useShortcut
 *  listen. Its target is the window, never a text field, so a chord acts
 *  as it does from the cell's card. */
export function dispatchForwardedKey(
    type: 'keydown' | 'keyup',
    key: { key: string; code?: string; altKey: boolean; ctrlKey: boolean; shiftKey: boolean; metaKey: boolean },
): void {
    window.dispatchEvent(new KeyboardEvent(type, {
        key: key.key, code: key.code, altKey: key.altKey, ctrlKey: key.ctrlKey,
        shiftKey: key.shiftKey, metaKey: key.metaKey, bubbles: true, cancelable: true,
    }));
}
