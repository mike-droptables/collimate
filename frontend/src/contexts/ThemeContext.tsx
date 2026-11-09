import React, { createContext, useContext, useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
    ThemeId, ThemeVars, ThemeVarKey, ThemeDef, ThemeScene, SceneKey, ThemeScheme, BuiltinThemeId, THEME_CSS_VARS,
    THEME_VAR_KEYS, getTheme, DEFAULT_THEME_ID, isUserThemeId, isBuiltinThemeId, themeCssVars, sceneCssVars,
    sceneGround,
} from '@/lib/themes';
import {
    UserTheme, FONT_ROLE_VARS, newUserTheme, copyUserTheme, normalizeUserThemes, uniqueLabel, themeDefOf,
} from '@/lib/userThemes';
import { FontRole, stackFor } from '@/lib/fonts';
import { fetchAppSettings, putAppSettings, putChannelSettings, type ThemeLibrary } from '@/lib/api';
import { readBootCache, writeBootCacheSection } from '@/lib/settingsCache';
import { THEME_LIBRARY_CHANGED_EVENT } from '@/lib/themeLibraryEvents';
import { useChannelSettingsStore } from '@/stores/useChannelSettingsStore';
import { desktopBridge } from '@/lib/desktop';

// Themes have two scopes. The SERVER holds the theme library (the user
// themes) and the default theme (lib/api/appSettings, one document per
// server, shared by every channel on it); a CHANNEL holds only its override,
// the `theme.override` of its settings doc (a theme id, or null to follow
// the default). The theme on show is the channel's override when it
// resolves, else the default; while the channels page is mounted
// (useDefaultThemeSurface) it is the default, so the page shows the look it
// edits.
//
// The library is fetched on mount, on every `app_settings_changed`
// (lib/themeLibraryEvents) and whenever the channels page opens, and saved
// with a debounced, section-wholesale PUT. A read is adopted only while no
// save of this window's is pending or in flight and none started since the
// read began, so neither the echo of an edit nor a stale read reverts it.
// The override is adopted whenever useChannelSettingsStore loads a
// channel's doc (keyed on the section itself, so a patch of another section
// never re-adopts the load-time override) and saved, debounced, to the
// channel it was adopted from. The localStorage boot cache paints both
// before the first load, so boot never flashes. Both are stored data, so
// every user skin read from them is normalised (lib/userThemes) on the way in.

