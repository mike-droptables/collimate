import { useKeybindingsStore } from './store';
import { displayComboString } from './display';
import { ShortcutId } from './types';

/**
 * Reactive display string for a shortcut — re-renders when the
 * user remaps. Suitable for tooltip titles and help strings.
 *
 *   <Button title={useShortcutTooltip(SHORTCUT_IDS.GLOBAL_OPEN_PALETTE, 'Commands')}>
 *     → "Commands (⌘K)" on Mac, "Commands (Ctrl+K)" elsewhere
 */
export function useShortcutDisplay(id: ShortcutId): string {
    const comboString = useKeybindingsStore(s => s.resolveString(id));
    return displayComboString(comboString);
}

/**
 * Convenience for the common `"<label> (<display>)"` pattern.
 * When the shortcut has no effective combo (unbound), returns
 * just the label.
 */
export function useShortcutTooltip(id: ShortcutId, label: string): string {
    const display = useShortcutDisplay(id);
    return display ? `${label} (${display})` : label;
}
