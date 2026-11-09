// The profile's editing preferences: spellcheck, and status_on_run (a run
// start opens its cell's status section, lib/runAttach). They belong to the
// profile the page runs in, never to a channel or a server:
//
//   - in the desktop shell, the app profile's settings.json (`spellcheck`,
//     `openStatusOnRun`), through the bridge's getEditingPrefs (synchronous,
//     from the preload's bootstrap, so the first paint has them),
//     setEditingPrefs and onEditingPrefsChanged; localStorage is never used
//     for them there;
//   - in a browser, this browser's localStorage under PROFILE_PREFS_KEY, per
//     server origin, with the `storage` event carrying a change made in
//     another tab.
//
// A shell without the editing members (they joined the bridge without a
// protocol bump) is treated as a browser. The bridge's names
// ({spellcheck, statusOnRun}) are mapped to the store's
// ({spellcheck, status_on_run}) here and nowhere else.

import { desktopBridge, type EditingPrefs } from '@/lib/desktop';

/** The profile's editing preferences, in the store's shape (lib/runAttach
 *  reads status_on_run). */
export interface EditingPreferences {
    spellcheck: boolean;
    status_on_run: boolean;
}

export const DEFAULT_EDITING: Readonly<EditingPreferences> = Object.freeze({ spellcheck: true, status_on_run: true });

/** The browser's localStorage key: `{ editing: {spellcheck, status_on_run} }`. */
export const PROFILE_PREFS_KEY = 'collimate.profile.v1';

/** Preferences as the store holds them: a missing or non-boolean flag reads
 *  as its default, each on its own. */
export function sanitizeEditing(raw: unknown): EditingPreferences {
    const section = (typeof raw === 'object' && raw !== null ? raw : {}) as Partial<Record<keyof EditingPreferences, unknown>>;
    const flag = (key: keyof EditingPreferences): boolean => {
        const value = section[key];
        return typeof value === 'boolean' ? value : DEFAULT_EDITING[key];
    };
    return { spellcheck: flag('spellcheck'), status_on_run: flag('status_on_run') };
}

const fromBridge = (prefs: Partial<EditingPrefs> | null | undefined): EditingPreferences =>
    sanitizeEditing({ spellcheck: prefs?.spellcheck, status_on_run: prefs?.statusOnRun });

const toBridge = (prefs: EditingPreferences): EditingPrefs =>
    ({ spellcheck: prefs.spellcheck, statusOnRun: prefs.status_on_run });

/** The shell's editing members when it has them all; undefined in a browser
 *  and in a shell from before they existed. */
function profileShell() {
    const shell = desktopBridge();
    if (!shell?.getEditingPrefs || !shell.setEditingPrefs || !shell.onEditingPrefsChanged) return undefined;
    return {
        get: shell.getEditingPrefs.bind(shell),
        set: shell.setEditingPrefs.bind(shell),
        on: shell.onEditingPrefsChanged.bind(shell),
    };
}

/** The stored browser document, or an empty one when it is missing, not an
 *  object, or storage is unreadable (a private window, blocked site data). */
function readStored(): Record<string, unknown> {
    try {
        const parsed: unknown = JSON.parse(localStorage.getItem(PROFILE_PREFS_KEY) ?? 'null');
        return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
    } catch {
        return {};
    }
}

/** Synchronous, for the first paint: the shell's bootstrap value on the
 *  desktop, localStorage in a browser, else the defaults. */
export function readEditingPreferences(): EditingPreferences {
    const shell = profileShell();
    if (shell) return fromBridge(shell.get());
    return sanitizeEditing(readStored().editing);
}

/** Persist; resolves with what was saved (desktop: settings.json through the
 *  bridge; browser: localStorage). Rejects when it could not be saved. */
export async function writeEditingPreferences(next: EditingPreferences): Promise<EditingPreferences> {
    const prefs = sanitizeEditing(next);
    const shell = profileShell();
    if (shell) return fromBridge(await shell.set(toBridge(prefs)));
    // Other keys of the profile document ride along untouched.
    localStorage.setItem(PROFILE_PREFS_KEY, JSON.stringify({ ...readStored(), editing: prefs }));
    return prefs;
}

/** Changes made elsewhere: the shell's push (a load, a set from this or
 *  another page of the same profile); in a browser, the `storage` event from
 *  another tab of this origin. Returns an unsubscribe. */
export function onEditingPreferencesChanged(cb: (prefs: EditingPreferences) => void): () => void {
    const shell = profileShell();
    if (shell) return shell.on(prefs => cb(fromBridge(prefs)));
    if (typeof window === 'undefined') return () => {};
    const onStorage = (event: StorageEvent) => {
        // A null key is a clear() of the whole storage: the defaults again.
        if (event.key !== PROFILE_PREFS_KEY && event.key !== null) return;
        cb(sanitizeEditing(readStored().editing));
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
}
