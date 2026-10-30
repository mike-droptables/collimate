import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { create } from 'zustand';
import { subscribeWithSelector } from 'zustand/middleware';
import { SHORTCUT_REGISTRY } from './registry';
import { parseCombo, formatCombo } from './parser';
import { bindingPermitted } from './record';
import { KeyCombo, ShortcutId } from './types';
import { putChannelSettings } from '@/lib/api';
import { readBootCache, writeBootCacheSection } from '@/lib/settingsCache';

// Bindings are PER CHANNEL — the `keyboard` section of the channel settings
// doc: `overrides` rebinds registry shortcuts (`{ [id]: "Mod+K" }`; a
// missing entry falls back to the registry default) and `commands` binds
// Commands pane rows (`{ [commandKey]: "Alt+L" }`; a missing entry is
// unbound). useChannelSettingsStore adopts the active channel's section on
// switch via `adoptOverrides` / `adoptCommandBindings`; local edits persist
// through `putChannelSettings` (section-wholesale, so both maps travel in
// every write). The boot cache paints the last-active channel's bindings
// before the first settings load.

/** The longest command key a binding is kept for (mirrors
 *  api/channel_settings.py COMMAND_KEY_MAX). */
const COMMAND_KEY_MAX = 200;

function canonical(value: unknown): KeyCombo | null {
    return typeof value === 'string' ? parseCombo(value) : null;
}

function sanitizeOverrides(raw: unknown): Record<string, string> {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    const out: Record<string, string> = {};
    for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
        const combo = SHORTCUT_REGISTRY[id] ? canonical(value) : null;
        if (combo && bindingPermitted(id, combo)) out[id] = formatCombo(combo);
    }
    return out;
}

function commandKeyValid(key: string): boolean {
    return key.length > 0 && key.length <= COMMAND_KEY_MAX;
}

function sanitizeCommands(raw: unknown): Record<string, string> {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    const out: Record<string, string> = {};
    for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
        const combo = commandKeyValid(key) ? canonical(value) : null;
        if (combo) out[key] = formatCombo(combo);
    }
    return out;
}

function persistKeyboard(overrides: Record<string, string>, commands: Record<string, string>) {
    const keyboard = { overrides, commands };
    writeBootCacheSection('keyboard', keyboard);
    // Capture the adopted channel before any asynchronous work.
    const channel = useChannelSettingsStore.getState().loadedFor;
    if (!channel) return;
    void putChannelSettings(channel, { keyboard }).catch(() => {
        // The in-memory bindings apply; a later save can retry them.
    });
}

/** A combo given as a KeyCombo or a canonical string, parsed; null when it
 *  does not parse. */
function parsed(combo: KeyCombo | string): KeyCombo | null {
    return parseCombo(typeof combo === 'string' ? combo : formatCombo(combo));
}

export interface KeybindingsState {
    /** Registry overrides (id → canonical-string combo). */
    overrides: Record<string, string>;
    /** Commands pane row bindings (command key → canonical-string combo). */
    commands: Record<string, string>;
    /** The pane row whose binding is being recorded (its command key), or
     *  null. Session state: bindings do not fire while it is set. */
    capturing: string | null;

    /** Resolve a shortcut id to its effective combo (override ?? default). */
    resolve: (id: ShortcutId) => KeyCombo | null;
    /** Raw canonical-string form of the effective combo. */
    resolveString: (id: ShortcutId) => string;
    /** Set / clear a single override. Passing null resets to default. */
    setOverride: (id: ShortcutId, combo: KeyCombo | string | null) => void;
    /** Bind / unbind a Commands pane row by its command key. */
    setCommandBinding: (key: string, combo: KeyCombo | string | null) => void;
    setCapturing: (key: string | null) => void;
    /** Reset every override and clear every command binding. */
    resetAll: () => void;
    /** Replace overrides from a loaded channel-settings doc — no persist
     *  (the server is the source of what we're adopting). */
    adoptOverrides: (overrides: Record<string, string>) => void;
    /** Replace command bindings from a loaded channel-settings doc — no
     *  persist. */
    adoptCommandBindings: (commands: Record<string, string>) => void;
}

const boot = readBootCache().keyboard;

export const useKeybindingsStore = create<KeybindingsState>()(
    subscribeWithSelector((set, get) => ({
        overrides: sanitizeOverrides(boot?.overrides),
        commands: sanitizeCommands(boot?.commands),
        capturing: null,

        resolve: (id) => {
            const entry = SHORTCUT_REGISTRY[id];
            if (!entry) return null;
            return parseCombo(get().overrides[id] ?? entry.default);
        },

        resolveString: (id) => {
            const combo = get().resolve(id);
            return combo ? formatCombo(combo) : '';
        },

        setOverride: (id, combo) => {
            if (!SHORTCUT_REGISTRY[id]) return;
            const next = { ...get().overrides };
            if (combo === null) {
                delete next[id];
            } else {
                const c = parsed(combo);
                if (!c || !bindingPermitted(id, c)) return;
                next[id] = formatCombo(c);
            }
            persistKeyboard(next, get().commands);
            set({ overrides: next });
        },

        setCommandBinding: (key, combo) => {
            if (!commandKeyValid(key)) return;
            const next = { ...get().commands };
            if (combo === null) {
                delete next[key];
            } else {
                const c = parsed(combo);
                if (!c) return;
                next[key] = formatCombo(c);
            }
            persistKeyboard(get().overrides, next);
            set({ commands: next });
        },

        setCapturing: (key) => set({ capturing: key }),

        resetAll: () => {
            persistKeyboard({}, {});
            set({ overrides: {}, commands: {} });
        },

        adoptOverrides: (overrides) => {
            set({ overrides: sanitizeOverrides(overrides) });
        },

        adoptCommandBindings: (commands) => {
            set({ commands: sanitizeCommands(commands) });
        },
    })),
);
