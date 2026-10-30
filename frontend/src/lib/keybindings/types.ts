// Keybinding primitives. Kept pure + framework-agnostic so they can
// be unit-tested without React and reused from the iframe-forwarding
// layer (which has no React context).

/**
 * A single key combination.
 *
 * `key` is the logical character (the physical US key for macOS Option),
 * lowercased for single letters and
 * preserved verbatim for named keys (`Enter`, `Escape`, `ArrowUp`,
 * …). Modifier keys (`Meta`, `Control`, `Alt`, `Shift`) never
 * appear as the `key`.
 *
 * `mod` is the platform-adaptive "primary modifier": true iff the
 * user held ⌘ on macOS or Ctrl on Windows/Linux. Prefer `mod` over
 * `ctrl` / `meta` for cross-platform bindings. `ctrl` and `meta`
 * stay available for shortcuts that must be literal on both
 * platforms (rare — generally a mistake).
 */
export interface KeyCombo {
    key: string;
    mod?: boolean;
    ctrl?: boolean;
    meta?: boolean;
    alt?: boolean;
    shift?: boolean;
}

/**
 * Stable identifier for a shortcut. The id survives remapping —
 * `global.openPalette` keeps its meaning even if the default
 * `⌘K` is changed. Every handler, tooltip, settings
 * row, and iframe-forward probe keys off this id.
 */
export type ShortcutId = string;

/**
 * Grouping bucket a shortcut is listed under. Descriptive
 * only — dispatch is not gated by context; handlers use their own
 * `enabled` guards.
 */
export type ShortcutContext =
    | 'global'
    | 'list'
    | 'cell'
    | 'editor'
    | 'chat'
    | 'settings';

/** Display order of shortcut contexts (allShortcuts, so the Commands pane's
 *  Keyboard rows). Every ShortcutContext must appear here. */
export const SHORTCUT_CONTEXT_ORDER: readonly ShortcutContext[] = ['global', 'list', 'cell', 'editor', 'chat', 'settings'];

/** A context's label: the meta of the Commands pane's Keyboard rows. */
export const SHORTCUT_CONTEXT_LABEL: Record<ShortcutContext, string> = {
    global: 'Global',
    list: 'Commands pane and cell panels',
    cell: 'Selected cell',
    editor: 'File editor',
    chat: 'Chat',
    settings: 'Settings',
};

export interface ShortcutEntry {
    id: ShortcutId;
    /** Short human-visible label ("Run", "Find file"). */
    label: string;
    /** Longer description: the Commands pane row's tooltip, and its
     *  search matches it. */
    description?: string;
    context: ShortcutContext;
    /** Default key combo in canonical-string form (see parser.ts). */
    default: string;
    /**
     * True iff the iframe-forwarding layer should forward this key
     * combo to the host when a renderer has focus. Only set for
     * shortcuts whose global effect outweighs an editor's claim on
     * the key (Monaco etc. — see the plan). Default false.
     */
    allowedInRenderer?: boolean;
}
