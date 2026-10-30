import { shallow } from 'zustand/shallow';
import { useKeybindingsStore } from '@/lib/keybindings/store';
import { SHORTCUT_REGISTRY } from '@/lib/keybindings/registry';
import { parseCombo } from '@/lib/keybindings/parser';
import { isMacPlatform, platformModifiers, physicalCodeForKey } from '@/lib/keybindings/match';
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
 * Build the list of combos the sandbox iframe should forward to the host:
 * every `allowedInRenderer` shortcut and every command binding (the
 * Commands pane's `keyboard.commands`, which fire wherever the page's
 * shortcuts do), except a bare printable key.
 * Resolves on the host's platform (Mod → metaKey on Mac, ctrlKey on
 * Windows/Linux) so sandbox.html just does a boolean compare on the raw
 * KeyboardEvent fields — no shared parser, no Mod translation inside the
 * iframe. Regenerated on every remap and pushed via SET_KEYBINDINGS
 * (onForwardedCombosChange).
 *
 * Shared by the pooled SandboxIframe and the StandaloneSandboxIframe
 * (fullscreen) so both forward the same combos.
 */
export function buildForwardedCombos(): ForwardedCombo[] {
    const mac = isMacPlatform();
    const keys = useKeybindingsStore.getState();
    const combos: KeyCombo[] = [
        ...Object.values(SHORTCUT_REGISTRY).flatMap(entry => {
            const combo = entry.allowedInRenderer ? keys.resolve(entry.id) : null;
            return combo ? [combo] : [];
        }),
        ...Object.values(keys.commands).flatMap(comboString => {
            const combo = parseCombo(comboString);
            return combo ? [combo] : [];
        }),
    ];
    const out: ForwardedCombo[] = [];
    for (const combo of combos) {
        const { meta, ctrl } = platformModifiers(combo, mac);
        // A bare printable key is the renderer's own typing: never
        // forwarded, whatever a remap or a binding made of it.
        if (!meta && !ctrl && !combo.alt && combo.key.length === 1) continue;
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
