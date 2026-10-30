import { useEffect, useRef } from 'react';
import { sceneKeyboardOwned } from '@/lib/keyboardOwner';
import { useCollimateStore } from '@/stores/useCollimateStore';
import { comboHasModifier, isTypingTarget, matchCombo } from './match';
import { formatCombo, parseCombo } from './parser';
import { SHORTCUT_REGISTRY } from './registry';
import { useKeybindingsStore } from './store';
import type { ShortcutId } from './types';

// The Commands pane's command bindings (`keyboard.commands`: a combo bound
// to a pane row by its command key) fire like shortcuts, with the pane open
// or closed. Registry-backed rows need none of this: their shortcut ids fire
// through useShortcut and the scene keyboard.

/** Keys every surface uses for its own close and submit: a binding on one
 *  clashes only with a shortcut of the same context (a command binding's
 *  context is `global`). */
export const UNIVERSAL_COMBOS: ReadonlySet<string> = new Set(['Escape', 'Enter', 'Shift+Enter']);

/** A registry shortcut holds the press where a command binding would clash
 *  with it (the clash the Commands pane shows in red): the shortcut's
 *  combo, unless it is a universal key of another context. */
function registryHolds(e: KeyboardEvent): boolean {
    const keys = useKeybindingsStore.getState();
    return Object.values(SHORTCUT_REGISTRY).some(entry => {
        const combo = keys.resolve(entry.id);
        if (!combo || !matchCombo(e, combo)) return false;
        return entry.context === 'global' || !UNIVERSAL_COMBOS.has(formatCombo(combo));
    });
}

/** What a binding runs: a pane row (PaneCommand) as the bindings see it. */
export interface BindableCommand {
    key: string;
    /** A registry-backed row binds through its shortcut, not its key. */
    shortcut?: ShortcutId;
    run: () => void;
}

/**
 * One window keydown listener (capture phase) for the command bindings.
 * `commands` returns the pane's rows built from the latest snapshot, so a
 * row's `run` acts on the current selection. A press that matches a bound
 * row's combo runs the first such row in the pane's order, records it
 * among the pane's recents and takes the key (preventDefault). It stays
 * out of a surface that owns the keyboard (`sceneKeyboardOwned`), of a
 * press something already took, of the pane's binding capture and of a
 * press a registry shortcut holds (the shortcut has it, whichever window
 * listener runs first); a bare combo (no Mod/Ctrl/Meta/Alt) never fires
 * from a text field, a modified chord fires from anywhere.
 */
export function useCommandBindings(commands: () => BindableCommand[]): void {
    const commandsRef = useRef(commands);
    useEffect(() => { commandsRef.current = commands; });

    useEffect(() => {
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.defaultPrevented) return;
            const { commands: bound, capturing } = useKeybindingsStore.getState();
            if (capturing !== null) return;
            const entries = Object.entries(bound);
            if (entries.length === 0) return;
            if (sceneKeyboardOwned(e)) return;
            const typing = isTypingTarget(e);
            const matched = new Set(entries.flatMap(([key, comboString]) => {
                const combo = parseCombo(comboString);
                return combo && matchCombo(e, combo) && (!typing || comboHasModifier(combo)) ? [key] : [];
            }));
            if (matched.size === 0 || registryHolds(e)) return;
            const cmd = commandsRef.current().find(c => !c.shortcut && matched.has(c.key));
            if (!cmd) return;
            e.preventDefault();
            cmd.run();
            useCollimateStore.getState().recordCommandPaneRecent(cmd.key);
        };
        window.addEventListener('keydown', onKeyDown, { capture: true });
        return () => window.removeEventListener('keydown', onKeyDown, { capture: true });
    }, []);
}