interface ThemeContextValue {
    /** The theme on show: the default on the channels page, else the
     *  channel's override when it resolves, else the default. */
    themeId: ThemeId;
    effectiveVars: ThemeVars;
    /** The active skin's scene (the host-only ground, pattern and edge). */
    scene: ThemeScene;
    /** The server's theme library. */
    userThemes: UserTheme[];
    /** The server's default theme. */
    defaultThemeId: ThemeId;
    /** This channel's override; null follows the default (an override
     *  naming a theme no longer in the library follows it too). */
    overrideThemeId: ThemeId | null;
    setDefaultThemeId: (id: ThemeId) => void;
    setOverrideThemeId: (id: ThemeId | null) => void;
    // ── User skins (the theme editor) ─────────────────────────────
    /** The "+" card: a new user skin copying the theme `target` shows (the
     *  default, or the channel's theme; a built-in becomes its base, with no
     *  overrides), added to the library and selected as the default
     *  ('default') or as this channel's override ('override'); returns its id. */
    createUserTheme: (target: 'default' | 'override') => string;
    renameUserTheme: (id: string, label: string) => void;
    /** Delete a user skin from the library: a default that named it becomes
     *  its base, this channel's override that named it follows the default. */
    deleteUserTheme: (id: string) => void;
    /** Rebase a user skin: its overrides stay, every other token and the
     *  scheme come from the new base. */
    setUserThemeBase: (id: string, base: BuiltinThemeId) => void;
    setUserThemeScheme: (id: string, scheme: ThemeScheme) => void;
    /** Override one token. */
    setUserThemeVar: (id: string, key: ThemeVarKey, value: string) => void;
    /** Drop one override: the token returns to its base (or, for a soft
     *  accent, to the fill derived from its accent). */
    resetUserThemeVar: (id: string, key: ThemeVarKey) => void;
    /** Set one font role to a catalogue family. */
    setUserThemeFont: (id: string, role: FontRole, family: string) => void;
    /** Override one field of the scene. */
    setUserThemeScene: <K extends SceneKey>(id: string, key: K, value: ThemeScene[K]) => void;
    /** Drop one scene override: the field returns to its base. */
    resetUserThemeScene: (id: string, key: SceneKey) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/** Enter the default-theme surface; the returned function leaves it. */
const ThemeSurfaceContext = createContext<(() => () => void) | null>(null);

/** Write every token on <html> under its CSS name, the scene's host-only
 *  properties, the scheme and the skin id. Tailwind's utilities resolve the
 *  tokens through app/globals.css; SceneBackground and the selection edge
 *  read the scene's. */
function applyTheme(themeId: ThemeId, vars: ThemeVars, scene: ThemeScene, scheme: ThemeScheme) {
    const root = document.documentElement;
    for (const key of THEME_VAR_KEYS) root.style.setProperty(THEME_CSS_VARS[key], vars[key]);
    for (const [name, value] of Object.entries(sceneCssVars(scene, vars))) root.style.setProperty(name, value);
    root.style.setProperty('color-scheme', scheme);
    root.setAttribute('data-theme', themeId);
    root.setAttribute('data-scheme', scheme);
}

/** Suppress CSS transitions for one frame so a change of look (another
 *  channel, the channels page, another window's edit) is a clean swap, not
 *  a cross-fade of every themed token. */
function swapFrame() {
    document.documentElement.setAttribute('data-theme-swapping', '');
    requestAnimationFrame(() => {
        document.documentElement.removeAttribute('data-theme-swapping');
    });
}

/** Whether `id` names a theme there is: a built-in, or a user theme in
 *  the given library. */
function resolves(id: ThemeId | null, library: UserTheme[]): id is ThemeId {
    return id !== null && (isBuiltinThemeId(id) || library.some(u => u.id === id));
}

/** A stored default is only trustworthy if it resolves; anything else is
 *  the built-in default. */
function validThemeId(id: unknown, library: UserTheme[]): ThemeId {
    const stored = storedThemeId(id);
    return resolves(stored, library) ? stored : DEFAULT_THEME_ID;
}

/** A stored theme id in a theme id's shape, else null. A user id is kept
 *  even when the library lacks it: the library may not have arrived yet. */
function storedThemeId(id: unknown): ThemeId | null {
    return typeof id === 'string' && (isBuiltinThemeId(id) || isUserThemeId(id)) ? id : null;
}

interface LibraryState {
    library: UserTheme[];
    defaultThemeId: ThemeId;
}

/** A stored library (the server's, or the boot cache's) as state. */
function readLibrary(raw: Partial<ThemeLibrary> | undefined): LibraryState {
    const library = normalizeUserThemes(raw?.userThemes);
    return { library, defaultThemeId: validThemeId(raw?.defaultThemeId, library) };
}

/** The channel doc's theme section: the override alone. */
type ChannelThemeSection = { override: ThemeId | null };

function librarySection({ library, defaultThemeId }: LibraryState): ThemeLibrary {
    return { defaultThemeId, userThemes: library };
}

/** The first paint's state, from the boot cache (an older cache shape
 *  reads as the defaults). */
function readBootState() {
    const cached = typeof window === 'undefined' ? {} : readBootCache();
    const lib = readLibrary(cached.themeLibrary);
    const override = storedThemeId(cached.theme?.override);
    return {
        lib, override,
        libraryJson: JSON.stringify(librarySection(lib)),
        overrideJson: JSON.stringify({ override }),
    };
}

// Persist debounce: skin edits arrive per color-input tick, so both the
// change comparison and the PUT are debounced behind one timer.
const PERSIST_DEBOUNCE_MS = 400;

export function ThemeProvider({ children }: { children: React.ReactNode }) {
    // Initial paint = boot cache: the server's library and the last-active
    // channel's override adopt over it as soon as they load.
    const [boot] = useState(readBootState);
    const [lib, setLib] = useState<LibraryState>(boot.lib);
    const [override, setOverride] = useState<ThemeId | null>(boot.override);
    // Mounted channels pages (useDefaultThemeSurface): while above zero the
    // default is on show.
    const [pageCount, setPageCount] = useState(0);

    // ── The library (server-wide) ──────────────────────────────────
    // JSON of the last adopted-or-saved library section: the persist effect
    // compares against it so adopting the server's never echoes a PUT back.
    const librarySyncedRef = useRef(boot.libraryJson);
    // The armed-but-unfired save, the saves in flight, and a count bumped as
    // each save starts and settles: a read that began before it is stale.
    const libraryPendingRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const libraryInFlightRef = useRef(0);
    const librarySavesRef = useRef(0);

    const reloadLibrary = useCallback(() => {
        const saves = librarySavesRef.current;
        fetchAppSettings().then(doc => {
            if (libraryPendingRef.current !== null || libraryInFlightRef.current > 0
                || librarySavesRef.current !== saves) return;
            const next = readLibrary(doc?.theme);
            const section = librarySection(next);
            const json = JSON.stringify(section);
            if (json === librarySyncedRef.current) return;
            librarySyncedRef.current = json;
            writeBootCacheSection('themeLibrary', section);
            swapFrame();
            setLib(next);
        }, () => {
            // Unreachable server: the boot cache's paint stays.
        });
    }, []);

    useEffect(() => {
        reloadLibrary();
        window.addEventListener(THEME_LIBRARY_CHANGED_EVENT, reloadLibrary);
        return () => window.removeEventListener(THEME_LIBRARY_CHANGED_EVENT, reloadLibrary);
    }, [reloadLibrary]);

    useEffect(() => {
        const section = librarySection(lib);
        const json = JSON.stringify(section);
        if (json === librarySyncedRef.current) return;  // adopted or already saved
        const timer = setTimeout(() => {
            if (libraryPendingRef.current === timer) libraryPendingRef.current = null;
            librarySyncedRef.current = json;
            writeBootCacheSection('themeLibrary', section);
            librarySavesRef.current += 1;
            libraryInFlightRef.current += 1;
            const settle = () => {
                librarySavesRef.current += 1;
                libraryInFlightRef.current -= 1;
            };
            // Best-effort: the look is already applied locally, and the
            // next edit saves the whole section again.
            putAppSettings({ theme: section }).then(settle, settle);
        }, PERSIST_DEBOUNCE_MS);
        libraryPendingRef.current = timer;
        return () => {
            clearTimeout(timer);
            if (libraryPendingRef.current === timer) libraryPendingRef.current = null;
        };
    }, [lib]);

    // ── The channel's override ─────────────────────────────────────
    const overrideSyncedRef = useRef(boot.overrideJson);
    // The persist effect's armed-but-unfired save: the channel captured at
    // edit time and the section it would PUT. The adopt effect flushes it.
    const overridePendingRef = useRef<{
        channel: string | null;
        section: ChannelThemeSection;
        timer: ReturnType<typeof setTimeout>;
    } | null>(null);

    // The doc is a load-time snapshot (useChannelSettingsStore): only a new
    // theme section — a load or a forced reload — is something to adopt.
    // useChannelSettingsStore writes it to the boot cache itself.
    const themeSection = useChannelSettingsStore(s => s.doc?.theme ?? null);
    useEffect(() => {
        if (!themeSection) return;
        // An override still inside the persist debounce belongs to the
        // channel it was chosen in. Adopting another channel's doc
        // re-renders with new state and the persist cleanup would cancel
        // that timer, so PUT it now. Same channel (forced reload): the
        // server doc wins. No channel (boot, a switch still loading): cache
        // only, nothing to PUT.
        const pending = overridePendingRef.current;
        if (pending) {
            clearTimeout(pending.timer);
            overridePendingRef.current = null;
            const newChannel = useChannelSettingsStore.getState().loadedFor;
            if (pending.channel && pending.channel !== newChannel
                && JSON.stringify(pending.section) !== overrideSyncedRef.current) {
                void putChannelSettings(pending.channel, { theme: pending.section }).catch(() => {});
            }
        }
        const next = storedThemeId(themeSection.override);
        overrideSyncedRef.current = JSON.stringify({ override: next });
        swapFrame();
        setOverride(next);
    }, [themeSection]);

    // The PUT targets the channel whose doc this override was ADOPTED from
    // (loadedFor), captured at edit time — never the live active channel,
    // which may already have switched by the time the debounce fires (that
    // gap would write channel A's override into channel B). A save still
    // pending when another channel's doc is adopted is flushed to its
    // captured channel by the adopt effect above. An archived channel
    // rejects the PUT (ChannelReadOnlyError), swallowed like any failure.
    useEffect(() => {
        const section: ChannelThemeSection = { override };
        const json = JSON.stringify(section);
        if (json === overrideSyncedRef.current) return;  // adopted or already saved
        const channel = useChannelSettingsStore.getState().loadedFor;
        const timer = setTimeout(() => {
            if (overridePendingRef.current?.timer === timer) overridePendingRef.current = null;
            overrideSyncedRef.current = json;
            writeBootCacheSection('theme', section);
            if (!channel) return;  // the channels page, boot, a switch still loading — cache only
            void putChannelSettings(channel, { theme: section }).catch(() => {});
        }, PERSIST_DEBOUNCE_MS);
        overridePendingRef.current = { channel, section, timer };
        return () => {
            clearTimeout(timer);
            if (overridePendingRef.current?.timer === timer) overridePendingRef.current = null;
        };
    }, [override]);

    // ── The default-theme surface (the channels page) ──────────────
    // The page may be open with no channel, so no stream tells it of
    // another window's edit: entering re-reads the library.
    const enterDefaultSurface = useCallback(() => {
        swapFrame();
        setPageCount(c => c + 1);
        reloadLibrary();
        return () => {
            swapFrame();
            setPageCount(c => c - 1);
        };
    }, [reloadLibrary]);

    // ── Resolution ─────────────────────────────────────────────────
    const { library: userThemes, defaultThemeId } = lib;
    const overrideThemeId = resolves(override, userThemes) ? override : null;
    const channelThemeId = overrideThemeId ?? defaultThemeId;
    const themeId = pageCount > 0 ? defaultThemeId : channelThemeId;

    const baseTheme = useMemo<ThemeDef>(() => themeDefOf(themeId, userThemes), [themeId, userThemes]);

    const scheme: ThemeScheme = baseTheme.scheme;
    // User skins already carry their base's tokens and scene under their overrides.
    const effectiveVars = baseTheme.vars;
    const scene = baseTheme.scene;

    // Apply the skin, then tell renderers whenever the record they receive
    // changed: the id, the scheme or any token (the scene is not theirs). A
    // new user skin copying the active one changes only the id, and the
    // frames still need it (their data-theme mirrors it); nothing else is
    // re-sent.
    const dispatchedTheme = useRef<string | null>(null);
    useEffect(() => {
        if (typeof window === 'undefined') return;
        applyTheme(themeId, effectiveVars, scene, scheme);
        const vars = themeCssVars(effectiveVars);
        const visibleKey = JSON.stringify([themeId, scheme, vars]);
        if (dispatchedTheme.current === visibleKey) return;
        dispatchedTheme.current = visibleKey;
        window.dispatchEvent(new CustomEvent('collimate:theme-changed', {
            detail: {
                themeId, scheme, known: true, vars,
                fonts: { ui: effectiveVars.fontUi, mono: effectiveVars.fontMono, prose: effectiveVars.fontProse },
            },
        }));
    }, [themeId, scheme, effectiveVars, scene]);

    // The desktop window's colour is what a resize or a GPU restart exposes
    // before the page repaints: the scene's ground, which fills the window
    // below the bars (docs/ai_tools note feedback_flicker_app_side_levers).
    const windowBackground = sceneGround(scene, effectiveVars);
    const { sheet: windowOverlay, ink: windowSymbol } = effectiveVars;
    useEffect(() => {
        desktopBridge()?.setWindowTheme?.({ background: windowBackground, overlay: windowOverlay, symbol: windowSymbol });
    }, [windowBackground, windowOverlay, windowSymbol]);

    // ── Selection ──────────────────────────────────────────────────

    const setDefaultThemeId = useCallback((id: ThemeId) => {
        setLib(prev => ({ ...prev, defaultThemeId: id }));
    }, []);

    const setOverrideThemeId = useCallback((id: ThemeId | null) => {
        setOverride(id);
    }, []);

    // ── User skins (the theme editor) ─────────────────────────────

    const createUserTheme = useCallback((target: 'default' | 'override'): string => {
        const source = target === 'default' ? defaultThemeId : channelThemeId;
        const label = uniqueLabel('My theme', userThemes.map(u => u.label));
        const sourceUser = userThemes.find(u => u.id === source);
        const created = sourceUser
            ? copyUserTheme(sourceUser, label)
            : newUserTheme(isBuiltinThemeId(source) ? source : DEFAULT_THEME_ID, label);
        setLib(prev => ({
            library: [...prev.library, created],
            defaultThemeId: target === 'default' ? created.id : prev.defaultThemeId,
        }));
        if (target === 'override') setOverride(created.id);
        return created.id;
    }, [defaultThemeId, channelThemeId, userThemes]);

    const updateUserTheme = useCallback((id: string, fn: (u: UserTheme) => UserTheme) => {
        setLib(prev => ({ ...prev, library: prev.library.map(u => u.id === id ? fn(u) : u) }));
    }, []);

    const renameUserTheme = useCallback((id: string, label: string) => {
        updateUserTheme(id, u => ({ ...u, label }));
    }, [updateUserTheme]);

    const deleteUserTheme = useCallback((id: string) => {
        setLib(prev => {
            const deleted = prev.library.find(u => u.id === id);
            return {
                library: prev.library.filter(u => u.id !== id),
                defaultThemeId: prev.defaultThemeId === id
                    ? deleted?.baseThemeId ?? DEFAULT_THEME_ID : prev.defaultThemeId,
            };
        });
        // Other channels whose override names it follow the default at
        // resolution; their docs keep the id until they next save it.
        setOverride(prev => (prev === id ? null : prev));
    }, []);

    const setUserThemeBase = useCallback((id: string, base: BuiltinThemeId) => {
        updateUserTheme(id, u => ({ ...u, baseThemeId: base, scheme: getTheme(base).scheme }));
    }, [updateUserTheme]);

    const setUserThemeScheme = useCallback((id: string, nextScheme: ThemeScheme) => {
        updateUserTheme(id, u => ({ ...u, scheme: nextScheme }));
    }, [updateUserTheme]);

    const setUserThemeVar = useCallback((id: string, key: ThemeVarKey, value: string) => {
        updateUserTheme(id, u => ({ ...u, vars: { ...u.vars, [key]: value } }));
    }, [updateUserTheme]);

    const resetUserThemeVar = useCallback((id: string, key: ThemeVarKey) => {
        updateUserTheme(id, u => {
            const vars = { ...u.vars };
            delete vars[key];
            return { ...u, vars };
        });
    }, [updateUserTheme]);

    const setUserThemeFont = useCallback((id: string, role: FontRole, family: string) => {
        const stack = stackFor(family);
        if (stack) setUserThemeVar(id, FONT_ROLE_VARS[role], stack);
    }, [setUserThemeVar]);

    const setUserThemeScene = useCallback(<K extends SceneKey>(id: string, key: K, value: ThemeScene[K]) => {
        updateUserTheme(id, u => ({ ...u, scene: { ...u.scene, [key]: value } }));
    }, [updateUserTheme]);

    const resetUserThemeScene = useCallback((id: string, key: SceneKey) => {
        updateUserTheme(id, u => {
            const next = { ...u.scene };
            delete next[key];
            return { ...u, scene: next };
        });
    }, [updateUserTheme]);

    const value = useMemo<ThemeContextValue>(() => ({
        themeId, effectiveVars, scene, userThemes, defaultThemeId, overrideThemeId,
        setDefaultThemeId, setOverrideThemeId,
        createUserTheme, renameUserTheme, deleteUserTheme, setUserThemeBase,
        setUserThemeScheme, setUserThemeVar, resetUserThemeVar, setUserThemeFont,
        setUserThemeScene, resetUserThemeScene,
    }), [
        themeId, effectiveVars, scene, userThemes, defaultThemeId, overrideThemeId,
        setDefaultThemeId, setOverrideThemeId,
        createUserTheme, renameUserTheme, deleteUserTheme, setUserThemeBase,
        setUserThemeScheme, setUserThemeVar, resetUserThemeVar, setUserThemeFont,
        setUserThemeScene, resetUserThemeScene,
    ]);

    return (
        <ThemeSurfaceContext.Provider value={enterDefaultSurface}>
            <ThemeContext.Provider value={value}>
                {children}
            </ThemeContext.Provider>
        </ThemeSurfaceContext.Provider>
    );
}

export function useThemeContext() {
    const ctx = useContext(ThemeContext);
    if (!ctx) throw new Error('useThemeContext must be used within ThemeProvider');
    return ctx;
}

/** Show the default theme while the calling component is mounted: the
 *  channels page, which edits the default, renders in it, also when it is
 *  open over a channel that overrides it. Outside a ThemeProvider there is
 *  no look to change. */
export function useDefaultThemeSurface(): void {
    const enter = useContext(ThemeSurfaceContext);
    useEffect(() => enter?.(), [enter]);
}
